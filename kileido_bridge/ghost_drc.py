"""KiCad's own DRC on the LLM's suggestions (ghosts), on top of the bridge's quick fit check.

`collect` takes the ghosts a findings payload drew (`findings.resolve_file`: resolved on
the live board, nm, KiCad's frame). `build` writes ghosts into a copy of KiCad's raw
board text, each item with a fresh uuid; `copies` decides which go together: every part
move in a copy of its own (a part one suggestion moves still stands for the others), all
other ghosts in one shared copy:

- a via: through (F.Cu to B.Cu), its drill as the board's vias of that size have it
  (else from the board's usual annular ring, else half its size); a row: every via of it;
- a route: a segment between each two points of its path, on its layer, at its width;
- a pad: a footprint of one square pad on its outer layer (mask opened), turned as drawn;
- a cap: a footprint of its body as a courtyard only, so KiCad checks where it lands
  against other parts' courtyards (its pads are not known; the quick check keeps its say);
- a part moved: its footprint's position and angle changed in place. The file keeps pad
  and text angles absolute (a part at 90 degrees writes its pads at 90 too), so each turns
  by the same amount; zones the footprint owns are kept in board coordinates, so their
  points move with it.

`run_drc` runs kicad-cli's DRC on the copy, zones refilled (a stitching via joins its pour,
a pour makes room for another net's via), in a run folder of its own (`RUN_KIND`, pruned
apart from the runs an answer echoes). `judge` maps every report entry that names a
ghost's uuid back to that ghost:

- a rule that says a suggestion does not fit (`FIT`: clearance, hole clearance, holes,
  board edge, short, crossing tracks, keepout, mask bridge, courtyard, sizes): KiCad's own
  rule and numbers, in plain words;
- a ghost KiCad names on another net than the one written was joined to the copper it
  touches (KiCad renets a via or track that touches a net's copper, and never reports
  that short): "shorts to NET";
- `NOTES`: a via that joins nothing of its net with the zones refilled, a route end that
  reaches nothing of its net. Said, but they do not make a suggestion not fit: a stitching
  via in a pour is not reported at all, and one that joins nothing is shown as such;
- unconnected items (a net's other unrouted links, the tracks a moved part leaves behind)
  and anything else are left out, as are entries between suggestions of different
  findings (each is judged against the board, not against another finding's idea).
  Entries that name no ghost are the board's own: the DRC tab reports those.

`apply` puts the outcome on the payload: each ghost draw's `kicad_drc` gets "state"
("pending" | "done" | "failed" | "skipped"), "problems" and "notes" (plain text lines) and
"error"; with KiCad's verdict in, `fits` and `problem` are KiCad's (its word wins over the
quick check's, which a note keeps), else the quick check's. A touch the quick check saw
stays a short whatever KiCad says (it cannot report it), and reasons KiCad does not judge
(`kicad_drc["kept"]`: not inside the board, outside a layer change's reach) stand, with
the row's vias they name (`kept_row`). Everything taken from the findings file is untrusted: net
names go into the copy only when they cannot break out of their quotes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field

from . import drc_findings
from .findings import _CONTROL, CAP_GHOST_NM, GHOST_VIA_NM
from .hatch import layer_names

RUN_KIND = "ghost_drc"  # kls_folder.SIDE_RUNS
KINDS = ("via", "pad", "cap", "track", "via_row", "part")
COVERED = frozenset({"via", "pad", "track", "via_row", "part"})  # KiCad judges what the quick check does
OUTER = ("F.Cu", "B.Cu")
MIN_DRILL_NM = 100_000
MAX_NET = 200
MAX_LINES = 6  # problem lines per ghost; the rest counted
MAX_ITEM_TEXT = 80
NOT_CHECKED = "not in the board copy KiCad checked"
NO_CLI = "kicad-cli not found; set KILEIDO_KICAD_CLI to it"
MAX_MOVE_COPIES = 8  # part moves KiCad checks, each in a copy of its own (`copies`)
TOO_MANY_MOVES = f"KiCad checks the first {MAX_MOVE_COPIES} moved parts of a file, each on its own"
FIT = frozenset({
    "clearance", "hole_clearance", "hole_to_hole", "holes_co_located", "copper_edge_clearance", "shorting_items",
    "tracks_crossing", "items_not_allowed", "solder_mask_bridge", "courtyards_overlap", "pth_inside_courtyard",
    "npth_inside_courtyard", "track_width", "via_diameter", "annular_width", "drill_out_of_range",
    "microvia_drill_out_of_range", "creepage"})
NOTES = frozenset({"via_dangling", "track_dangling"})
DRILLED = frozenset({"annular_width", "drill_out_of_range", "microvia_drill_out_of_range", "hole_clearance",
                     "hole_to_hole", "holes_co_located"})  # what a via's (assumed) drill decides
WORDS = {"clearance": "clearance", "hole_clearance": "hole clearance", "hole_to_hole": "hole to hole",
         "holes_co_located": "hole on another hole", "copper_edge_clearance": "board edge clearance",
         "shorting_items": "short", "tracks_crossing": "crosses", "items_not_allowed": "in a keepout area",
         "solder_mask_bridge": "solder mask bridge", "courtyards_overlap": "courtyard overlaps",
         "pth_inside_courtyard": "plated hole in the courtyard", "npth_inside_courtyard": "hole in the courtyard",
         "track_width": "track width", "via_diameter": "via diameter", "annular_width": "annular ring",
         "drill_out_of_range": "drill", "microvia_drill_out_of_range": "microvia drill", "creepage": "creepage"}
NAMED = frozenset({"courtyards_overlap", "tracks_crossing"})  # "<word> <the other item>"

_DECLARED = re.compile(r'\n\t\(net (\d+) "((?:[^"\\]|\\.)*)"\)')  # KiCad 9 and older: numbered nets
_VIA = re.compile(r"\n\t\(via\b(.*?)\n\t\)", re.S)
_SIZE = re.compile(r"\((size|drill) ([\d.]+)\)")
_FOOTPRINT = re.compile(r"\n\t\(footprint\s")
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')
_AT = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?((?:\s+unlocked)?)\)")
_XY = re.compile(r"\(xy (-?[\d.]+) (-?[\d.]+)\)")
_ZONE = re.compile(r"\(zone\b")
_UUID = re.compile(r'\((?:uuid|tstamp) "?([0-9A-Fa-f-]{8,})"?\)')
_REFERENCE = re.compile(r'\(property\s+"Reference"\s+"((?:[^"\\]|\\.)*)"'
                        r'|\(fp_text\s+reference\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
_BRACKET = re.compile(r"\[(.+?)\](?=[\s,]|$)")
_ESCAPE = re.compile(r"\\(.)")
_SPACES = re.compile(r"\s+")


class _Skip(Exception):
    """A ghost KiCad's DRC cannot take; the reason is said in its `kicad_drc`."""


@dataclass(frozen=True)
class Ghost:
    """One suggestion as drawn: its finding's and draw's places in the payload, its kind
    and net, and its geometry in nm: `spots` (a via's or pad's centre, a route's path, a
    row's vias, a moved part's new anchor), `layer`, `size`, a route's `width`, `rotation`
    (a moved part's new angle), a moved part's `part` (reference, else id)."""
    finding: int
    draw: int
    kind: str
    net: str
    spots: tuple
    layer: str = ""
    size: int = 0
    width: int = 0
    rotation: float = 0.0
    part: str = ""


@dataclass
class Placement:
    """What `build` wrote: each uuid's ghost (its index) and member (a row's via, else 0),
    the net written per ghost, a via's drill (nm, whether the board has vias of that
    size), and why a ghost is not in the copy."""
    ghosts: list
    members: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    drills: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """KiCad's DRC on one ghost: "done" (problems, notes, the nets it shorts to, the
    members with a problem) or "skipped" (`error`: why it was not in the copy)."""
    state: str
    problems: tuple = ()
    notes: tuple = ()
    error: str = ""
    shorts: frozenset = frozenset()
    bad: frozenset = frozenset()


@dataclass(frozen=True)
class Outcome:
    """A ghost DRC run: a verdict per (finding, draw), or `error` when the run failed."""
    verdicts: dict = field(default_factory=dict)
    error: str = ""


# the ghosts of a payload

def collect(payload: dict) -> list[Ghost]:
    """Every ghost draw of a source payload that resolved on the board, in payload order."""
    ghosts = []
    for fi, finding in enumerate(payload.get("findings") or ()):
        for di, draw in enumerate(finding.get("draws") or ()):
            if draw.get("tool") == "ghost" and draw.get("ok") and draw.get("kind") in KINDS:
                ghost = _ghost(fi, di, draw)
                if ghost is not None:
                    ghosts.append(ghost)
    return ghosts


def _ghost(fi: int, di: int, draw: dict) -> Ghost | None:
    kind = draw["kind"]
    points = draw.get("points") or []
    spots = draw.get("path_nm") if kind == "track" else draw.get("row") if kind == "via_row" else points[:1]
    spots = tuple((int(spot[0]), int(spot[1])) for spot in spots or () if len(spot) >= 2)
    if len(spots) < (2 if kind == "track" else 1):
        return None
    layer = draw.get("layer") or (points[0][2] if points and len(points[0]) > 2 else "")
    size = int(draw.get("size_nm") or 0) or (CAP_GHOST_NM if kind == "cap" else GHOST_VIA_NM)
    return Ghost(fi, di, kind, str(draw.get("net") or ""), spots, str(layer or ""), size,
                 int(draw.get("width_nm") or 0), float(draw.get("rotation_deg") or 0.0), str(draw.get("part") or ""))


def digest(ghosts: list[Ghost]) -> str:
    """What a run is keyed on besides the board: the ghosts as they would be written."""
    data = [[g.finding, g.draw, g.kind, g.net, g.spots, g.layer, g.size, g.width, round(g.rotation, 6), g.part]
            for g in ghosts]
    return hashlib.sha1(json.dumps(data).encode("utf-8")).hexdigest()


# writing the copy

@dataclass
class _Board:
    layers: frozenset
    declared: dict  # net name -> number (KiCad 9 and older); {} for KiCad 10's named nets
    vias: dict  # (size, drill) nm -> how many
    added: list = field(default_factory=list)  # net declarations written

    @property
    def numbered(self) -> bool:
        return bool(self.declared)


def _board(text: str) -> _Board:
    declared = {_ESCAPE.sub(r"\1", name): int(number) for number, name in _DECLARED.findall(text)}
    vias: dict[tuple[int, int], int] = {}
    for body in _VIA.findall(text):
        sizes = dict(_SIZE.findall(body))
        if "size" in sizes and "drill" in sizes:
            key = (round(float(sizes["size"]) * 1e6), round(float(sizes["drill"]) * 1e6))
            vias[key] = vias.get(key, 0) + 1
    return _Board(frozenset(layer_names(text).values()), declared, vias)


def _mm(nm: float) -> str:
    text = f"{nm / 1e6:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _angle(degrees: float) -> str:
    """" 90" for a turned item, "" for none (KiCad leaves 0 out)."""
    degrees = degrees % 360
    if math.isclose(degrees, 0.0, abs_tol=1e-9) or math.isclose(degrees, 360.0, abs_tol=1e-9):
        return ""
    return " " + (f"{degrees:.6f}".rstrip("0").rstrip("."))


def _shown(text: str) -> str:
    return _CONTROL.sub(" ", text)[:40]


def _net_node(board: _Board, net: str, pad: bool = False) -> str:
    """The `(net ...)` of an item ("" for none): KiCad 10 names the net, older files number
    it (a pad also names it), declaring a net the board lacks. A name that could break out
    of its quotes is refused."""
    if not net:
        return ""
    if len(net) > MAX_NET or '"' in net or "\\" in net or _CONTROL.search(net):
        raise _Skip(f"net {_shown(net)!r} cannot be written into a board file")
    if not board.numbered:
        return f'(net "{net}")'
    number = board.declared.get(net)
    if number is None:
        number = max(board.declared.values()) + 1
        board.declared[net] = number
        board.added.append(f'\n\t(net {number} "{net}")')
    return f'(net {number} "{net}")' if pad else f"(net {number})"


def _drill(board: _Board, size: int) -> tuple[int, bool]:
    """A suggested via's drill: as the board's vias of its size most often have it, else its
    size less the board's usual via ring, else half its size (never under 0.1 mm)."""
    same = {drill: count for (diameter, drill), count in board.vias.items() if diameter == size}
    if same:
        return max(same, key=lambda drill: (same[drill], -drill)), True
    if board.vias:
        diameter, drill = max(board.vias, key=lambda key: (board.vias[key], key))
        if size - (diameter - drill) >= MIN_DRILL_NM:
            return size - (diameter - drill), False
    return max(MIN_DRILL_NM, int(round(size / 2, -4))), False


def _layer(board: _Board, layer: str, outer: bool = False) -> str:
    if outer and layer not in OUTER:
        raise _Skip(f"KiCad has pads and parts on the outer layers only, not on {_shown(layer)}")
    if layer not in board.layers:
        raise _Skip(f"layer {_shown(layer)} is not on this board")
    return layer


def _uid(placement: Placement, gi: int, member: int = 0) -> str:
    made = str(uuid.uuid4())
    placement.members[made] = (gi, member)
    return made


def _via_text(placement, gi, member, at, size, drill, net) -> str:
    uid = _uid(placement, gi, member)
    net = f"\n\t\t{net}" if net else ""
    return (f"\t(via\n\t\t(at {_mm(at[0])} {_mm(at[1])})\n\t\t(size {_mm(size)})\n\t\t(drill {_mm(drill)})\n"
            f'\t\t(layers "F.Cu" "B.Cu"){net}\n\t\t(uuid "{uid}")\n\t)\n')


def _items(board: _Board, placement: Placement, gi: int, ghost: Ghost) -> str:
    """The text of a ghost that is added to the board (all but a moved part)."""
    if ghost.kind in ("via", "via_row"):
        net = _net_node(board, ghost.net)
        drill, same = _drill(board, ghost.size)
        placement.nets[gi], placement.drills[gi] = ghost.net, (drill, same)
        return "".join(_via_text(placement, gi, member, at, ghost.size, drill, net)
                       for member, at in enumerate(ghost.spots))
    if ghost.kind == "track":
        layer = _layer(board, ghost.layer)
        net = _net_node(board, ghost.net)
        if ghost.width <= 0:
            raise _Skip("the route has no width")
        placement.nets[gi] = ghost.net
        lines = [(a, b) for a, b in zip(ghost.spots, ghost.spots[1:]) if a != b]
        if not lines:
            raise _Skip("the route has no length")
        net = f"\t\t{net}\n" if net else ""
        return "".join(f"\t(segment\n\t\t(start {_mm(a[0])} {_mm(a[1])})\n\t\t(end {_mm(b[0])} {_mm(b[1])})\n"
                       f'\t\t(width {_mm(ghost.width)})\n\t\t(layer "{layer}")\n{net}'
                       f'\t\t(uuid "{_uid(placement, gi)}")\n\t)\n' for a, b in lines)
    side = _layer(board, ghost.layer if ghost.layer in OUTER or ghost.kind == "pad" else "F.Cu", outer=True)
    at, turn = ghost.spots[0], _angle(ghost.rotation)
    head = (f'\t(footprint "KLS:ghost_{ghost.kind}"\n\t\t(layer "{side}")\n\t\t(uuid "{_uid(placement, gi)}")\n'
            f"\t\t(at {_mm(at[0])} {_mm(at[1])}{turn})\n\t\t(attr smd)\n")
    if ghost.kind == "pad":
        net = _net_node(board, ghost.net, pad=True)
        placement.nets[gi] = ghost.net
        mask = side[0] + ".Mask"
        layers = f'"{side}"' + (f' "{mask}"' if mask in board.layers else "")
        net = f"\t\t\t{net}\n" if net else ""
        size = _mm(ghost.size)
        return (head + f'\t\t(pad "1" smd rect\n\t\t\t(at 0 0{turn})\n\t\t\t(size {size} {size})\n'
                f"\t\t\t(layers {layers})\n{net}\t\t\t(uuid \"{_uid(placement, gi)}\")\n\t\t)\n\t)\n")
    courtyard = side[0] + ".CrtYd"
    if courtyard not in board.layers:
        raise _Skip(f"the board has no {courtyard} layer")
    length, width = ghost.size, ghost.size / 2
    return (head + f"\t\t(fp_rect\n\t\t\t(start {_mm(-length / 2)} {_mm(-width / 2)})\n"
            f"\t\t\t(end {_mm(length / 2)} {_mm(width / 2)})\n\t\t\t(stroke\n\t\t\t\t(width 0.05)\n"
            f'\t\t\t\t(type default)\n\t\t\t)\n\t\t\t(fill no)\n\t\t\t(layer "{courtyard}")\n'
            f'\t\t\t(uuid "{_uid(placement, gi)}")\n\t\t)\n\t)\n')


def _node_end(text: str, start: int) -> int:
    """Just past the ")" that closes the node opening at `start`; quoted text skipped."""
    depth = 0
    for match in _TOKEN.finditer(text, start):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 0:
                return match.end()
    raise ValueError("the board text ends inside a footprint")


def _footprints(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, reference, uuid) of every footprint on the board."""
    found = []
    for match in _FOOTPRINT.finditer(text):
        start = match.start() + 2
        end = _node_end(text, start)
        body = text[start:end]
        reference = _REFERENCE.search(body)
        name = next((group for group in reference.groups() if group is not None), "") if reference else ""
        own = _UUID.search(body)
        found.append((start, end, _ESCAPE.sub(r"\1", name), own.group(1) if own else ""))
    return found


def _turner(origin, to, degrees: float):
    """Board points (nm) moved as a part turned `degrees` about `origin` and set at `to`
    (KiCad's sense, seen from the top: `findings._turner`)."""
    a = math.radians(degrees)
    cos, sin = math.cos(a), math.sin(a)

    def move(x, y):
        dx, dy = x - origin[0], y - origin[1]
        return to[0] + dx * cos + dy * sin, to[1] - dx * sin + dy * cos
    return move


def _moved(body: str, ghost: Ghost) -> str:
    """A footprint's text moved to the ghost's anchor and angle: its own `(at)` set, every
    other `(at)` in it (pads, texts: absolute angles) turned by the same amount, the points
    of zones it owns (board coordinates) moved with it."""
    first = _AT.search(body)
    if first is None:
        raise _Skip("the part has no position in the board text")
    origin = (float(first.group(1)) * 1e6, float(first.group(2)) * 1e6)
    turn = ghost.rotation - float(first.group(3) or 0.0)
    at = ghost.spots[0]
    head = body[:first.start()] + f"(at {_mm(at[0])} {_mm(at[1])}{_angle(ghost.rotation)})"
    rest = body[first.end():]
    if not math.isclose(turn % 360, 0.0, abs_tol=1e-9):
        rest = _AT.sub(lambda m: f"(at {m.group(1)} {m.group(2)}{_angle(float(m.group(3) or 0.0) + turn)}"
                                 f"{m.group(4)})", rest)
    move = _turner(origin, at, turn)
    pieces, last = [], 0
    for zone in _ZONE.finditer(rest):
        if zone.start() < last:
            continue
        end = _node_end(rest, zone.start())
        pieces += [rest[last:zone.start()], _XY.sub(
            lambda m: "(xy {} {})".format(*map(_mm, move(float(m.group(1)) * 1e6, float(m.group(2)) * 1e6))),
            rest[zone.start():end])]
        last = end
    return head + "".join(pieces) + rest[last:]


def build(text: str, ghosts: list[Ghost]) -> tuple[str, Placement]:
    """KiCad's board text with every ghost written in (moved parts changed in place, the
    rest added at the end), and what was written. A ghost that cannot be written is left
    out with the reason (`Placement.skipped`); the copy never holds part of one."""
    end = text.rstrip().rfind(")")
    if end < 0:
        raise ValueError("not a KiCad board")
    board = _board(text)
    placement = Placement(list(ghosts))
    edits, added = [], []
    footprints = _footprints(text) if any(ghost.kind == "part" for ghost in ghosts) else []
    moved: dict[int, int] = {}  # footprint start -> the ghost that moves it
    for gi, ghost in enumerate(ghosts):
        try:
            if ghost.kind != "part":
                kept = (dict(placement.nets), dict(placement.drills), dict(board.declared), list(board.added))
                try:
                    added.append(_items(board, placement, gi, ghost))
                except _Skip:  # nothing of it stays
                    for made in [uid for uid, (index, _) in placement.members.items() if index == gi]:
                        del placement.members[made]
                    placement.nets, placement.drills, board.declared, board.added = kept
                    raise
                continue
            matches = [span for span in footprints if span[2] == ghost.part] or \
                [span for span in footprints if span[3] == ghost.part]
            if not matches:
                raise _Skip(f"part {_shown(ghost.part)} is not in the board text")
            if len(matches) > 1:
                raise _Skip(f"{len(matches)} footprints are named {_shown(ghost.part)}")
            start, stop, _, _ = matches[0]
            if start in moved:
                raise _Skip(f"another suggestion in this file moves {_shown(ghost.part)}; KiCad checks the first")
            body = _moved(text[start:stop], ghost)
            moved[start] = gi
            for uid in _UUID.findall(body):
                placement.members[uid] = (gi, 0)
            edits.append((start, stop, body))
        except _Skip as why:
            placement.skipped[gi] = str(why)
    if board.added:
        last = list(_DECLARED.finditer(text))[-1].end()
        edits.append((last, last, "".join(board.added)))
    edits.append((end, end, "".join(added)))
    out, at = [], len(text)
    for start, stop, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        out += [text[stop:at], replacement]
        at = start
    out.append(text[:at])
    return "".join(reversed(out)), placement


# reading KiCad's report

def _entries(report: dict):
    for key in ("violations", "unconnected_items"):
        entries = report.get(key)
        if isinstance(entries, list):
            yield from (entry for entry in entries if isinstance(entry, dict) and not entry.get("excluded"))


def _item_text(item: dict) -> str:
    """KiCad's words for an item, short and plain: "Track [SIG_A] on F.Cu"."""
    text = _SPACES.sub(" ", _CONTROL.sub(" ", str(item.get("description", "")))).strip()
    text = text.split(", ")[0]
    return text if len(text) <= MAX_ITEM_TEXT else text[:MAX_ITEM_TEXT - 1] + "…"


def _net_shown(description: str, written: str) -> str | None:
    """The net KiCad names an item on, when it is not `written`; None when it is (or none shows)."""
    if f"[{written}]" in description:
        return None
    match = _BRACKET.search(description)
    return _CONTROL.sub(" ", match.group(1))[:MAX_NET] if match else None


def _num(mm: float) -> str:
    return f"{mm:.2f}" if abs(mm) >= 0.1 else f"{mm:.3f}"


def _line(kind: str, description: str, other: dict | None) -> tuple[str, str]:
    """(plain text of one entry against a ghost, the net it shorts to or "")."""
    word = WORDS.get(kind, kind.replace("_", " "))
    named = _item_text(other) if other else ""
    if kind == "shorting_items":
        match = _BRACKET.search(str(other.get("description", ""))) if other else None
        net = _CONTROL.sub(" ", match.group(1))[:MAX_NET] if match else ""
        return (f"shorts to {net}" if net else f"short with {named}" if named else "short"), net
    if kind in NAMED and named:
        return f"{word} {named}", ""
    limit, actual = drc_findings.parse_limits(kind, description)
    text = f"{word} {_num(limit)} mm needed, {_num(actual)} mm" if limit is not None and actual is not None else word
    return (f"{text} ({named})" if named else text), ""


def judge(report: dict, placement: Placement) -> Outcome:
    """KiCad's report on the copy, per ghost (the module's docstring says what counts)."""
    ghosts = placement.ghosts
    seen = {gi: {"problems": [], "notes": set(), "shorts": set(), "renet": [], "bad": set(), "drilled": False}
            for gi in range(len(ghosts)) if gi not in placement.skipped}
    for entry in _entries(report):
        kind = str(entry.get("type", ""))
        description = str(entry.get("description", ""))
        items = [item for item in entry.get("items", ()) if isinstance(item, dict)]
        mine = [(placement.members[str(item.get("uuid", ""))], item) for item in items
                if str(item.get("uuid", "")) in placement.members]
        if not mine:
            continue
        for (gi, member), item in mine:
            ghost = ghosts[gi]
            if ghost.kind in ("via", "via_row", "track") and ghost.net:
                shown = _net_shown(str(item.get("description", "")), ghost.net)
                if shown and (member, shown) not in seen[gi]["renet"]:
                    seen[gi]["renet"].append((member, shown))
        if len(mine) == len(items) and len({ghosts[gi].finding for (gi, _), _ in mine}) > 1:
            continue  # between suggestions of different findings
        for (gi, member), item in mine:
            found = seen[gi]
            if kind in FIT:
                other = next((each for each in items if each is not item), None)
                text, net = _line(kind, description, other)
                found["problems"].append((member, text))
                found["bad"].add(member)
                found["drilled"] |= kind in DRILLED
                if net:
                    found["shorts"].add(net)
            elif kind in NOTES:
                found["notes"].add((member, kind))
    verdicts = {}
    for gi, ghost in enumerate(ghosts):
        key = (ghost.finding, ghost.draw)
        if gi in placement.skipped:
            verdicts[key] = Verdict("skipped", error=placement.skipped[gi])
            continue
        verdicts[key] = _verdict(ghost, seen[gi], placement.drills.get(gi))
    return Outcome(verdicts)


def _vias(members: list[int]) -> str:
    """"via 3", "vias 1, 2, 5", "vias 1, 2, 3, 4, 5 and 7 more" (counted from 1)."""
    names = [str(member + 1) for member in sorted(members)]
    if len(names) == 1:
        return f"via {names[0]}"
    return "vias " + ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def _verdict(ghost: Ghost, found: dict, drill) -> Verdict:
    row = ghost.kind == "via_row" and len(ghost.spots) > 1
    texts: dict[str, list[int]] = {}  # each text once, with the members it is about
    for member, net in found["renet"]:
        texts.setdefault(f"shorts to {net} (KiCad joins it to that net)", []).append(member)
    for member, text in found["problems"]:
        members = texts.setdefault(text, [])
        if member not in members:
            members.append(member)
    lines = [f"{_vias(members)}: {text}" if row else text for text, members in texts.items()]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [f"… and {len(lines) - MAX_LINES + 1} more"]
    renet = {member for member, _ in found["renet"]}
    net = f"{ghost.net} copper" if ghost.net else "other copper"
    notes = []
    dangling = sorted(member for member, kind in found["notes"] if kind == "via_dangling" and member not in renet)
    if dangling and row:
        notes.append(f"{len(dangling)} of {len(ghost.spots)} vias join no {net}, with the zones refilled")
    elif dangling:
        notes.append(f"joins no {net}, with the zones refilled")
    if any(kind == "track_dangling" for _, kind in found["notes"]) and not renet:
        notes.append(f"an end reaches no {net}")
    if found["drilled"] and drill is not None:
        notes.append(f"with a {_num(drill[0] / 1e6)} mm drill, " +
                     ("as the board's vias of this size" if drill[1] else "assumed (the board has no such via)"))
    return Verdict("done", tuple(lines), tuple(notes), "", frozenset(found["shorts"] | {n for _, n in found["renet"]}),
                   frozenset(found["bad"] | renet))


# running

def run_drc(cli: str, run_dir) -> dict:
    """kicad-cli's DRC on a ghost run's copy, the zones refilled first."""
    return drc_findings.run_drc(cli, run_dir, refill_zones=True)


def copies(ghosts: list[Ghost]) -> list[list[Ghost]]:
    """The ghosts as KiCad checks them: every part move in a copy of its own, so a part
    one suggestion moves still stands where it is for the others; all other ghosts share
    one copy. Past MAX_MOVE_COPIES moves, the rest are not checked by KiCad."""
    moves = [ghost for ghost in ghosts if ghost.kind == "part"]
    shared = [ghost for ghost in ghosts if ghost.kind != "part"]
    return ([shared] if shared else []) + [[move] for move in moves[:MAX_MOVE_COPIES]]


def check(folder, text: str, ghosts: list[Ghost], cli: str, run=run_drc, lock=None) -> Outcome:
    """Write the ghosts into copies of `text` (`copies`), freeze each as a run of `folder`
    (under `lock`, which numbers runs), run KiCad's DRC on each in turn and judge the
    reports. Raises when kicad-cli fails (`drc_findings.DrcFailed`) or a run cannot be
    written."""
    verdicts = {}
    for group in copies(ghosts):
        copy, placement = build(text, group)
        if not placement.members:  # none KiCad could take: nothing to run
            verdicts.update(judge({}, placement).verdicts)
            continue
        with lock if lock is not None else nullcontext():
            made = folder.new_run(copy, RUN_KIND)
        verdicts.update(judge(run(cli, made.directory), placement).verdicts)
    for ghost in [ghost for ghost in ghosts if ghost.kind == "part"][MAX_MOVE_COPIES:]:
        verdicts[(ghost.finding, ghost.draw)] = Verdict("skipped", error=TOO_MANY_MOVES)
    return Outcome(verdicts)


# the payload

def apply(payload: dict, outcome: Outcome | None) -> dict:
    """The payload with KiCad's DRC on its ghosts: `outcome` None while it runs
    ("pending"), with `error` when it failed. Published dicts are never changed in place:
    rows and draws that change are new, the rest stay the same objects."""
    rows, changed = [], False
    for fi, row in enumerate(payload.get("findings") or ()):
        draws = row.get("draws") or []
        new = [_judged(draw, outcome, (fi, di)) for di, draw in enumerate(draws)]
        if any(a is not b for a, b in zip(new, draws)):
            row, changed = {**row, "draws": new}, True
        rows.append(row)
    return {**payload, "findings": rows} if changed else payload


def _judged(draw: dict, outcome: Outcome | None, key: tuple[int, int]) -> dict:
    kd = draw.get("kicad_drc")
    if draw.get("tool") != "ghost" or not isinstance(kd, dict) or not draw.get("ok"):
        return draw
    quick_fits, quick_problem, kept = kd["quick_fits"], kd["quick_problem"], list(kd["kept"])
    row = [list(spot) for spot in draw.get("row") or ()]
    for spot, fits in zip(row, kd.get("quick_row") or ()):
        if len(spot) > 2:
            spot[2] = fits
    fits = False if kept else quick_fits
    problem = "; ".join(filter(None, (quick_problem, *kept)))
    state, problems, notes, error = "pending", [], [], ""
    if outcome is not None and outcome.error:
        state, error = "failed", outcome.error
    elif outcome is not None:
        verdict = outcome.verdicts.get(key) or Verdict("skipped", error=NOT_CHECKED)
        state, error = verdict.state, verdict.error
        if state == "done":
            many = draw.get("kind") == "via_row" and len(row) > 1
            touched = [(member, net) for member, net in draw.get("touches") or () if net not in verdict.shorts]
            problems = list(verdict.problems) + [
                (f"via {member + 1}: " if many else "") + f"touches {net} copper: a short" for member, net in touched]
            notes = list(verdict.notes)
            reasons = list(kept)
            if draw.get("kind") not in COVERED and quick_fits is False:
                reasons.insert(0, quick_problem)  # what KiCad does not check of it (a cap's pads)
            elif quick_fits is False and not problems:
                notes.append(f"the quick check said: {quick_problem}; KiCad's rules allow it")
            fits = not problems and not reasons
            problem = "; ".join(filter(None, ("KiCad DRC: " + "; ".join(problems) if problems else "", *reasons)))
            bad = set(verdict.bad) | {member for member, _ in touched} | set(kd.get("kept_row") or ())
            if draw.get("kind") in COVERED:
                for member, spot in enumerate(row):
                    if len(spot) > 2:
                        spot[2] = member not in bad
    judged = {**kd, "state": state, "problems": problems, "notes": notes, "error": error}
    if judged == kd and fits == draw["fits"] and problem == draw["problem"] and row == draw.get("row"):
        return draw
    return {**draw, "kicad_drc": judged, "fits": fits, "problem": problem, "row": row}
