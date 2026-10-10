"""KiCad's DRC as findings: run kicad-cli, convert its report.

`run_drc` runs `kicad-cli pcb drc` on a frozen run folder (`board.kicad_pcb` with the
project's `.kicad_pro`/`.kicad_dru` copied beside it) and returns the JSON report.
`convert` turns that report into a `kls-findings 1` file, so DRC output takes the same
path (parse, resolve, draw) as a tool's or a script's findings file.

What the report gives and lacks (kicad-cli 10.0.3, RESEARCH-01): entries
`{type, description, severity, items[{description, pos, uuid}]}`; every uuid is the
file's (pads included), `pos` is each item's own anchor (a track's start, a zone's first
corner), never the marker. Numbers exist only inside the description, in the user's
locale (`0,2000 mm`). So the converter measures what it can on the `BoardIndex` and
parses the description only as a fallback; DRC's "actual" goes into the finding's
`values` (`distance_mm`), and the bridge measures every `clearance` and `edge_gap` draw
again on the live board. Track widths are drawn with `width`, which lists every segment
of the track against the limit; isolated copper and a dangling via are highlighted and
labelled. Coordinates in targets are mm in KiCad's frame (y down).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import model
from .board_index import BoardIndex, centre, has_copper, point_gap, top_layer
from .findings import FORMAT, MAX_TARGETS, MAX_TEXT
from .model import Point

BOARD = "board.kicad_pcb"  # in a run folder (kls_folder)
REPORT = "drc.json"
TIMEOUT_S = 180
ENTRY_LISTS = ("violations", "unconnected_items", "schematic_parity")
DROPPED = frozenset({"lib_footprint_issues", "lib_footprint_mismatch"})  # library setup, not the board
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# Types whose description ends in "(… <limit> …; actual <value>)" in length units.
MEASURED = frozenset({
    "clearance", "hole_clearance", "hole_to_hole", "copper_edge_clearance", "track_width",
    "via_diameter", "annular_width", "drill_out_of_range", "microvia_drill_out_of_range",
    "connection_width", "text_height", "text_thickness", "silk_overlap", "silk_edge_clearance",
    "silk_over_copper", "courtyards_overlap", "track_segment_length", "length_out_of_range",
    "diff_pair_gap_out_of_range", "diff_pair_uncoupled_length_too_long", "solder_mask_bridge",
    "creepage", "assertion_failure"})
SILK = frozenset({"silk_overlap", "silk_edge_clearance", "text_thickness", "silk_over_copper", "text_height",
                  "mirrored_text_on_front_layer", "nonmirrored_text_on_back_layer"})
COURTYARD = frozenset({"courtyards_overlap", "malformed_courtyard", "missing_courtyard",
                       "npth_inside_courtyard", "pth_inside_courtyard"})
FOOTPRINT = frozenset({"footprint_symbol_mismatch", "extra_footprint", "missing_footprint", "net_conflict",
                       "duplicate_footprints", "footprint_filters_mismatch", "footprint"})
TITLES = {
    "clearance": "Clearance", "shorting_items": "Short", "tracks_crossing": "Tracks cross",
    "hole_clearance": "Hole clearance", "hole_to_hole": "Holes too close",
    "copper_edge_clearance": "Copper near board edge", "unconnected_items": "Not connected",
    "track_dangling": "Dangling track", "via_dangling": "Unconnected via", "isolated_copper": "Isolated copper",
    "track_width": "Track width", "via_diameter": "Via diameter", "annular_width": "Annular ring",
    "drill_out_of_range": "Drill size", "microvia_drill_out_of_range": "Microvia drill",
    "connection_width": "Connection width", "starved_thermal": "Starved thermal",
    "solder_mask_bridge": "Solder mask bridge", "courtyards_overlap": "Courtyards overlap",
    "invalid_outline": "Board outline", "silk_overlap": "Silkscreen overlap",
    "silk_edge_clearance": "Silkscreen at board edge", "silk_over_copper": "Silkscreen on copper",
}
HOLE_LABELS = {"hole_clearance": "Hole clearance", "hole_to_hole": "Hole to hole"}  # what the dimension measures
# What the model knows of an item for a size check: (value key, label word, nm of a record or None).
SIZES = {
    "track_width": ("width_mm", "Width", lambda r: r.width if isinstance(r, (model.Track, model.Arc)) else None),
    "via_diameter": ("diameter_mm", "Diameter", lambda r: r.diameter if isinstance(r, model.Via) else None),
    "annular_width": ("annular_width_mm", "Annular ring",
                      lambda r: (r.diameter - r.drill) / 2 if isinstance(r, model.Via) else None),
    "drill_out_of_range": ("drill_mm", "Drill", lambda r: _drill(r)),
    "microvia_drill_out_of_range": ("drill_mm", "Drill", lambda r: _drill(r)),
    "connection_width": ("connection_width_mm", "Connection width", lambda r: None),  # not a track's width
}

_NUMBER = r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?"
_QUANTITY = re.compile(rf"(?<![\w.,])({_NUMBER})\s*(mm|mils?|[µμu]m|in)\b")
_SCALE = {"mm": 1.0, "mil": 0.0254, "mils": 0.0254, "µm": 1e-3, "μm": 1e-3, "um": 1e-3, "in": 25.4}
_NET = re.compile(r"\[(.+?)\]")
_ON = re.compile(r"\bon ([A-Za-z0-9_.]+)")
_QUOTED = re.compile(r"\[[^\]]*\]|'[^']*'")
_SPACES = re.compile(r"\s+")


class DrcFailed(RuntimeError):
    """kicad-cli did not produce a report; the message is the end of what it said."""


# kicad-cli: which, how, run

def find_kicad_cli(hint: str = "") -> str:
    """`hint` (the running KiCad's own, from IPC: a KiCad 9 kicad-cli cannot load a 10
    board), KILEIDO_KICAD_CLI, PATH, then installs, newest first; "" when none. The same
    order as the add-on's `kicad_cli.executable`, copied (the two never import each other)."""
    candidates = [hint, os.environ.get("KILEIDO_KICAD_CLI", ""), shutil.which("kicad-cli") or ""]
    candidates += [str(path) for path in _installed()]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.parent.parent.name))


def _installed() -> list[Path]:
    if sys.platform == "win32":
        roots = {Path(os.environ.get(name, "")) for name in ("ProgramFiles", "ProgramW6432")
                 if os.environ.get(name)} or {Path("C:/Program Files")}
        found = [path for root in roots for path in root.glob("KiCad/*/bin/kicad-cli.exe")]
        local = os.environ.get("LOCALAPPDATA")  # a per-user install (KiCad 10's default)
        if local:
            found += Path(local).glob("Programs/KiCad/*/bin/kicad-cli.exe")
        return sorted(found, key=_version_key, reverse=True)
    if sys.platform == "darwin":
        return [Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")]
    return [Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")]


def drc_command(cli: str, board, output) -> list[str]:
    """No zone refill (DRC sees the fills the user sees), no schematic parity (no schematic
    in a run folder), no excluded entries (the user's exclusions stand). --all-track-errors
    makes the report deterministic (without it a track reports one error, counts vary)."""
    return [str(cli), "pcb", "drc", "--format", "json", "--units", "mm", "--all-track-errors",
            "--severity-error", "--severity-warning", "--output", str(output), str(board)]


def run_drc(cli: str, run_dir) -> dict:
    """The DRC report of `run_dir`/board.kicad_pcb (a fresh run folder), written beside it
    as drc.json. kicad-cli exits 0 with violations; anything else raises `DrcFailed`."""
    run_dir = Path(run_dir)
    output = run_dir / REPORT
    try:
        result = subprocess.run(drc_command(cli, run_dir / BOARD, output), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise DrcFailed(f"kicad-cli took longer than {TIMEOUT_S} s") from None
    except OSError as exc:
        raise DrcFailed(f"kicad-cli could not start: {exc}") from None
    if result.returncode != 0 or not output.is_file():
        said = (result.stderr or result.stdout or "").strip()
        raise DrcFailed(said[-300:] or f"kicad-cli exited with code {result.returncode}")
    try:
        report = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DrcFailed(f"DRC report unreadable: {exc}") from None
    if not isinstance(report, dict):
        raise DrcFailed("DRC report is not a JSON object")
    return report


# numbers

def parse_limits(kind: str, description: str) -> tuple[float | None, float | None]:
    """(limit, actual) in mm from a description such as "Clearance violation (clearance
    0,2000 mm; actual 0,1000 mm)", or None for each that is not there. Only types in
    `MEASURED`, only the bracket that holds "actual" (a library name like
    `P2.77x2.84mm` elsewhere is no number); a decimal comma is a point (KiCad prints four
    decimals and no thousands separator); mm, mils, µm and inches."""
    if kind not in MEASURED:
        return None, None
    at = description.rfind("actual")
    if at >= 0:
        start = description.rfind("(", 0, at)
    else:  # KiCad in another UI language: the last bracket still reads "(… <limit>; <word> <actual>)"
        start = description.rfind("(")
        at = description.find(";", start) if start >= 0 else -1
    if start < 0 or at < 0:
        return None, None
    end = description.find(")", at)
    before = [_mm_of(match) for match in _QUANTITY.finditer(description, start, at)]
    after = [_mm_of(match) for match in _QUANTITY.finditer(description, at, end if end >= 0 else len(description))]
    return (before[-1] if before else None), (after[0] if after else None)


def _mm_of(match) -> float:
    return float(match.group(1).replace(",", ".")) * _SCALE[match.group(2)]


def _drill(record) -> float | None:
    if isinstance(record, model.Via):
        return record.drill or None
    if isinstance(record, model.Pad) and record.drill:
        return min(record.drill)
    return None


def _hole(record) -> tuple[Point, float] | None:
    """(centre, radius) of a via's or pad's hole; an oval pad hole counts as its narrow width."""
    drill = _drill(record)
    return (record.pos, drill / 2) if drill else None


def _size_span(kind: str, record) -> tuple[Point, Point, str] | None:
    """The two ends of what a size check measures, along +x from the hole's centre on its
    top copper layer: the drill across, a via across, or a via's ring from the drill wall
    to the copper's edge. None when the item lacks what is measured."""
    hole = _hole(record)
    if kind in ("via_diameter", "annular_width"):
        if not isinstance(record, model.Via) or not record.diameter:
            return None
        (cx, cy), half = record.pos, record.diameter / 2
        if kind == "via_diameter":
            return (cx - half, cy), (cx + half, cy), record.layer_top
        return (None if hole is None else ((cx + hole[1], cy), (cx + half, cy), record.layer_top))
    if kind in ("drill_out_of_range", "microvia_drill_out_of_range") and hole is not None:
        (cx, cy), bore = hole
        layer = record.layer_top if isinstance(record, model.Via) else next(iter(record.layers), "")
        return (cx - bore, cy), (cx + bore, cy), layer
    return None


def _wall(hole: tuple[Point, float], towards: Point) -> Point:
    """The point of a hole's wall facing `towards` (its centre when that is the centre)."""
    (cx, cy), radius = hole
    dx, dy = towards[0] - cx, towards[1] - cy
    length = math.hypot(dx, dy)
    return (cx, cy) if not length else (cx + dx / length * radius, cy + dy / length * radius)


def _hole_span(index: BoardIndex, a, b, to_hole: bool = False) -> tuple[float, Point, Point, str] | None:
    """KiCad's hole metrics, measured from the drill wall: (gap in nm, the hole's wall
    point, the other end, layer). `to_hole`: hole to hole, centre distance less both
    radii (hole_to_hole). Otherwise the gap to the other item's nearest copper, on the
    layer where that copper comes closest (the top one on a tie), the closer way round
    when both have holes (hole_clearance). None when the items lack what is measured."""
    hole_a, hole_b = _hole(a), _hole(b)
    if to_hole:
        if hole_a is None or hole_b is None:
            return None
        shared = [layer for layer in index.layers_of(a) if layer in index.layers_of(b)]
        return (max(0.0, math.hypot(hole_a[0][0] - hole_b[0][0], hole_a[0][1] - hole_b[0][1]) - hole_a[1] - hole_b[1]),
                _wall(hole_a, hole_b[0]), _wall(hole_b, hole_a[0]), shared[0] if shared else "")
    best = None
    for hole, other in ((hole_a, b), (hole_b, a)):
        if hole is None or not has_copper(other):
            continue
        for layer in index.layers_of(other) or (None,):
            shape = index.shape(other, layer)
            if not len(shape.segments):
                continue
            found = point_gap(shape, hole[0])
            gap = max(0.0, found.distance - hole[1])
            if best is None or gap < best[0]:
                best = (gap, _wall(hole, found.b), found.b, layer or "")
    return best


# targets

@dataclass(frozen=True)
class _Item:
    """One report item: its records on the board (none for silkscreen, outline, text...)."""
    uuid: str
    text: str
    pos: Point  # nm
    layer: str  # from the description: canonical for copper and Edge.Cuts, else as written
    records: tuple
    target: str

    @property
    def found(self) -> bool:
        return bool(self.records)

    @property
    def point(self) -> str:
        return _pt(self.pos, self.layer)


def _mm_text(nm: float) -> str:
    text = f"{nm / 1e6:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _pt(point: Point, layer: str = "") -> str:
    return f"pt:{_mm_text(point[0])},{_mm_text(point[1])}" + (f"@{layer}" if layer else "")


def _layer(index: BoardIndex, text: str) -> str:
    """The first layer an item description names ("Via [GND] on F.Cu - B.Cu" -> F.Cu); nets
    and quoted texts are skipped. A renamed copper layer or outline gets its canonical name."""
    text = _QUOTED.sub("", text)
    known = [(canonical, shown) for canonical, shown in index.snapshot.layer_display_names.items()
             if canonical == "Edge.Cuts" or canonical.endswith(".Cu")]
    match = _ON.search(text)
    if match:
        name = match.group(1)
        return next((canonical for canonical, shown in known if shown == name), name)
    # KiCad in another UI language ("auf F.Cu"): any copper or outline layer name the board has
    first = None
    for canonical, shown in {*known, *((layer, layer) for layer in (*index.copper_layers, "Edge.Cuts"))}:
        match = re.search(rf"(?<![\w.]){re.escape(shown)}(?![\w.])", text)
        if match and (first is None or match.start() < first[0]):
            first = (match.start(), canonical)
    return first[1] if first else ""


def _target(index: BoardIndex, uuid: str, records: tuple, layer: str, pos: Point) -> str:
    """A pad as `REF.num` when that names exactly one pad, a part by its reference when no
    other part shares it, else `uuid:<full uuid>` (exact on legacy uuids, whose last 8 hex
    may repeat). Items the index lacks: `edge` on Edge.Cuts, else `pt:x,y@layer`."""
    if records:
        record = records[0]
        if isinstance(record, model.Pad):
            owner = index.footprint_of(record)
            if owner is not None and _unique_reference(index, owner.reference) and \
                    len(index.pads(owner.reference, record.number)) == 1 and \
                    not index.footprints(f"{owner.reference}.{record.number}"):
                return f"{owner.reference}.{record.number}"
        elif isinstance(record, model.Footprint) and _unique_reference(index, record.reference):
            return record.reference
        return f"uuid:{uuid}"
    if layer == "Edge.Cuts":
        return "edge"
    return _pt(pos, layer)


def _unique_reference(index: BoardIndex, reference: str) -> bool:
    return bool(reference) and reference == reference.strip() and ":" not in reference and \
        reference != "edge" and len(index.footprints(reference)) == 1


def _item(index: BoardIndex, raw: dict) -> _Item:
    uuid = str(raw.get("uuid", "")).strip()
    text = str(raw.get("description", ""))
    position = raw.get("pos") if isinstance(raw.get("pos"), dict) else {}
    try:
        pos = (round(float(position.get("x", 0)) * 1e6), round(float(position.get("y", 0)) * 1e6))
    except (TypeError, ValueError):
        pos = (0, 0)
    layer = _layer(index, text)
    records = index.records(uuid) if uuid else ()
    return _Item(uuid, text, pos, layer, records, _target(index, uuid, records, layer, pos))


# measuring

def _closest(index: BoardIndex, a_records, b_records):
    try:
        return index.closest(a_records, b_records)
    except ValueError:
        return None


def _touch_point(index: BoardIndex, a: _Item, b: _Item) -> str | None:
    """Where two items touch or cross, as a `pt:` target on a's layer; None if they are apart."""
    if not (a.found and b.found):
        return None
    found = _closest(index, a.records, b.records)
    if found is None or found[0].distance > 0:
        return None
    return _pt(found[0].a, top_layer(found[1]))


def _open_end(index: BoardIndex, item: _Item) -> str:
    """The end of a dangling track that touches nothing of its net (DRC gives its start)."""
    record = item.records[0] if item.found else None
    if not isinstance(record, (model.Track, model.Arc)):
        return item.point
    others = [other for other in (index.net_items(record.net, record.layer) if record.net
                                  else index.layer_items(record.layer)) if other is not record]
    apart = []
    for end in (record.start, record.end):
        near = index.nearest(end, others, record.layer)
        apart.append(near.gap.distance if near else math.inf)
    if apart[0] == apart[1]:
        return item.point
    return _pt(record.start if apart[0] > apart[1] else record.end, record.layer)


# converting

def _cut(text: str, limit: int = MAX_TEXT) -> str:
    text = _SPACES.sub(" ", text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _short(description: str) -> str:
    """The description without its bracketed numbers: "Clearance violation"."""
    head = description.split("(")[0].strip()
    return _cut(head or description, 80)


def _number(mm: float) -> str:
    return f"{mm:.2f}" if abs(mm) >= 0.1 else f"{mm:.3f}"


def _verdict(what: str, value: float, limit: float | None) -> str:
    """Every measured label reads the same: what, then the value against the limit with
    units on both ("Hole clearance error 0.23 mm < 0.25 mm min"); Blender puts the numbers on line two."""
    if limit is None:
        return f"{what} {_number(value)} mm"
    below = value < limit
    return f"{what} {_number(value)} mm {'<' if below else '>'} {_number(limit)} mm {'min' if below else 'max'}"


def _highlight(items) -> list[dict]:
    targets = list(dict.fromkeys(item.target for item in items if item.found))[:MAX_TARGETS]
    return [{"tool": "highlight", "targets": targets}] if targets else []


def _label(at: str, text: str) -> dict:
    return {"tool": "label", "at": at, "text": _cut(text)}


def _distance(a: str, b: str, label: str) -> dict:
    return {"tool": "distance", "from": a, "to": b, "mode": "edge", "label": label}


def _limited(draw: dict, key: str, limit: float | None) -> dict:
    """`draw` with its limit (mm) under `key`, when DRC gave one."""
    if limit is not None:
        draw[key] = round(limit, 6)
    return draw


def _is_track(item: _Item) -> bool:
    return item.found and isinstance(item.records[0], (model.Track, model.Arc))


def _is_via(item: _Item) -> bool:
    return item.found and isinstance(item.records[0], model.Via)


def _gap_mm(index: BoardIndex, a_records, b_records) -> float | None:
    found = _closest(index, a_records, b_records)
    return round(found[0].distance / 1e6, 6) if found else None


def _draws(index: BoardIndex, kind: str, items: list[_Item], description: str, count: int):
    """(draws, values) for one deduplicated entry, per the type table."""
    a = items[0] if items else None
    b = items[1] if len(items) > 1 else None
    limit, actual = parse_limits(kind, description)
    values = {}
    if limit is not None:
        values["limit_mm"] = round(limit, 6)
    if a is None:
        return [], values
    short = _short(description)
    if kind == "clearance" and b is not None:
        claimed = actual if actual is not None else \
            (_gap_mm(index, a.records, b.records) if a.found and b.found else None)
        if claimed is not None:
            values["distance_mm"] = round(claimed, 6)
        return _highlight(items) + [_limited({"tool": "clearance", "a": a.target, "b": b.target},
                                             "required", limit)], values
    if kind == "copper_edge_clearance":
        copper = next((item for item in items if item.target != "edge"), a)
        claimed = actual if actual is not None else \
            (_gap_mm(index, copper.records, (index.snapshot.outline,)) if copper.found else None)
        if claimed is not None:
            values["distance_mm"] = round(claimed, 6)
        return _highlight([copper]) + [_limited({"tool": "edge_gap", "target": copper.target}, "min_mm", limit)], values
    if kind in ("hole_clearance", "hole_to_hole") and b is not None:
        spans = [_hole_span(index, ra, rb, kind == "hole_to_hole") for ra in a.records for rb in b.records] \
            if a.found and b.found else []  # a zone is one record per layer: the closest layer counts
        span = min((s for s in spans if s is not None), key=lambda s: s[0], default=None)
        gap = span[0] / 1e6 if span is not None else actual
        what = HOLE_LABELS[kind]
        if gap is not None:
            values["hole_gap_mm"] = round(gap, 6)
        if span is not None:  # a dimension from the drill wall to what is too close (Blender measures it again;
            # a hole gap is always a minimum: measured at or over the limit, it reads as within it)
            dimension = {"tool": "distance", "from": _pt(span[1], span[3]), "to": _pt(span[2], span[3]),
                         "mode": "centre", "label": what, "limit_is": "min"}
            return _highlight(items) + [_limited(dimension, "limit", limit)], values
        label = what if gap is None else _verdict(what, gap, limit)  # unmeasured: an arrow between the items
        return _highlight(items) + [{"tool": "arrow", "from": a.target, "to": b.target, "label": label}], values
    if kind in ("shorting_items", "tracks_crossing") and b is not None:
        nets = _nets(items)
        text = "Tracks cross" if kind == "tracks_crossing" else \
            "Short: " + (" / ".join(nets) if nets else "two nets")
        return _highlight(items) + [_label(_touch_point(index, a, b) or a.target, text)], values
    if kind == "unconnected_items" and b is not None:
        return _highlight(items) + [_distance(a.target, b.target, "Not connected")], values
    if kind == "track_dangling" and not _is_via(a):
        return _highlight([a]) + [_label(_open_end(index, a), "Unconnected end")], values
    if kind in ("via_dangling", "track_dangling"):  # a via
        return _highlight([a]) + [_label(a.point, "Unconnected via")], values
    if kind == "isolated_copper":
        text = "Isolated copper" + (f" ({count} islands)" if count > 1 else "")
        return _highlight([a]) + [_label(a.point, text)], values
    if kind in SIZES:
        key, word, size_of = SIZES[kind]
        size = next((size_of(record) for record in a.records if size_of(record) is not None), None)
        size = size / 1e6 if size is not None else actual
        if size is None:
            return _highlight([a]) + [_label(a.target if a.found else a.point, short)], values
        values[key] = round(size, 6)
        span = _size_span(kind, a.records[0]) if a.found else None
        if span is not None:  # a dimension across what is measured: Blender draws it with end ticks, on the cut face
            (x0, y0), (x1, y1), layer = span
            dimension = {"tool": "distance", "from": _pt((x0, y0), layer), "to": _pt((x1, y1), layer), "mode": "centre",
                         "label": word, "limit_is": "min" if limit is None or size < limit else "max"}
            return _highlight([a]) + [_limited(dimension, "limit", limit)], values
        label = _label(a.target if a.found else a.point, _verdict(word, size, limit))
        if kind in ("track_width", "connection_width") and _is_track(a):
            width = _limited({"tool": "width", "target": a.target}, "required", limit)
            # track width: the width draw says it all; a connection's width is not the track's
            return _highlight([a]) + [width] + ([label] if kind == "connection_width" else []), values
        return _highlight([a]) + [label], values
    if kind in SILK:
        silk = next((item for item in items if not item.found and item.target != "edge"), a)
        return _highlight(items) + [_label(silk.point, short)], values
    if kind == "starved_thermal":
        pad = next((item for item in items if item.found and isinstance(item.records[0], model.Pad)), a)
        return _highlight(items) + [_label(pad.target if pad.found else pad.point, short)], values
    if kind in COURTYARD:
        parts = [item for item in items if item.found and isinstance(item.records[0], model.Footprint)]
        if len(parts) >= 2:
            (x0, y0), (x1, y1) = centre(parts[0].records[0]), centre(parts[1].records[0])
            side = "B" if parts[0].records[0].side == "bottom" else "F"
            at = _pt(((x0 + x1) // 2, (y0 + y1) // 2), f"{side}.Courtyard")
        else:
            at = parts[0].target if parts else a.point
        return _highlight(items) + [_label(at, short)], values
    if kind == "invalid_outline":
        return [{"tool": "highlight", "targets": ["edge"]}, _label(_pt(a.pos, "Edge.Cuts"), short)], values
    if kind in FOOTPRINT:
        return _highlight(items) + [_label(a.target if a.found else a.point, short)], values
    return _highlight(items) + [_label(a.point, short)], values  # anything else


def _nets(items) -> list[str]:
    found = (match for item in items for match in _NET.findall(item.text))
    return list(dict.fromkeys(net for net in found if net != "<no net>"))


def _finding(index: BoardIndex, entry: dict, count: int) -> dict:
    kind = str(entry.get("type", "")) or "unknown"
    description = str(entry.get("description", ""))
    items = [_item(index, raw) for raw in entry.get("items", ()) if isinstance(raw, dict)]
    severity = str(entry.get("severity", ""))
    nets = _nets(items)
    title = TITLES.get(kind, kind.replace("_", " ").capitalize()) + (" " + " / ".join(nets) if nets else "")
    repeats = f" (reported {count} times)" if count > 1 else ""
    draws, values = _draws(index, kind, items, description, count)
    return {"check": f"drc.{kind}", "severity": severity if severity in ("error", "warning") else "info",
            "title": _cut(title), "message": _cut(description, MAX_TEXT - len(repeats)) + repeats,
            "values": values, "draw": draws}


def _entries(report: dict):
    for key in ENTRY_LISTS:
        entries = report.get(key)
        if isinstance(entries, list):
            yield from (entry for entry in entries if isinstance(entry, dict) and not entry.get("excluded"))


def dedupe(report: dict) -> tuple[list[tuple[dict, int]], int]:
    """([(entry, times reported)], library notices dropped). One entry per (type, sorted
    item uuids): KiCad repeats a pair per segment, per layer, per island."""
    kept: dict[tuple, list] = {}
    dropped = 0
    for entry in _entries(report):
        kind = str(entry.get("type", ""))
        if kind in DROPPED:
            dropped += 1
            continue
        uuids = tuple(sorted(str(item.get("uuid", "")) for item in entry.get("items", ()) if isinstance(item, dict)))
        if (kind, uuids) in kept:
            kept[(kind, uuids)][1] += 1
        else:
            kept[(kind, uuids)] = [entry, 1]
    return [(entry, count) for entry, count in kept.values()], dropped


def convert(report: dict, index: BoardIndex, run: str, stamp: str) -> dict:
    """The `kls-findings 1` file of a DRC report, errors first. Pure: no files, no KiCad."""
    entries, dropped = dedupe(report)
    findings = [_finding(index, entry, count) for entry, count in entries]
    findings.sort(key=lambda finding: SEVERITY_ORDER[finding["severity"]])
    version = str(report.get("kicad_version", "")).strip()
    tool = f"KiCad DRC {version}".strip()
    if dropped:
        tool += f"; {dropped} library notice{'s' if dropped != 1 else ''} left out"
    return {"format": FORMAT, "run": run, "stamp": stamp, "tool": _cut(tool), "findings": findings}
