"""Board digest of a review run (`board.kls-board.json`, LLM Visualizer spec §3).

What the LLM reads before the board file: the exact names a findings file must use
(references, pad numbers, net names, layer names, short uuids, spec §5) and enough
geometry to reason with, but no fill polygons and no track coordinates. A dump of the
snapshot, not an analysis. Millimetres with 4 decimals in KiCad's board frame (x right,
y down), as in the board file. Digest v1: netclasses, zone priority and zone islands are v2.

The bulky records (pads, nets, vias, tracks) are rows whose fields `COLUMNS` names once,
near the top of the file: about 0.55 kB per part, and every row is one line.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict

from . import model
from .board_index import SHORT_ID, BoardIndex, copper_rank

VERSION = 1
COLUMNS = {
    "nets": ["name", "pads", "vias", "length"],  # length: routed mm per copper layer
    # w, h: copper box; drill 0: none; pin, pin_type: the schematic pin ("" for none), as pinfunction and pintype
    "pads": ["number", "net", "x", "y", "w", "h", "type", "layers", "drill", "pin", "pin_type"],
    "vias": ["uuid", "net", "x", "y", "top", "bottom", "drill", "diameter"],
    "tracks": ["net", "layer", "segments", "arcs", "length"],  # segments counts arcs too
}
WIDTH = 160  # a value longer than this as one line is split, so no line gets very long
_DIGITS = re.compile(r"(\d+)")


def _mm(nm: float) -> float | int:
    value = round(nm / 1e6, 4)
    return int(value) if value == int(value) else value


def _mm2(nm2: float) -> float | int:
    return _mm(nm2 / 1e6)


def _xy(point: model.Point) -> list:
    return [_mm(point[0]), _mm(point[1])]


def _natural(text: str) -> tuple:
    """C2 before C10, pad 2 before pad 10."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in _DIGITS.split(text) if part)


def _layer_key(layer: str) -> tuple:
    rank = copper_rank(layer)
    return (rank if rank is not None else math.inf, layer)


def arc_length(start: model.Point, mid: model.Point, end: model.Point) -> float:
    """Length in nm of the circular arc from `start` through `mid` to `end`."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if d == 0:  # collinear: a straight piece
        return math.dist(start, end)
    s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    ux = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    uy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in (start, mid, end))
    sweep = (a3 - a1) % math.tau
    if (a2 - a1) % math.tau > sweep:  # mid lies the other way round
        sweep = math.tau - sweep
    return math.hypot(x1 - ux, y1 - uy) * sweep


def _ring_area(ring: model.Ring) -> float:
    """Unsigned area in nm² (shoelace; a fractured fill's slits cancel)."""
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2


def _polygon_area(polygon: model.Polygon) -> float:
    return (_ring_area(polygon[0]) - sum(_ring_area(hole) for hole in polygon[1:])) if polygon else 0.0


def _inside(point: model.Point, ring: model.Ring) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def outline_area(outline: model.Outline) -> float:
    """Board area in nm², even-odd over every outline ring: KiCad may hand a cutout over
    as a separate polygon rather than as a hole."""
    rings = [ring for polygon in outline.polygons for ring in polygon if len(ring) >= 3]
    total = 0.0
    for i, ring in enumerate(rings):
        depth = sum(_inside(ring[0], other) for j, other in enumerate(rings) if j != i)
        total += _ring_area(ring) * (-1 if depth % 2 else 1)
    return max(total, 0.0)


def _bbox(points) -> list | None:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [_mm(min(xs)), _mm(min(ys)), _mm(max(xs)), _mm(max(ys))] if xs else None


def short_id(index: BoardIndex, item_id: str) -> str:
    """The uuid's last 8 hex, as `targets.resolve` reads `uuid:`; the full uuid when
    another item shares those 8 (a short form that names two items names none)."""
    key = item_id.lower()
    short = key[-SHORT_ID:]
    return short if index.ids_ending(short) == (key,) else key


def _copper_names(snapshot: model.BoardSnapshot, index: BoardIndex) -> tuple[str, ...]:
    names = tuple(layer.name for layer in snapshot.stackup.layers if layer.type == "copper")
    return names or index.copper_layers


def _stackup(snapshot: model.BoardSnapshot, index: BoardIndex, board_area: float) -> list:
    layers = [layer for layer in snapshot.stackup.layers if layer.type in ("copper", "dielectric")]
    if not layers:  # stackup missing (a warning says so): copper layers in order, nothing else known
        layers = [model.StackupLayer(name, "copper", None, None, None, None) for name in index.copper_layers]
    cover: dict[str, Counter] = defaultdict(Counter)
    for zone in snapshot.zones:
        cover[zone.layer][zone.net] += sum(_polygon_area(polygon) for polygon in zone.polygons)
    result = []
    for layer in layers:
        entry = {"name": layer.name, "type": layer.type}
        user_name = snapshot.layer_display_names.get(layer.name)
        if user_name and user_name != layer.name:
            entry["user_name"] = user_name
        if layer.thickness_nm is not None:
            entry["thickness"] = _mm(layer.thickness_nm)
        if layer.material:
            entry["material"] = layer.material
        if layer.epsilon_r is not None:
            entry["epsilon_r"] = layer.epsilon_r
        if layer.loss_tangent is not None:
            entry["loss_tangent"] = layer.loss_tangent
        if layer.type == "copper":  # zone fill per net, as a share of the board's area
            areas = sorted(((net, area) for net, area in cover[layer.name].items() if area > 0),
                           key=lambda item: (-item[1], item[0]))
            entry["zone_cover"] = [{"net": net, "cover": round(area / board_area, 3) if board_area else None,
                                    "area": _mm2(area)} for net, area in areas]
        result.append(entry)
    return result


def _pad(pad: model.Pad, all_copper: tuple[str, ...]) -> list:
    """A row of `COLUMNS["pads"]`."""
    copper = sorted((layer for layer in pad.polygons if layer not in model.PASTE_LAYERS), key=_layer_key)
    drilled = pad.drill is not None and min(pad.drill) > 0
    kind = ("th" if copper else "npth") if drilled else "smd"
    points = [point for layer in copper for polygon in pad.polygons[layer] for point in polygon[0]]
    if points:
        x0, y0 = min(p[0] for p in points), min(p[1] for p in points)
        size = (_mm(max(p[0] for p in points) - x0), _mm(max(p[1] for p in points) - y0))
    else:  # a plain hole: its drill is its size
        size = (_mm(pad.drill[0]), _mm(pad.drill[1])) if drilled else (0, 0)
    drill = (_mm(pad.drill[0]) if pad.drill[0] == pad.drill[1] else [_mm(pad.drill[0]), _mm(pad.drill[1])])         if drilled else 0
    return [pad.number, pad.net, *_xy(pad.pos), *size, kind,
            ["*.Cu"] if kind == "th" and tuple(copper) == all_copper else copper, drill, pad.pin_name, pad.pin_type]


def _rotation(radians: float) -> float | int:
    value = round(math.degrees(radians) % 360, 3) % 360
    return int(value) if value == int(value) else value


def _footprints(snapshot: model.BoardSnapshot, index: BoardIndex, all_copper: tuple[str, ...]) -> list:
    pads = defaultdict(list)
    for pad in snapshot.pads:
        pads[pad.footprint_id].append(pad)
    shared = Counter(footprint.reference for footprint in snapshot.footprints)
    result = []
    for footprint in sorted(snapshot.footprints, key=lambda f: (_natural(f.reference), f.id)):
        entry = {"ref": footprint.reference}
        if shared[footprint.reference] > 1:  # the reference alone names them all
            entry["uuid"] = short_id(index, footprint.id)
        entry.update({"value": footprint.value, "lib_id": footprint.lib_id, "side": footprint.side,
                      "pos": _xy(footprint.pos), "rot": _rotation(footprint.rotation_rad), "dnp": footprint.dnp,
                      "pads": [_pad(pad, all_copper) for pad in
                               sorted(pads[footprint.id], key=lambda p: (_natural(p.number), p.id))]})
        result.append(entry)
    return result


def build(snapshot: model.BoardSnapshot, run: str, stamp: str, board_file: str = "board.kicad_pcb") -> dict:
    """The digest of `snapshot` for review run `run` of the board text hashing to `stamp`."""
    index = BoardIndex(snapshot)
    board_area = outline_area(snapshot.outline)
    all_copper = _copper_names(snapshot, index)

    length: dict[tuple[str, str], float] = defaultdict(float)
    segments, arcs = Counter(), Counter()
    for track in snapshot.tracks:
        length[(track.net, track.layer)] += math.dist(track.start, track.end)
        segments[(track.net, track.layer)] += 1
    for arc in snapshot.arcs:
        length[(arc.net, arc.layer)] += arc_length(arc.start, arc.mid, arc.end)
        segments[(arc.net, arc.layer)] += 1
        arcs[(arc.net, arc.layer)] += 1
    routed = sorted(length, key=lambda key: (key[0], _layer_key(key[1])))

    pad_count = Counter(pad.net for pad in snapshot.pads if pad.net)
    via_count = Counter(via.net for via in snapshot.vias if via.net)
    names = {*pad_count, *via_count, *(net for net, _ in length),
             *(zone.net for zone in snapshot.zones), *(graphic.net for graphic in snapshot.graphics)}
    per_net = defaultdict(dict)
    for net, layer in routed:
        per_net[net][layer] = _mm(length[(net, layer)])
    nets = [[net, pad_count[net], via_count[net], per_net.get(net, {})] for net in sorted(names - {""})]
    vias = [[short_id(index, via.id), via.net, *_xy(via.pos), via.layer_top, via.layer_bottom,
             _mm(via.drill), _mm(via.diameter)]
            for via in sorted(snapshot.vias, key=lambda v: (v.net, v.pos, v.id))]
    zones = [{"uuid": short_id(index, zone.id), "net": zone.net, "layer": zone.layer,
              "bbox": _bbox([point for polygon in zone.polygons for point in polygon[0]]),
              "area": _mm2(sum(_polygon_area(polygon) for polygon in zone.polygons))}
             for zone in sorted(snapshot.zones, key=lambda z: (_layer_key(z.layer), z.net, z.id))]
    tracks = [[net, layer, segments[(net, layer)], arcs[(net, layer)], _mm(length[(net, layer)])]
              for net, layer in routed]
    footprints = _footprints(snapshot, index, all_copper)

    return {
        "digest": VERSION, "run": run, "stamp": stamp, "board": board_file, "units": "mm",
        "frame": "board file: x right, y down",
        "bbox": _bbox([point for polygon in snapshot.outline.polygons for ring in polygon for point in ring] or
                      [point for stroke in snapshot.outline.strokes for point in stroke]),  # malformed: its lines
        "counts": {"footprints": len(footprints), "pads": len(snapshot.pads), "nets": len(nets),
                   "vias": len(snapshot.vias), "zones": len(zones), "tracks": len(snapshot.tracks), "arcs": len(snapshot.arcs)},
        "columns": COLUMNS,
        "stackup": _stackup(snapshot, index, board_area),
        "nets": nets, "footprints": footprints, "vias": vias, "zones": zones, "tracks": tracks,
    }


def _compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render(value, indent: int) -> str:
    """One line when short; a long list puts one item per line, a long dict keeps its
    keys inline and lets its long values break (a part's header, then its pads)."""
    text = _compact(value)
    if len(text) <= WIDTH:
        return text
    if isinstance(value, list):
        inner = " " * (indent + 1)
        return "[\n" + ",\n".join(inner + _render(item, indent + 1) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(key, ensure_ascii=False)}:{_render(item, indent)}"
                              for key, item in value.items()) + "}"
    return text


def dumps(digest: dict) -> str:
    """The digest as JSON text: one top-level key per line, one record per line. Write it as UTF-8."""
    return "{\n" + ",\n".join(f" {json.dumps(key, ensure_ascii=False)}:{_render(value, 1)}"
                              for key, value in digest.items()) + "\n}\n"
