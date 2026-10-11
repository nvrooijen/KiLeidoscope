"""Findings files (`kls-findings 1`): read, check, resolve.

A findings file is untrusted data: an LLM, a tool, a script or our own DRC converter
wrote it. `parse` checks every field against fixed limits and keeps only plain text,
numbers and target strings; nothing in it is ever opened, run or read as markup.
`resolve_file` finds the targets on the live board (`targets.resolve`) and turns the file
into the `findings` payload Blender draws: item ids, anchor points and measured distances
in nm, KiCad's board frame (y down).

The draw tools (`TOOLS`) mark and annotate: `highlight`, `isolate`, `label`, `arrow`,
`distance`, `ghost` (a suggested via, cap or pad, a part moved, a route, a row of vias:
each checked for fit where the file puts it, never moved; KiCad's own DRC judges it too,
`ghost_drc`, from the quick verdict kept in its `kicad_drc`), `area` (copper
items or a polygon on one layer), `layers`, and `bars` (the file's own numbers as a
chart). The measured ones: `clearance` and `edge_gap` (closest copper points), `width`
(every track segment with its width), `routed_length` (the copper path between two pads,
`BoardIndex.route`). Planes are measured with `planes`: `layer_change` (a signal via's
nearest return via), `plane_gap` and `return_path` (where a track has no plane under it),
`plane_islands` (a net's fill pieces and what ties them), `stitching` (a net's vias along
a pour's outline or the board edge, their largest spacing), `stub` (the unused barrel of
a via or plated hole, its length from the stackup) and `discontinuity` (what changes at
points of a net: width, layer, reference plane). A plane is named `net@layer` and must be
fill of that net on that layer.

Every measured draw names what it measured in `what`, so Blender labels them all alike;
a finding whose every measured limit is met `passes`, and a claimed value the bridge
measured too is listed in `disagree` when the two are far apart. Confirm/dismiss state is
keyed on the check and the named targets (`named`, `finding_key`), so it survives
re-routing that changes uuids. Every file is drawn; `handshake` says where it came from:
a run the bridge made, another run, or outside any run. A file from a run the bridge made
has its `stamp` compared with the live board's: `changed` says the board was edited since.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from . import mentions, model, planes
from .board_index import BoardIndex, Gap, _bbox_gap, _inside, centre, copper_rank, on_layer, point_gap, shape_of
from .board_index import gap as shapes_gap
from .board_index import top_layer, union
from .geometry import sample_arc
from .targets import SNAP_NM, Resolved, copper_layer, resolve
from .targets import kind as kind_of

FORMAT = "kls-findings 1"
MAX_FILE_BYTES = 2 << 20
MAX_FINDINGS = 300
MAX_PER_CHECK = 20  # findings of one check kept; the rest only counted (`_kept`)
MAX_DRAWS = 20
MAX_TEXT = 300
MAX_VALUES = 20
MAX_SKIPPED = 300
MAX_TARGETS = 50  # per list
MAX_TARGET_TEXT = 200
MAX_POLYGON = 1000
MAX_IDS_DRAW = 5_000
MAX_IDS_FILE = 100_000
MAX_PAIRS = 250_000  # item pairs one edge distance may measure
MAX_SEGMENTS = 2_000  # width: track segments drawn; plane_gap, return_path: stretches of each kind
MAX_EDGE_POINTS = 4_000  # plane_gap: points of the plane's edges drawn
MAX_RETURNS = 8  # layer_change: return vias drawn
MAX_ISLANDS = 64
MAX_ISLAND_POINTS = 2_000  # per island outline
MAX_PLANES = 2
MAX_STITCHES = 500  # stitching: vias drawn
MAX_GAPS = 200  # stitching: stretches over the limit drawn
GHOST_CLEAR_NM = 100_000  # a suggested via or pad this close to another net's copper clashes
TOUCH_NM = 1  # a suggestion's copper this close to another net's touches it: a short
GHOST_VIA_NM = 600_000  # a suggested via without a size
GHOST_NEAR_NM = 10_000_000  # copper whose centre is further than this from a suggested via is never near it
CAP_GHOST_NM = 1_000_000  # a cap without a size: an 0402's body (1.0 x 0.5 mm), as Blender draws it
GHOST_TRACK_NM = 200_000  # a suggested track's width when its net has no tracks to take one from
MAX_GHOST_WIDTH_MM = 50.0  # a suggested track wider than this is not a track
MAX_GHOST_PATH = 50  # points of a suggested track
MAX_GHOST_PADS = 200  # pads of a moved part drawn; past it only its body
MAX_ROW_VIAS = 100  # vias of one suggested row; more is refused, never cut
MAX_LINE_POINTS = 2_000  # stitching: points of the line followed
STITCH_REACH_NM = 2_000_000  # stitching: a via this close to the line (centre) stitches along it
MAX_SPOTS = 20  # discontinuity: points
SPOT_REACH_NM = 1_000_000  # discontinuity: the net's copper, and a change, this close to a point is at it
RETURN_LIMIT_MM = 1.0  # layer_change: a return via this close, unless the draw gives its own limit
EDGE_REACH_NM = 1_000_000  # plane_gap: the plane's edges this far around each gap
MAX_BARS = 12
MAX_BAR_LABEL = 40
MAX_BAR_VALUE = 1e12
BAR_UNITS = ("mm", "ps", "mv", "v", "a", "ma", "pct")
MAX_MM = 10_000.0  # coordinates and sizes past 10 m are not a board
DISAGREE = 0.10  # claimed vs measured, relative
DISAGREE_FLOOR_MM = 0.001  # KiCad prints 4 decimals; closer than 1 µm always agrees
KEY_GRID_NM = 500_000  # anchors in keys rounded to 0.5 mm
UNITS = ("_mm2", "_cm2", "_mm", "_ps", "_mv", "_a", "_pct")  # longest first
TOOLS = frozenset({"highlight", "isolate", "label", "arrow", "distance", "ghost", "area", "layers",  # marks
                   "clearance", "width", "routed_length", "edge_gap", "bars",  # measured geometry, charts
                   "return_path", "plane_gap", "layer_change", "plane_islands", "stitching", "stub",  # planes
                   "discontinuity"})
DRAWN = TOOLS - {"layers"}  # what Blender draws in this version
WHAT = {"clearance": "Clearance", "edge_gap": "Edge gap", "width": "Width", "routed_length": "Routed length",
        "layer_change": "Return via", "plane_gap": "Gap", "return_path": "Without plane", "plane_islands": "Islands",
        "stitching": "Stitching gap", "stub": "Stub", "discontinuity": "Discontinuity"}
OWN_IDS = frozenset({"width", "layer_change", "plane_gap", "return_path", "plane_islands", "stitching", "stub",
                     "discontinuity"})  # ids: what they measure
UNTARGETED = ("plane_islands", "stitching")  # measure the board with no target named
# claimed value -> the tools that measure it (both shown when > 10 % apart)
CLAIMS = (("distance_mm", ("distance", "clearance", "edge_gap", "layer_change")), ("width_mm", ("width",)),
          ("length_mm", ("routed_length", "return_path")), ("gap_mm", ("plane_gap",)), ("stub_mm", ("stub",)))
ROUTE_LIMIT = "tracks, arcs and vias are followed, zones are not"
ROLES = ("at", "from", "to", "a", "b", "target", "via", "pad", "crossing", "signal", "part",
         "path")  # draw fields with targets
SEVERITIES = ("error", "warning", "info")
CONFIDENCES = ("high", "medium", "low")
GHOSTS = ("via", "cap", "track", "pad", "part", "via_row")
GHOST_SPOTS = ("at", "from", "to", "path")  # a ghost's roles whose `pt:` is that point, never snapped
FIXED = (model.Track, model.Arc, model.Via, model.Pad)  # copper a suggestion must keep clear of (zones refill)
GROUPS = frozenset({"drc", "plane", "decap", "power", "dc", "hs", "dp", "emc", "analog", "thermal", "fab"})
STATES = ("confirmed", "dismissed")
NOT_CONNECTED = "KiCad not connected"

_CHECK = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_VALUE_KEY = re.compile(r"[a-z0-9_]{1,40}")
_STAMP = re.compile(r"[0-9a-f]{8}")
# C0/C1 controls, line/paragraph separators and bidi overrides (text that reads differently than it is)
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


class FindingsError(ValueError):
    """The whole file is refused: too big, not JSON, wrong format. `status` is the
    payload status to show ("error" or "unknown_format")."""

    def __init__(self, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Draw:
    tool: str
    fields: dict  # validated tool fields; targets kept as strings, lengths in mm
    label: str = ""
    step: int = 0
    emphasis: str = "normal"


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    confidence: str
    title: str
    message: str
    fix: str
    values: dict  # name -> float, unit from the name's suffix
    values_text: dict  # name -> text, shown, never compared
    draws: tuple[Draw, ...]
    problems: tuple[str, ...]  # skipped draws, cut text, bad enums


@dataclass(frozen=True)
class FindingsFile:
    run: str
    stamp: str  # "" when missing or not 8 hex
    tool: str
    findings: tuple[Finding, ...]
    skipped: tuple[tuple[str, str], ...]  # (check, reason)
    problems: tuple[str, ...]
    left_out: tuple[tuple[str, int], ...] = ()  # (check, findings of it not kept: `_kept`)


@dataclass(frozen=True)
class Context:
    """What the payload needs besides the file."""
    path: str
    source: str  # "drc" | "file"
    runs: dict  # known run id -> stamp (KlsFolder.runs())
    live_stamp: str  # stamp of KiCad's raw board text now, "" if unknown
    states: dict  # finding key -> "confirmed" | "dismissed"


# parsing

class _Bad(Exception):
    """A field that skips its draw, with the reason."""


class _TooMany(_Bad):
    """Past an id cap: the draw is left out with its ids."""


def _show(text: str, limit: int = 40) -> str:
    return _CONTROL.sub(" ", text)[:limit]


def _text(value, limit: int, what: str, problems: list) -> str | None:
    """Plain text: control characters as spaces, cut to `limit` with "…". None (and a
    problem) for anything that is not a string."""
    if not isinstance(value, str):
        problems.append(f"{what} is not text")
        return None
    text = _CONTROL.sub(" ", value).strip()
    if len(text) > limit:
        problems.append(f"{what} cut to {limit} characters")
        text = text[:limit - 1] + "…"
    return text


def _number(value) -> float | None:
    """A finite JSON number (not true/false), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _unit(key: str) -> str:
    return next((unit for unit in UNITS if key.endswith(unit)), "")


def _need(raw: dict, key: str):
    if key not in raw:
        raise _Bad(f"{key} missing")
    return raw[key]


def _target(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Bad(f"{what} is not a target")
    if len(value) > MAX_TARGET_TEXT:
        raise _Bad(f"{what} is longer than {MAX_TARGET_TEXT} characters")
    return _CONTROL.sub(" ", value).strip()


def _targets(value, what: str = "targets") -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _Bad(f"{what} is not a list of targets")
    if len(value) > MAX_TARGETS:
        raise _Bad(f"{what} has more than {MAX_TARGETS} entries")
    return tuple(_target(item, f"{what}[{i}]") for i, item in enumerate(value))


def _mm(value, what: str, positive: bool = False) -> float:
    number = _number(value)
    if number is None or abs(number) > MAX_MM or (positive and number <= 0):
        raise _Bad(f"{what} is not a {'positive ' if positive else ''}number in mm")
    return number


def _limit(raw: dict, key: str) -> float | None:
    """An optional limit in mm: None when absent, else a number that is not negative."""
    if raw.get(key) is None:
        return None
    limit = _mm(raw[key], key)
    if limit < 0:
        raise _Bad(f"{key} is negative")
    return limit


def _net(value) -> str:
    """A net name, with or without `net:` in front."""
    if not isinstance(value, str) or len(value) > MAX_TARGET_TEXT:
        raise _Bad("net is not a net name")
    name = _CONTROL.sub(" ", value).strip()
    if name[:4].lower() == "net:":
        name = name[4:].strip()
    if not name:
        raise _Bad("net is empty")
    return name


def _bars(value, problems: list) -> tuple[tuple[str, float, float | None], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BARS:
        raise _Bad(f"items is not a list of 1 to {MAX_BARS} bars")
    bars = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise _Bad(f"items[{i}] is not an object")
        label = _text(item.get("label"), MAX_BAR_LABEL, f"items[{i}] label", problems)
        if not label:
            raise _Bad(f"items[{i}] has no label")
        numbers = []
        for key in ("value", "limit"):
            number = _number(item.get(key))
            if (item.get(key) is not None or key == "value") and (number is None or abs(number) > MAX_BAR_VALUE):
                raise _Bad(f"items[{i}] {key} is not a number")
            numbers.append(number)
        bars.append((label, *numbers))
    return tuple(bars)


def _layer(value, what: str) -> str:
    layer = copper_layer(value) if isinstance(value, str) else None
    if layer is None or copper_rank(layer) is None:
        raise _Bad(f"{what} is not a copper layer")
    return layer


def _plane(value, what: str) -> tuple[str, str]:
    """A plane as (net, layer) from `net@layer`, with or without `net:` in front."""
    if not isinstance(value, str) or len(value) > MAX_TARGET_TEXT:
        raise _Bad(f"{what} is not net@layer")
    name, at, layer = _CONTROL.sub(" ", value).strip().rpartition("@")
    layer = copper_layer(layer) if at else None
    net = name.strip()
    if net[:4].lower() == "net:":
        net = net[4:].strip()
    if layer is None or not net:
        raise _Bad(f"{what} is not net@layer (e.g. GND@In1.Cu)")
    return net, layer


def _mm_text(mm: float) -> str:
    text = f"{mm:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _spot(value, what: str) -> str:
    """A target, or a point [x, y] in mm as the `pt:x,y` target it is."""
    if isinstance(value, list):
        if len(value) != 2:
            raise _Bad(f"{what} is not [x, y]")
        return f"pt:{_mm_text(_mm(value[0], what))},{_mm_text(_mm(value[1], what))}"
    return _target(value, what)


def _polygon(value) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list) or not 3 <= len(value) <= MAX_POLYGON:
        raise _Bad(f"polygon needs 3 to {MAX_POLYGON} [x, y] points")
    points = []
    for i, point in enumerate(value):
        if not isinstance(point, list) or len(point) != 2:
            raise _Bad(f"polygon[{i}] is not [x, y]")
        points.append((_mm(point[0], f"polygon[{i}]"), _mm(point[1], f"polygon[{i}]")))
    return tuple(points)


def _ghost_fields(raw: dict) -> dict:
    """A ghost's fields by kind: a via, cap or pad `at` a spot; a `part` moved `at` a spot
    (`rotation` None: it keeps its own); a track along a `path` of 2 to MAX_GHOST_PATH
    spots on one `layer` (None: the layer its points name) for a `net`, `width` None for
    the net's usual one; a via_row `from` one spot `to` another, vias at most `pitch` apart."""
    ghost = raw.get("kind")
    if ghost not in GHOSTS:
        raise _Bad(f"kind is not one of {', '.join(GHOSTS)}")
    size = _mm(raw["size"], "size", positive=True) if raw.get("size") is not None else None
    net = raw.get("net")
    if net is not None and (not isinstance(net, str) or not net.strip()):
        raise _Bad("net is not a net name")
    rotation = _number(raw["rotation"]) if raw.get("rotation") is not None else None
    if raw.get("rotation") is not None and rotation is None:
        raise _Bad("rotation is not a number of degrees")
    fields = {"kind": ghost, "size": size, "net": net.strip().removeprefix("net:").strip() if net else "",
              "rotation": rotation % 360 if rotation is not None else None if ghost == "part" else 0.0}
    if ghost == "part":
        return {**fields, "part": _target(_need(raw, "part"), "part"), "at": _target(_need(raw, "at"), "at")}
    if ghost == "track":
        path = raw.get("path")
        if path is None:
            raise _Bad(f"a track needs path: 2 to {MAX_GHOST_PATH} points or targets along it (at is for a via, "
                       "cap or pad)")
        if not isinstance(path, list) or not 2 <= len(path) <= MAX_GHOST_PATH:
            raise _Bad(f"path is not a list of 2 to {MAX_GHOST_PATH} points or targets")
        if not fields["net"]:
            raise _Bad("net missing: the net the track would carry")
        width = _mm(raw["width"], "width", positive=True) if raw.get("width") is not None else None
        if width is not None and width > MAX_GHOST_WIDTH_MM:
            raise _Bad(f"width is over {MAX_GHOST_WIDTH_MM:g} mm")
        layer = _layer(raw["layer"], "layer") if raw.get("layer") is not None else None
        return {**fields, "path": tuple(_spot(spot, f"path[{i}]") for i, spot in enumerate(path)), "layer": layer,
                "width": width}
    if ghost == "via_row":
        return {**fields, "from": _spot(_need(raw, "from"), "from"), "to": _spot(_need(raw, "to"), "to"),
                "pitch": _mm(_need(raw, "pitch"), "pitch", positive=True)}
    return {**fields, "at": _target(_need(raw, "at"), "at")}


def _fields(tool: str, raw: dict, problems: list) -> dict:
    """The validated fields of one tool; raises _Bad for a missing or bad field."""
    if tool in ("highlight", "isolate"):
        return {"targets": _targets(_need(raw, "targets"))}
    if tool == "label":
        text = _text(_need(raw, "text"), MAX_TEXT, "label text", problems)
        if not text:
            raise _Bad("text is empty")
        return {"at": _target(_need(raw, "at"), "at"), "text": text}
    if tool == "arrow":
        return {"from": _target(_need(raw, "from"), "from"), "to": _target(_need(raw, "to"), "to")}
    if tool == "distance":
        mode = raw.get("mode", "edge")
        mode = "centre" if mode == "center" else mode
        if mode not in ("centre", "edge"):
            raise _Bad("mode is not centre or edge")
        fields = {"from": _target(_need(raw, "from"), "from"), "to": _target(_need(raw, "to"), "to"),
                  "mode": mode, "limit": _limit(raw, "limit")}
        limit_is = raw.get("limit_is", "max")
        if limit_is not in ("min", "max"):
            raise _Bad("limit_is is not min or max")
        fields["limit_is"] = limit_is
        return fields
    if tool == "clearance":
        return {"a": _target(_need(raw, "a"), "a"), "b": _target(_need(raw, "b"), "b"),
                "required": _limit(raw, "required")}
    if tool == "edge_gap":
        return {"target": _target(_need(raw, "target"), "target"), "min_mm": _limit(raw, "min_mm")}
    if tool == "width":
        return {"target": _target(_need(raw, "target"), "target"), "required": _limit(raw, "required")}
    if tool == "routed_length":
        return {"net": _net(_need(raw, "net")), "from": _target(_need(raw, "from"), "from"),
                "to": _target(_need(raw, "to"), "to")}
    if tool == "bars":
        unit = raw.get("unit", "")
        unit = unit.strip().lower() if isinstance(unit, str) else None
        if unit not in ("", *BAR_UNITS):
            raise _Bad(f"unit is not one of {', '.join(BAR_UNITS)}")
        at = _target(raw["at"], "at") if raw.get("at") is not None else None
        return {"items": _bars(_need(raw, "items"), problems), "unit": unit, "at": at}
    if tool == "ghost":
        return _ghost_fields(raw)
    if tool == "area":
        layer = _layer(_need(raw, "layer"), "layer")
        has_targets, has_polygon = raw.get("targets") is not None, raw.get("polygon") is not None
        if has_targets == has_polygon:
            raise _Bad("give targets or polygon (one of them)")
        if has_targets:
            return {"layer": layer, "targets": _targets(raw["targets"]), "polygon": ()}
        return {"layer": layer, "targets": (), "polygon": _polygon(raw["polygon"])}
    if tool == "layer_change":
        given = raw.get("planes")
        if given is not None and (not isinstance(given, list) or not 1 <= len(given) <= MAX_PLANES):
            raise _Bad(f"planes is not a list of 1 to {MAX_PLANES} planes (net@layer)")
        limit = _mm(raw["limit"], "limit", positive=True) if raw.get("limit") is not None else RETURN_LIMIT_MM
        return {"via": _target(_need(raw, "via"), "via"), "limit": limit,
                "planes": tuple(_plane(plane, f"planes[{i}]") for i, plane in enumerate(given or ()))}
    if tool == "plane_gap":
        return {"plane": _plane(_need(raw, "plane"), "plane"), "crossing": _target(_need(raw, "crossing"), "crossing")}
    if tool == "return_path":
        return {"signal": _target(_need(raw, "signal"), "signal"), "plane": _plane(_need(raw, "plane"), "plane")}
    if tool == "plane_islands":
        layer = _layer(raw["layer"], "layer") if raw.get("layer") is not None else None
        return {"net": _net(_need(raw, "net")), "layer": layer}
    if tool == "stitching":
        along = _need(raw, "along")
        along = "edge" if isinstance(along, str) and along.strip().lower() == "edge" else _plane(along, "along")
        net = _net(raw["net"]) if raw.get("net") is not None else None
        if along == "edge" and net is None:
            raise _Bad("along edge needs net: the stitching net (e.g. GND)")
        if along != "edge" and net is not None and net != along[0]:
            raise _Bad(f"net {_show(net)} is not the pour's net {_show(along[0])}")
        maximum = _mm(raw["max_mm"], "max_mm", positive=True) if raw.get("max_mm") is not None else None
        spacing = _mm(_need(raw, "spacing_mm"), "spacing_mm", positive=True)
        return {"along": along, "net": net or along[0], "spacing": spacing, "max": maximum}
    if tool == "stub":
        has_via, has_pad = raw.get("via") is not None, raw.get("pad") is not None
        if has_via == has_pad:
            raise _Bad("give via or pad (one of them)")
        return {"via": _target(raw["via"], "via") if has_via else None,
                "pad": _target(raw["pad"], "pad") if has_pad else None}
    if tool == "discontinuity":
        spots = _need(raw, "at")
        if not isinstance(spots, list) or not 1 <= len(spots) <= MAX_SPOTS:
            raise _Bad(f"at is not a list of 1 to {MAX_SPOTS} points or targets")
        return {"net": _net(_need(raw, "net")), "at": tuple(_spot(spot, f"at[{i}]") for i, spot in enumerate(spots))}
    if tool == "layers":
        layers = _need(raw, "layers")
        if not isinstance(layers, list) or not layers or len(layers) > MAX_TARGETS:
            raise _Bad(f"layers is not a list of 1 to {MAX_TARGETS} copper layers")
        return {"layers": tuple(_layer(layer, f"layers[{i}]") for i, layer in enumerate(layers))}
    raise _Bad(f"unknown tool {_show(tool)}")


def _draw(raw, number: int, problems: list) -> Draw | None:
    """One draw, or None with the reason in `problems` (the finding stays)."""
    tool = raw.get("tool") if isinstance(raw, dict) else None
    what = f"draw {number}" + (f" ({_show(tool)})" if isinstance(tool, str) else "")
    if not isinstance(tool, str):
        problems.append(f"{what} skipped: no tool")
        return None
    notes = []
    try:
        fields = _fields(tool, raw, notes)
    except _Bad as error:
        problems.append(f"{what} skipped: {error}")
        return None
    label = ""
    if raw.get("label") is not None:
        label = _text(raw["label"], MAX_TEXT, "label", notes) or ""
    step = raw.get("step", 0)
    if "step" in raw and (isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= 100):
        notes.append("step is not a whole number 1..100")
        step = 0
    emphasis = raw.get("emphasis", "normal")
    if emphasis not in ("normal", "strong"):
        notes.append("emphasis is not normal or strong")
        emphasis = "normal"
    problems.extend(f"{what}: {note}" for note in notes)
    return Draw(tool, fields, label, step, emphasis)


def _values(raw, problems: list) -> tuple[dict, dict]:
    """(numbers whose name ends in a unit (`distance_mm`), everything else as text: shown,
    never compared)."""
    values, text = {}, {}
    if raw is None:
        return values, text
    if not isinstance(raw, dict):
        problems.append("values is not an object")
        return values, text
    for i, (key, value) in enumerate(raw.items()):
        if i >= MAX_VALUES:
            problems.append(f"values past {MAX_VALUES} left out")
            break
        if not _VALUE_KEY.fullmatch(key):
            problems.append(f"value name {_show(key)!r} is not lower-case letters, digits and _")
            continue
        number = _number(value)
        if number is not None and _unit(key):
            values[key] = number
        elif isinstance(value, str):
            text[key] = _text(value, MAX_TEXT, f"value {key}", problems)
        else:  # a number without a known unit, true/false, a list: shown as written
            shown = json.dumps(value, ensure_ascii=False)
            text[key] = _CONTROL.sub(" ", shown if len(shown) <= MAX_TEXT else shown[:MAX_TEXT - 1] + "…")
    return values, text


def _finding(raw, number: int, file_problems: list) -> Finding | None:
    if not isinstance(raw, dict):
        file_problems.append(f"finding {number} is not an object; left out")
        return None
    problems: list[str] = []
    check = raw.get("check")
    if isinstance(check, str) and _CHECK.fullmatch(check.strip()):
        check = check.strip()
    else:
        problems.append("check id missing or not valid")
        check = "other"
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        problems.append("severity is not error, warning or info; shown as warning")
        severity = "warning"
    confidence = raw.get("confidence", "")
    if confidence not in CONFIDENCES:
        if "confidence" in raw:
            problems.append("confidence is not high, medium or low")
        confidence = ""
    title = _text(raw["title"], MAX_TEXT, "title", problems) if "title" in raw else None
    if not title:
        problems.append("no title")
        title = check
    message = _text(raw["message"], MAX_TEXT, "message", problems) if "message" in raw else ""
    fix = _text(raw["fix"], MAX_TEXT, "fix", problems) if raw.get("fix") is not None else ""
    values, values_text = _values(raw.get("values"), problems)
    draws = []
    raw_draws = raw.get("draw", [])
    if raw_draws is None:
        raw_draws = []
    if not isinstance(raw_draws, list):
        problems.append("draw is not a list")
        raw_draws = []
    if len(raw_draws) > MAX_DRAWS:
        problems.append(f"draws past {MAX_DRAWS} left out")
    for i, item in enumerate(raw_draws[:MAX_DRAWS]):
        draw = _draw(item, i + 1, problems)
        if draw is not None:
            draws.append(draw)
    return Finding(check, severity, confidence, title, message or "", fix or "", values, values_text,
                   tuple(draws), tuple(problems))


def _no_constant(name: str):
    raise ValueError(f"{name} is not a number")


def parse(data: bytes) -> FindingsFile:
    """Check a findings file; raises FindingsError when the whole file is refused."""
    if len(data) > MAX_FILE_BYTES:
        raise FindingsError(f"file is over {MAX_FILE_BYTES >> 20} MB")
    try:
        raw = json.loads(data.decode("utf-8-sig"), parse_constant=_no_constant)
    except UnicodeDecodeError:
        raise FindingsError("file is not UTF-8 text") from None
    except RecursionError:
        raise FindingsError("file is nested too deeply") from None
    except ValueError as error:
        raise FindingsError(f"not JSON: {_show(str(error), MAX_TEXT)}") from None
    if not isinstance(raw, dict):
        raise FindingsError("unknown format: not a JSON object", "unknown_format")
    if raw.get("format") != FORMAT:
        given = raw.get("format")
        shown = f' "{_show(given)}"' if isinstance(given, str) else ""
        raise FindingsError(f"unknown format{shown}; expected \"{FORMAT}\"", "unknown_format")
    problems: list[str] = []
    run = _text(raw["run"], 32, "run", problems) if raw.get("run") is not None else ""
    stamp = raw.get("stamp")
    stamp = stamp.strip().lower() if isinstance(stamp, str) else ""
    if not _STAMP.fullmatch(stamp):
        if raw.get("stamp") is not None:
            problems.append("stamp is not 8 hex characters")
        stamp = ""
    tool = _text(raw["tool"], MAX_TEXT, "tool", problems) if raw.get("tool") is not None else ""
    raw_findings = raw.get("findings", [])
    if not isinstance(raw_findings, list):
        problems.append("findings is not a list")
        raw_findings = []
    kept, left_out = _kept(raw_findings)
    if left_out:
        problems.append(f"{sum(count for _, count in left_out)} findings not shown: at most {MAX_PER_CHECK} of "
                        f"each check, {MAX_FINDINGS} in all, the worst first")
    findings = [_finding(raw_findings[i], i + 1, problems) for i in kept]
    skipped = []
    raw_skipped = raw.get("skipped", [])
    if not isinstance(raw_skipped, list):
        problems.append("skipped is not a list")
        raw_skipped = []
    if len(raw_skipped) > MAX_SKIPPED:
        problems.append(f"skipped entries past {MAX_SKIPPED} left out")
    for item in raw_skipped[:MAX_SKIPPED]:
        if not isinstance(item, dict):
            problems.append("a skipped entry is not an object")
            continue
        check = _text(item.get("check", ""), 64, "skipped check", problems) or ""
        reason = _text(item.get("reason", ""), MAX_TEXT, "skipped reason", problems) or ""
        skipped.append((check, reason))
    return FindingsFile(run or "", stamp, tool or "", tuple(f for f in findings if f is not None),
                        tuple(skipped), tuple(problems), left_out)


def _kept(raw_findings: list) -> tuple[list[int], tuple[tuple[str, int], ...]]:
    """Which findings of a file are kept, as indexes in file order, and how many of each
    check are not: at most MAX_PER_CHECK of each check, errors before warnings before info
    (file order within a severity), and MAX_FINDINGS in all, shared out one per check in
    turn, so thousands of one kind never push another kind out."""
    by_check: dict[str, list[int]] = {}
    for i, item in enumerate(raw_findings):
        check = item.get("check") if isinstance(item, dict) else None
        by_check.setdefault(check if isinstance(check, str) else "", []).append(i)

    def rank(i):
        severity = raw_findings[i].get("severity") if isinstance(raw_findings[i], dict) else None
        return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)

    queues = [sorted(indexes, key=rank)[:MAX_PER_CHECK] for indexes in by_check.values()]
    queues.sort(key=lambda queue: rank(queue[0]))  # the checks with errors take their turn first
    kept = []
    for turn in range(MAX_PER_CHECK):
        for queue in queues:
            if turn < len(queue) and len(kept) < MAX_FINDINGS:
                kept.append(queue[turn])
    chosen = set(kept)
    counts: dict[str, int] = {}
    for check, indexes in by_check.items():
        missing = sum(1 for i in indexes if i not in chosen)
        if missing:
            name = check if _CHECK.fullmatch(check) else "other"
            counts[name] = counts.get(name, 0) + missing
    left_out = sorted(counts.items(), key=lambda item: -item[1])[:MAX_FINDINGS]  # a file of a thousand kinds
    return sorted(kept), tuple(left_out)


# keys

def _half_mm(nm: float) -> str:
    value = round(nm / KEY_GRID_NM) / 2 + 0.0  # + 0.0: no "-0"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _name(index: BoardIndex, record) -> str:
    """The named form of one item: what a findings file would write for it."""
    if isinstance(record, model.Outline):
        return "edge"
    if isinstance(record, model.Footprint):
        return record.reference or f"uuid:{record.id}"
    if isinstance(record, model.Pad):
        owner = index.footprint_of(record)
        reference = owner.reference if owner else ""
        return f"{reference}.{record.number}" if reference and record.number else f"uuid:{record.id}"
    layer, net = top_layer(record), getattr(record, "net", "")
    if isinstance(record, model.ZoneFill) and net:
        return f"net:{net}@{layer}"
    x, y = centre(record)
    where = f"{_half_mm(x)},{_half_mm(y)}"
    if net and isinstance(record, (model.Track, model.Arc, model.Via)):
        return f"net:{net}@{layer}~{where}"
    return f"layer:{layer}~{where}"


def named(index: BoardIndex | None, resolved: Resolved) -> list[str]:
    """Names for keying a target: `uuid:` and `pt:` targets, and targets that pick one
    item by `~x,y`, become the named form of what they found (pad `U3.4`, part `U3`,
    `net:SIG@F.Cu~15,10.5` for a track, arc or via, `net:GND@In1.Cu` for a zone, the
    anchor on a 0.5 mm grid). Anything else, and anything unresolved, stays as written."""
    text = resolved.target.strip()
    head, colon, _ = text.partition(":")
    head = head.strip().lower() if colon else ""
    picked = head in ("uuid", "pt") or (head in ("net", "layer") and resolved.point is not None)
    if index is None or not picked or not resolved.items:
        return [text]
    return [_name(index, record) for record in resolved.items]


def finding_key(check: str, names: Iterable[str]) -> str:
    """Confirm/dismiss key of a finding: its check and its named targets, order-free."""
    text = check + "|" + "|".join(sorted(set(names)))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# resolving

class _Board:
    """The live board for one file: targets resolved once, ids counted against the caps."""

    def __init__(self, index: BoardIndex | None):
        self.index = index
        self.ids = 0
        self._cache: dict[str, Resolved] = {}

    def resolve(self, text: str) -> Resolved:
        found = self._cache.get(text)
        if found is None:
            if self.index is None:
                found = Resolved(text, note=NOT_CONNECTED)
            else:
                try:
                    found = resolve(self.index, text)
                except (ValueError, TypeError, ArithmeticError) as error:  # never lose the file to one target
                    found = Resolved(text, note=f"could not resolve: {_show(str(error), 80)}")
            self._cache[text] = found
        return found

    def target(self, tool: str, role: str, text: str) -> Resolved:
        """A draw's target in its role. A ghost's spot (`GHOST_SPOTS`: its `at`, a row's
        ends, a track's points) given as `pt:` is that point, used as given: the ghost shows
        what is missing there, so it never snaps to the copper nearby, lights it up or notes it."""
        found = self.resolve(text)
        if tool == "ghost" and role in GHOST_SPOTS and found.point is not None and text.strip()[:3].lower() == "pt:":
            return Resolved(found.target, (), found.layer, found.point)
        return found


def _target_layer(resolved: Resolved) -> str:
    """The layer a target names (`@layer`, also a non-copper one on `pt:`), else its first item's."""
    if resolved.layer:
        return resolved.layer
    text = resolved.target.strip()
    if text.lower().startswith("pt:") and "@" in text:
        return text.partition("@")[2].strip()[:40]
    return top_layer(resolved.items[0]) if resolved.items else ""


def _anchor(resolved: Resolved, role: str) -> list:
    """[x_nm, y_nm, layer] of a target: its named point, else its first item's centre."""
    if not resolved.found:
        raise _Bad(f"{role}: {resolved.note or 'not found on board'}")
    point = resolved.point if resolved.point is not None else centre(resolved.items[0])
    return [int(point[0]), int(point[1]), _target_layer(resolved)]


def _anchor_pair(index: BoardIndex | None, a: Resolved, b: Resolved) -> list[list]:
    """Both ends of an arrow or centre distance. Items that share a copper layer, and no
    layer named, put both ends on it (where they come closest; the top one on a tie): a
    via against a bottom pad is a problem on B.Cu, not a slant from the via's top."""
    ends = [_anchor(a, "from"), _anchor(b, "to")]
    if index is not None and a.items and b.items and not a.layer and not b.layer             and a.point is None and b.point is None:
        _, layer_a, layer_b = _on_one_layer(index, Gap(0.0, (0, 0), (0, 0)), a.items[0], b.items[0])
        if layer_a == layer_b and layer_a:
            ends[0][2] = ends[1][2] = layer_a
    return ends


def _edge_gap(index: BoardIndex, a: Resolved, b: Resolved, roles=("from", "to")) -> tuple[Gap, str, str]:
    """Closest approach of two targets' copper, with the layer at each end (the board
    edge's end on the other side's layer). A `pt:` that snapped to nothing measures as
    its point."""
    for resolved, role in zip((a, b), roles):
        if not resolved.found:
            raise _Bad(f"{role}: {resolved.note or 'not found on board'}")
    if a.items and b.items:
        if len(a.items) * len(b.items) > MAX_PAIRS:
            raise _Bad(f"{len(a.items):,} × {len(b.items):,} items to measure; name single items")
        layer = a.layer if a.layer == b.layer else None
        result = index.closest(a.items, b.items, layer)
        if result is None:
            raise _Bad("no copper to measure")
        found, record_a, record_b = result
        if a.layer or b.layer:
            layer_a, layer_b = a.layer or top_layer(record_a), b.layer or top_layer(record_b)
            if isinstance(record_a, model.Outline):
                layer_a = layer_b
            if isinstance(record_b, model.Outline):
                layer_b = layer_a
            return found, layer_a, layer_b
        return _on_one_layer(index, found, record_a, record_b)
    if a.items or b.items:
        point_side, item_side = (b, a) if a.items else (a, b)
        near = index.nearest(point_side.point, item_side.items, item_side.layer)
        if near is None:
            raise _Bad("no copper to measure")
        item_layer = item_side.layer or (_target_layer(point_side) if isinstance(near.record, model.Outline)
                                         else top_layer(near.record))
        if point_side is a:
            return near.gap, _target_layer(a), item_layer
        return Gap(near.gap.distance, near.gap.b, near.gap.a), item_layer, _target_layer(b)
    return (Gap(math.hypot(a.point[0] - b.point[0], a.point[1] - b.point[1]), a.point, b.point),
            _target_layer(a), _target_layer(b))


def _on_one_layer(index: BoardIndex, found: Gap, record_a, record_b) -> tuple[Gap, str, str]:
    """Items that share a copper layer are measured, and drawn, on it (the one where they
    come closest, the top one on a tie), not from one item's top layer to the other's: a
    via and a pad on In2.Cu are a gap across In2.Cu, not through the board. The board
    edge takes the other side's layer."""
    if isinstance(record_a, model.Outline) or isinstance(record_b, model.Outline):
        layer = top_layer(record_b if isinstance(record_a, model.Outline) else record_a)
        return found, layer, layer
    shared = [layer for layer in index.layers_of(record_a) if layer in index.layers_of(record_b)]
    best = None
    for layer in shared:
        result = index.closest((record_a,), (record_b,), layer)
        if result is not None and (best is None or result[0].distance < best[0].distance):
            best = (result[0], layer)
    if best is None:
        return found, top_layer(record_a), top_layer(record_b)
    return best[0], best[1], best[1]


def _roles(draw: Draw) -> list[tuple[str, str]]:
    """(role, target text) of every target a draw names, in order (a role holding a list,
    as discontinuity's `at`, once per target)."""
    fields = draw.fields
    if "targets" in fields:
        return [("targets", text) for text in fields["targets"]]
    return [(role, text) for role in ROLES if fields.get(role)
            for text in (fields[role] if isinstance(fields[role], tuple) else (fields[role],))]


def _ids(board: _Board, resolved: list[Resolved]) -> tuple[list[str], bool]:
    """Item ids of every target (outline: a flag, it has no id), within the caps."""
    ids: dict[str, None] = {}
    outline = False
    for found in resolved:
        for record in found.items:
            if isinstance(record, model.Outline):
                outline = True
            else:
                ids.setdefault(record.id)
    if len(ids) > MAX_IDS_DRAW:
        biggest = max(resolved, key=lambda found: len(found.items))
        raise _TooMany(f"{biggest.target.strip()} has {len(biggest.items):,} items; add @layer or ~x,y")
    return _capped(board, list(ids)), outline


def _capped(board: _Board, ids: list[str]) -> list[str]:
    if board.ids + len(ids) > MAX_IDS_FILE:
        raise _TooMany(f"over {MAX_IDS_FILE:,} items in this file; draw left out")
    return ids


def _empty_draw(draw: Draw) -> dict:
    return {"tool": draw.tool, "step": draw.step, "emphasis": draw.emphasis, "label": draw.label, "text": "",
            "ok": True, "problem": "", "targets": [], "ids": [], "outline": False, "points": [],
            "measured_nm": None, "limit_nm": None, "limit_is": "", "over": False,
            "polygon_nm": [], "layer": "", "layers": [], "kind": "", "size_nm": 0,
            "what": "", "segments_nm": [], "narrow": [], "min_nm": None, "path_nm": [], "bars": [], "unit": "",
            "planes": [], "spans_nm": [], "covered_nm": [], "edges_nm": [], "returns": [], "fits": None,
            "islands": [], "barrel": {}, "marks": [], "rotation_deg": 0.0,
            "part": "", "from": [], "moved_nm": None, "pads_nm": [], "width_nm": 0, "length_nm": None, "row": [],
            "pitch_nm": 0, "net": "", "touches": [], "kicad_drc": None}


def kicad_drc_state(out: dict) -> dict:
    """A ghost's `kicad_drc` before KiCad's DRC has judged it: "skipped" (not asked), with
    the quick check's verdict kept (`quick_fits`, `quick_problem`, a row's `quick_row`) so
    KiCad's can replace it and fall back to it; `kept` holds reasons KiCad's DRC does not
    judge (a via outside a layer change's reach), which stand whatever it finds."""
    return {"state": "skipped", "problems": [], "notes": [], "error": "", "quick_fits": out["fits"],
            "quick_problem": out["problem"], "quick_row": [bool(spot[2]) for spot in out["row"] if len(spot) > 2],
            "kept": []}


def _measured(out: dict, measured: float, limit_mm: float | None, limit_is: str) -> None:
    """`measured_nm`, and the limit with `over` when there is one."""
    out["measured_nm"] = int(round(measured))
    if limit_mm is not None:
        out["limit_nm"] = int(round(limit_mm * 1e6))
        out["limit_is"] = limit_is
        out["over"] = (out["measured_nm"] < out["limit_nm"] if limit_is == "min"
                       else out["measured_nm"] > out["limit_nm"])


def _gap_points(found: Gap, layer_a: str, layer_b: str) -> list[list]:
    return [[int(found.a[0]), int(found.a[1]), layer_a], [int(found.b[0]), int(found.b[1]), layer_b]]


def _width(board: _Board, out: dict, target: Resolved, required: float | None) -> None:
    """Every track and arc of the target as segments with their width (arcs sampled),
    the ones thinner than `required`, and the thinnest marked."""
    if not target.found:
        raise _Bad(f"target: {target.note or 'not found on board'}")
    tracks = [record for record in target.items if isinstance(record, (model.Track, model.Arc))]
    if not tracks:
        raise _Bad(f"target: {target.target.strip()} has no tracks or arcs")
    ids = list(dict.fromkeys(record.id for record in tracks))
    if len(ids) > MAX_IDS_DRAW:
        raise _TooMany(f"{target.target.strip()} has {len(ids):,} tracks; add @layer or ~x,y")
    out["ids"] = _capped(board, ids)
    segments, total = [], 0
    for record in tracks:
        if isinstance(record, model.Arc):
            points = sample_arc(record.start, record.mid, record.end)
            lines = list(zip(points, points[1:])) or [(record.start, record.end)]
        else:
            lines = [(record.start, record.end)]
        total += len(lines)
        segments += [[int(a[0]), int(a[1]), int(b[0]), int(b[1]), record.layer, int(record.width)]
                     for a, b in lines[:max(0, MAX_SEGMENTS - len(segments))]]
    if total > MAX_SEGMENTS:
        out["problem"] = f"{total:,} segments; the first {MAX_SEGMENTS:,} drawn"
    thinnest = min(tracks, key=lambda record: record.width)
    out["segments_nm"] = segments
    out["min_nm"] = int(thinnest.width)
    x, y = centre(thinnest)
    out["points"] = [[int(x), int(y), thinnest.layer]]
    _measured(out, thinnest.width, required, "min")
    if required is not None:
        out["narrow"] = [i for i, segment in enumerate(segments) if segment[5] < out["limit_nm"]]


def _route(board: _Board, out: dict, net: str, start: Resolved, end: Resolved) -> None:
    """The copper path along `net` between two pads, its length and items."""
    index = board.index
    for found, role in ((start, "from"), (end, "to")):
        if not found.found:
            raise _Bad(f"{role}: {found.note or 'not found on board'}")
    if net not in index.nets:
        raise _Bad(f"net {_show(net, 80)} not found on board")
    pads = []
    for found, role in ((start, "from"), (end, "to")):
        named_pads = [record for record in found.items if isinstance(record, model.Pad)]
        if not named_pads:
            raise _Bad(f"{role}: {found.target.strip()} is not a pad (give one, e.g. U3.4)")
        on_net = [pad for pad in named_pads if pad.net == net]
        if not on_net:
            raise _Bad(f"{role}: {found.target.strip()} is not on net {_show(net, 80)}")
        pads.append(on_net)
    found = index.route(net, pads[0], pads[1])
    if found is None:
        raise _Bad(f"no copper path between {start.target.strip()} and {end.target.strip()} on net "
                   f"{_show(net, 80)} ({ROUTE_LIMIT})")
    length, path, ids = found
    out["ids"] = _capped(board, ids)
    out["path_nm"] = [[x, y, layer] for x, y, layer in path]
    out["points"] = [list(path[0]), list(path[-1])]
    out["measured_nm"] = int(round(length))


# planes

def _plane_name(plane: tuple[str, str]) -> str:
    return f"{plane[0]}@{plane[1]}"


def _check_plane(index: BoardIndex, plane: tuple[str, str], role: str) -> None:
    """A named plane must be fill of its net on its layer."""
    net, layer = plane
    name = _show(_plane_name(plane), 80)
    if net not in index.nets:
        raise _Bad(f"{role} {name}: net {_show(net, 80)} not found on board")
    if not planes.plane(index, net, layer):
        raise _Bad(f"{role} {name}: {_show(net, 80)} has no zone fill on {layer}")


def _tracks_next_to(board: _Board, target: Resolved, role: str, layer: str) -> list:
    """The tracks and arcs of a target on the copper layers right above and below `layer`."""
    if not target.found:
        raise _Bad(f"{role}: {target.note or 'not found on board'}")
    tracks = [record for record in target.items if isinstance(record, (model.Track, model.Arc))]
    if not tracks:
        raise _Bad(f"{role}: {target.target.strip()} has no tracks or arcs")
    sides = planes.next_to(board.index.snapshot, layer)
    near = [record for record in tracks if record.layer in sides]
    if not near:
        raise _Bad(f"{role}: {target.target.strip()} has no tracks on {' or '.join(sides) or 'a layer'} "
                   f"next to {layer}")
    ids = list(dict.fromkeys(record.id for record in near))
    if len(ids) > MAX_IDS_DRAW:
        raise _TooMany(f"{target.target.strip()} has {len(ids):,} tracks; add @layer or ~x,y")
    return near


def _stretches(out: dict, key: str, stretches: list) -> None:
    """Stretches as [x0, y0, x1, y1, layer, width] under `key`, at most MAX_SEGMENTS."""
    out[key] = [[*stretch.start, *stretch.end, stretch.layer, stretch.width] for stretch in stretches[:MAX_SEGMENTS]]
    if len(stretches) > MAX_SEGMENTS:
        out["problem"] = f"{len(stretches):,} stretches; the first {MAX_SEGMENTS:,} drawn"


def _coverage(board: _Board, out: dict, target: Resolved, role: str, plane: tuple[str, str]):
    """(tracks measured, plane as they see it, their stretches) of a target next to a plane."""
    index = board.index
    _check_plane(index, plane, "plane")
    net, layer = plane
    tracks = _tracks_next_to(board, target, role, layer)
    out["ids"] = _capped(board, list(dict.fromkeys(record.id for record in tracks)))
    out["layer"], out["planes"] = layer, [_plane_name(plane)]
    copper = planes.plane_under(index, net, layer, tracks)
    return tracks, copper, planes.coverage(tracks, copper)


def _at_track(record) -> list:
    x, y = centre(record)
    return [int(x), int(y), record.layer]


def _plane_gap(board: _Board, out: dict, crossing: Resolved, plane: tuple[str, str]) -> None:
    """Where the crossing has no plane under it, the plane's edges around each such
    span, and the longest gap along the track (spans that meet count as one)."""
    tracks, copper, stretches = _coverage(board, out, crossing, "crossing", plane)
    spans = [stretch for stretch in stretches if not stretch.covered]
    _stretches(out, "spans_nm", spans)
    edges, points = [], 0
    for line in planes.edges_near(copper, [(min(s.start[0], s.end[0]), min(s.start[1], s.end[1]),
                                            max(s.start[0], s.end[0]), max(s.start[1], s.end[1]))
                                           for s in spans[:MAX_SEGMENTS]], EDGE_REACH_NM):
        line = line[:MAX_EDGE_POINTS - points]
        if len(line) < 2:
            out["problem"] = out["problem"] or f"plane edges past {MAX_EDGE_POINTS:,} points left out"
            break
        edges.append([list(point) for point in line])
        points += len(line)
    out["edges_nm"] = edges
    out["points"] = [[*span.middle, span.layer] for span in spans[:MAX_SEGMENTS]] or [_at_track(tracks[0])]
    out["measured_nm"] = int(round(planes.runs(spans)[0][0])) if spans else 0


def _return_path(board: _Board, out: dict, signal: Resolved, plane: tuple[str, str]) -> None:
    """The signal's stretches with and without the plane under them, and the length without."""
    tracks, _, stretches = _coverage(board, out, signal, "signal", plane)
    spans = [stretch for stretch in stretches if not stretch.covered]
    _stretches(out, "covered_nm", [stretch for stretch in stretches if stretch.covered])
    _stretches(out, "spans_nm", spans)
    longest = max(spans, key=lambda stretch: stretch.length, default=None)
    out["points"] = [[*longest.middle, longest.layer]] if longest else [_at_track(tracks[0])]
    out["measured_nm"] = int(round(sum(stretch.length for stretch in spans)))


def _via(index: BoardIndex, target: Resolved) -> model.Via:
    """The via a target names; a `net@layer~x,y` target at a via picks the via of that net there."""
    if not target.found:
        raise _Bad(f"via: {target.note or 'not found on board'}")
    via = next((record for record in target.items if isinstance(record, model.Via)), None)
    if via is None and target.point is not None:  # net@layer~x,y at a via picks the track ending there
        nets = {getattr(record, "net", "") for record in target.items} - {""}
        near = index.nearest(target.point, [record for net in nets for record in index.net_items(net)
                                            if isinstance(record, model.Via)], max_distance=SNAP_NM)
        via = near.record if near else None
    if via is None:
        raise _Bad(f"via: {target.target.strip()} is not a via")
    return via


def _layer_change(board: _Board, out: dict, target: Resolved, given: tuple, limit_mm: float) -> None:
    """A signal via (a `net@layer~x,y` target picks the via of that net at the point): the
    layers it joins, their reference planes (named, or the fill of another net next to
    each layer at the via), the return vias that join both planes with the nearest one
    measured centre to centre against the limit. Where a return via should go is the
    LLM's to suggest (a `ghost`); the bridge only checks it fits (`_fit_ghost`)."""
    index = board.index
    via = _via(index, target)
    used = planes.via_layers(index, via)
    if len(used) < 2:
        used = (via.layer_top, via.layer_bottom)
        out["problem"] = "the via meets its net on fewer than two layers; its span's ends are taken"
    signal = (used[0], used[-1])
    nets = planes.signal_nets(index, {via.net})
    for plane in given:
        _check_plane(index, plane, "plane")
    picked = [planes.reference_at(index, layer, via.pos, nets) for layer in signal] if not given else []
    if picked and not any(picked):
        raise _Bad(f"no plane next to {signal[0]} or {signal[1]} at the via; name them in planes")
    references = list(dict.fromkeys(given or (plane for plane in picked if plane)))
    out["planes"] = [_plane_name(plane) for plane in references]
    out["limit_nm"], out["limit_is"] = int(round(limit_mm * 1e6)), "max"
    out["points"] = [[int(via.pos[0]), int(via.pos[1]), signal[0]]]
    if None in picked:
        missing = signal[picked.index(None)]
        out["problem"] = f"no plane next to {missing} at the via; return vias to {out['planes'][0]} measured"
    elif len(references) == 1 and not given:
        out["ids"] = _capped(board, [via.id])
        out["problem"] = "; ".join(filter(None, (out["problem"], f"both layers return on {out['planes'][0]}; "
                                                                  "no return via needed")))
        return
    layers = {layer for _, layer in references}
    found = planes.return_vias(index, {net for net, _ in references}, layers, via.pos, skip=via)
    reach = max(3 * limit_mm * 1e6, 2_000_000)  # the nearest one, and others within 3 limits (2 mm at least)
    shown = found[:1] + [item for item in found[1:MAX_RETURNS] if item[0] <= reach]
    out["returns"] = [[int(record.pos[0]), int(record.pos[1]),
                       int(record.diameter if isinstance(record, model.Via) else max(record.drill)), record.id]
                      for _, record in shown]
    out["ids"] = _capped(board, list(dict.fromkeys([via.id, *(record.id for _, record in shown)])))
    if found:
        distance, nearest = found[0]
        out["points"].append([int(nearest.pos[0]), int(nearest.pos[1]), signal[0]])
        _measured(out, distance, limit_mm, "max")
    else:
        out["over"] = True


def _islands(board: _Board, out: dict, net: str, layer: str | None, label: str) -> None:
    """The net's fill pieces (on `layer`, or every layer), which are tied to the main
    plane through vias and plated holes, each one's area; the label counts them."""
    index = board.index
    if net not in index.nets:
        raise _Bad(f"net {_show(net, 80)} not found on board")
    shown = [island for island in planes.islands(index, net) if layer is None or island.layer == layer]
    if not shown:
        raise _Bad(f"{_show(net, 80)} has no zone fill" + (f" on {layer}" if layer else ""))
    out["ids"] = _capped(board, list(dict.fromkeys(island.zone for island in shown)))
    out["layer"] = layer or ""
    picked = sorted(shown, key=lambda island: (not island.main, island.tied, -island.area_nm2))[:MAX_ISLANDS]
    picked.sort(key=lambda island: (copper_rank(island.layer), -island.area_nm2))
    if len(shown) > MAX_ISLANDS:
        out["problem"] = f"{len(shown):,} pieces; {MAX_ISLANDS} drawn, the main ones and the loose ones first"
    out["islands"] = [{"index": number, "layer": island.layer, "polygon_nm": _outline(island.rings[0]),
                       "area_mm2": round(island.area_nm2 / 1e12, 3), "main": island.main, "tied": island.tied}
                      for number, island in enumerate(picked, 1)]
    loose = sum(not island.tied for island in shown)
    summary = f"{len(shown)} piece{'s' if len(shown) != 1 else ''}, {loose} not tied"
    out["label"] = f"{label} ({summary})" if label else summary


def _outline(ring) -> list[list[int]]:
    """A ring as [x, y] points, every n-th one past MAX_ISLAND_POINTS."""
    step = math.ceil(len(ring) / MAX_ISLAND_POINTS)
    return [[int(round(x)), int(round(y))] for x, y in ring[::step]]


def _stitching(board: _Board, out: dict, along, net: str, spacing_mm: float, max_mm: float | None) -> None:
    """The vias of `net` along one closed line, in order along it: the outline of a pour's
    largest piece (`along` net@layer: vias through its layer whose land meets the pour) or
    of the board (`along` "edge": vias inside the board), each within STITCH_REACH_NM of
    it. Their largest spacing, measured along the line between neighbours (where the line
    passes nearest each), against max_mm (spacing_mm without one): `points` the two vias
    around it; `edges_nm` that stretch of the line, then every other stretch over the
    limit. Where vias should go is the LLM's to suggest (`ghost`). With fewer than two
    vias the spacing is the way round the line."""
    index = board.index
    if net not in index.nets:
        raise _Bad(f"net {_show(net, 80)} not found on board")
    if along == "edge":
        polygons = list(planes.outline(index.snapshot))
        if not polygons:
            raise _Bad("the board has no outline")
        layer, what = "F.Cu", "board outline"
    else:
        _check_plane(index, along, "along")
        layer, what = along[1], "pour"
        polygons = list(planes.plane(index, net, layer))
        out["planes"] = [_plane_name(along)]
    rings = planes.largest(polygons)
    line = planes.Line.of(rings[0])
    notes = [f"{len(polygons) - 1} smaller piece{'s' if len(polygons) > 2 else ''} of the {what} not followed"] \
        if len(polygons) > 1 else []
    found = planes.stitches(index, net, rings, line, STITCH_REACH_NM, None if along == "edge" else layer)
    limit_mm = max_mm if max_mm is not None else spacing_mm
    limit = limit_mm * 1e6
    out["layer"] = layer
    step = math.ceil((len(line.corners) + 1) / MAX_LINE_POINTS)
    corners = [*line.corners[::step], line.corners[0]]
    out["path_nm"] = [[int(round(x)), int(round(y)), layer] for x, y in corners]
    positions = [position for position, _, _ in found]
    stretches = planes.gaps(positions, line.length)  # the stretch after each via, to the next
    widest = max(range(len(stretches)), key=lambda i: stretches[i][1])
    start, measured = stretches[widest]
    over = [stretch for i, stretch in enumerate(stretches) if i != widest and stretch[1] > limit]
    out["edges_nm"] = [[list(point) for point in line.stretch(begin, size)[:MAX_EDGE_POINTS]]
                       for begin, size in [(start, measured), *over[:MAX_GAPS - 1]]]
    if len(found) > 1:
        ends = [found[widest][2].pos, found[(widest + 1) % len(found)][2].pos]
    else:
        ends = [found[0][2].pos] if found else [line.at(0.0)]
    out["points"] = [[int(x), int(y), layer] for x, y in ends]
    if len(found) < 2:
        notes.append((f"one {_show(net, 80)} via" if found else f"no {_show(net, 80)} via") +
                     f" within {STITCH_REACH_NM / 1e6:g} mm of the {what}; the spacing is the way round it")
    _measured(out, measured, limit_mm, "max")
    shown = [via for _, _, via in found[:MAX_STITCHES]]
    out["returns"] = [[int(via.pos[0]), int(via.pos[1]), int(via.diameter), via.id] for via in shown]
    out["ids"] = _capped(board, [via.id for via in shown])
    if len(found) > MAX_STITCHES:
        notes.append(f"{len(found):,} vias; the first {MAX_STITCHES} drawn")
    if len(over) >= MAX_GAPS:
        notes.append(f"{len(over) + 1:,} stretches over the limit; the first {MAX_GAPS} drawn")
    out["problem"] = "; ".join(notes)


def _stub(board: _Board, out: dict, via: Resolved | None, pad: Resolved | None) -> None:
    """The barrel of a via (or a pad's plated hole): the layers its net meets it on, and
    the unused part past the last of them to the barrel's end with its length from the
    stackup (0 without a stub). A stub at each end: the longer one, the other in `problem`."""
    index = board.index
    if via is not None:
        record = _via(index, via)
        size, word = record.drill, "via"
    else:
        if not pad.found:
            raise _Bad(f"pad: {pad.note or 'not found on board'}")
        record = next((item for item in pad.items if planes.plated(item)), None)
        if record is None:
            raise _Bad(f"pad: {pad.target.strip()} is not a plated hole")
        size, word = max(record.drill), "hole"
    found = planes.stub(index, record)
    if found is None:
        raise _Bad("the stackup has no layer thicknesses to measure the stub with (KiCad: Board Setup, "
                   "Physical Stackup)")
    out["ids"] = _capped(board, [record.id])
    out["barrel"] = {"x": int(record.pos[0]), "y": int(record.pos[1]), "size_nm": int(size),
                     "used": list(found.used), "stub": list(found.stub)}
    at = found.stub[0] if found.stub else (found.span[0] if found.span else top_layer(record))
    out["points"] = [[int(record.pos[0]), int(record.pos[1]), at]]
    out["measured_nm"] = int(found.length_nm)
    notes = []
    if not found.used:
        notes.append(f"the {word} meets its net on no layer; its whole barrel is unused")
    elif found.used[0] == found.used[-1]:
        notes.append(f"the {word} meets its net on {found.used[0]} only")
    if found.other:
        notes.append(f"also a {found.other_nm / 1e6:.3f} mm stub {found.other[0]} to {found.other[1]}")
    if found.stub and not found.exact:
        notes.append("the stackup lacks thicknesses; the board's thickness split evenly between its copper layers")
    out["problem"] = "; ".join(notes)


def _width_text(widths) -> str:
    """Track widths, widest first: "0.20 -> 0.15 mm"."""
    def mm(nm):
        text = f"{nm / 1e6:.3f}"
        return text[:-1] if text.endswith("0") else text
    return " -> ".join(mm(width) for width in sorted(set(widths), reverse=True)) + " mm"


def _reference_text(reference) -> str:
    return _plane_name(reference) if reference else "no plane"


def _changes(index: BoardIndex, net: str, record, point) -> list[tuple[dict, list[str]]]:
    """(mark, ids) of what changes where `point` meets the net's `record`: tracks of
    different widths meeting (the meeting nearest the point), a via or plated hole that
    takes the net to another layer, and the reference plane changing across the point
    along the track, or between the via's layers."""
    nets = planes.signal_nets(index, {net})
    found = []
    spot = getattr(record, "pos", None)
    if isinstance(record, (model.Track, model.Arc)):
        spot, (dx, dy) = planes.on_centre_line(record, point)
        layer = record.layer
        near = [item for item in index.net_items(net, layer) if isinstance(item, (model.Track, model.Arc)) and
                point_gap(index.shape(item), spot).distance <= 2 * SPOT_REACH_NM]
        best = None
        for item in near:
            for end in (item.start, item.end):
                apart = math.dist(end, spot)
                if apart > SPOT_REACH_NM or (best is not None and apart >= best[0]):
                    continue
                meeting = [other for other in near if other is item or
                           point_gap(index.shape(other), end).distance <= planes.TOUCH_NM]
                if len({other.width for other in meeting}) > 1:
                    best = (apart, end, meeting)
        if best is not None:
            _, end, meeting = best
            found.append(({"x": int(end[0]), "y": int(end[1]), "layer": layer, "kind": "width",
                           "text": _width_text(other.width for other in meeting)},
                          [other.id for other in meeting]))
        ends = [(spot[0] - dx * SPOT_REACH_NM, spot[1] - dy * SPOT_REACH_NM),
                (spot[0] + dx * SPOT_REACH_NM, spot[1] + dy * SPOT_REACH_NM)]
        across = planes.reference_across(index, layer, ends[0], ends[1], nets)
        if across is not None:
            before, after, at = across
            found.append(({"x": at[0], "y": at[1], "layer": layer, "kind": "reference",
                           "text": f"{_reference_text(before)} -> {_reference_text(after)}"}, [record.id]))
    holes = [item for item in index.net_items(net) if isinstance(item, model.Via) or planes.plated(item)]
    near = index.nearest(spot, holes, max_distance=SPOT_REACH_NM)
    if near is not None:
        hole = near.record
        used = planes.via_layers(index, hole)
        if len(set(used)) > 1:
            x, y = int(hole.pos[0]), int(hole.pos[1])
            found.append(({"x": x, "y": y, "layer": used[0], "kind": "layer", "text": f"{used[0]} -> {used[-1]}"},
                          [hole.id]))
            top, bottom = (planes.reference_at(index, layer, hole.pos, nets) for layer in (used[0], used[-1]))
            if top != bottom:
                found.append(({"x": x, "y": y, "layer": used[0], "kind": "reference",
                               "text": f"{_reference_text(top)} -> {_reference_text(bottom)}"}, [hole.id]))
    return found


def _discontinuity(board: _Board, out: dict, net: str, spots: list[Resolved]) -> None:
    """At each point, the net's nearest copper (within SPOT_REACH_NM; on the point's layer
    when it names one) and what changes there (`_changes`), one mark per change. A point
    with no change is listed in `problem`."""
    index = board.index
    if net not in index.nets:
        raise _Bad(f"net {_show(net, 80)} not found on board")
    copper = [item for item in index.net_items(net)
              if isinstance(item, (model.Track, model.Arc, model.Via)) or planes.plated(item)]
    marks, ids, notes, snapped = [], [], [], []
    for i, found in enumerate(spots):
        if not found.found:
            notes.append(f"at[{i}]: {found.note or 'not found on board'}")
            continue
        point = found.point if found.point is not None else centre(found.items[0])
        where = f"{point[0] / 1e6:.2f},{point[1] / 1e6:.2f}"
        layer = found.layer if found.layer and copper_rank(found.layer) is not None else None
        near = index.nearest(point, [item for item in copper if layer is None or on_layer(item, layer)], layer,
                             max_distance=SPOT_REACH_NM)
        if near is None:
            notes.append(f"{where}: {_show(net, 80)} has no copper within {SPOT_REACH_NM / 1e6:.2f} mm")
            continue
        changes = _changes(index, net, near.record, point)
        if not changes:
            notes.append(f"no change found at {where}")
            record = near.record
            spot = record.pos if not isinstance(record, (model.Track, model.Arc)) else \
                planes.on_centre_line(record, point)[0]
            snapped.append([int(spot[0]), int(spot[1]), top_layer(record)])
        for mark, items in changes:
            if mark not in marks:
                marks.append(mark)
                ids += items
    if not marks and not snapped:
        raise _Bad("; ".join(notes))
    out["marks"] = marks
    out["points"] = [[mark["x"], mark["y"], mark["layer"]] for mark in marks] or snapped
    out["ids"] = _capped(board, list(dict.fromkeys(ids)))
    out["problem"] = "; ".join(notes)


def _resolve_draw(board: _Board, draw: Draw) -> tuple[dict, list[Resolved]]:
    """The payload of one draw, and its resolved targets."""
    out = _empty_draw(draw)
    fields = draw.fields
    roles = _roles(draw)
    resolved = [board.target(draw.tool, role, text) for role, text in roles]
    by_role = {role: found for (role, _), found in zip(roles, resolved)}
    out["targets"] = [{"text": found.target.strip(), "found": found.found, "note": found.note,
                       "kind": kind_of(found.items[0]) if found.items else ("point" if found.found else "")}
                      for found in resolved]
    tool = draw.tool
    out["what"] = draw.label if tool == "distance" else WHAT.get(tool, "")
    try:
        if (resolved or tool in UNTARGETED) and board.index is None:
            raise _Bad(NOT_CONNECTED)
        if tool not in OWN_IDS:  # width and the plane tools: the items they measure, not the whole target
            out["ids"], out["outline"] = _ids(board, resolved)
        if tool in ("highlight", "isolate") or (tool == "area" and fields["targets"]):
            if not out["ids"] and not out["outline"]:
                missing = next((found for found in resolved if found.note), None)
                raise _Bad(missing.note if missing else "no target found on board")
        if tool == "label":
            out["text"] = fields["text"]
            out["points"] = [_anchor(by_role["at"], "at")]
        elif tool == "ghost":
            out["kind"] = fields["kind"]
            out["size_nm"] = round(fields["size"] * 1e6) if fields["size"] else 0
            out["net"] = fields["net"] if fields["kind"] != "part" else ""
            if fields["kind"] == "part":
                _ghost_part(board, out, by_role["part"], by_role["at"], fields["rotation"])
            elif fields["kind"] == "track":
                _ghost_track(board, out, [found for (role, _), found in zip(roles, resolved) if role == "path"],
                             fields)
            elif fields["kind"] == "via_row":
                _via_row(board, out, by_role["from"], by_role["to"], fields)
            else:
                out["points"] = [_anchor(by_role["at"], "at")]
                out["rotation_deg"] = fields["rotation"]
                _fit_ghost(board, out, fields["net"])
            out["kicad_drc"] = kicad_drc_state(out)
        elif tool == "arrow":
            out["points"] = _anchor_pair(board.index, by_role["from"], by_role["to"])
        elif tool == "distance":
            if fields["mode"] == "centre":
                out["points"] = _anchor_pair(board.index, by_role["from"], by_role["to"])
                (xa, ya, _), (xb, yb, _) = out["points"]
                measured = math.hypot(xa - xb, ya - yb)
            else:
                found, layer_a, layer_b = _edge_gap(board.index, by_role["from"], by_role["to"])
                out["points"] = _gap_points(found, layer_a, layer_b)
                measured = found.distance
            _measured(out, measured, fields["limit"], fields["limit_is"])
        elif tool == "clearance":
            found, layer_a, layer_b = _edge_gap(board.index, by_role["a"], by_role["b"], ("a", "b"))
            out["points"] = _gap_points(found, layer_a, layer_b)
            _measured(out, found.distance, fields["required"], "min")
        elif tool == "edge_gap":
            edge = board.resolve("edge")
            found, layer_a, layer_b = _edge_gap(board.index, by_role["target"], edge, ("target", "edge"))
            out["points"] = _gap_points(found, layer_a, layer_b)
            out["outline"] = True
            _measured(out, found.distance, fields["min_mm"], "min")
        elif tool == "width":
            _width(board, out, by_role["target"], fields["required"])
        elif tool == "routed_length":
            _route(board, out, fields["net"], by_role["from"], by_role["to"])
        elif tool == "layer_change":
            _layer_change(board, out, by_role["via"], fields["planes"], fields["limit"])
        elif tool == "plane_gap":
            _plane_gap(board, out, by_role["crossing"], fields["plane"])
        elif tool == "return_path":
            _return_path(board, out, by_role["signal"], fields["plane"])
        elif tool == "plane_islands":
            _islands(board, out, fields["net"], fields["layer"], draw.label)
        elif tool == "stitching":
            _stitching(board, out, fields["along"], fields["net"], fields["spacing"], fields["max"])
        elif tool == "stub":
            _stub(board, out, by_role.get("via"), by_role.get("pad"))
        elif tool == "discontinuity":
            _discontinuity(board, out, fields["net"], [found for (role, _), found in zip(roles, resolved)
                                                       if role == "at"])
        elif tool == "bars":
            out["unit"] = fields["unit"]
            out["bars"] = [{"label": label, "value": value, "limit": limit,
                            "over": limit is not None and value > limit} for label, value, limit in fields["items"]]
            if fields["at"]:
                out["points"] = [_anchor(by_role["at"], "at")]  # else the finding's first target (_place_bars)
        elif tool == "area":
            out["layer"] = fields["layer"]
            out["polygon_nm"] = [[int(round(x * 1e6)), int(round(y * 1e6))] for x, y in fields["polygon"]]
        elif tool == "layers":
            out["layers"] = list(fields["layers"])
    except _TooMany as error:
        out.update(ok=False, problem=str(error), ids=[], outline=False)
    except _Bad as error:
        out["ok"], out["problem"] = False, str(error)
    board.ids += len(out["ids"])
    return out, resolved


def _bbox(board: _Board, drawn: list[tuple[dict, list[Resolved]]]) -> list[int] | None:
    """Box around the items of every draw that kept its ids (not the whole outline) and
    every point: Blender frames the views on it. A zone counts only when nothing smaller
    does: a finding at a point of the ground plane is framed on that point, not the plane."""
    boxes, zones, points = [], [], []
    for draw, resolved in drawn:
        for found in resolved:
            if found.point is not None:
                points.append(found.point)
            if not draw["ids"] or board.index is None:
                continue
            for record in found.items:
                if isinstance(record, model.Outline):
                    continue
                # a part frames on its box (its body), though it measures as its pads' copper
                box = (shape_of(record) if isinstance(record, model.Footprint) else board.index.shape(record)).bbox
                if all(map(math.isfinite, box)):
                    (zones if isinstance(record, model.ZoneFill) else boxes).append(box)
    for draw, _ in drawn:
        points += [(x, y) for x, y, *_ in (*draw["points"], *draw["polygon_nm"], *draw["path_nm"],
                                           *draw["returns"], *draw["row"], *([draw["from"]] if draw["from"] else ()))]
        points += [(x, y) for pad in draw["pads_nm"] for x, y in pad["polygon_nm"]]
        points += [end for x0, y0, x1, y1, *_ in draw["spans_nm"] for end in ((x0, y0), (x1, y1))]
        points += [(x, y) for line in draw["edges_nm"] for x, y in line]
        loose = [island for island in draw["islands"] if not island["tied"]] or draw["islands"]
        points += [(x, y) for island in loose for x, y in island["polygon_nm"]]  # the loose pieces, if any
    if not boxes and not points:
        boxes = zones
    boxes += [(x, y, x, y) for x, y in points]
    if not boxes:
        return None
    x0, y0 = min(box[0] for box in boxes), min(box[1] for box in boxes)
    x1, y1 = max(box[2] for box in boxes), max(box[3] for box in boxes)
    return [int(math.floor(x0)), int(math.floor(y0)), int(math.ceil(x1)), int(math.ceil(y1))]


def _disagree(finding: Finding, draws: list[dict]) -> list[dict]:
    """Each claimed value the bridge measures too (`CLAIMS`: distance_mm, width_mm,
    length_mm, gap_mm, stub_mm) against the first draw that measured it, when > 10 % apart."""
    out = []
    for key, tools in CLAIMS:
        claimed = finding.values.get(key)
        measured = next((draw["measured_nm"] for draw in draws
                         if draw["tool"] in tools and draw["measured_nm"] is not None), None)
        if claimed is None or measured is None:
            continue
        measured_mm = measured / 1e6
        gap = abs(claimed - measured_mm)
        if gap <= DISAGREE_FLOOR_MM or gap <= DISAGREE * max(abs(claimed), abs(measured_mm)):
            continue
        out.append({"key": key, "claimed": claimed, "measured": round(measured_mm, 4)})
    return out


def _passes(draws: list[dict]) -> bool:
    """Every limit a draw measured is met (and there is one): the measurement does not
    back the finding up, e.g. an edge gap of 1.91 mm against a 0.50 mm minimum."""
    judged = [draw for draw in draws if draw["ok"] and draw["limit_nm"] is not None]
    return bool(judged) and not any(draw["over"] for draw in judged)


def _clash_text(record, gap: float) -> tuple[str, str]:
    """("GND track", "0.05 mm from its copper, 0.10 mm needed"): what a suggestion clashes with, and how near."""
    return (f"{record.net or 'unconnected'} {kind_of(record)}",
            f"{gap / 1e6:.2f} mm from its copper, {GHOST_CLEAR_NM / 1e6:.2f} mm needed")


def _touch(out: dict, member: int, record, gap: float) -> None:
    """Note in `touches` ([member, net]) that a ghost's copper (a row's via `member`, else 0)
    touches copper of another net: a short. KiCad merges a touching via or track into the
    net it touches, so its DRC cannot report that; the quick check's word stands."""
    if gap <= TOUCH_NM and record.net and [member, record.net] not in out["touches"]:
        out["touches"].append([member, record.net])


def _fit_ghost(board: _Board, out: dict, net: str) -> None:
    """Does a suggested via or pad fit where the LLM put it: `fits` False, and why in
    `problem`, when its copper comes within GHOST_CLEAR_NM of fixed copper (tracks, arcs,
    vias, pads) of another net, or of any net when it names none. A via spans every
    layer; a pad its own. Zones refill around it and never clash. A cap: `_fit_cap`. A
    part moved, a track, a row of vias: `_ghost_part`, `_ghost_track`, `_via_row`."""
    if out["kind"] == "cap" and board.index is not None:
        _fit_cap(board.index, out)
    if out["kind"] not in ("via", "pad") or board.index is None:
        return
    x, y, layer = out["points"][0]
    radius = (out["size_nm"] or GHOST_VIA_NM) / 2
    near = _via_near(board.index, (x, y), layer, radius, net, out["kind"], board.index.copper)
    out["fits"] = near is None
    if near is not None:
        gap = max(0.0, near.gap.distance - radius)
        what, how = _clash_text(near.record, gap)
        out["problem"] = f"clashes with {what} ({how})"
        _touch(out, 0, near.record, gap)


def _via_near(index: BoardIndex, point, layer: str, radius: float, net: str, kind: str, copper):
    """The fixed copper among `copper` of a net other than `net` nearest a suggested via
    (every layer) or pad (its `layer`) of `radius` at `point`, within GHOST_CLEAR_NM of its
    edge (`Near`, measured from the point), or None. Copper whose centre is further than
    GHOST_NEAR_NM past that is never measured."""
    reach = radius + GHOST_CLEAR_NM
    candidates = [record for record in copper
                  if isinstance(record, FIXED) and record.net != net
                  and (kind == "via" or on_layer(record, layer))
                  and math.dist(centre(record), point) < reach + GHOST_NEAR_NM]
    return index.nearest(point, candidates, None if kind == "via" else layer, reach)


def _near_box(index: BoardIndex, box, reach: float) -> list:
    """The fixed copper whose box comes within `reach` of `box`: a track's and a via's from
    their ends, an arc's from its shape, a pad's as GHOST_NEAR_NM around its position."""
    found = []
    for record in index.copper:
        if isinstance(record, model.Track):
            half = record.width / 2
            (x0, y0), (x1, y1) = record.start, record.end
            other = (min(x0, x1) - half, min(y0, y1) - half, max(x0, x1) + half, max(y0, y1) + half)
        elif isinstance(record, model.Via):
            half = record.diameter / 2
            other = (record.pos[0] - half, record.pos[1] - half, record.pos[0] + half, record.pos[1] + half)
        elif isinstance(record, model.Pad):
            x, y = record.pos
            other = (x - GHOST_NEAR_NM, y - GHOST_NEAR_NM, x + GHOST_NEAR_NM, y + GHOST_NEAR_NM)
        elif isinstance(record, model.Arc):
            other = index.shape(record).bbox
        else:
            continue
        if _bbox_gap(box, other) < reach:
            found.append(record)
    return found


def _clash(index: BoardIndex, shape, layer: str, net: str, candidates, skip: str = ""):
    """(gap, record) of the fixed copper among `candidates` on `layer`, of a net other than
    `net` (and of no part `skip`), nearest `shape` within GHOST_CLEAR_NM; None when clear."""
    best = None
    for record in candidates:
        if record.net == net or not on_layer(record, layer) or (skip and getattr(record, "footprint_id", "") == skip):
            continue
        other = index.shape(record, layer)
        if not len(other.segments) or _bbox_gap(shape.bbox, other.bbox) >= GHOST_CLEAR_NM:
            continue
        found = shapes_gap(shape, other).distance
        if found < GHOST_CLEAR_NM and (best is None or found < best[0]):
            best = (found, record)
    return best


def _off_board(index: BoardIndex, shape) -> str:
    """Why `shape` is not inside the board outline ("crosses the board edge", "outside the
    board"), or "" when it is, or when the board has no closed outline to tell by. Rings
    count even-odd: KiCad may hand a cutout over as a polygon of its own."""
    outline = index.snapshot.outline
    rings = [np.asarray(ring, dtype=float) for polygon in outline.polygons for ring in polygon if len(ring) >= 3]
    if not rings or not len(shape.probes):
        return ""
    if shapes_gap(shape, index.shape(outline)).distance <= 0:
        return "crosses the board edge"
    inside = sum(_inside(shape.probes, (ring,)).astype(int) for ring in rings) % 2  # no crossing: one probe a piece
    return "" if inside.all() else "outside the board"


def _turner(origin, to, degrees: float):
    """A function moving board points (nm) as a part is moved from `origin` to `to` and
    turned `degrees` counter-clockwise about it as seen from the top (KiCad's sense)."""
    a = math.radians(degrees)
    cos, sin = math.cos(a), math.sin(a)

    def move(point):
        dx, dy = point[0] - origin[0], point[1] - origin[1]
        return (int(round(to[0] + dx * cos + dy * sin)), int(round(to[1] - dx * sin + dy * cos)))
    return move


def _ghost_part(board: _Board, out: dict, target: Resolved, at: Resolved, rotation: float | None) -> None:
    """A part moved to `at` (its anchor, KiCad's footprint position) and turned to
    `rotation` (None: as it is): its pads' copper and its body (`_body`) moved with it. It
    does not fit (`fits` False, why in `problem`) where it is not inside the board outline,
    where a pad comes within GHOST_CLEAR_NM of fixed copper of another net on that pad's
    layer (its own pads aside), or where its body lands on another part's on its side.
    Sent: the moved pads per layer (outer copper, else their first layer; none past
    MAX_GHOST_PADS, which is noted), the body as `polygon_nm`, `from` its place now,
    `moved_nm` how far."""
    index = board.index
    if not target.found:
        raise _Bad(f"part: {target.note or 'not found on board'}")
    parts = [record for record in target.items if isinstance(record, model.Footprint)]
    if not parts:
        raise _Bad(f"part: {target.target.strip()} is not a part (give its reference, e.g. C12)")
    if len(parts) > 1:
        raise _Bad(f"part: {len(parts)} parts share reference {target.target.strip()}")
    part = parts[0]
    x, y, _ = _anchor(at, "at")
    layer = top_layer(part)
    now = math.degrees(part.rotation_rad) % 360
    turned = now if rotation is None else rotation
    move = _turner(part.pos, (x, y), turned - now)
    pads = list(index.part_pads(part.id))
    moved = [(pad, {name: tuple(tuple(tuple(map(move, ring)) for ring in polygon) for polygon in polygons)
                    for name, polygons in pad.polygons.items() if copper_rank(name) is not None})
             for pad in pads]
    body = _body(part, pads)
    body = tuple(map(move, body)) if body is not None else None
    shapes = [(pad, name, shape_of(model.CopperGraphic("ghost", pad.net, name, polygons)))
              for pad, by_layer in moved for name, polygons in by_layer.items() if polygons]
    body_shape = shape_of(model.CopperGraphic("body", "", layer, ((body,),))) if body is not None else None
    whole = union([shape for _, _, shape in shapes] + ([body_shape] if body_shape is not None else []))
    out.update(part=part.reference or part.id, layer=layer, points=[[x, y, layer]], rotation_deg=turned,
               moved_nm=int(round(math.dist(part.pos, (x, y)))), polygon_nm=[list(point) for point in body or ()])
    out["from"] = [int(part.pos[0]), int(part.pos[1]), layer]
    if len(pads) > MAX_GHOST_PADS:
        out["problem"] = f"{len(pads):,} pads; only the body drawn"
    else:
        out["pads_nm"] = [{"layer": name, "polygon_nm": [list(point) for point in polygon[0]]}
                          for pad, by_layer in moved for name in _shown_layers(by_layer)
                          for polygon in by_layer[name]]
    reasons = [_off_board(index, whole)] if len(whole.segments) else []
    candidates = _near_box(index, whole.bbox, GHOST_CLEAR_NM) if len(whole.segments) else []
    for pad, name, shape in shapes:
        found = _clash(index, shape, name, pad.net, candidates, skip=part.id)
        if found is not None:
            what, how = _clash_text(found[1], found[0])
            reasons.append(f"{part.reference}.{pad.number} clashes with {what} ({how})")
            _touch(out, 0, found[1], found[0])
            break
    if body_shape is not None:
        under = _part_under(index, body_shape, layer, _pads_near(index, body_shape.bbox), skip=part.id)
        if under is not None:
            reasons.append(f"lands on {under.reference or 'a part'}")
    reasons = [reason for reason in reasons if reason]
    out["fits"] = not reasons
    if reasons:
        out["problem"] = "; ".join(filter(None, (*reasons, out["problem"])))


def _shown_layers(by_layer: dict) -> list[str]:
    """The layers a moved pad is drawn on: its outer copper, else its first copper layer."""
    outer = [name for name in ("F.Cu", "B.Cu") if by_layer.get(name)]
    return outer or [name for name in by_layer if by_layer[name]][:1]


def _usual_width(index: BoardIndex, net: str) -> int:
    """The width most of a net's tracks and arcs have (the wider on a tie), else GHOST_TRACK_NM."""
    counts: dict[int, int] = {}
    for record in index.net_items(net):
        if isinstance(record, (model.Track, model.Arc)):
            counts[record.width] = counts.get(record.width, 0) + 1
    return max(counts, key=lambda width: (counts[width], width)) if counts else GHOST_TRACK_NM


def _ghost_track(board: _Board, out: dict, path: list[Resolved], fields: dict) -> None:
    """A suggested route for `net`: straight segments through the path's points on one
    copper layer (the draw's, else the one every point names), `width` (else the net's
    usual one, `_usual_width`). It does not fit (`fits` False, why in `problem`) where it
    comes within GHOST_CLEAR_NM of fixed copper of another net on that layer, or leaves the
    board outline. Zones refill and never clash. Sent: `path_nm`, `width_nm`, `length_nm`."""
    index = board.index
    net = fields["net"]
    if net not in index.nets:
        raise _Bad(f"net {_show(net, 80)} not found on board")
    spots = [_anchor(found, f"path[{i}]") for i, found in enumerate(path)]
    named_layers = [found.layer for found in path]
    layer = fields["layer"]
    if layer is None:
        if None in named_layers or len(set(named_layers)) != 1:
            raise _Bad("layer missing: give layer, or the same @layer on every point of path")
        layer = named_layers[0]
        if copper_rank(layer) is None:
            raise _Bad(f"{_show(layer)} is not a copper layer")
    else:
        other = next((i for i, name in enumerate(named_layers) if name and name != layer), None)
        if other is not None:
            raise _Bad(f"path[{other}] is on {_show(named_layers[other])}, the track on {layer}")
    width = int(round(fields["width"] * 1e6)) if fields["width"] else _usual_width(index, net)
    ends = [(x, y) for x, y, _ in spots]
    shape = union(shape_of(model.Track("ghost", layer, net, a, b, width)) for a, b in zip(ends, ends[1:]))
    out.update(layer=layer, path_nm=[[x, y, layer] for x, y in ends], points=[[*ends[0], layer], [*ends[-1], layer]],
               width_nm=width, length_nm=int(round(sum(math.dist(a, b) for a, b in zip(ends, ends[1:])))))
    reasons = []
    found = _clash(index, shape, layer, net, _near_box(index, shape.bbox, GHOST_CLEAR_NM))
    if found is not None:
        what, how = _clash_text(found[1], found[0])
        reasons.append(f"clashes with {what} ({how})")
        _touch(out, 0, found[1], found[0])
    reasons.append(_off_board(index, shape))
    reasons = [reason for reason in reasons if reason]
    out["fits"] = not reasons
    out["problem"] = "; ".join(reasons)


def _via_row(board: _Board, out: dict, start: Resolved, end: Resolved, fields: dict) -> None:
    """A row of suggested vias from `start` to `end`, both ends included, spaced evenly and
    no further apart than `pitch`; each checked as a via ghost is (`_via_near`). More than
    MAX_ROW_VIAS refuses the draw. Sent: `row` [x, y, fits] per via, `pitch_nm` the spacing
    used; `fits` False when any via clashes, `problem` how many and the first."""
    index = board.index
    a, b = _anchor(start, "from"), _anchor(end, "to")
    length = math.dist(a[:2], b[:2])
    gaps = math.ceil(length / (fields["pitch"] * 1e6) - 1e-9) if length > 0 else 0
    if gaps + 1 > MAX_ROW_VIAS:
        raise _Bad(f"{gaps + 1:,} vias at {fields['pitch']:g} mm pitch over {length / 1e6:.2f} mm; at most "
                   f"{MAX_ROW_VIAS} in one row")
    spots = [(a[0] + (b[0] - a[0]) * i / gaps, a[1] + (b[1] - a[1]) * i / gaps) for i in range(gaps + 1)] \
        if gaps else [(a[0], a[1])]
    radius = (out["size_nm"] or GHOST_VIA_NM) / 2
    row_box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
    copper = [record for record in index.copper
              if isinstance(record, FIXED) and _bbox_gap(row_box, (*centre(record), *centre(record)))
              < radius + GHOST_CLEAR_NM + GHOST_NEAR_NM]
    row, first, clashing = [], None, 0
    for member, (x, y) in enumerate(spots):
        near = _via_near(index, (x, y), a[2], radius, fields["net"], "via", copper)
        row.append([int(round(x)), int(round(y)), near is None])
        if near is not None:
            clashing += 1
            gap = max(0.0, near.gap.distance - radius)
            first = first or _clash_text(near.record, gap)
            _touch(out, member, near.record, gap)
    out.update(row=row, points=[a, b], layer=a[2], pitch_nm=int(round(length / gaps)) if gaps else 0,
               fits=not clashing)
    if clashing:
        out["problem"] = f"{clashing} of {len(row)} vias clash (first: {first[0]} {first[1]})"


def _box(centre_nm, length: float, width: float, degrees: float, frame: tuple[float, float] = (1.0, 0.0)) -> tuple:
    """A ring of the four corners of a length x width box around `centre_nm`, turned
    `degrees` counter-clockwise as seen from the top (KiCad's sense; the board frame's y
    points down), `frame` an extra turn as (cos, sin)."""
    a = math.radians(degrees)
    cos, sin = math.cos(a) * frame[0] - math.sin(a) * frame[1], math.sin(a) * frame[0] + math.cos(a) * frame[1]
    u, v = (cos, -sin), (sin, cos)
    return tuple((int(round(centre_nm[0] + su * length / 2 * u[0] + sv * width / 2 * v[0])),
                  int(round(centre_nm[1] + su * length / 2 * u[1] + sv * width / 2 * v[1])))
                 for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)))


def _body(footprint: model.Footprint, pads: list) -> tuple | None:
    """A part's body as the box of its pads' copper in its own frame (a ring), or None
    with fewer than two pads: what a suggested part must not land on."""
    points = [point for pad in pads for layer, polygons in pad.polygons.items() if layer not in model.PASTE_LAYERS
              for polygon in polygons for point in polygon[0]]
    if len(pads) < 2 or not points:
        return None
    r = footprint.rotation_rad
    u, v = (math.cos(r), -math.sin(r)), (math.sin(r), math.cos(r))
    x0, y0 = footprint.pos
    along = [(px - x0) * u[0] + (py - y0) * u[1] for px, py in points]
    across = [(px - x0) * v[0] + (py - y0) * v[1] for px, py in points]
    middle = ((min(along) + max(along)) / 2, (min(across) + max(across)) / 2)
    centre_nm = (x0 + middle[0] * u[0] + middle[1] * v[0], y0 + middle[0] * u[1] + middle[1] * v[1])
    return _box(centre_nm, max(along) - min(along), max(across) - min(across), 0.0, (math.cos(r), math.sin(r)))


def _fit_cap(index: BoardIndex, out: dict) -> None:
    """A suggested cap (its body, `size` long and half as wide, turned `rotation_deg`)
    must land clear of other parts on its side: of their pads by GHOST_CLEAR_NM, and of
    their bodies (`_body`). `fits` False, and why in `problem`, when it does not."""
    x, y, layer = out["points"][0]
    layer = layer if copper_rank(layer or "") is not None else "F.Cu"
    length = out["size_nm"] or CAP_GHOST_NM
    ghost = model.CopperGraphic("ghost", "", layer, ((_box((x, y), length, length / 2, out["rotation_deg"]),),))
    shape = shape_of(ghost)
    near = [pad for pad in index.snapshot.pads if math.dist(pad.pos, (x, y)) < GHOST_NEAR_NM]
    for pad in near:
        if on_layer(pad, layer) and len(index.shape(pad, layer).segments) and \
                shapes_gap(shape, index.shape(pad, layer)).distance < GHOST_CLEAR_NM:
            owner = index.footprint_of(pad)
            name = f"{owner.reference}.{pad.number}" if owner else f"pad {pad.number}"
            out["fits"], out["problem"] = False, f"lands on {name}" + (f" ({pad.net})" if pad.net else "")
            return
    under = _part_under(index, shape, layer, near)
    if under is not None:
        out["fits"], out["problem"] = False, f"lands on {under.reference or 'a part'}"
        return
    out["fits"] = True


def _pads_near(index: BoardIndex, box) -> list:
    """The pads whose position lies within GHOST_NEAR_NM of `box`."""
    return [pad for pad in index.snapshot.pads if _bbox_gap(box, (*pad.pos, *pad.pos)) < GHOST_NEAR_NM]


def _part_under(index: BoardIndex, shape, layer: str, near: list, skip: str = ""):
    """The first part (but `skip`) on `layer`'s side whose body, the box of its pads among
    `near` (`_body`), `shape` lands on; None when it lands on none."""
    side = "bottom" if layer == "B.Cu" else "top" if layer == "F.Cu" else None
    by_part: dict[str, list] = {}
    for pad in near:
        if pad.footprint_id != skip:
            by_part.setdefault(pad.footprint_id, []).append(pad)
    for pads in by_part.values():
        footprint = index.footprint_of(pads[0])
        if footprint is None or (side is not None and footprint.side != side):
            continue
        body = _body(footprint, pads)
        if body is not None and shapes_gap(shape, shape_of(model.CopperGraphic("body", "", layer, ((body,),)))
                                           ).distance == 0:
            return footprint
    return None


def _ghosts_in_reach(drawn: list[tuple[dict, list[Resolved]]]) -> None:
    """A suggested via in a finding with a layer change must sit within that via's limit
    (a reason KiCad's DRC does not judge: kept in `kicad_drc`)."""
    reach = next((draw for draw, _ in drawn if draw["tool"] == "layer_change" and draw["ok"] and draw["points"]),
                 None)
    if reach is None:
        return
    vx, vy, _ = reach["points"][0]
    for draw, _ in drawn:
        if draw["tool"] == "ghost" and draw["kind"] == "via" and draw["ok"] and draw["points"]:
            apart = math.dist(draw["points"][0][:2], (vx, vy))
            if apart > reach["limit_nm"]:
                reason = f"{apart / 1e6:.2f} mm from the via, outside its {reach['limit_nm'] / 1e6:.2f} mm limit"
                draw["fits"] = False
                draw["problem"] = "; ".join(filter(None, (draw["problem"], reason)))
                if draw["kicad_drc"] is not None:
                    draw["kicad_drc"]["kept"].append(reason)


def _place_bars(board: _Board, drawn: list[tuple[dict, list[Resolved]]], resolved: list[Resolved]) -> None:
    """Bars with no `at` stand at the finding's first target found on the board."""
    first = next((found for found in resolved if found.found), None)
    for draw, _ in drawn:
        if draw["tool"] != "bars" or not draw["ok"] or draw["points"]:
            continue
        if board.index is None:
            draw["ok"], draw["problem"] = False, NOT_CONNECTED
        elif first is None:
            draw["ok"], draw["problem"] = False, "no target on the board to stand at; give at"
        else:
            draw["points"] = [_anchor(first, "at")]


def handshake(found: FindingsFile, context: Context) -> str:
    """Where the file came from: "known" (a run this bridge made, with that run's stamp;
    or our own DRC file), "other_run" (it names a run or stamp, but not one of ours: an
    older or foreign run) or "outside" (no run: another tool or script wrote it)."""
    if found.stamp and (context.runs.get(found.run) == found.stamp or context.source == "drc"):
        return "known"
    if found.run or found.stamp:
        return "other_run"
    return "outside"


def status_payload(status: str, context: Context | None = None, error: str = "",
                   connected: bool = False) -> dict:
    """A payload with no findings: "none" (no file), "unsaved", "error", "unknown_format"."""
    return {"status": status, "error": error,
            "source": context.source if context else "", "path": context.path if context else "",
            "run": "", "stamp": "", "live_stamp": context.live_stamp if context else "",
            "handshake": "", "changed": False, "connected": connected, "tool": "", "problems": [],
            "drc": {"state": "idle", "error": "", "run": ""}, "skipped": [], "left_out": {}, "findings": []}


def _group(check: str) -> str:
    family = check.partition(".")[0]
    return family if "." in check and family in GROUPS else "other"


def resolve_file(found: FindingsFile, index: BoardIndex | None, context: Context) -> dict:
    """The `findings` payload. `index` None: KiCad is not connected; the findings are
    listed and every target says so. `handshake`: where the file came from (`handshake()`);
    every file is drawn. `changed`: a file of a known run whose stamp differs from the live
    board's (the board was edited since the run)."""
    payload = status_payload("ok", context, connected=index is not None)
    agreed = handshake(found, context)
    payload.update(run=found.run, stamp=found.stamp, handshake=agreed, tool=found.tool,
                   changed=agreed == "known" and bool(context.live_stamp) and context.live_stamp != found.stamp,
                   problems=list(found.problems),
                   skipped=[{"check": check, "reason": reason} for check, reason in found.skipped],
                   left_out=dict(found.left_out))
    board = _Board(index)
    keys: dict[str, int] = {}
    for finding in found.findings:
        resolved = [board.target(draw.tool, role, text) for draw in finding.draws for role, text in _roles(draw)]
        names = [name for item in resolved for name in named(index, item)] or [f"title:{finding.title}"]
        key = finding_key(finding.check, names)
        keys[key] = keys.get(key, 0) + 1
        if keys[key] > 1:  # the same finding twice in one file: keys stay unique
            key = f"{key}-{keys[key]}"
        drawn = [_resolve_draw(board, draw) for draw in finding.draws]
        _place_bars(board, drawn, resolved)
        _ghosts_in_reach(drawn)
        draws = [draw for draw, _ in drawn]
        payload["findings"].append({
            "key": key, "check": finding.check, "group": _group(finding.check),
            "severity": finding.severity, "confidence": finding.confidence,
            "title": finding.title, "message": finding.message, "fix": finding.fix,
            "values": dict(finding.values), "values_text": dict(finding.values_text),
            "state": context.states.get(key, ""),
            "faded": index is not None and bool(resolved) and not any(item.found for item in resolved),
            "problems": list(finding.problems),
            "bbox_nm": _bbox(board, drawn),
            "disagree": _disagree(finding, draws),
            "passes": _passes(draws),
            "mentions": mentions.find(index, finding.title, finding.message, finding.fix),
            "draws": draws,
        })
    return payload
