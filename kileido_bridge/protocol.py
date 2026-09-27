"""Bridge -> Blender frames: a small JSON header plus raw little-endian arrays.

Frame layout (all lengths big-endian uint32):

    [frame_length][header_length][header JSON, UTF-8][array bytes, in header order]

`frame_length` counts everything after itself. The header lists each array as
{"name", "dtype", "shape"}; the receiver reads them with numpy.frombuffer, which
costs ~0.02 ms for 52k points versus ~8 ms for the same data as JSON. The
decoder needs only the standard library and numpy, so the Blender add-on
vendors a copy of `FrameDecoder` and `decode_frame` (blender_addon/kileido/client.py).
"""

import json
import struct

import numpy as np

from . import model
from .diff import Key
from .geometry import sample_arc

PROTOCOL = 1
MAX_FRAME_BYTES = 64 * 1024 * 1024
DTYPES = {"<i4", "|u1", "<f4"}  # "|" = single byte, no byte order
DEFAULT_THICKNESS_NM = 1_600_000  # board thickness when the stackup cannot give one
# Every frame type the bridge sends; the add-on's client.py lists the same ones.
MESSAGE_TYPES = ("snapshot_begin", "board", "layer_data", "footprints", "stackup", "snapshot_end",
                 "appearance", "selection", "return_path", "dc_setup", "dc_status", "dc_result", "status")
_LENGTH = struct.Struct(">I")


def encode_frame(header: dict, arrays: dict[str, np.ndarray] | None = None) -> bytes:
    arrays = arrays or {}
    specs, blobs = [], []
    for name, array in arrays.items():
        array = np.ascontiguousarray(array)
        dtype = array.dtype.newbyteorder("<").str
        if dtype not in DTYPES:
            raise ValueError(f"unsupported array dtype {array.dtype} for {name}")
        specs.append({"name": name, "dtype": dtype, "shape": list(array.shape)})
        blobs.append(array.astype(dtype, copy=False).tobytes())
    head = json.dumps({**header, "protocol": PROTOCOL, "arrays": specs}, separators=(",", ":")).encode("utf-8")
    body = _LENGTH.pack(len(head)) + head + b"".join(blobs)
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"frame of {len(body)} bytes exceeds {MAX_FRAME_BYTES}")
    return _LENGTH.pack(len(body)) + body


def decode_frame(body: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    """Decode one frame body (without its leading frame_length)."""
    (head_length,) = _LENGTH.unpack_from(body, 0)
    header = json.loads(body[4:4 + head_length].decode("utf-8"))
    offset = 4 + head_length
    arrays = {}
    for spec in header.pop("arrays"):
        if spec["dtype"] not in DTYPES:
            raise ValueError(f"unsupported dtype {spec['dtype']}")
        dtype = np.dtype(spec["dtype"])
        count = int(np.prod(spec["shape"], dtype=np.int64))
        size = count * dtype.itemsize
        if offset + size > len(body):
            raise ValueError("frame shorter than its array table")
        arrays[spec["name"]] = np.frombuffer(body, dtype, count, offset).reshape(spec["shape"])
        offset += size
    if offset != len(body):
        raise ValueError("trailing bytes after the last array")
    return header, arrays


class FrameDecoder:
    """Accumulates stream bytes; returns every complete frame. Handles split and merged reads."""

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES):
        self._buffer = bytearray()
        self.max_frame_bytes = max_frame_bytes

    def feed(self, data: bytes) -> list[tuple[dict, dict[str, np.ndarray]]]:
        self._buffer += data
        frames = []
        while len(self._buffer) >= 4:
            (length,) = _LENGTH.unpack_from(self._buffer, 0)
            if length > self.max_frame_bytes:
                raise ValueError(f"incoming frame of {length} bytes exceeds {self.max_frame_bytes}")
            if len(self._buffer) < 4 + length:
                break
            body = bytes(self._buffer[4:4 + length])
            del self._buffer[:4 + length]
            frames.append(decode_frame(body))
        return frames


# --- Message builders (bridge side) -----------------------------------------------------

def _rings(entries: list[tuple[int, bool, model.Ring]]) -> dict[str, np.ndarray]:
    """Flatten (item_index, is_hole, ring) into offset-indexed arrays."""
    starts = np.zeros(len(entries) + 1, dtype="<i4")
    starts[1:] = np.cumsum([len(ring) for _, _, ring in entries], dtype=np.int64)
    points = np.array([p for _, _, ring in entries for p in ring], dtype="<i4").reshape(-1, 2)
    return {"points": points, "ring_start": starts,
            "ring_item": np.array([i for i, _, _ in entries], dtype="<i4"),
            "ring_hole": np.array([h for _, h, _ in entries], dtype="|u1")}


def _polygon_entries(index: int, polygons) -> list[tuple[int, bool, model.Ring]]:
    return [(index, n > 0, ring) for polygon in polygons for n, ring in enumerate(polygon)]


def tracks_message(snapshot: model.BoardSnapshot, layer: str, revision: int) -> bytes:
    """Straight tracks and sampled arcs of one layer as independent segments."""
    ids, rows, items = [], [], []
    for track in snapshot.tracks:
        if track.layer == layer:
            items.append(len(ids))
            rows.append((*track.start, *track.end, track.width))
            ids.append(track.id)
    for arc in snapshot.arcs:
        if arc.layer == layer:
            points = sample_arc(arc.start, arc.mid, arc.end)
            for a, b in zip(points, points[1:]):
                items.append(len(ids))
                rows.append((*a, *b, arc.width))
            ids.append(arc.id)
    header = {"type": "layer_data", "layer": layer, "kind": "tracks", "revision": revision, "ids": ids}
    return encode_frame(header, {"seg": np.array(rows, dtype="<i4").reshape(-1, 5),
                                 "item": np.array(items, dtype="<i4")})


def _pad_drill(pad: model.Pad) -> tuple[tuple[int, int, int, int], float, int, int] | None:
    """A pad's hole as ((x, y, width, height), angle, oval, plated); None without one."""
    if not pad.drill or min(pad.drill) <= 0:
        return None
    oval = pad.drill_shape == "oval"
    # Plated when the pad has copper anywhere; np_thru_hole pads have none.
    plated = any(name not in model.PASTE_LAYERS for name in pad.polygons)
    return (*pad.pos, *pad.drill), pad.drill_angle_rad, int(oval), int(plated)


def fill_message(snapshot: model.BoardSnapshot, layer: str, kind: str, revision: int) -> bytes:
    """Pads, stencil apertures (`paste`), zones or copper graphics of one layer as rings;
    `ring_hole` marks inner rings."""
    ids, entries = [], []
    if kind == "graphics":  # one item per polygon: a graphic's strokes overlap at their joints
        for graphic in snapshot.graphics:
            if graphic.layer == layer:
                for polygon in graphic.polygons:
                    entries += _polygon_entries(len(ids), (polygon,))
                    ids.append(graphic.id)
        header = {"type": "layer_data", "layer": layer, "kind": kind, "revision": revision, "ids": ids}
        return encode_frame(header, _rings(entries))
    drills = []  # (item index, *_pad_drill(pad)) for pads with a hole
    for item in snapshot.zones if kind == "zones" else snapshot.pads:
        if kind == "zones":
            polygons = item.polygons if item.layer == layer else ()
            if not polygons:
                continue
        else:
            polygons = item.polygons.get(layer, ())
            if not polygons and not (kind == "pads" and layer in item.layers):
                continue  # bare holes have no rings but are kept on their pad layers
            drill = _pad_drill(item) if kind == "pads" else None
            if drill is not None:
                drills.append((len(ids), *drill))
        entries += _polygon_entries(len(ids), polygons)
        ids.append(item.id)
    header = {"type": "layer_data", "layer": layer, "kind": kind, "revision": revision, "ids": ids}
    arrays = _rings(entries)
    if kind == "pads":
        drill_items, rows, angles, ovals, plated = zip(*drills) if drills else ((),) * 5
        arrays.update({"drill": np.array(rows, dtype="<i4").reshape(-1, 4),
                       "drill_item": np.array(drill_items, dtype="<i4"),
                       "drill_angle": np.array(angles, dtype="<f4"),
                       "drill_oval": np.array(ovals, dtype="|u1"),
                       "drill_plated": np.array(plated, dtype="|u1")})
    return encode_frame(header, arrays)


def vias_message(snapshot: model.BoardSnapshot, revision: int) -> bytes:
    layers = sorted({v.layer_top for v in snapshot.vias} | {v.layer_bottom for v in snapshot.vias})
    index = {name: i for i, name in enumerate(layers)}
    header = {"type": "layer_data", "layer": "", "kind": "vias", "revision": revision,
              "ids": [v.id for v in snapshot.vias], "layers": layers}
    return encode_frame(header, {
        "via": np.array([(*v.pos, v.diameter, v.drill) for v in snapshot.vias], dtype="<i4").reshape(-1, 4),
        "span": np.array([(index[v.layer_top], index[v.layer_bottom]) for v in snapshot.vias],
                         dtype="<i4").reshape(-1, 2)})


def outline_message(snapshot: model.BoardSnapshot, revision: int) -> bytes:
    """The board outline's rings; while it is malformed also every Edge.Cuts line as
    segments (`stroke`: x1, y1, x2, y2) and the problem locations (`problem`: x, y)."""
    outline = snapshot.outline
    header = {"type": "layer_data", "layer": "", "kind": "outline", "revision": revision, "ids": []}
    arrays = _rings(_polygon_entries(0, outline.polygons))
    arrays["stroke"] = np.array([(*a, *b) for line in outline.strokes for a, b in zip(line, line[1:])],
                                dtype="<i4").reshape(-1, 4)
    arrays["problem"] = np.array(outline.problems, dtype="<i4").reshape(-1, 2)
    return encode_frame(header, arrays)


def footprints_message(snapshot: model.BoardSnapshot, revision: int) -> bytes:
    return encode_frame({"type": "footprints", "revision": revision, "footprints": [
        {"id": f.id, "ref": f.reference, "x": f.pos[0], "y": f.pos[1], "rot": f.rotation_rad,
         "side": f.side, "bbox_nm": f.bbox_nm, "model_paths": f.model_paths,
         "model_visible": f.model_visible}
        for f in snapshot.footprints]})


def selection_message(selected, pair, footprints, pads, revision: int) -> bytes:
    """What to highlight: the selected nets' tracks/arcs/vias, their differential-pair
    partner's, and selected footprints with their pads."""
    return encode_frame({"type": "selection", "revision": revision,
                         "selected": list(selected), "pair": list(pair),
                         "footprints": list(footprints), "pads": list(pads)})


def return_path_message(nets, issues, revision: int, error: str = "", elapsed_ms: float | None = None) -> bytes:
    """Return-path issues (return_path.Issue) of the checked nets. Each issue is a
    header entry; what to highlight goes in `mark` (x1, y1, x2, y2, width) rows, a
    point as a zero-length row, with `mark_issue` (index into `issues`) and
    `mark_layer` (index into `layers`); the broken parts of reference planes go in
    `area` (left, top, right, bottom) rects with `area_issue` and `area_layer`."""
    layers = sorted({layer for issue in issues for layer, _, _ in issue.marks} |
                    {layer for issue in issues for layer, _ in issue.areas})
    index = {layer: position for position, layer in enumerate(layers)}
    rows, owners, on_layer = [], [], []
    for number, issue in enumerate(issues):
        for layer, points, width in issue.marks:
            for a, b in zip(points, points[1:]) if len(points) > 1 else ((points[0], points[0]),):
                rows.append((*a, *b, width))
                owners.append(number)
                on_layer.append(index[layer])
    areas = [(*rect, number, index[layer]) for number, issue in enumerate(issues)
             for layer, rects in issue.areas for rect in rects]
    area = np.array(areas, dtype=np.int64).reshape(-1, 6)
    header = {"type": "return_path", "revision": revision, "nets": sorted(nets), "layers": layers,
              "error": error, "elapsed_ms": elapsed_ms,
              "issues": [{"kind": issue.kind, "net": issue.net, "item": issue.item, "layer": issue.layer,
                          "reference": issue.reference, "at": list(issue.at), "length_nm": issue.length_nm,
                          "message": issue.message} for issue in issues]}
    return encode_frame(header, {"mark": np.array(rows, dtype="<i4").reshape(-1, 5),
                                 "mark_issue": np.array(owners, dtype="<i4"),
                                 "mark_layer": np.array(on_layer, dtype="<i4"),
                                 "area": area[:, :4].astype("<i4"), "area_issue": area[:, 4].astype("<i4"),
                                 "area_layer": area[:, 5].astype("<i4")})


def dc_setup_message(setup: dict, markers, revision: int) -> bytes:
    """The DC analysis setup (dc.DcAnalysis.setup_state) and where its parts are:
    `marker` (x, y, size) rows with `marker_terminal` (index into its terminals) and
    `marker_side` (1 top, -1 bottom, 0 through the board)."""
    rows = np.array([row[:3] for row in markers], dtype=np.int64).reshape(-1, 3)
    return encode_frame({"type": "dc_setup", "revision": revision, **setup},
                        {"marker": rows.astype("<i4"),
                         "marker_terminal": np.array([row[3] for row in markers], dtype="<i4"),
                         "marker_side": np.array([row[4] for row in markers], dtype="<i4")})


def dc_status_message(status: dict, revision: int) -> bytes:
    """Where the DC solve is: "idle" (with what is missing), "waiting" for edits to
    settle, "solving" (stage, elapsed_s), "done" or "error" (message)."""
    return encode_frame({"type": "dc_status", "revision": revision, **status})


def dc_result_message(result: dict | None, revision: int, net: str = "") -> bytes:
    """A finished DC solve of one net (dcworker.solve), or None to clear `net`'s
    result ("": every net's). Per copper
    layer in `layers`, on the grid (x0_nm, y0_nm, pitch_nm): `j` |J| in A/mm2, `v` the
    potential in V, `jx`, `jy` the current density along KiCad's x and y (NaN off
    copper). Barrels (vias and plated pads) by current: `via` (x, y, land size) rows,
    `via_current` (A) and `via_power` (W), with their ids and layer spans in the header."""
    if result is None:
        return encode_frame({"type": "dc_result", "revision": revision, "net": net, "clear": True})
    fields, barrels = result["fields"], result["barrels"]
    header = {key: value for key, value in result.items() if key not in ("fields", "barrels")}
    header.update(type="dc_result", revision=revision, via_ids=list(barrels["ids"]),
                  via_spans=[list(span) for span in barrels["spans"]])
    via = np.column_stack([barrels["xy"].reshape(-1, 2), barrels["size"].reshape(-1)]).astype("<i4")
    return encode_frame(header, {**{key: np.asarray(fields[key], dtype="<f4") for key in ("j", "v", "jx", "jy")},
                                 "via": via, "via_current": np.asarray(barrels["current_a"], dtype="<f4"),
                                 "via_power": np.asarray(barrels["power_w"], dtype="<f4")})


def stackup_message(snapshot: model.BoardSnapshot, revision: int) -> bytes:
    return encode_frame({"type": "stackup", "revision": revision, "warnings": list(snapshot.warnings),
                         "display_names": snapshot.layer_display_names,
                         "layers": [model.to_jsonable(layer) for layer in snapshot.stackup.layers]})


def messages_for(snapshot: model.BoardSnapshot, dirty: frozenset[Key], revision: int) -> list[bytes]:
    """One frame per dirty display group; arcs and tracks share a layer's track object."""
    frames = []
    for layer in sorted({layer for layer, kind in dirty if kind in ("tracks", "arcs")}):
        frames.append(tracks_message(snapshot, layer, revision))
    for layer, kind in sorted(k for k in dirty if k[1] in ("pads", "paste", "zones", "graphics")):
        frames.append(fill_message(snapshot, layer, kind, revision))
    builders = {"vias": vias_message, "outline": outline_message,
                "footprints": footprints_message, "stackup": stackup_message}
    for kind, build in builders.items():
        if ("", kind) in dirty:
            frames.append(build(snapshot, revision))
    return frames


# --- Complete snapshots (bridge start-up, resync and dump files) -------------------------

def copper_layers(snapshot: model.BoardSnapshot) -> set[str]:
    names = {t.layer for t in snapshot.tracks} | {a.layer for a in snapshot.arcs} | {z.layer for z in snapshot.zones}
    names |= {layer for pad in snapshot.pads for layer in pad.layers}
    names |= {v.layer_top for v in snapshot.vias} | {v.layer_bottom for v in snapshot.vias}
    names |= {g.layer for g in snapshot.graphics}
    names |= {entry.name for entry in snapshot.stackup.layers if entry.type == "copper"}
    return names


def layer_heights_nm(snapshot: model.BoardSnapshot) -> tuple[dict[str, int], int, list[str]]:
    """Copper heights: B.Cu = 0, other layers at the top of their copper, walking the top-first stackup upward.

    Missing or incomplete stackups give evenly spaced layers over 1.6 mm plus a warning, never invented materials.
    """
    names = copper_layers(snapshot)
    stack = snapshot.stackup.layers
    order = [entry.name for entry in stack]
    if "F.Cu" in order and "B.Cu" in order:
        span = stack[order.index("F.Cu"):order.index("B.Cu") + 1]
        if all(entry.thickness_nm is not None for entry in span):
            heights, z = {}, 0
            for entry in reversed(span):
                z += entry.thickness_nm
                if entry.type == "copper":
                    heights[entry.name] = 0 if entry.name == "B.Cu" else z
            if names <= heights.keys():
                return heights, z, []
    inner = sorted(n for n in names if n not in ("F.Cu", "B.Cu"))
    bottom_up = ["B.Cu", *reversed(inner), "F.Cu"]
    gaps = max(1, len(bottom_up) - 1)
    heights = {name: i * DEFAULT_THICKNESS_NM // gaps for i, name in enumerate(bottom_up)}  # F.Cu exactly on top
    return heights, DEFAULT_THICKNESS_NM, ["Stackup height unavailable; showing evenly spaced layers over 1.6 mm"]


def board_origin_nm(snapshot: model.BoardSnapshot) -> tuple[int, int]:
    """Centre of the Edge.Cuts bounding box; the live bridge fixes it at its first snapshot."""
    points = [p for polygon in snapshot.outline.polygons for ring in polygon for p in ring]
    if not points:
        return 0, 0
    xs, ys = zip(*points)
    return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2


def board_message(snapshot: model.BoardSnapshot, revision: int, origin_nm: tuple[int, int] | None = None,
                  board_path: str = "", appearance: dict | None = None, export: dict | None = None) -> bytes:
    """`export`: where Blender's kicad-cli workers read the board (the bridge's live
    copy with unsaved edits), the project folder for ${KIPRJMOD}, and kicad-cli."""
    heights, thickness, warnings = layer_heights_nm(snapshot)
    return encode_frame({
        "type": "board", "revision": revision, "board_name": snapshot.board_name,
        "origin_nm": list(origin_nm or board_origin_nm(snapshot)),
        "layers": [{"name": name, "z_m": heights.get(name, 0) * 1e-9, "copper": True} for name in sorted(heights)],
        "board_thickness_m": thickness * 1e-9, "warnings": [*snapshot.warnings, *warnings],
        "board_path": board_path, "appearance": appearance or {}, "export": export or {},
        # Copper, mask and other non-dielectric thicknesses from KiCad's stackup (for 3D copper).
        "layer_thickness_m": {entry.name: entry.thickness_nm * 1e-9 for entry in snapshot.stackup.layers
                              if entry.type != "dielectric" and entry.thickness_nm},
    })


def all_keys(snapshot: model.BoardSnapshot) -> frozenset[Key]:
    keys = {(t.layer, "tracks") for t in snapshot.tracks} | {(a.layer, "arcs") for a in snapshot.arcs}
    keys |= {group for pad in snapshot.pads for group in pad.groups}
    keys |= {(z.layer, "zones") for z in snapshot.zones}
    keys |= {(g.layer, "graphics") for g in snapshot.graphics}
    keys |= {("", "vias"), ("", "outline"), ("", "footprints"), ("", "stackup")}
    return frozenset(keys)


def snapshot_frames(snapshot: model.BoardSnapshot, revision: int = 1,
                    origin_nm: tuple[int, int] | None = None, board_path: str = "",
                    appearance: dict | None = None, export: dict | None = None) -> list[bytes]:
    """Everything Blender needs to show a board from scratch, in apply order."""
    return [encode_frame({"type": "snapshot_begin", "revision": revision}),
            board_message(snapshot, revision, origin_nm, board_path, appearance, export),
            *messages_for(snapshot, all_keys(snapshot), revision),
            encode_frame({"type": "snapshot_end", "revision": revision})]
