"""The bridge's only KiCad dependency. Every board call here is read-only."""

import math
import queue
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from kipy import KiCad
from kipy.proto.board.board_pb2 import BoardStackupLayerType
from kipy.proto.board.board_types_pb2 import BoardLayer, DrillShape
from kipy.proto.common import ApiStatusCode, commands
from kipy.proto.common.types import KIID
from kipy.util.board_layer import CANONICAL_LAYER_NAMES, is_copper_layer

from . import model
from .diff import Key, combine, fingerprint, item_digest
from .geometry import (circle_ring, outline_crossings, outline_warning, polyline_strokes, sample_arc,
                       sample_bezier, stroke)

# Tracks and vias are polled every cycle (~13 ms); the rest only on the slow
# tier, after an edit, or when KiCad was busy since the last read.
FAST_SOURCES = {"tracks": "get_tracks", "vias": "get_vias"}
SLOW_SOURCES = {
    "pads": "get_pads", "footprints": "get_footprints", "zones": "get_zones",
    "shapes": "get_shapes", "stackup": "get_stackup", "enabled_layers": "get_enabled_layers",
}
_DRILL_SHAPES = {DrillShape.DS_OBLONG: "oval", DrillShape.DS_CIRCLE: "round"}


class KiCadBusy(Exception):
    """KiCad is running an interactive tool; the edit is not committed yet."""


def _is_busy(error: Exception) -> bool:
    return getattr(error, "code", None) == ApiStatusCode.AS_BUSY


@contextmanager
def _busy_aware() -> Iterator[None]:
    """Turn KiCad's AS_BUSY reply into KiCadBusy; every other error passes through."""
    try:
        yield
    except Exception as exc:
        if _is_busy(exc):
            raise KiCadBusy(str(exc)) from exc
        raise


def canonical_layer(layer) -> str:
    return CANONICAL_LAYER_NAMES.get(layer, BoardLayer.Name(layer).removeprefix("BL_"))


# A closed KiCad can keep running without a window and keep the API (seen 2026-09-25:
# a windowless kicad.exe with 3.2 GB held the pipe until it was ended).
if sys.platform == "win32":
    _LEFTOVER = ("If no other KiCad is open, look in Task Manager for a kicad.exe without a window "
                 "and end it, then reopen this KiCad.")
else:
    _LEFTOVER = ("If no other KiCad is open, check for a leftover KiCad process (pgrep -a kicad) "
                 "and end it, then reopen this KiCad.")


def explain_connection_error(exc: Exception) -> str:
    """What to tell the user when the open board cannot be reached.

    Windows lets only one KiCad at a time serve plugins: a second instance's API
    server never starts (KiCad issue #20880, open in 10.0), and a plugin launched
    from it reaches the first instance, which refuses its token (measured, 10.0.3).
    """
    if getattr(exc, "code", None) == ApiStatusCode.AS_TOKEN_MISMATCH:
        return ("This KiCad has no plugin connection: another KiCad instance holds it. On Windows only "
                "one KiCad at a time can serve plugins (KiCad issue #20880). Close the other KiCad, "
                "then close and reopen this one. " + _LEFTOVER)
    if isinstance(exc, ConnectionError) or "Connection refused" in str(exc):
        return ("KiCad's plugin connection is not reachable. Check that Preferences > Plugins > "
                "Enable KiCad API is on. Another KiCad may be holding it. " + _LEFTOVER)
    return f"{type(exc).__name__}: {exc}"


def connect_board(timeout_ms: int = 3000):
    """Connect to the PCB editor without changing its document or selection."""
    return KiCad(timeout_ms=timeout_ms).get_board()


def saved_board_path(board) -> str:
    """Resolve the official document specifier without opening or changing it."""
    document = getattr(board, "_doc", None)
    filename = getattr(document, "board_filename", "") or ""
    if not filename:
        return ""
    path = Path(filename)
    if not path.is_absolute():
        project = getattr(document, "project", None)
        project_path = Path(getattr(project, "path", "") or "")
        if not str(project_path) or str(project_path) == ".":
            return ""
        path = (project_path.parent if project_path.suffix else project_path) / path
    return str(path.resolve()) if path.is_file() else ""


def board_text(board) -> str:
    """The open board, unsaved edits included, in KiCad's file format.

    `SaveDocumentToString` serializes the document in memory; it does not save,
    rename or mark the board (unlike `save`/`save_as`, which the bridge never calls).
    Measured at ~75 ms for the 2.6 MB reference board.
    """
    with _busy_aware():
        return board.get_as_string()


def selected_ids(board) -> frozenset[str]:
    """Ids of everything selected in KiCad (read only; ~0.16 ms), groups expanded.

    KiCad 10.0.3's API has no generator type: a selected tuning pattern (meander)
    arrives as a `Group` whose `items` are its member tracks (measured), and a
    type-filtered request drops it. So the selection is read unfiltered, and every
    group contributes its members; `selection` ignores ids it has no use for (text, ...).
    """
    with _busy_aware():
        items = board.get_selection()
    ids = set()
    for item in items:
        ids.add(item.id.value)
        proto = getattr(item, "proto", None)  # kipy's Group.items wrapper returns [] (measured)
        if proto is not None and hasattr(proto, "items"):
            ids.update(member.value for member in proto.items)
    return frozenset(ids)


def net_classes(board, names) -> dict[str, tuple[str, str]]:
    """Net name -> (its effective netclass, that netclass's tuning profile or ""), read
    only. KiCad resolves the netclass (patterns, schematic directives, composites);
    kipy 0.7 has no accessor for the tuning profile, so it is read from the proto."""
    wanted = set(names)
    with _busy_aware():
        nets = [net for net in board.get_nets() if net.name in wanted]
        classes = board.get_netclass_for_nets(nets) if nets else {}
    found = {}
    for name, netclass in classes.items():
        proto = getattr(netclass, "proto", None)
        settings = proto.board if proto is not None and proto.HasField("board") else None
        profile = settings.tuning_profile if settings is not None and settings.HasField("tuning_profile") else ""
        found[name] = (netclass.name, profile)
    return found


def select_in_kicad(board, ids, extend: bool = False) -> None:
    """The one KiCad-changing call (tests/test_boundaries.py allows only this one):
    replace (or extend) KiCad's selection with these item ids after a click in
    Blender. Selection is editor state only; nothing on the board changes."""
    with _busy_aware():
        if not extend:
            board.clear_selection()
        if ids:
            board.add_to_selection([SimpleNamespace(id=KIID(value=item_id)) for item_id in ids])


def kicad_tools(board) -> dict:
    """Where the running KiCad keeps kicad-cli, and its version (for its config folder)."""
    tools = {}
    client = getattr(board, "_kicad", None)
    if client is None:
        return tools
    try:
        request = commands.GetKiCadBinaryPath()
        request.binary_name = "kicad-cli"
        tools["kicad_cli"] = client.send(request, commands.PathResponse).path
    except Exception:
        pass  # optional: Blender then gets an empty "kicad_cli" path
    try:
        version = client.send(commands.GetVersion(), commands.GetVersionResponse).version
        tools["kicad_version"] = f"{version.major}.{version.minor}"
    except Exception:
        pass  # optional: board_specs keeps its default settings folder
    return tools


def _point(value) -> model.Point:
    return int(value.x), int(value.y)


def _append_arc(points: list[model.Point], start: model.Point, mid: model.Point, end: model.Point) -> None:
    sampled = sample_arc(start, mid, end)
    points.extend(sampled[1:] if points and points[-1] == sampled[0] else sampled)


def _closed_ring(points: list[model.Point]) -> model.Ring:
    if len(points) > 1 and points[-1] == points[0]:
        points.pop()
    return tuple(points)


def _ring(polyline) -> model.Ring:
    """Consecutive duplicate points dropped, arcs sampled, closing point removed.

    Two loops with the same steps: the public proto avoids allocating two Python
    wrappers per fill vertex, and an inline loop beats a shared generator by ~10%
    on 50k-vertex fills (measured); test doubles only have the wrapper API.
    """
    points: list[model.Point] = []
    proto = getattr(polyline, "proto", None)
    if proto is not None:
        for node in proto.nodes:
            if node.HasField("point"):
                point = (node.point.x_nm, node.point.y_nm)
                if not points or points[-1] != point:
                    points.append(point)
            elif node.HasField("arc"):
                arc = node.arc
                _append_arc(points, (arc.start.x_nm, arc.start.y_nm), (arc.mid.x_nm, arc.mid.y_nm),
                            (arc.end.x_nm, arc.end.y_nm))
        return _closed_ring(points)
    for node in polyline.nodes:
        if node.has_point:
            point = _point(node.point)
            if not points or points[-1] != point:
                points.append(point)
        elif node.has_arc:
            arc = node.arc
            _append_arc(points, _point(arc.start), _point(arc.mid), _point(arc.end))
    return _closed_ring(points)


def _polygon(value) -> model.Polygon:
    return (_ring(value.outline), *(_ring(hole) for hole in value.holes))


# KiCad joins Edge.Cuts ends up to 10 um apart: KiCad 10.0.6's DRC accepts a 9.9 um gap and
# reports invalid_outline at 10.1 um (measured). Boards carry such gaps (CM5 MINIMA: 33 nm).
CHAIN_TOLERANCE_NM = 10_000


def _near(a: model.Point, b: model.Point) -> bool:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 <= CHAIN_TOLERANCE_NM ** 2


def _nearest_part(remaining: list[list[model.Point]], end: model.Point) -> tuple[int, bool] | None:
    """(index, reversed) of the stroke with an end nearest `end`, within the tolerance."""
    best = None
    for index, part in enumerate(remaining):
        for flipped, point in ((False, part[0]), (True, part[-1])):
            gap = (point[0] - end[0]) ** 2 + (point[1] - end[1]) ** 2
            if gap <= CHAIN_TOLERANCE_NM ** 2 and (best is None or gap < best[0]):
                best = (gap, index, flipped)
    return None if best is None else best[1:]


def _stitch_outline(parts: list[tuple[model.Point, ...]], warnings: list[str],
                    problems: list[model.Point]) -> list[model.Polygon]:
    """Join Edge.Cuts strokes whose ends meet within KiCad's chaining tolerance; never
    union or triangulate."""
    remaining = [list(part) for part in parts if len(part) >= 2]
    polygons: list[model.Polygon] = []
    while remaining:
        chain = remaining.pop()
        turned = False  # stuck at one end: grow from the other before calling it open
        while not (len(chain) >= 4 and _near(chain[-1], chain[0])):
            match = _nearest_part(remaining, chain[-1])
            if match is None and not turned:
                chain.reverse()
                turned = True
                continue
            if match is None:
                warnings.append(outline_warning("open Edge.Cuts chain left out", chain[-1]))
                problems += [chain[0], chain[-1]]  # both loose ends
                break
            index, flipped = match
            part = remaining.pop(index)
            chain.extend(list(reversed(part[:-1])) if flipped else part[1:])
        else:
            ring = tuple(chain[:-1])
            if len(ring) >= 3:
                polygons.append((ring,))
    return polygons


def _outline(shapes, warnings: list[str]) -> model.Outline:
    polygons: list[model.Polygon] = []
    parts: list[tuple[model.Point, ...]] = []
    problems: list[model.Point] = []
    before = len(warnings)
    for shape in shapes:
        if shape.layer != BoardLayer.BL_Edge_Cuts:
            continue
        if hasattr(shape, "polygons"):
            polygons.extend(_polygon(p) for p in shape.polygons)
        elif hasattr(shape, "top_left") and hasattr(shape, "bottom_right"):
            left, top = _point(shape.top_left)
            right, bottom = _point(shape.bottom_right)
            polygons.append((((left, top), (right, top), (right, bottom), (left, bottom)),))
        elif hasattr(shape, "radius_point") and hasattr(shape, "center"):
            cx, cy = _point(shape.center)
            px, py = _point(shape.radius_point)
            radius = math.hypot(px - cx, py - cy)
            if radius:
                polygons.append((circle_ring((cx, cy), radius),))
        elif hasattr(shape, "control1") and hasattr(shape, "control2"):
            parts.append(sample_bezier(_point(shape.start), _point(shape.control1),
                                       _point(shape.control2), _point(shape.end)))
        elif hasattr(shape, "mid"):
            parts.append(sample_arc(_point(shape.start), _point(shape.mid), _point(shape.end)))
        elif hasattr(shape, "start") and hasattr(shape, "end"):
            parts.append((_point(shape.start), _point(shape.end)))
        else:
            warnings.append(outline_warning(f"unsupported Edge.Cuts shape {type(shape).__name__}"))
    strokes = [ring + ring[:1] for polygon in polygons for ring in polygon] + [tuple(part) for part in parts]
    polygons.extend(_stitch_outline(parts, warnings, problems))
    if not polygons:
        warnings.append(outline_warning("no closed Edge.Cuts outline"))
    for warning, where in outline_crossings(polygons):
        warnings.append(warning)
        problems.append(where)
    if len(warnings) == before:
        return model.Outline(tuple(polygons))
    return model.Outline(tuple(polygons), tuple(strokes), tuple(problems))


def _copper_graphic(shape) -> model.CopperGraphic | None:
    """A board or footprint shape on a copper layer as filled polygons, or None."""
    attributes = getattr(shape, "attributes", None)
    width = max(0, int(attributes.stroke.width)) if attributes is not None else 0
    filled = bool(attributes.fill.filled) if attributes is not None else False
    polygons: tuple = ()
    if hasattr(shape, "polygons"):
        rings = [_polygon(p) for p in shape.polygons]
        polygons = tuple(rings) if filled else ()
        if width:
            polygons += tuple(part for polygon in rings for ring in polygon
                              for part in polyline_strokes(ring, width, closed=True))
    elif hasattr(shape, "top_left") and hasattr(shape, "bottom_right"):
        (left, top), (right, bottom) = _point(shape.top_left), _point(shape.bottom_right)
        corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
        polygons = ((tuple(corners),),) if filled else ()
        if width:
            polygons += polyline_strokes(corners, width, closed=True)
    elif hasattr(shape, "radius_point") and hasattr(shape, "center"):
        center, edge = _point(shape.center), _point(shape.radius_point)
        radius = math.hypot(edge[0] - center[0], edge[1] - center[1])
        outer, inner = radius + width / 2, radius - width / 2
        if filled or inner <= 0:
            polygons = ((circle_ring(center, outer),),) if outer > 0 else ()
        elif width:
            polygons = ((circle_ring(center, outer), circle_ring(center, inner)),)  # a ring: outer, hole
    elif width and hasattr(shape, "control1") and hasattr(shape, "control2"):
        polygons = polyline_strokes(sample_bezier(_point(shape.start), _point(shape.control1),
                                                  _point(shape.control2), _point(shape.end)), width)
    elif width and hasattr(shape, "mid"):
        polygons = polyline_strokes(sample_arc(_point(shape.start), _point(shape.mid), _point(shape.end)), width)
    elif width and hasattr(shape, "start") and hasattr(shape, "end"):
        polygons = (stroke(_point(shape.start), _point(shape.end), width),)
    if not polygons:
        return None
    net = getattr(getattr(shape, "net", None), "name", "") or ""
    return model.CopperGraphic(shape.id.value, net, canonical_layer(shape.layer), polygons)


def _copper_graphics(raw_shapes, raw_footprints) -> tuple[model.CopperGraphic, ...]:
    """Copper drawn as graphics: board shapes and footprint shapes on copper layers."""
    shapes = list(raw_shapes) + [item for footprint in raw_footprints
                                 for item in getattr(footprint.definition, "shapes", ())]
    graphics = (_copper_graphic(shape) for shape in shapes if canonical_layer(shape.layer).endswith(".Cu"))
    return tuple(graphic for graphic in graphics if graphic is not None)


def _optional_float(wrapper, field: str) -> float | None:
    value = float(getattr(wrapper, field))
    proto = getattr(wrapper, "proto", None)
    if proto is not None:
        try:
            if not proto.HasField(field):
                return None
            return value
        except ValueError:  # proto3 scalar without presence information
            pass
    return value if value != 0 else None


def _stackup(source, layer_name, warnings: list[str]) -> model.Stackup:
    if not source.layers:
        warnings.append("Board stackup is missing; physical properties are unavailable")
        return model.Stackup(())
    result: list[model.StackupLayer] = []
    for index, layer in enumerate(source.layers):
        layer_type = BoardStackupLayerType.Name(layer.type).removeprefix("BSLT_").lower()
        name = (layer_name(layer.layer) if layer.layer != BoardLayer.BL_UNDEFINED
                else (layer.user_name or f"Dielectric {index + 1}"))
        # kicad-python 0.8 represents an absent dielectric block as None;
        # 0.7 exposed an empty wrapper. Neither supplies dielectric values.
        dielectric = layer.dielectric
        sublayers = dielectric.layers if dielectric is not None else ()
        if sublayers:
            for subindex, sublayer in enumerate(sublayers):
                result.append(model.StackupLayer(
                    name if len(sublayers) == 1 else f"{name} / {subindex + 1}",
                    layer_type,
                    int(sublayer.thickness) or None,
                    sublayer.material_name or None,
                    _optional_float(sublayer, "epsilon_r"),
                    _optional_float(sublayer, "loss_tangent"),
                ))
        else:
            result.append(model.StackupLayer(
                name, layer_type, int(layer.thickness) or None,
                layer.material_name or None, None, None,
            ))
    dielectrics = [layer for layer in result if layer.type == "dielectric"]
    if dielectrics and all(layer.epsilon_r is None for layer in dielectrics):
        # Measured on KiCad 10.0.3: IPC returns an empty dielectric block even
        # when the saved board defines material, epsilon_r and loss tangent.
        warnings.append("Stackup has no dielectric properties from KiCad IPC; "
                        "epsilon_r and loss tangent are unavailable")
    return model.Stackup(tuple(result))


def _convert_track(item) -> model.Track | model.Arc:
    common = (item.id.value, canonical_layer(item.layer), item.net.name, _point(item.start))
    if hasattr(item, "mid"):
        return model.Arc(*common, _point(item.mid), _point(item.end), int(item.width))
    return model.Track(*common, _point(item.end), int(item.width))


def _convert_via(item) -> model.Via:
    return model.Via(
        item.id.value, item.net.name, _point(item.position), int(item.diameter),
        int(item.drill_diameter), canonical_layer(item.padstack.drill.start_layer),
        canonical_layer(item.padstack.drill.end_layer),
    )


def _convert_footprint(item) -> tuple[model.Footprint, tuple[str, ...]]:
    """The footprint record plus the ids of its pads."""
    return model.Footprint(
        item.id.value, item.reference_field.text.value, _point(item.position),
        float(item.orientation.to_radians()),
        "bottom" if item.layer == BoardLayer.BL_B_Cu else "top",
        tuple(model_item.filename for model_item in item.definition.models),
        model_visible=tuple(bool(model_item.visible) for model_item in item.definition.models),
    ), tuple(child.id.value for child in item.definition.pads)


def _convert_pad(item, shapes: dict[str, dict[str, tuple[model.Polygon, ...]]]) -> model.Pad:
    """The pad record; its footprint id is filled in by the caller."""
    diameter = _point(item.padstack.drill.diameter)
    return model.Pad(item.id.value, "", item.number, item.net.name, _point(item.position),
                     diameter if diameter != (0, 0) else None, dict(shapes.get(item.id.value, {})),
                     _DRILL_SHAPES.get(item.padstack.drill.shape), float(item.padstack.angle.to_radians()))


def _convert_zone(item) -> tuple[model.ZoneFill, ...]:
    if item.is_rule_area():
        return ()
    return tuple(
        model.ZoneFill(item.id.value, item.net.name, canonical_layer(layer),
                       tuple(_polygon(polygon) for polygon in polygons))
        for layer, polygons in item.filled_polygons.items() if is_copper_layer(layer)
    )


def _pad_polygons(board, call, raw_pads, layers) -> dict[str, dict[str, tuple[model.Polygon, ...]]]:
    """Copper and paste shapes of the given pads, per canonical layer (two RPCs per layer)."""
    polygons_by_pad: dict[str, dict[str, tuple[model.Polygon, ...]]] = defaultdict(dict)
    if not raw_pads or not layers:
        return polygons_by_pad
    presence = call("check_padstack_presence_on_layers",
                    board.check_padstack_presence_on_layers, raw_pads, layers)
    for layer in layers:
        candidates = [pad for pad in raw_pads if presence.get(pad, {}).get(layer, False)]
        if not candidates:
            continue
        shapes = call("get_pad_shapes_as_polygons", board.get_pad_shapes_as_polygons, candidates, layer)
        if len(shapes) != len(candidates):
            # kipy's list overload omits absent polygons rather than returning None slots.
            shapes = [call("get_pad_shapes_as_polygons", board.get_pad_shapes_as_polygons, pad, layer)
                      for pad in candidates]
        for pad, shape in zip(candidates, shapes):
            if shape is not None:
                polygons_by_pad[pad.id.value][canonical_layer(layer)] = (_polygon(shape),)
    return polygons_by_pad


def _track_groups(raw_tracks, digests) -> dict[Key, list[bytes]]:
    groups: dict[Key, list[bytes]] = defaultdict(list)
    for item, digest in zip(raw_tracks, digests):
        groups[(canonical_layer(item.layer), "arcs" if hasattr(item, "mid") else "tracks")].append(digest)
    return groups


def _timed_call(board, method: str) -> tuple[object, Exception | None, float]:
    """(result, error, ms) of one board getter; the error is re-raised by the polling thread."""
    before = time.perf_counter_ns()
    try:
        return getattr(board, method)(), None, (time.perf_counter_ns() - before) / 1e6
    except Exception as exc:
        return None, exc, (time.perf_counter_ns() - before) / 1e6


class _ConnectionPool:
    """One KiCad connection per worker thread; a connection never serves two requests at once.

    KiCad answers mostly one request at a time, but Python decodes one reply while KiCad
    works on the next: 4 calls took 29 ms in parallel versus 41 ms sequentially (measured).
    """

    def __init__(self, boards):
        self._boards: queue.Queue = queue.Queue()
        for board in boards:
            self._boards.put(board)
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(len(boards), thread_name_prefix="kicad-ipc",
                                            initializer=self._claim)

    def _claim(self):
        self._local.board = self._boards.get_nowait()

    def _run(self, method: str) -> tuple[object, Exception | None, float]:
        return _timed_call(self._local.board, method)

    def read(self, methods: dict[str, str]) -> dict[str, tuple[object, Exception | None, float]]:
        futures = {source: self._executor.submit(self._run, method) for source, method in methods.items()}
        return {source: future.result() for source, future in futures.items()}

    def close(self) -> None:
        self._executor.shutdown(wait=True)


@dataclass(frozen=True)
class PollResult:
    snapshot: model.BoardSnapshot
    dirty: frozenset[Key]  # (layer, kind) groups whose content changed since the last poll
    full_read: bool


class BoardReader:
    """Incremental reader: fingerprint raw IPC items, convert only new or changed items.

    `pool_boards` are extra connections to the same board for parallel reads (see
    `connect_reader`). One reader per open board; create a new one after a board switch.
    """

    def __init__(self, board, slow_interval_s: float = 1.0, clock=time.monotonic, pool_boards=()):
        self.board = board
        self.slow_interval_s = slow_interval_s
        self._clock = clock
        self._pool = _ConnectionPool(pool_boards) if pool_boards else None
        self._last_slow: float | None = None
        self._busy_since_read = True
        self._hashes: dict[object, bytes] = {}
        self._pad_layers: list = []
        self._cache: dict[str, dict[bytes, object]] = {}  # source -> item digest -> converted record
        self._parts: dict[str, object] = {
            "tracks": (), "arcs": (), "vias": (), "footprints": (), "pads": (), "zones": (),
            "outline": model.Outline(()), "stackup": model.Stackup(()), "display": {}, "graphics": (),
            "footprint_by_pad": {},
        }
        self._warnings: dict[str, list[str]] = {}
        self._timings: dict[str, float] = {}  # of the poll in progress, in ms

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    # --- Timed KiCad calls --------------------------------------------------------------

    def _timed(self, name: str, function, *args):
        before = time.perf_counter_ns()
        result = function(*args)
        self._timings[name] = self._timings.get(name, 0.0) + (time.perf_counter_ns() - before) / 1e6
        return result

    def _call(self, name: str, function, *args):
        try:
            return self._timed(name, function, *args)
        except Exception as exc:
            self._raise(exc)

    def _digests(self, items) -> list[bytes]:
        return self._timed("hash", lambda: [item_digest(item) for item in items])

    def _read(self, methods: dict[str, str]) -> dict[str, object]:
        if self._pool is None:
            replies = {source: _timed_call(self.board, method) for source, method in methods.items()}
        else:
            replies = self._pool.read(methods)
        raw = {}
        for source, (result, error, ms) in replies.items():
            self._timings[methods[source]] = self._timings.get(methods[source], 0.0) + ms
            if error is not None:
                self._raise(error)
            raw[source] = result
        return raw

    def _raise(self, error: Exception):
        if _is_busy(error):
            self._busy_since_read = True
            raise KiCadBusy(str(error)) from error
        raise error

    # --- Change detection ---------------------------------------------------------------

    def _slow_due(self) -> bool:
        return (self._busy_since_read or self._last_slow is None
                or self._clock() - self._last_slow >= self.slow_interval_s)

    def _slow_hashes(self, raw: dict[str, object], digests: dict[str, list[bytes]]) -> dict[object, bytes]:
        """Hashes of the slow-tier sources; also stores their item digests in `digests`."""
        hashes: dict[object, bytes] = {}
        for source in ("pads", "footprints", "zones"):
            digests[source] = self._digests(raw[source])
            hashes[source] = combine(digests[source])
        hashes["shapes"] = self._timed("hash", fingerprint, raw["shapes"])
        hashes["stackup"] = self._timed("hash", fingerprint, [raw["stackup"]])
        hashes["enabled_layers"] = self._timed("hash", fingerprint, raw["enabled_layers"])
        return hashes

    def _convert_cached(self, source, items, digests, convert):
        """Reuse records for unchanged digests. Returns (all, added, removed) records."""
        old = self._cache.get(source, {})
        new = {digest: old[digest] if digest in old else convert(item) for item, digest in zip(items, digests)}
        self._cache[source] = new
        return ([new[d] for d in digests], [new[d] for d in new.keys() - old.keys()],
                [old[d] for d in old.keys() - new.keys()])

    def poll(self, full: bool = False) -> PollResult:
        """Raises KiCadBusy while an interactive KiCad tool runs; retry on the next poll."""
        started = time.perf_counter_ns()
        self._timings = timings = {}
        slow = full or self._slow_due()
        raw = self._read(FAST_SOURCES if not slow else {**FAST_SOURCES, **SLOW_SOURCES})
        digests = {source: self._digests(raw[source]) for source in ("tracks", "vias")}
        hashes: dict[object, bytes] = {key: combine(group)
                                       for key, group in _track_groups(raw["tracks"], digests["tracks"]).items()}
        hashes["vias"] = combine(digests["vias"])
        fast_keys = set(hashes) | {k for k in self._hashes if k not in SLOW_SOURCES}
        if not slow and any(hashes.get(k) != self._hashes.get(k) for k in fast_keys):
            slow = True  # an edit just landed; footprints or pads may have changed with it
            raw.update(self._read(SLOW_SOURCES))
        if slow:
            hashes.update(self._slow_hashes(raw, digests))
            self._last_slow = self._clock()
            self._busy_since_read = False
        else:
            hashes.update({k: v for k, v in self._hashes.items() if k in SLOW_SOURCES})

        changed = {k for k in hashes.keys() | self._hashes.keys() if hashes.get(k) != self._hashes.get(k)}
        dirty = self._convert_changed(raw, digests, changed)
        self._hashes = hashes
        timings["total"] = (time.perf_counter_ns() - started) / 1e6
        parts, warnings = self._parts, self._warnings
        snapshot = model.BoardSnapshot(
            self.board.name, dict(parts["display"]), parts["tracks"], parts["arcs"], parts["vias"],
            parts["pads"], parts["footprints"], parts["zones"], parts["outline"], parts["stackup"],
            tuple(w for group in warnings.values() for w in group), timings,
            graphics=parts["graphics"],
        )
        return PollResult(snapshot, frozenset(dirty), slow)

    # --- Conversion of changed sources (order matters: pads need footprints and layers) --

    def _convert_changed(self, raw: dict[str, object], digests: dict[str, list[bytes]],
                         changed: set) -> set[Key]:
        """Update `_parts` for every changed source; returns the dirty display groups."""
        dirty: set[Key] = {k for k in changed if isinstance(k, tuple)}
        if dirty:  # only track/arc groups so far
            self._convert_tracks(raw["tracks"], digests["tracks"])
        if "vias" in changed:
            records, _, _ = self._timed("convert", self._convert_cached, "vias", raw["vias"],
                                        digests["vias"], _convert_via)
            self._parts["vias"] = tuple(records)
            dirty.add(("", "vias"))
        if "footprints" in changed:
            self._convert_footprints(raw["footprints"], digests["footprints"])
            dirty.add(("", "footprints"))
        if "enabled_layers" in changed:
            dirty |= self._set_pad_layers(raw["enabled_layers"])
        if changed & {"stackup", "enabled_layers"}:
            self._convert_stackup(raw["stackup"], raw["enabled_layers"])
            dirty.add(("", "stackup"))
        if changed & {"pads", "footprints", "enabled_layers"}:
            dirty |= self._convert_pads(raw["pads"], digests["pads"])
        if "zones" in changed:
            records, added, removed = self._timed("convert", self._convert_cached, "zones", raw["zones"],
                                                  digests["zones"], _convert_zone)
            self._parts["zones"] = tuple(fill for fills in records for fill in fills)
            dirty |= {(fill.layer, "zones") for fills in added + removed for fill in fills}
        if "shapes" in changed:
            self._warnings["outline"] = []
            self._parts["outline"] = self._timed("convert", _outline, raw["shapes"], self._warnings["outline"])
            dirty.add(("", "outline"))
        if changed & {"shapes", "footprints"}:
            old = set(self._parts["graphics"])
            self._parts["graphics"] = self._timed("convert", _copper_graphics, raw["shapes"], raw["footprints"])
            dirty |= {(graphic.layer, "graphics") for graphic in old ^ set(self._parts["graphics"])}
        return dirty

    def _convert_tracks(self, raw_tracks, digests: list[bytes]) -> None:
        records, _, _ = self._timed("convert", self._convert_cached, "tracks", raw_tracks,
                                    digests, _convert_track)
        self._parts["tracks"] = tuple(r for r in records if isinstance(r, model.Track))
        self._parts["arcs"] = tuple(r for r in records if isinstance(r, model.Arc))

    def _convert_footprints(self, raw_footprints, digests: list[bytes]) -> None:
        records, _, _ = self._timed("convert", self._convert_cached, "footprints", raw_footprints,
                                    digests, _convert_footprint)
        get_box = self.board.get_item_bounding_box
        boxes = self._call("get_item_bounding_box", get_box, raw_footprints) if raw_footprints else []
        if len(boxes) != len(records):
            boxes = [self._call("get_item_bounding_box", get_box, item) for item in raw_footprints]
        self._parts["footprints"] = tuple(
            replace(record, bbox_nm=(int(box.pos.x), int(box.pos.y),
                                     int(box.size.x), int(box.size.y))) if box is not None else record
            for (record, _), box in zip(records, boxes)
        )
        self._parts["footprint_by_pad"] = {pad: record.id for record, pads in records for pad in pads}

    def _set_pad_layers(self, enabled_layers) -> set[Key]:
        """Layers whose pad shapes are read; every existing pad group is redrawn."""
        dirty = {group for record in self._parts["pads"] for group in record.groups}
        self._pad_layers = [layer for layer in enabled_layers if is_copper_layer(layer)]
        # Stencil apertures: KiCad's pad shape on the paste layer (~2 ms per layer, measured).
        self._pad_layers += [layer for layer in enabled_layers
                             if layer in (BoardLayer.BL_F_Paste, BoardLayer.BL_B_Paste)]
        self._cache.pop("pads", None)  # pad shapes depend on which copper layers exist
        return dirty

    def _convert_stackup(self, raw_stackup, enabled_layers) -> None:
        get_name = self.board.get_layer_name
        self._parts["display"] = {canonical_layer(layer): self._call("get_layer_name", get_name, layer)
                                  for layer in enabled_layers}
        self._warnings["stackup"] = []
        self._parts["stackup"] = self._timed("convert", _stackup, raw_stackup, canonical_layer,
                                             self._warnings["stackup"])

    def _convert_pads(self, raw_pads, digests: list[bytes]) -> set[Key]:
        footprint_by_pad = self._parts["footprint_by_pad"]
        cached = self._cache.get("pads", {})
        fresh = [pad for pad, digest in zip(raw_pads, digests) if digest not in cached]
        shapes = _pad_polygons(self.board, self._call, fresh, self._pad_layers)  # RPCs for new pads only
        records, added, removed = self._timed("convert", self._convert_cached, "pads", raw_pads, digests,
                                              lambda item: _convert_pad(item, shapes))
        owner = [footprint_by_pad.get(record.id, "") for record in records]
        self._parts["pads"] = tuple(record if record.footprint_id == fp else replace(record, footprint_id=fp)
                                    for record, fp in zip(records, owner))
        self._warnings["pads"] = [f"Pad {record.id} has no matching footprint in the snapshot"
                                  for record, fp in zip(records, owner) if not fp]
        return {group for record in added + removed for group in record.groups}


def connect_reader(connections: int = 4, timeout_ms: int = 3000, **options) -> BoardReader:
    """A reader with `connections` parallel IPC connections (1 = sequential)."""
    boards = [connect_board(timeout_ms) for _ in range(max(1, connections))]
    return BoardReader(boards[0], pool_boards=boards if connections > 1 else (), **options)


def read_snapshot(board) -> model.BoardSnapshot:
    """One complete read (used by the dump command and tests)."""
    return BoardReader(board).poll(full=True).snapshot
