"""Flex mode's model of a rigid-flex board: its flex zones, bends and the flat regions between.

Read from drawings on user layers named Flex, Bend and Stiffener (any user layers, found
by name) and from the stackup's flexible part (`board_specs.flex_stack`). Coordinates are
KiCad's integer nanometres, y down.

- A flex zone is a closed shape (rectangle, polygon, circle) on the Flex layer. A pure flex
  board (every copper layer in the flex stack) needs none: all of it is flex.
- A bend is a straight line on the Bend layer across a flex zone, edge to edge. A text
  beside it may give its angle and radius, such as "90° R1.5": degrees, positive folding
  towards the top (F.Cu) side, and the inside radius in mm; "#2" puts it in step 2 of the
  folding sequence. What the text leaves out is assumed: 90°, and the smallest radius the
  flex takes bent once (`default_radius_nm`). The text is read forgivingly ("R2 90",
  "-90 deg r2mm", "angle 45 radius 3").
- A bend can instead be drawn as its area: a closed shape on the Bend layer whose two
  sides across the flex are where it starts and stops curving. Parallel sides make a bend
  whose radius follows from their distance and its angle; sides that converge make a cone
  (a tapered bend), its tip where they would meet, its radius growing away from the tip.
  Its text gives only the angle ("90°", "#2").
- A twist is a straight line on the Bend layer along a flex tail, with a text saying
  "twist" ("twist 90°"; 90° without a number): the tail turns about that line, evenly
  along its length, and what lies past its far end turns with it. Its chord is square to
  the line through its middle, across the tail; its strip is the line's length.
- A wrap closes a part of the board into a cone (or a cylinder's piece of one) whose two
  ends meet: a wedge on the Bend layer, its tip where the cone's tip goes (the centre of a
  ring sector), its two straight sides from the tip where the ends close, with a text
  saying "wrap" ("wrap 360°"; 360° without a number). Its sides may run along or past the
  board's edges. The cone's half angle follows from the wedge: sin = wedge / wrap angle.
  A positive angle wraps towards the top, so F.Cu ends inside; "wrap -360°" puts it
  outside. Board past the second side runs on round, under the start. The wrap does not
  split the board: it bends the region it lies on, and what hangs off that region by
  other bends follows the cone where they join it.
- A dome is a line (an arc, mostly) on the Bend layer across a row of fingers, with a
  text saying "dome" and a radius or angle ("dome R25", "dome 30°", "#2"): each finger it
  crosses curls from it towards its tip, square to its length, at that radius (or by that
  angle at its tip). It curls the same way as a wrap it hangs off (inwards; a negative
  angle curls it outwards), else towards the top. All its fingers fold as one.
- A stiffener is a closed shape on the Stiffener layer with a text inside or beside it
  such as "Polyimide 0.2 mm bottom": its material, thickness and side; without them FR4,
  0.2 mm, bottom.
- A text on the Flex layer saying "Coverlay black" (or white, amber) colours the coverlay;
  amber without one.

What was assumed is reported as a note (`Problem.level`), so it is never silent.

Each bend's chord across the board splits the outline; the pieces are the flat regions,
joined by the bends in a tree rooted at the largest piece. Problems say where they are. A
bend that cannot fold (not straight, outside the flex, crossing another) is left out.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .model import BoardSnapshot, Drawing, Point, Ring

LAYER_NAMES = frozenset({"flex", "bend", "stiffener"})  # casefolded
SHORT_NM = 100_000  # a bend line may stop this short of the board edge
STRAIGHT_NM = 10_000  # how far a bend line's points may stray from straight
LABEL_REACH_NM = 5_000_000  # a text further than this from a bend or stiffener labels neither
SIDE_STEP_NM = 50_000
AREA_EDGE_NM = 500_000  # a bend area's side across the flex: its middle at least this far inside the board
PARALLEL_DEG = 1.0  # a bend area's sides closer than this to parallel make a bend, not a cone  # off a bend's chord, to find the regions either side

COVERLAY_NM = 50_000  # per side: 25 um polyimide film and 25 um adhesive, the usual coverlay
# Minimum bend radius over flex thickness by flex copper layers (IPC-2223's usual figures):
# static (bent once, at assembly) and dynamic (flexing in use; not for 3 layers or more).
STATIC_RATIO = {1: 6, 2: 10}
STATIC_RATIO_MULTI = 20
DYNAMIC_RATIO = {1: 100, 2: 150}
DEFAULT_ANGLE = 90.0
DEFAULT_STIFFENER = ("FR4", 200_000, "bottom")
COVERLAYS = {"amber": "AMBER", "yellow": "AMBER", "orange": "AMBER", "black": "BLACK", "white": "WHITE"}

_NUMBER = r"([+-]?\d+(?:[.,]\d+)?)"
_ANGLE = re.compile(rf"{_NUMBER}\s*(?:°|º|deg(?:rees?)?)", re.I)
_ANGLE_WORD = re.compile(rf"\bangle\s*[:=]?\s*{_NUMBER}", re.I)
_RADIUS = re.compile(rf"(?:(?<![A-Za-z])R|\bradius)\s*[:=]?\s*{_NUMBER}\s*(mm|µm|um)?", re.I)
_TWIST = re.compile(r"\btwist", re.I)
_WRAP = re.compile(r"\b(?:wrap|closed)", re.I)
_DOME = re.compile(r"\bdome", re.I)
DEFAULT_WRAP = 360.0
DEFAULT_DOME = 90.0  # at the tips, for a dome text with neither radius nor angle
_LONE = re.compile(rf"(?<![\w.,#]){_NUMBER}(?![\w.,])")
_THICKNESS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mm|µm|um)\b", re.I)
_BARE_THICKNESS = re.compile(r"(?<![\w.,#])(\d*[.,]\d+|\d+)(?![\w.,])")
_SIDE = re.compile(r"\b(top|bottom)\b", re.I)
_STEP = re.compile(r"#\s*(\d+)")


@dataclass(frozen=True)
class Problem:
    message: str
    where: Point | None = None
    level: str = "error"  # "note": something assumed, not wrong


@dataclass(frozen=True)
class Finger:
    """One finger of a dome: where it curls from (its chord, square to the finger), how far
    and at what radius, and which side of the chord its tip is on (as `_beside`)."""
    start: Point
    end: Point
    angle_deg: float = 0.0
    radius_nm: int = 0
    length_nm: int = 0  # from the chord to the far end of its tip
    side: int = 0


@dataclass(frozen=True)
class Bend:
    id: str  # the Bend layer line's KiCad UUID
    start: Point  # the chord: where the bend line crosses the board's edges
    end: Point
    angle_deg: float
    radius_nm: int
    step: int = 1  # in the folding sequence: bends with the same step fold together
    assumed: tuple[str, ...] = ()  # "angle", "radius": what its text left out
    kind: str = "bend"  # or "twist": the tail turns about its drawn line; or "cone", "dome"
    length_nm: int = 0  # a twist's: its line's length, its strip along the tail
    pivot: Point | None = None  # a twist's: the middle of its line, on the axis it turns about
    area: Ring | None = None  # a bend drawn as its area (a bend or a cone): the shape drawn
    sides: tuple | None = None  # its two sides across the flex, ((x0, y0), (x1, y1)) each
    apex: Point | None = None  # a cone's tip: where its sides meet
    alpha: float = 0.0  # a cone's: the angle between its sides, radians
    psi: float = 0.0  # a cone's: how far it turns about its axis at its angle, radians (`cone_turn`)
    radius_max_nm: int = 0  # a cone's inside radius at its wide end (`radius_nm`: at its narrow end)
    closed: bool = False  # a cone drawn as a wrap: no chord, it bends the region it lies on
    fingers: tuple[Finger, ...] = ()  # a dome's

    @property
    def name(self) -> str:
        if self.closed:
            return "Wrap"
        return {"twist": "Twist", "cone": "Cone", "dome": "Dome"}.get(self.kind, "Bend")

    @property
    def cuts(self) -> tuple[tuple[Point, Point], ...]:
        """The chords it splits the board along: a dome's fingers', a wrap's none."""
        if self.closed:
            return ()
        if self.kind == "dome":
            return tuple((finger.start, finger.end) for finger in self.fingers)
        return ((self.start, self.end),)


@dataclass(frozen=True)
class Region:
    ring: Ring
    parent: int | None  # the region it folds from; None for the root (the largest piece)
    bend: int | None  # the bend joining it to its parent, an index into `FlexModel.bends`


@dataclass(frozen=True)
class Stiffener:
    id: str
    ring: Ring
    material: str
    thickness_nm: int | None
    side: str  # "top" or "bottom"
    assumed: tuple[str, ...] = ()  # "material", "thickness", "side": what its text left out


@dataclass(frozen=True)
class FlexModel:
    layers: tuple[str, ...]  # copper layers that continue into the flex, top first
    thickness_nm: int  # of the flexible part, coverlay not included
    zones: tuple[Ring, ...]
    bends: tuple[Bend, ...]
    regions: tuple[Region, ...]
    stiffeners: tuple[Stiffener, ...]
    problems: tuple[Problem, ...]
    coverlay: str = "AMBER"  # "AMBER", "BLACK" or "WHITE"

    def region_at(self, point: Point) -> int | None:
        return next((index for index, region in enumerate(self.regions) if _inside(point, region.ring)), None)


def _value(text: str) -> float:
    return float(text.replace(",", "."))


def parse_bend(text: str) -> tuple[float | None, int | None] | None:
    """(degrees, inside radius in nm), either None when the text does not give it; None when
    it gives neither. Forgiving: "90° R1.5", "-90 deg r2mm", "R2 90", "angle 45 radius 3".
    A lone number is the angle only beside a radius ("Bend 2" is no angle)."""
    rest = _STEP.sub(" ", text)
    radius = None
    if match := _RADIUS.search(rest):
        unit = (match.group(2) or "mm").casefold()
        radius = round(abs(_value(match.group(1))) * (1e6 if unit == "mm" else 1e3))
        rest = rest[:match.start()] + " " + rest[match.end():]
    found = _ANGLE.search(rest) or _ANGLE_WORD.search(rest) or (_LONE.search(rest) if radius is not None else None)
    angle = _value(found.group(1)) if found else None
    return None if angle is None and radius is None else (angle, radius)


def parse_twist(text: str) -> float | None:
    """A twist's degrees from "twist 90°", "Twist -45", "twist 180 deg #2"; None without one."""
    rest = _STEP.sub(" ", _TWIST.sub(" ", text))
    found = _ANGLE.search(rest) or _ANGLE_WORD.search(rest) or _LONE.search(rest)
    return _value(found.group(1)) if found else None


def cone_turn(alpha: float, fold: float) -> tuple[float, float]:
    """A cone that folds by `fold` (radians, the angle between the flat parts either side)
    from a flat wedge of angle `alpha`: (psi, beta), how far it turns about its axis and its
    half angle at the tip. The wedge keeps its angle on the cone (psi * sin(beta) = alpha),
    so flat is psi = alpha (beta 90 degrees: the axis upright) and the cone narrows as it
    folds; the fold is cos(fold) = cos(psi) + sin(beta)^2 * (1 - cos(psi))."""
    fold = min(abs(fold), math.pi - 1e-6)

    def folded(psi):
        sine = alpha / psi
        return math.acos(max(-1.0, min(1.0, math.cos(psi) + sine * sine * (1 - math.cos(psi)))))

    low, high = alpha, max(alpha, 2 * math.pi)
    for _ in range(80):  # bisection: the fold grows with psi from alpha
        middle = (low + high) / 2
        low, high = (middle, high) if folded(middle) < fold else (low, middle)
    psi = (low + high) / 2
    return psi, math.asin(min(1.0, alpha / psi))


def default_radius_nm(thickness_nm: int, copper_layers: int) -> int:
    """The smallest inside radius a flex of `thickness_nm` (coverlay included) takes bent
    once, rounded up to 0.1 mm: what a bend without a radius is assumed to have."""
    ratio = STATIC_RATIO.get(copper_layers, STATIC_RATIO_MULTI)
    return max(100_000, math.ceil(thickness_nm * ratio / 100_000) * 100_000)


def bend_note(angle_deg: float, radius_nm: int, step: int | None = None, kind: str = "bend",
              area: bool = False, closed: bool = False) -> str:
    """A bend's text as KiLeidoscope writes it: "90° R1.9", "twist 90°", "#2" with a step; a
    bend drawn as its area (and a cone) only its angle: its shape gives the radius."""
    if kind == "twist":
        said = f"twist {angle_deg:g}°"
    elif closed:
        said = f"wrap {angle_deg:g}°"
    elif kind == "dome":
        said = f"dome R{radius_nm / 1e6:g}" if radius_nm else f"dome {angle_deg:g}°"
    elif area or kind == "cone":
        said = f"{angle_deg:g}°"
    else:
        said = f"{angle_deg:g}° R{radius_nm / 1e6:g}"
    return said + (f" #{step}" if step else "")


def parse_step(text: str) -> int:
    """The fold's step in the sequence from "#2" in its text; 1 without one."""
    match = _STEP.search(text)
    return int(match.group(1)) if match else 1


def parse_stiffener(text: str) -> tuple[str, int | None, str | None]:
    """(material, thickness in nm or None, "top"/"bottom" or None) from "Polyimide 0.2 mm
    bottom", "SUS 0.3mm", "steel 0.3 top" (a number without a unit is mm)."""
    thickness, rest = None, text
    if match := _THICKNESS.search(rest):
        thickness = round(_value(match.group(1)) * (1e6 if match.group(2).lower() == "mm" else 1e3))
        rest = rest[:match.start()] + " " + rest[match.end():]
    elif (match := _BARE_THICKNESS.search(rest)) and 0 < _value(match.group(1)) <= 5:
        thickness = round(_value(match.group(1)) * 1e6)
        rest = rest[:match.start()] + " " + rest[match.end():]
    side = _SIDE.search(rest)
    material = " ".join(_SIDE.sub(" ", rest).replace(",", " ").split())
    return material, thickness, side.group(1).lower() if side else None


def stiffener_note(material: str, thickness_nm: int, side: str) -> str:
    return f"{material} {thickness_nm / 1e6:g} mm {side}"


def parse_coverlay(texts: list[str]) -> tuple[str, str | None]:
    """The coverlay's colour from the Flex layer's texts ("Coverlay black"): (colour, the
    text when it names no colour KiLeidoscope knows, else None)."""
    for text in texts:
        if "coverlay" not in text.casefold():
            continue
        for word, colour in COVERLAYS.items():
            if word in text.casefold():
                return colour, None
        return "AMBER", text
    return "AMBER", None


def build(snapshot: BoardSnapshot, stack: dict) -> FlexModel | None:
    """The flex model, or None for a board with no flex at all (no Polyimide, no drawings)."""
    found: dict[str, list[Drawing]] = {name: [] for name in LAYER_NAMES}
    for drawing in snapshot.drawings:
        name = snapshot.layer_display_names.get(drawing.layer, "").casefold()
        if name in found:
            found[name].append(drawing)
    if not stack and not any(found.values()):
        return None
    problems: list[Problem] = []
    if not stack:
        problems.append(Problem("The stackup has no Polyimide dielectric, so no layers continue into the flex"))
    zones = []
    for drawing in found["flex"]:
        if drawing.kind == "closed":
            zones.append(drawing.points)
        elif drawing.kind == "line":
            problems.append(Problem("A flex zone must be a closed shape (a rectangle or polygon)", drawing.points[0]))
    coverlay, unknown = parse_coverlay([drawing.text for drawing in found["flex"] if drawing.kind == "text"])
    if unknown is not None:
        problems.append(Problem(f'"{unknown}" names no coverlay colour KiLeidoscope knows (amber, black, white): '
                                f'shown as amber', level="note"))
    rings = [ring for polygon in snapshot.outline.polygons for ring in polygon[:1]]
    copper = {entry.name for entry in snapshot.stackup.layers if entry.type == "copper"}
    if not zones and stack and copper and copper <= set(stack.get("layers", ())):
        # Pure flex (every copper layer in the flex stack): the whole board is flex.
        zones = [ring for ring in rings if sum(_inside(ring[0], other) for other in rings if other is not ring) % 2 == 0]
    layers = tuple(stack.get("layers", ()))
    thickness = int(stack.get("thickness_nm", 0))
    radius = default_radius_nm(thickness + 2 * COVERLAY_NM, len(layers)) if layers else 1_000_000
    bends = _bends(found["bend"], zones, rings, problems, radius, thickness)
    regions = _regions(rings, bends)
    bends = [_dome_fingers(bend, index, bends, regions, problems, thickness) if bend.kind == "dome" else bend
             for index, bend in enumerate(bends)]
    stiffeners = _stiffeners(found["stiffener"], problems)
    return FlexModel(layers, thickness, tuple(zones), tuple(bends), tuple(regions), tuple(stiffeners),
                     tuple(problems), coverlay)


# --- Bends ----------------------------------------------------------------------------------

def _bends(drawings: list[Drawing], zones: list[Ring], rings: list[Ring], problems: list[Problem],
           default_radius: int, thickness_nm: int) -> list[Bend]:
    lines = [drawing for drawing in drawings if drawing.kind == "line"]
    texts = [drawing for drawing in drawings if drawing.kind == "text"]
    areas = [drawing for drawing in drawings if drawing.kind == "closed"]

    def distance(text, target):
        if target.kind == "closed":
            if _DOME.search(text.text):
                return math.inf  # a dome is a line
            return 0 if _inside(text.points[0], target.points) else min(
                _segment_distance(text.points[0], p, q) for p, q in _edges(target.points))
        if _WRAP.search(text.text):
            return math.inf  # a wrap is an area
        return min(_segment_distance(text.points[0], p, q) for p, q in zip(target.points, target.points[1:]))
    labels = _labels(texts, lines + areas, distance)
    for index, text in enumerate(texts):
        if index not in labels.values() and parse_bend(text.text) is not None:
            problems.append(Problem(f'"{text.text}" is not beside a bend line', text.points[0]))
    bends: list[Bend] = []
    for area_index, area in enumerate(areas):
        label = texts[labels[len(lines) + area_index]] if len(lines) + area_index in labels else None
        if label is not None and _WRAP.search(label.text):
            found = _wrap(area, label, rings, problems, thickness_nm)
            if found is not None:
                bends.append(found)
            continue
        found = _area_bend(area, label, zones, rings, problems, thickness_nm)
        if found is not None:
            if any(_segments_cross((found.start, found.end), cut) for bend in bends for cut in bend.cuts):
                problems.append(Problem("Two bends cross; this one is left flat", area.points[0]))
                continue
            bends.append(found)
            if found.assumed:
                problems.append(Problem(f"{found.name} {len(bends)}: angle not in its text, so 90° is assumed. "
                                        f"Copy its text to keep it, or write your own", area.points[0],
                                        level="note"))
    for line_index, line in enumerate(lines):
        start, end = line.points[0], line.points[-1]
        middle = _rounded(_lerp(start, end, 0.5))
        label = texts[labels[line_index]] if line_index in labels else None
        if label is not None and _DOME.search(label.text):
            found = _dome(line, label, zones, rings, problems)
            if found is not None:
                bends.append(found)
            continue
        if start == end or any(_segment_distance(p, start, end) > STRAIGHT_NM for p in line.points):
            problems.append(Problem("A bend must be one straight line", middle))
            continue
        label = texts[labels[line_index]] if line_index in labels else None
        twist = label is not None and _TWIST.search(label.text) is not None
        if twist:
            angle, radius = parse_twist(label.text), 0
            assumed = () if angle is not None else ("angle",)
            length = math.dist(start, end)
            across = (-(end[1] - start[1]) / length, (end[0] - start[0]) / length)  # square to the twist line
            far = 1e10  # 10 m: past any board, so the chord is wherever the board's edges are
            chord = _chord(rings, zones, (middle[0] - across[0] * far, middle[1] - across[1] * far),
                           (middle[0] + across[0] * far, middle[1] + across[1] * far), problems)
            if chord is not None and not any(all(_inside(_lerp(start, end, f / 10), zone) for f in range(11))
                                             for zone in zones):
                problems.append(Problem("A twist must lie inside a flex zone; this one reaches rigid board", middle))
                continue
        else:
            angle, radius = (parse_bend(label.text) if label else None) or (None, None)
            assumed = tuple(part for part, value in (("angle", angle), ("radius", radius)) if value is None)
            chord = _chord(rings, zones, start, end, problems)
        angle = DEFAULT_ANGLE if angle is None else angle
        radius = default_radius if radius is None else radius
        if chord is None:
            continue
        if not any(all(_inside(_lerp(*chord, f / 10), zone) for f in range(1, 10)) for zone in zones):
            problems.append(Problem("A bend must lie inside a flex zone; this one crosses rigid board", middle))
            continue
        crossed = next((bend for bend in bends for cut in bend.cuts if _segments_cross(chord, cut)), None)
        if crossed is not None:
            problems.append(Problem("Two bends cross; this one is left flat", middle))
            continue
        step = parse_step(label.text) if label else 1
        bends.append(Bend(line.id, *chord, angle, radius, step, assumed, "twist" if twist else "bend",
                          round(math.dist(start, end)) if twist else 0, middle if twist else None))
        if assumed:
            said = f' "{label.text}"' if label else ""
            note = bend_note(angle, radius, kind=bends[-1].kind)
            problems.append(Problem(f"{bends[-1].name} {len(bends)}: {' and '.join(assumed)} not in its text{said}, "
                                    f'so {note} is assumed. Copy its text to keep it, or write your own',
                                    middle, level="note"))
    return bends


def _area_bend(area: Drawing, label: Drawing | None, zones: list[Ring], rings: list[Ring],
               problems: list[Problem], thickness_nm: int) -> Bend | None:
    """A bend drawn as its area: a bend between parallel sides, else a cone (see the module)."""
    where = area.points[0]
    sides = []
    for p, q in _edges(area.points):
        middle = _lerp(p, q, 0.5)
        clear = min(_segment_distance(middle, a, b) for ring in rings for a, b in _edges(ring))
        if sum(_inside(middle, ring) for ring in rings) % 2 == 1 and clear >= AREA_EDGE_NM:
            sides.append((p, q))
    if len(sides) != 2:
        problems.append(Problem("A bend area needs exactly two sides across the flex (where it starts and "
                                "stops curving); draw it as a four-sided shape", where))
        return None
    on_board = [point for side in sides for f in range(11) if sum(
        _inside(point := _lerp(*side, f / 10), ring) for ring in rings) % 2 == 1]  # drawn past the edges is fine
    if not any(all(_inside(p, zone) or min(_segment_distance(p, a, b) for a, b in _edges(zone)) < AREA_EDGE_NM
                   for p in on_board) for zone in zones):
        problems.append(Problem("A bend must lie inside a flex zone; this one crosses rigid board", where))
        return None
    text = label.text if label else ""
    angle = (parse_bend(text) or (None, None))[0] if label else None
    assumed = () if angle is not None else ("angle",)
    angle = DEFAULT_ANGLE if angle is None else angle
    step = parse_step(text) if label else 1
    (a0, a1), (b0, b1) = sides
    da, db = (a1[0] - a0[0], a1[1] - a0[1]), (b1[0] - b0[0], b1[1] - b0[1])
    cross = da[0] * db[1] - da[1] * db[0]
    turn = math.radians(abs(angle))
    far = 1e10
    if abs(cross) <= math.sin(math.radians(PARALLEL_DEG)) * math.hypot(*da) * math.hypot(*db):
        # Parallel: a bend, its chord half way between the sides, its radius from their distance.
        if da[0] * db[0] + da[1] * db[1] < 0:
            b0, b1 = b1, b0
        middle0, middle1 = _lerp(a0, b0, 0.5), _lerp(a1, b1, 0.5)
        width = abs((b0[0] - a0[0]) * da[1] - (b0[1] - a0[1]) * da[0]) / math.hypot(*da)
        direction = (middle1[0] - middle0[0], middle1[1] - middle0[1])
        length = math.hypot(*direction)
        centre = _lerp(middle0, middle1, 0.5)
        chord = _chord(rings, zones, (centre[0] - direction[0] / length * far, centre[1] - direction[1] / length * far),
                       (centre[0] + direction[0] / length * far, centre[1] + direction[1] / length * far), problems)
        if chord is None:
            return None
        radius = round(max(width / max(turn, 1e-9) - (thickness_nm + 2 * COVERLAY_NM) / 2, 0))
        return Bend(area.id, *chord, angle, radius, step, assumed, "bend", 0, None, area.points)
    # Converging: a cone, its tip where the sides' lines meet.
    t = ((b0[0] - a0[0]) * db[1] - (b0[1] - a0[1]) * db[0]) / cross
    apex = (a0[0] + t * da[0], a0[1] + t * da[1])
    rays = []
    for p, q in sides:
        middle = _lerp(p, q, 0.5)
        span = math.dist(apex, middle)
        rays.append(((middle[0] - apex[0]) / span, (middle[1] - apex[1]) / span))
    alpha = math.acos(max(-1.0, min(1.0, rays[0][0] * rays[1][0] + rays[0][1] * rays[1][1])))
    bisector = (rays[0][0] + rays[1][0], rays[0][1] + rays[1][1])
    size = math.hypot(*bisector)
    centre = _lerp(_lerp(*sides[0], 0.5), _lerp(*sides[1], 0.5), 0.5)
    # The chord that splits the board: the middle generator, along the bisector, across the flex.
    along = (bisector[0] / size, bisector[1] / size)
    chord = _chord(rings, zones, (centre[0] - along[0] * far, centre[1] - along[1] * far),
                   (centre[0] + along[0] * far, centre[1] + along[1] * far), problems)
    if chord is None:
        return None
    psi, beta = cone_turn(alpha, turn)
    reach = [math.dist(apex, p) for side in sides for p in side]
    half = (thickness_nm + 2 * COVERLAY_NM) / 2
    narrow, wide = (round(max(min(reach) * math.tan(beta) - half, 0)), round(max(reach) * math.tan(beta) - half))
    return Bend(area.id, *chord, angle, narrow, step, assumed, "cone", 0, None, area.points, tuple(sides),
                _rounded(apex), alpha, psi, wide)


def wrap_beta(alpha: float, psi: float) -> float:
    """A wrap's half angle at the tip: the wedge (alpha) closes round psi."""
    return math.asin(min(1.0, alpha / psi)) if psi > 0 else math.pi / 2


def _wrap(area: Drawing, label: Drawing, rings: list[Ring], problems: list[Problem],
          thickness_nm: int) -> Bend | None:
    """A wrap (see the module): the wedge's tip is the vertex between its two longest sides."""
    ring = area.points
    count = len(ring)
    where = ring[0]
    if count < 3:
        return None
    lengths = [math.dist(ring[k], ring[(k + 1) % count]) for k in range(count)]
    tip_index = max(range(count), key=lambda k: min(lengths[k - 1], lengths[k]))
    tip = ring[tip_index]
    first, second = ring[tip_index - 1], ring[(tip_index + 1) % count]
    a = (first[0] - tip[0], first[1] - tip[1])
    b = (second[0] - tip[0], second[1] - tip[1])
    opening = math.atan2(abs(a[0] * b[1] - a[1] * b[0]), a[0] * b[0] + a[1] * b[1])
    turn = (tip[0] - first[0]) * (second[1] - tip[1]) - (tip[1] - first[1]) * (second[0] - tip[0])
    convex = turn * _area(ring) > 0
    alpha = opening if convex else 2 * math.pi - opening
    # Order the sides so the wedge sweeps from the first to the second as x turns to y.
    middle = (a[0] / math.hypot(*a) + b[0] / math.hypot(*b), a[1] / math.hypot(*a) + b[1] / math.hypot(*b))
    if not convex:
        middle = (-middle[0], -middle[1])
    if a[0] * middle[1] - a[1] * middle[0] < 0:
        first, second = second, first
    angle = parse_twist(_WRAP.sub(" ", label.text))
    angle = DEFAULT_WRAP if angle is None else angle
    psi = math.radians(abs(angle))
    if psi < alpha - 1e-6:
        problems.append(Problem(f"A wrap of {abs(angle):g}° cannot close a {math.degrees(alpha):.0f}° wedge", where))
        return None
    beta = wrap_beta(alpha, psi)
    reach = [_segment_distance(tip, p, q) for board in rings for p, q in _edges(board)]
    far = max(math.dist(tip, p) for board in rings for p in board)
    half = (thickness_nm + 2 * COVERLAY_NM) / 2
    narrow, wide = (round(max(min(reach) * math.tan(beta) - half, 0)), round(max(far * math.tan(beta) - half, 0)))
    return Bend(area.id, tip, first, angle, narrow, parse_step(label.text), (), "cone", 0, None, ring,
                ((tip, first), (tip, second)), tip, alpha, psi, wide, closed=True)


def _dome(line: Drawing, label: Drawing, zones: list[Ring], rings: list[Ring],
          problems: list[Problem]) -> Bend | None:
    """A dome (see the module): a finger wherever its line runs over the board, its chord
    square to the finger through the middle of where the line crosses it."""
    angle, radius = parse_bend(_DOME.sub(" ", label.text)) or (None, None)
    fingers = []
    path = list(line.points)
    hits = []  # (distance along the line, point, edge direction)
    walked = 0.0
    for p, q in zip(path, path[1:]):
        direction = (q[0] - p[0], q[1] - p[1])
        span = math.hypot(*direction)
        if span == 0:
            continue
        for board in rings:
            for t, edge, point in _crossings(board, p, direction):
                if 0 <= t < 1:
                    a, b = board[edge], board[(edge + 1) % len(board)]
                    hits.append((walked + t * span, point, (b[0] - a[0], b[1] - a[1])))
        walked += span
    hits.sort(key=lambda hit: hit[0])
    inside = sum(_inside(path[0], board) for board in rings) % 2 == 1
    spans = list(zip(hits[1::2], hits[2::2])) if inside else list(zip(hits[::2], hits[1::2]))
    for (_, p, edge_p), (_, q, edge_q) in spans:
        if math.dist(p, q) < SHORT_NM:
            continue
        if edge_p[0] * edge_q[0] + edge_p[1] * edge_q[1] < 0:
            edge_q = (-edge_q[0], -edge_q[1])
        along = (edge_p[0] / math.hypot(*edge_p) + edge_q[0] / math.hypot(*edge_q),
                 edge_p[1] / math.hypot(*edge_p) + edge_q[1] / math.hypot(*edge_q))
        size = math.hypot(*along)
        across = (-along[1] / size, along[0] / size)
        middle = _lerp(p, q, 0.5)
        reach = 3 * math.dist(p, q)
        chord = _chord(rings, zones, (middle[0] - across[0] * reach, middle[1] - across[1] * reach),
                       (middle[0] + across[0] * reach, middle[1] + across[1] * reach), problems)
        if chord is not None:
            fingers.append(Finger(*chord))
    if not fingers:
        problems.append(Problem("A dome line must cross the board's fingers", line.points[0]))
        return None
    assumed = () if angle is not None or radius is not None else ("angle",)  # the text's: 0 when it gives none
    return Bend(line.id, line.points[0], line.points[-1], DEFAULT_DOME if assumed else (angle or 0.0),
                radius or 0, parse_step(label.text), assumed, "dome", fingers=tuple(fingers))


def _dome_fingers(dome: Bend, index: int, bends: list[Bend], regions: list[Region], problems: list[Problem],
                  thickness_nm: int) -> Bend:
    """A dome with each finger's side, length (chord to tip), angle and radius, curling the
    way the wrap it hangs off curls (see the module)."""
    half = (thickness_nm + 2 * COVERLAY_NM) / 2
    text_radius = dome.radius_nm or None
    text_angle = dome.angle_deg if dome.angle_deg or text_radius is None else None
    wraps = [bend for bend in bends if bend.closed]
    inward = math.copysign(1.0, wraps[0].angle_deg) if wraps else 1.0
    if text_angle is not None and text_angle < 0:
        inward = -inward
    fingers = []
    for finger in dome.fingers:
        probe = Bend("", finger.start, finger.end, 0.0, 0)
        at = {s: next((k for k, region in enumerate(regions) if _inside(_beside(probe, s), region.ring)), None)
              for s in (1, -1)}
        side = next((s for s in (1, -1) if at[s] is not None and regions[at[s]].bend == index
                     and regions[at[s]].parent == at[-s]), 0)
        if not side:
            problems.append(Problem("A dome finger does not split off a tip",
                                    _rounded(_lerp(finger.start, finger.end, 0.5))))
            continue
        child = regions[at[side]].ring
        dx, dy = finger.end[0] - finger.start[0], finger.end[1] - finger.start[1]
        size = math.hypot(dx, dy)
        normal = (-dy / size * side, dx / size * side)
        length = max((p[0] - finger.start[0]) * normal[0] + (p[1] - finger.start[1]) * normal[1] for p in child)
        if text_radius is not None and text_angle is None:
            radius, turn = text_radius, length / (text_radius + half)
        elif text_radius is None:
            turn = math.radians(abs(text_angle))
            radius = round(max(length / turn - half, 0))
        else:
            radius, turn = text_radius, math.radians(abs(text_angle))
        fingers.append(Finger(finger.start, finger.end, inward * math.degrees(turn), radius, round(length), side))
    if not fingers:
        return dome
    widest = max(fingers, key=lambda finger: abs(finger.angle_deg))
    if dome.assumed:
        problems.append(Problem(f"Dome {index + 1}: neither radius nor angle in its text, so {DEFAULT_DOME:g}° at "
                                f"the tips is assumed. Copy its text to keep it, or write your own",
                                dome.start, level="note"))
    return Bend(dome.id, dome.start, dome.end, widest.angle_deg, min(finger.radius_nm for finger in fingers),
                dome.step, dome.assumed, "dome", fingers=tuple(fingers))


def _chord(rings: list[Ring], zones: list[Ring], start: Point, end: Point,
           problems: list[Problem]) -> tuple[Point, Point] | None:
    """Where the bend line's own line crosses the board's edges either side of its middle."""
    direction = (end[0] - start[0], end[1] - start[1])
    length = math.hypot(*direction)
    crossings = sorted(t for ring in rings for t, _, _ in _crossings(ring, start, direction))
    spans = list(zip(crossings[::2], crossings[1::2]))  # along the line, on the board
    around = next((k for k, (a, b) in enumerate(spans) if a <= 0.5 <= b), None)
    if around is None:  # its middle is off the board: in a cutout, or beside the board
        on_board = any(a < 1 and b > 0 for a, b in spans)
        problems.append(Problem("A bend must not run into a cutout" if on_board else "A bend line must cross the board",
                                _rounded(_lerp(start, end, 0.5))))
        return None
    a, b = spans[around]
    # A gap to the next span that is still inside a flex zone is a cutout in the flex, not its edge.
    for gap in ((spans[around - 1][1], a) if around else None, (b, spans[around + 1][0]) if around + 1 < len(spans)
                else None):
        if gap and any(_inside(_lerp(start, end, sum(gap) / 2), zone) for zone in zones):
            problems.append(Problem("A bend must not run into a cutout", _rounded(_lerp(start, end, gap[0]))))
            return None
    for t, short in ((a, -a), (b, b - 1)):
        if short * length > SHORT_NM:
            problems.append(Problem("A bend line should run edge to edge of the flex",
                                    _rounded(_lerp(start, end, t))))
    return _rounded(_lerp(start, end, a)), _rounded(_lerp(start, end, b))


# --- Regions --------------------------------------------------------------------------------

def _regions(rings: list[Ring], bends: list[Bend]) -> list[Region]:
    """Split the board's pieces at every bend chord, then hang them in trees off the largest."""
    pieces = [ring for ring in rings if sum(_inside(ring[0], other) for other in rings if other is not ring) % 2 == 0]
    cuts = [(start, end, index) for index, bend in enumerate(bends) for start, end in bend.cuts]
    for start, end, _ in cuts:
        middle = _lerp(start, end, 0.5)
        index = next((k for k, piece in enumerate(pieces) if _inside(middle, piece)), None)
        if index is None:
            continue
        direction = (end[0] - start[0], end[1] - start[1])
        crossings = _crossings(pieces[index], start, direction)
        around = next(((a, b) for a, b in zip(crossings[::2], crossings[1::2]) if a[0] <= 0.5 <= b[0]), None)
        if around is None or around[0][1] == around[1][1]:
            continue
        pieces[index:index + 1] = _split(pieces[index], *around)
    joins: dict[int, list[tuple[int, int]]] = {k: [] for k in range(len(pieces))}
    for start, end, bend_index in cuts:
        sides = []
        for side in (-1, 1):
            point = _beside(Bend("", start, end, 0.0, 0), side)
            sides.append(next((k for k, piece in enumerate(pieces) if _inside(point, piece)), None))
        if None not in sides and sides[0] != sides[1]:
            joins[sides[0]].append((sides[1], bend_index))
            joins[sides[1]].append((sides[0], bend_index))
    parent: dict[int, tuple[int | None, int | None]] = {}
    for root in sorted(range(len(pieces)), key=lambda k: -abs(_area(pieces[k]))):
        if root in parent:
            continue
        parent[root] = (None, None)
        queue = [root]
        while queue:
            here = queue.pop(0)
            for there, bend_index in joins[here]:
                if there not in parent:
                    parent[there] = (here, bend_index)
                    queue.append(there)
    return [Region(piece, *parent[k]) for k, piece in enumerate(pieces)]


def _split(ring: Ring, first, second) -> tuple[Ring, Ring]:
    """The ring cut along the chord between two edge crossings (t, edge index, point)."""
    (_, i, a), (_, j, b) = first, second
    a, b = _rounded(a), _rounded(b)
    count = len(ring)

    def walk(start: int, stop: int) -> list[Point]:  # the vertices after edge `start` up to edge `stop`
        points, k = [], (start + 1) % count
        while True:
            points.append(ring[k])
            if k == stop:
                return points
            k = (k + 1) % count

    return _clean((a, *walk(i, j), b)), _clean((b, *walk(j, i), a))


def _beside(bend: Bend, side: int) -> Point:
    dx, dy = bend.end[0] - bend.start[0], bend.end[1] - bend.start[1]
    length = math.hypot(dx, dy)
    middle = _lerp(bend.start, bend.end, 0.5)
    return middle[0] - side * dy / length * SIDE_STEP_NM, middle[1] + side * dx / length * SIDE_STEP_NM


# --- Stiffeners -----------------------------------------------------------------------------

def _stiffeners(drawings: list[Drawing], problems: list[Problem]) -> list[Stiffener]:
    shapes = [drawing for drawing in drawings if drawing.kind == "closed"]
    texts = [drawing for drawing in drawings if drawing.kind == "text"]
    for drawing in drawings:
        if drawing.kind == "line":
            problems.append(Problem("A stiffener must be a closed shape (a rectangle or polygon)", drawing.points[0]))
    labels = _labels(texts, shapes, lambda text, shape: 0 if _inside(text.points[0], shape.points) else min(
        _segment_distance(text.points[0], p, q) for p, q in _edges(shape.points)))
    stiffeners = []
    for index, shape in enumerate(shapes):
        material, thickness, side = parse_stiffener(texts[labels[index]].text) if index in labels else ("", None, None)
        found = {"material": material or None, "thickness": thickness, "side": side}
        assumed = tuple(part for part, value in found.items() if value is None)
        material, thickness, side = (value if value is not None else default for value, default
                                     in zip(found.values(), DEFAULT_STIFFENER))
        stiffeners.append(Stiffener(shape.id, shape.points, material, thickness, side, assumed))
        if assumed:
            problems.append(Problem(f"Stiffener {len(stiffeners)}: {', '.join(assumed)} not in its text, so "
                                    f'"{stiffener_note(material, thickness, side)}" is assumed. Copy its text to '
                                    f"keep it, or write your own", shape.points[0], level="note"))
    return stiffeners


# --- Geometry -------------------------------------------------------------------------------

def _labels(texts: list[Drawing], targets: list[Drawing], distance) -> dict[int, int]:
    """target index -> text index, nearest pairs first, each text and target used once."""
    pairs = sorted((distance(text, target), t, k) for t, text in enumerate(texts) for k, target in enumerate(targets))
    labels: dict[int, int] = {}
    used: set[int] = set()
    for gap, t, k in pairs:
        if gap <= LABEL_REACH_NM and k not in labels and t not in used:
            labels[k] = t
            used.add(t)
    return labels


def _crossings(ring: Ring, origin: Point, direction) -> list[tuple[float, int, tuple[float, float]]]:
    """(t, edge index, point) where the line origin + t * direction crosses the ring, by t."""
    found = []
    for index, (p, q) in enumerate(_edges(ring)):
        ex, ey = q[0] - p[0], q[1] - p[1]
        denominator = direction[0] * ey - direction[1] * ex
        if denominator == 0:
            continue
        wx, wy = p[0] - origin[0], p[1] - origin[1]
        t = (wx * ey - wy * ex) / denominator
        u = (wx * direction[1] - wy * direction[0]) / denominator
        if 0 <= u < 1:
            found.append((t, index, (origin[0] + t * direction[0], origin[1] + t * direction[1])))
    return sorted(found)


def _edges(ring: Ring):
    return zip(ring, ring[1:] + ring[:1])


def _inside(point, ring: Ring) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in _edges(ring):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _area(ring: Ring) -> float:
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in _edges(ring)) / 2


def _segment_distance(point, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = dx * dx + dy * dy
    t = 0.0 if span == 0 else max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / span))
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def _segments_cross(first, second) -> bool:
    (a, b), (c, d) = first, second

    def turn(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return turn(a, b, c) * turn(a, b, d) < 0 and turn(c, d, a) * turn(c, d, b) < 0


def _lerp(a, b, t: float) -> tuple[float, float]:
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def _rounded(point) -> Point:
    return round(point[0]), round(point[1])


def _clean(points) -> Ring:
    ring: list[Point] = []
    for point in points:
        if not ring or ring[-1] != point:
            ring.append(point)
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    return tuple(ring)
