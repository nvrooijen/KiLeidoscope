"""Findings files (`kls-findings 1`): read, check, resolve.

A findings file is untrusted data: a tool, a script or our own DRC converter wrote it.
`parse` checks every field against fixed limits and keeps only plain text, numbers and
target strings; nothing in it is ever opened, run or read as markup. `resolve_file`
finds the targets on the live board (`targets.resolve`) and turns the file into the
`findings` payload Blender draws: item ids, anchor points and measured distances in nm,
KiCad's board frame (y down). The draw tools are `highlight`, `label`, `arrow`,
`distance`, `area` (copper items on one layer) and the measured ones: `clearance` and
`edge_gap` (closest copper points) and `width` (every track segment with its width).
Every measured draw names what it measured in `what`, so Blender labels them all alike;
a finding whose every measured limit is met `passes`. Confirm/dismiss state is keyed on
the check and the named targets (`named`, `finding_key`), so it survives re-routing that
changes uuids. A file's `stamp` against the live board's says whether the board
`changed` since the file was written.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Iterable

from . import model
from .board_index import BoardIndex, Gap, centre, copper_rank, shape_of, top_layer
from .geometry import sample_arc
from .targets import Resolved, copper_layer, resolve
from .targets import kind as kind_of

FORMAT = "kls-findings 1"
MAX_FILE_BYTES = 2 << 20
MAX_FINDINGS = 300
MAX_DRAWS = 20
MAX_TEXT = 300
MAX_VALUES = 20
MAX_TARGETS = 50  # per list
MAX_TARGET_TEXT = 200
MAX_IDS_DRAW = 5_000
MAX_IDS_FILE = 100_000
MAX_PAIRS = 250_000  # item pairs one edge distance may measure
MAX_SEGMENTS = 2_000  # width: track segments drawn
MAX_MM = 10_000.0  # coordinates and sizes past 10 m are not a board
KEY_GRID_NM = 500_000  # anchors in keys rounded to 0.5 mm
UNITS = ("_mm2", "_cm2", "_mm", "_ps", "_mv", "_a", "_pct")  # longest first
WHAT = {"clearance": "Clearance", "edge_gap": "Edge gap", "width": "Width"}
ROLES = ("at", "from", "to", "a", "b", "target")  # draw fields with targets
SEVERITIES = ("error", "warning", "info")
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
    emphasis: str = "normal"


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    title: str
    message: str
    values: dict  # name -> float, unit from the name's suffix
    draws: tuple[Draw, ...]
    problems: tuple[str, ...]  # skipped draws, cut text, bad enums


@dataclass(frozen=True)
class FindingsFile:
    run: str
    stamp: str  # "" when missing or not 8 hex
    tool: str
    findings: tuple[Finding, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True)
class Context:
    """What the payload needs besides the file."""
    path: str
    source: str  # "drc" | "file"
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


def _layer(value, what: str) -> str:
    layer = copper_layer(value) if isinstance(value, str) else None
    if layer is None or copper_rank(layer) is None:
        raise _Bad(f"{what} is not a copper layer")
    return layer


def _fields(tool: str, raw: dict, problems: list) -> dict:
    """The validated fields of one tool; raises _Bad for a missing or bad field."""
    if tool == "highlight":
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
    if tool == "area":
        return {"layer": _layer(_need(raw, "layer"), "layer"), "targets": _targets(_need(raw, "targets"))}
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
    emphasis = raw.get("emphasis", "normal")
    if emphasis not in ("normal", "strong"):
        notes.append("emphasis is not normal or strong")
        emphasis = "normal"
    problems.extend(f"{what}: {note}" for note in notes)
    return Draw(tool, fields, label, emphasis)


def _values(raw, problems: list) -> dict:
    """Numbers whose name ends in a unit (`distance_mm`); anything else is left out with a note."""
    values = {}
    if raw is None:
        return values
    if not isinstance(raw, dict):
        problems.append("values is not an object")
        return values
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
        else:
            problems.append(f"value {key} is not a number with a unit; left out")
    return values


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
    title = _text(raw["title"], MAX_TEXT, "title", problems) if "title" in raw else None
    if not title:
        problems.append("no title")
        title = check
    message = _text(raw["message"], MAX_TEXT, "message", problems) if "message" in raw else ""
    values = _values(raw.get("values"), problems)
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
    return Finding(check, severity, title, message or "", values, tuple(draws), tuple(problems))


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
    if len(raw_findings) > MAX_FINDINGS:
        problems.append(f"{len(raw_findings) - MAX_FINDINGS} findings past {MAX_FINDINGS} left out")
    findings = [_finding(item, i + 1, problems) for i, item in enumerate(raw_findings[:MAX_FINDINGS])]
    return FindingsFile(run or "", stamp, tool or "", tuple(f for f in findings if f is not None), tuple(problems))


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
    """(role, target text) of every target a draw names, in order."""
    fields = draw.fields
    if "targets" in fields:
        return [("targets", text) for text in fields["targets"]]
    return [(role, fields[role]) for role in ROLES if fields.get(role)]


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
    return {"tool": draw.tool, "emphasis": draw.emphasis, "label": draw.label, "text": "",
            "ok": True, "problem": "", "targets": [], "ids": [], "outline": False, "points": [],
            "measured_nm": None, "limit_nm": None, "limit_is": "", "over": False, "layer": "",
            "what": "", "segments_nm": [], "narrow": [], "min_nm": None}


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


def _resolve_draw(board: _Board, draw: Draw) -> tuple[dict, list[Resolved]]:
    """The payload of one draw, and its resolved targets."""
    out = _empty_draw(draw)
    fields = draw.fields
    roles = _roles(draw)
    resolved = [board.resolve(text) for _, text in roles]
    by_role = {role: found for (role, _), found in zip(roles, resolved)}
    out["targets"] = [{"text": found.target.strip(), "found": found.found, "note": found.note,
                       "kind": kind_of(found.items[0]) if found.items else ("point" if found.found else "")}
                      for found in resolved]
    tool = draw.tool
    out["what"] = draw.label if tool == "distance" else WHAT.get(tool, "")
    try:
        if resolved and board.index is None:
            raise _Bad(NOT_CONNECTED)
        if tool != "width":  # width: the items it measures, not the whole target
            out["ids"], out["outline"] = _ids(board, resolved)
        if tool in ("highlight", "area"):
            if not out["ids"] and not out["outline"]:
                missing = next((found for found in resolved if found.note), None)
                raise _Bad(missing.note if missing else "no target found on board")
        if tool == "label":
            out["text"] = fields["text"]
            out["points"] = [_anchor(by_role["at"], "at")]
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
        elif tool == "area":
            out["layer"] = fields["layer"]
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
        points += [(x, y) for x, y, *_ in draw["points"]]
    if not boxes and not points:
        boxes = zones
    boxes += [(x, y, x, y) for x, y in points]
    if not boxes:
        return None
    x0, y0 = min(box[0] for box in boxes), min(box[1] for box in boxes)
    x1, y1 = max(box[2] for box in boxes), max(box[3] for box in boxes)
    return [int(math.floor(x0)), int(math.floor(y0)), int(math.ceil(x1)), int(math.ceil(y1))]


def _passes(draws: list[dict]) -> bool:
    """Every limit a draw measured is met (and there is one): the measurement does not
    back the finding up, e.g. an edge gap of 1.91 mm against a 0.50 mm minimum."""
    judged = [draw for draw in draws if draw["ok"] and draw["limit_nm"] is not None]
    return bool(judged) and not any(draw["over"] for draw in judged)


def status_payload(status: str, context: Context | None = None, error: str = "",
                   connected: bool = False) -> dict:
    """A payload with no findings: "none" (no file), "unsaved", "error", "unknown_format"."""
    return {"status": status, "error": error,
            "source": context.source if context else "", "path": context.path if context else "",
            "run": "", "stamp": "", "live_stamp": context.live_stamp if context else "",
            "changed": False, "connected": connected, "tool": "", "problems": [], "findings": []}


def _group(check: str) -> str:
    family = check.partition(".")[0]
    return family if "." in check and family in GROUPS else "other"


def resolve_file(found: FindingsFile, index: BoardIndex | None, context: Context) -> dict:
    """The `findings` payload. `index` None: KiCad is not connected; the findings are
    listed and every target says so. `changed`: the file carries a stamp and the live
    board's differs (the board was edited since the file was written)."""
    payload = status_payload("ok", context, connected=index is not None)
    payload.update(run=found.run, stamp=found.stamp, tool=found.tool,
                   changed=bool(found.stamp) and bool(context.live_stamp) and context.live_stamp != found.stamp,
                   problems=list(found.problems))
    board = _Board(index)
    keys: dict[str, int] = {}
    for finding in found.findings:
        resolved = [board.resolve(text) for draw in finding.draws for _, text in _roles(draw)]
        names = [name for item in resolved for name in named(index, item)] or [f"title:{finding.title}"]
        key = finding_key(finding.check, names)
        keys[key] = keys.get(key, 0) + 1
        if keys[key] > 1:  # the same finding twice in one file: keys stay unique
            key = f"{key}-{keys[key]}"
        drawn = [_resolve_draw(board, draw) for draw in finding.draws]
        draws = [draw for draw, _ in drawn]
        payload["findings"].append({
            "key": key, "check": finding.check, "group": _group(finding.check),
            "severity": finding.severity, "title": finding.title, "message": finding.message,
            "values": dict(finding.values),
            "state": context.states.get(key, ""),
            "faded": index is not None and bool(resolved) and not any(item.found for item in resolved),
            "problems": list(finding.problems),
            "bbox_nm": _bbox(board, drawn),
            "passes": _passes(draws),
            "draws": draws,
        })
    return payload
