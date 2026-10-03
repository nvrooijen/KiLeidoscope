"""A board's cross section along a vertical cut plane, as flat rectangles.

Pure numpy, no bpy: cut.py reads the board from Blender and draws what this returns.
The cut runs through `origin` (board XY, metres) with `across` the plane's horizontal
normal; a point's place along the cut is `s`. The section is built in horizontal slabs:
in each, every stretch of the cut is laminate, copper, via plating, a via's core, or open
(not drawn). Nothing is drawn twice, so nothing overlaps; mask tents and a plated board
edge, which lie outside the board, are added after.

What a micrograph shows, in sRGB: bare copper (cut metal never has the finish), pale
cured core, a darker prepreg, and a few named laminates.
"""

from typing import NamedTuple

import numpy as np

COPPER = (0.82, 0.50, 0.34)  # polished copper in a micrograph: lighter and pinker than a copper swatch
CORE = (0.89, 0.83, 0.62)
PREPREG = (0.80, 0.72, 0.46)
RESIN = (0.88, 0.87, 0.80)  # epoxy filling a via (or a buried via's prepreg): milky, the barrel shows through
LAMINATES = {"polyimide": (0.80, 0.50, 0.05), "ptfe": (0.94, 0.94, 0.90), "rogers": (0.92, 0.90, 0.84)}
WOVEN = {CORE, PREPREG, *LAMINATES.values()}  # glass-reinforced: cut.py draws the weave in these
ALUMINUM = (0.80, 0.81, 0.83)  # polished aluminum in a micrograph: bright, a cool grey
BASE_METALS = {"AL": ALUMINUM, "CU": COPPER}  # an IMS board's metal base (ims.METALS keys)
METALS = {COPPER, ALUMINUM}  # cut.py polishes these
IMS_DIELECTRIC = (0.93, 0.92, 0.86)  # an IMS's thermal dielectric: ceramic-filled epoxy, chalky, no glass
DEFAULT_COPPER_M = 35e-6  # KiCad's own default copper thickness, when the stackup has none

EMPTY = np.empty((0, 2))


# --- Intervals along the cut (sorted, disjoint (n, 2) arrays) ---------------------------------

def merge(intervals):
    intervals = np.asarray(intervals, np.float64).reshape(-1, 2)
    intervals = intervals[intervals[:, 1] > intervals[:, 0]]
    if not len(intervals):
        return EMPTY
    intervals = intervals[np.argsort(intervals[:, 0], kind="stable")]
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return np.array(merged)


def subtract(keep, cut):
    out = []
    for start, end in keep:
        for cut_start, cut_end in cut:
            if cut_end <= start:
                continue
            if cut_start >= end:
                break
            if cut_start > start:
                out.append((start, cut_start))
            start = max(start, cut_end)
            if start >= end:
                break
        if start < end:
            out.append((start, end))
    return merge(out)


def intersect(a, b):
    return subtract(a, subtract(a, b))


# --- Shapes crossed by the cut ---------------------------------------------------------------

class Line:
    """The cut in board XY: `s` along it, signed distance `d` across it."""

    def __init__(self, origin, across):
        across = np.asarray(across, np.float64)
        self.origin = np.asarray(origin, np.float64)
        self.across = across / np.hypot(*across)
        self.along = np.array((-self.across[1], self.across[0]))

    def s(self, points):
        return (np.asarray(points, np.float64).reshape(-1, 2) - self.origin) @ self.along

    def d(self, points):
        return (np.asarray(points, np.float64).reshape(-1, 2) - self.origin) @ self.across


def rings(line, a, b, item):
    """Even-odd inside of closed rings given as edges a -> b, separately per item, then
    united (overlapping pads stay copper; a zone's holes stay holes)."""
    da, db = line.d(a), line.d(b)
    crossing = (da > 0) != (db > 0)  # half-open, so a vertex on the cut counts once
    if not crossing.any():
        return EMPTY
    da, db = da[crossing], db[crossing]
    sa, sb = line.s(np.asarray(a)[crossing]), line.s(np.asarray(b)[crossing])
    s = sa + (sb - sa) * da / (da - db)
    item = np.asarray(item)[crossing]
    order = np.lexsort((s, item))
    s, item = s[order], item[order]
    out = []
    for group in np.split(s, np.flatnonzero(np.diff(item)) + 1):
        pairs = len(group) // 2 * 2  # an unclosed ring leaves one crossing over: dropped
        out.extend(group[:pairs].reshape(-1, 2))
    return merge(out)


def discs(line, centre, radius):
    d, s = line.d(centre), line.s(centre)
    half = np.sqrt(np.clip(np.asarray(radius, np.float64) ** 2 - d ** 2, 0, None))
    hit = half > 0
    return merge(np.column_stack((s - half, s + half))[hit])


def capsules(line, a, b, radius):
    """Tracks (and oblong drills): the stretch of the cut within `radius` of each segment.
    A capsule is convex, so it is the hull of its two end discs and its body."""
    a, b = np.asarray(a, np.float64).reshape(-1, 2), np.asarray(b, np.float64).reshape(-1, 2)
    radius = np.broadcast_to(np.asarray(radius, np.float64), (len(a),))
    start, end = np.full(len(a), np.inf), np.full(len(a), -np.inf)
    for centre in (a, b):
        d, s = line.d(centre), line.s(centre)
        half = np.sqrt(np.clip(radius ** 2 - d ** 2, 0, None))
        hit = half > 0
        start = np.where(hit, np.minimum(start, s - half), start)
        end = np.where(hit, np.maximum(end, s + half), end)
    edge = b - a
    length = np.hypot(edge[:, 0], edge[:, 1])
    real = length > 0
    unit = edge / np.where(real, length, 1.0)[:, None]
    normal = np.column_stack((-unit[:, 1], unit[:, 0]))
    offset = line.origin - a
    low, high = np.full(len(a), -np.inf), np.full(len(a), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        for direction, least, most in ((unit, np.zeros(len(a)), length), (normal, -radius, radius)):
            rate = line.along @ direction.T
            at = np.einsum("ij,ij->i", offset, direction)
            first, second = (least - at) / rate, (most - at) / rate
            flat = rate == 0
            inside = (least <= at) & (at <= most)
            low = np.maximum(low, np.where(flat, np.where(inside, -np.inf, np.inf), np.minimum(first, second)))
            high = np.minimum(high, np.where(flat, np.where(inside, np.inf, -np.inf), np.maximum(first, second)))
    body = real & (low < high)
    start = np.where(body, np.minimum(start, low), start)
    end = np.where(body, np.maximum(end, high), end)
    return merge(np.column_stack((start, end))[start < end])


def drills(line, rows):
    """Pad drills (x, y, width, height, angle, plated) as round or oblong holes."""
    rows = np.asarray(rows, np.float64).reshape(-1, 6)
    if not len(rows):
        return EMPTY
    x, y, width, height, angle = rows[:, :5].T
    long_axis = np.where(width >= height, 0.0, np.pi / 2) + angle
    half = np.abs(width - height) / 2
    reach = np.column_stack((np.cos(long_axis), np.sin(long_axis))) * half[:, None]
    centre = np.column_stack((x, y))
    return capsules(line, centre - reach, centre + reach, np.minimum(width, height) / 2)


def copper_along(line, shapes):
    """One layer's copper along a line: `shapes` holds its closed rings ("rings": (a, b,
    item) edges, even-odd per item) and its tracks ("tracks": (a, b, radius) segments)."""
    found = []
    if shapes.get("rings"):
        found.append(rings(line, *(np.concatenate(parts) for parts in zip(*shapes["rings"]))))
    if shapes.get("tracks"):
        found.append(capsules(line, *(np.concatenate(parts) for parts in zip(*shapes["tracks"]))))
    return merge(np.vstack(found)) if found else EMPTY


# --- Plated board edge ------------------------------------------------------------------------

EDGE_INSET_M = 50e-6  # copper this close to the edge reaches it (edge plating is drawn there)
EDGE_SHORTEST_M = 20e-6  # plated stretches shorter than this are noise
EDGE_CORNER_SNAP_M = 0.2e-3  # copper stopping this close to a sharp corner of the outline reaches it
EDGE_SMOOTH_TURN = np.cos(np.radians(30))  # as edge_plating.SMOOTH_TURN: a turn beyond this is a corner


def _sharp_ends(a, b):
    """Per outline segment, (start is a sharp corner, end is one): another segment meets
    it there at more than a smooth bend (EDGE_SMOOTH_TURN), whichever way either runs."""
    def key(point):
        return tuple(np.round(point / 1e-9).astype(np.int64))

    edge = b - a
    length = np.hypot(edge[:, 0], edge[:, 1])
    units = edge / np.maximum(length, 1e-12)[:, None]
    leaving = {}  # point -> [(segment, its direction away from the point)]
    for index in np.flatnonzero(length >= 1e-9):  # a zero-length segment turns nothing
        leaving.setdefault(key(a[index]), []).append((index, units[index]))
        leaving.setdefault(key(b[index]), []).append((index, -units[index]))

    def sharp(index, point, away):
        # Running straight on, the two segments leave a point in opposite directions.
        return any(float(np.dot(away, other_away)) > -EDGE_SMOOTH_TURN
                   for other, other_away in leaving.get(key(point), ()) if other != index)

    return [(sharp(index, a[index], units[index]), sharp(index, b[index], -units[index])) for index in range(len(a))]


def plated_edges(outline, front, back, inset=EDGE_INSET_M, snap=EDGE_CORNER_SNAP_M):
    """Stretches of the board outline that a plated board edge covers: where copper on
    both outer layers reaches the edge. Each is (start xy, end xy, outward unit normal).

    Per outline segment, a line just inside the board runs along it; the front and back
    copper crossing that line within the segment, both at once, are the plated stretches.
    KiCad rounds a zone fill's corners, so copper filling a corner stops just short of it:
    a stretch ending within `snap` of a sharp corner of the outline runs on to the corner.
    """
    a, b, item = (np.asarray(part) for part in outline)
    a, b = a.astype(np.float64).reshape(-1, 2), b.astype(np.float64).reshape(-1, 2)
    stretches = []
    for start, end, (sharp_start, sharp_end) in zip(a, b, _sharp_ends(a, b)):
        edge = end - start
        length = float(np.hypot(*edge))
        if length < 1e-9:
            continue
        unit = edge / length
        normal = np.array((-unit[1], unit[0]))
        middle = (start + end) / 2
        inside = rings(Line(middle, unit), a, b, item)  # along the normal through the middle: s is along it
        if not len(inside):
            continue
        if ((inside[:, 0] <= inset) & (inset < inside[:, 1])).any():
            inward = normal
        elif ((inside[:, 0] <= -inset) & (-inset < inside[:, 1])).any():
            inward = -normal
        else:
            continue
        line = Line(start + inward * inset, inward)
        span = np.sort(line.s(np.vstack((start + inward * inset, end + inward * inset))))
        reach = intersect(intersect(copper_along(line, front), copper_along(line, back)), np.array([span]))
        forward = float(np.dot(line.along, unit)) > 0  # s grows as the outline runs
        low_sharp, high_sharp = (sharp_start, sharp_end) if forward else (sharp_end, sharp_start)
        found = []
        for s0, s1 in reach:
            s0 = span[0] if low_sharp and s0 - span[0] <= snap else s0
            s1 = span[1] if high_sharp and span[1] - s1 <= snap else s1
            if s1 - s0 >= EDGE_SHORTEST_M:
                found.append((s0, s1) if forward else (s1, s0))
        for first, last in found if forward else found[::-1]:  # stretches run as the outline does
            stretches.append((line.origin + first * line.along - inward * inset,
                              line.origin + last * line.along - inward * inset, -inward))
    return stretches


# --- The stackup ------------------------------------------------------------------------------

def _laminate_color(saved):
    material = str((saved or {}).get("material") or "").casefold()
    for name, color in LAMINATES.items():
        if name in material:
            return color
    return PREPREG if (saved or {}).get("type") == "prepreg" else CORE


def stack_layout(heights, thickness, stack, saved, base=None):
    """(copper, bands): each copper layer's z range, and the laminate between the outer
    copper as (z0, z1, colour) bands, from the bottom up.

    `heights` puts B.Cu's bottom at 0 and every other layer's top at its height (the
    bridge's layer_heights_nm). Between two copper layers, the stack's dielectrics share
    the gap by their thickness; `saved` (the saved board's types, top first) colours
    them when it matches the stack. Inner copper's own z range takes the colour of the
    laminate below it: that is what fills between its traces.

    `base`: an IMS board's metal base as (z0, z1, colour), in B.Cu's place (the heights
    already leave it room): a band of its own, under the IMS's thermal dielectric. B.Cu
    then has no copper of its own (an empty range at the base's bottom): its via lands
    would otherwise stand the base's whole height.
    """
    copper = {}
    for name, height in heights.items():
        t = float(thickness.get(name) or DEFAULT_COPPER_M)
        copper[name] = (height, height + t) if name == "B.Cu" else (height - t, height)
    order = sorted(copper, key=lambda name: -copper[name][1])  # top first
    dielectrics = [entry for entry in stack if entry.get("type") == "dielectric"]
    saved = list(saved or [])
    colors = {id(entry): _laminate_color(saved[index] if len(saved) == len(dielectrics) else None)
              for index, entry in enumerate(dielectrics)}
    between, upper = {}, None
    for entry in stack:
        if entry.get("type") == "copper":
            upper = entry.get("name")
        elif entry.get("type") == "dielectric" and upper is not None:
            between.setdefault(upper, []).append(entry)
    bands = []
    for upper, lower in zip(order, order[1:]):
        top, bottom = copper[upper][0], copper[lower][1]
        entries = between.get(upper) or [{}]
        weights = np.array([float(entry.get("thickness_nm") or 0) for entry in entries])
        weights = weights if weights.sum() > 0 else np.ones(len(entries))
        edges = top - np.concatenate(([0.0], np.cumsum(weights) / weights.sum())) * (top - bottom)
        for entry, z1, z0 in zip(entries, edges, edges[1:]):
            bands.append((float(z0), float(z1), colors.get(id(entry), CORE)))
    bands.sort()
    for name in order[1:-1]:  # inner copper: the laminate below it fills between its traces
        z0, z1 = copper[name]
        below = next((color for b0, b1, color in reversed(bands) if b1 <= z0 + 1e-12), CORE)
        bands.append((z0, z1, below))
    if base is not None:
        bands = [(z0, z1, IMS_DIELECTRIC) for z0, z1, _ in bands] + [tuple(base)]
        copper["B.Cu"] = (base[0], base[0])
    bands.sort()
    return copper, bands


# --- The section ------------------------------------------------------------------------------

OUTER = ("F.Cu", "B.Cu")


class _Hole(NamedTuple):
    """A hole the cut crosses, from z0 to z1: its drill and its bore inside the plating
    (intervals along the cut), the core in its bore ((z0, z1, colour) or None), and the
    z ranges where a cap plates over it."""
    z0: float
    z1: float
    full: np.ndarray
    bore: np.ndarray
    core: tuple = None
    caps: tuple = ()


def _union(parts):
    return merge(np.vstack([EMPTY, *parts]))


def _copper_extent(copper):
    """(bottom, top) of the copper stack."""
    return min(z[0] for z in copper.values()), max(z[1] for z in copper.values())


def _via_parts(line, vias, layers, copper, plating, cap_plating, tents, land_lift, plug_color):
    """The vias the cut crosses, as (holes, lands, plated, films) for `cross_section`.

    holes: `_Hole`s. lands: {layer: land intervals}, on both ends and on the inner layers
    with an annular ring. plated: (z0, z1, intervals), copper outward of the outer copper:
    the lands' lift and cap plating. films: (z0, z1, intervals, colour), mask tents.
    """
    holes, lands, plated, films = [], {}, [], []
    if vias is None or not len(vias["xy"]):
        return holes, lands, plated, films
    xy = np.asarray(vias["xy"], np.float64).reshape(-1, 2)
    near = np.abs(line.d(xy)) < np.maximum(vias["diameter"], vias["drill"]) / 2
    flag = {name: np.asarray(vias.get(name, np.zeros(len(xy))), bool).reshape(-1)
            for name in ("core_top", "core_bottom", "fill_copper", "plug_ink", "capped", "tent_top", "tent_bottom")}
    ringed = vias.get("ringed")  # per via, the layers with an annular ring; else every layer
    for index in np.flatnonzero(near):
        top, bottom = vias["top"][index], vias["bottom"][index]
        if top not in copper or bottom not in copper:
            continue
        centre = xy[index:index + 1]
        radius = vias["drill"][index] / 2
        full, bore = discs(line, centre, radius), discs(line, centre, max(radius - plating, 0.0))
        land = discs(line, centre, vias["diameter"][index] / 2)
        ends = {top, bottom}
        outer = [name for name in OUTER if name in ends]
        ringed_here = ringed[index] if ringed is not None else set(copper)
        pad = {name: land if name in ringed_here else EMPTY for name in outer}  # an outer end without a ring: none
        # Each end's land; an outer one stands `land_lift` out of its copper, as in 3D.
        reach = {name: (copper[name][0] - (land_lift if name == "B.Cu" else 0.0),
                        copper[name][1] + (land_lift if name == "F.Cu" else 0.0)) for name in ends}
        z0, z1 = min(r[0] for r in reach.values()), max(r[1] for r in reach.values())
        if (top == "F.Cu") != (bottom == "B.Cu"):  # blind: drilled down onto its inner land, which stays whole
            inner = bottom if top == "F.Cu" else top
            z0, z1 = (copper[inner][1], z1) if top == "F.Cu" else (z0, copper[inner][0])
        core = None
        if flag["core_top"][index] or flag["core_bottom"][index]:
            middle = (z0 + z1) / 2
            color = (COPPER if flag["fill_copper"][index] else tuple(plug_color) if flag["plug_ink"][index]
                     else RESIN)
            core = (z0 if flag["core_bottom"][index] else middle, z1 if flag["core_top"][index] else middle, color)
        capped = bool(flag["capped"][index] and core)  # plated over a core, at its outer ends
        holes.append(_Hole(z0, z1, full, bore, core, tuple(reach[name] for name in outer) if capped else ()))
        # Outward from each outer end's copper, on the land and across the drill: the land's
        # lift, a cap's plating, then a tent's mask film.
        for name in outer:
            sign = 1.0 if name == "F.Cu" else -1.0
            face = copper[name][1] if sign > 0 else copper[name][0]
            tent = flag["tent_top" if sign > 0 else "tent_bottom"][index] and name in tents
            cover = land if len(pad[name]) else full  # without a ring: over the drill alone
            for thickness, color, across in ((land_lift if len(pad[name]) else 0.0, None, land),
                                             (cap_plating if capped else 0.0, None, cover),
                                             (*(tents[name] if tent else (0.0, None)), cover)):
                if thickness > 0:
                    span = sorted((face, face + sign * thickness))
                    if color is None:
                        plated.append((*span, across))
                    else:
                        films.append((*span, across, tuple(color)))
                    face += sign * thickness
        # Rings: on the layers KiCad gives one; an inner end (a blind via's floor) always has its land.
        between = [name for name, (c0, c1) in copper.items() if name not in ends and z0 < c0 and c1 < z1]
        for name in (*ends, *between):
            if name in ringed_here or (name in ends and name not in OUTER):
                lands.setdefault(name, []).append(land)
    return holes, {name: _union(found) for name, found in lands.items()}, plated, films


def _pad_holes(line, pad_drills, copper, plating):
    """Pad drills the cut crosses, as `_Hole`s through the copper stack; a plated one's
    bore is `plating` inside its drill."""
    if pad_drills is None or not len(pad_drills) or not copper:
        return []
    z0, z1 = _copper_extent(copper)
    holes = []
    for x, y, width, height, angle, plated in np.asarray(pad_drills, np.float64).reshape(-1, 6):
        full = drills(line, [(x, y, width, height, angle, plated)])
        if not len(full):
            continue
        wall = plating if plated else 0.0
        bore = drills(line, [(x, y, max(width - 2 * wall, 0.0), max(height - 2 * wall, 0.0), angle, plated)])
        holes.append(_Hole(z0, z1, full, bore))
    return holes


def _slab(middle, inside, laminate_z, bands, layers, copper, lands, plated, holes):
    """What the cut shows at height `middle`: [(intervals, colour)], none overlapping."""
    level = [name for name, (c0, c1) in copper.items() if c0 <= middle < c1]
    metal = [layers.get(name, EMPTY) for name in level] + [lands[name] for name in level if name in lands]
    metal += [found for c0, c1, found in plated if c0 <= middle < c1]
    drilled_holes, capped = [], []
    for hole in holes:
        if hole.z0 <= middle < hole.z1:
            (capped if any(c0 <= middle < c1 for c0, c1 in hole.caps) else drilled_holes).append(hole)
    metal = _union([*metal, *(hole.full for hole in capped)])  # plated over: the drill is copper here
    drilled = _union(hole.full for hole in drilled_holes)
    walls = subtract(drilled, _union(hole.bore for hole in drilled_holes))
    cores = [hole for hole in drilled_holes if hole.core and hole.core[0] <= middle < hole.core[1]]
    filled = _union(hole.bore for hole in cores if hole.core[2] == COPPER)
    laminate = inside if laminate_z[0] <= middle < laminate_z[1] else EMPTY
    band = next((color for b0, b1, color in bands if b0 <= middle < b1), CORE)
    parts = [(subtract(subtract(laminate, metal), drilled), band), (subtract(metal, drilled), COPPER),
             (merge(np.vstack([walls, filled])), COPPER)]
    plugs = {}  # resin, plug ink: by colour
    for hole in cores:
        if hole.core[2] != COPPER:
            plugs.setdefault(hole.core[2], []).append(hole.bore)
    taken = filled
    for color, found in plugs.items():
        plug = subtract(_union(found), taken)
        parts.append((plug, color))
        taken = merge(np.vstack([taken, plug]))
    return parts


def _stack(slabs):
    """Rectangles from (z0, z1, parts) slabs, bottom up: a stretch drawn in the slab below
    grows up into this one instead of starting another rectangle."""
    growing, done = {}, []
    for z0, z1, parts in slabs:
        for intervals, color in parts:
            for s0, s1 in intervals:
                key = (round(s0, 12), round(s1, 12), color)
                rect = growing.get(key)
                if rect is not None and abs(rect[3] - z0) < 1e-12:
                    rect[3] = z1
                    continue
                if rect is not None:
                    done.append(tuple(rect))
                growing[key] = [s0, s1, z0, z1, color]
    return done + [tuple(rect) for rect in growing.values()]


def _edge_strips(line, stretches, copper, thickness):
    """Where the cut crosses a plated board edge's stretch: a `thickness` copper strip
    outside the edge, the copper stack's whole height."""
    if not stretches or not copper:
        return []
    bottom, top = _copper_extent(copper)
    strips = []
    for start, end, outward in stretches:
        d0, d1 = line.d(start)[0], line.d(end)[0]
        if (d0 > 0) == (d1 > 0):
            continue
        s = float(line.s(start + (end - start) * d0 / (d0 - d1))[0])
        s0, s1 = (s, s + thickness) if float(np.dot(outward, line.along)) > 0 else (s - thickness, s)
        strips.append((s0, s1, bottom, top, COPPER))
    return strips


def cross_section(line, outline, layers, copper, bands, vias=None, pad_drills=None,
                  plating=25e-6, cap_plating=0.0, plated_edges=(), edge_plating=25e-6, tents=None,
                  land_lift=0.0, plug_color=RESIN):
    """Rectangles (s0, s1, z0, z1, sRGB colour), none overlapping.

    outline: (a, b, item) edges of the board outline (even-odd: cutouts are holes).
    layers: {layer name: interval array of its copper along the cut}, already crossed.
    copper, bands: from `stack_layout`.
    vias: dict of arrays xy, diameter, drill, and per via the names `top` and `bottom`
    of the layers it joins (lands on those two, a barrel between). Optional per via, as
    protection.resolve settles them:
    `core_top`/`core_bottom`: a core fills the barrel's upper / lower half; it is copper
    (`fill_copper`), solder mask ink in `plug_color` (`plug_ink`), else resin.
    `capped`: plated over at its outer ends: copper across the whole drill in the outer
    copper (the core ends under it), and `cap_plating` more over the land, outside it.
    `tent_top`/`tent_bottom`: the solder mask spans its drill there, drawn as a film over
    the land (outside any cap) from `tents` ({"F.Cu"/"B.Cu": (mask thickness, sRGB)}).
    `ringed`: per via, the set of layers with an annular ring (KiCad's "Annular rings",
    worked out by the bridge); without it, every layer. An outer end without one has no
    land (a cap or tent then spans the drill alone); an inner end always keeps its land.
    land_lift: the 3D outer lands stand this far out of the copper; their section does too.
    pad_drills: rows (x, y, width, height, angle, plated), through the whole board.
    plating: barrel wall thickness, in vias and plated pad drills.
    plated_edges: stretches of a plated board edge (`plated_edges`): where the cut crosses
    one, an `edge_plating` thick copper strip outside the edge, the copper's whole height.
    """
    inside = rings(line, *outline) if outline is not None else EMPTY
    laminate_z = (min(b[0] for b in bands), max(b[1] for b in bands)) if bands else (0.0, 0.0)
    holes, lands, plated, films = _via_parts(line, vias, layers, copper, plating, cap_plating, tents or {},
                                             land_lift, plug_color)
    holes += _pad_holes(line, pad_drills, copper, plating)
    cuts = {z for band in bands for z in band[:2]} | {z for zr in copper.values() for z in zr}
    cuts |= {z for found in plated for z in found[:2]}
    for hole in holes:
        cuts |= {hole.z0, hole.z1, *(hole.core[:2] if hole.core else ())}
    cuts = sorted(cuts)
    rects = _stack((z0, z1, _slab((z0 + z1) / 2, inside, laminate_z, bands, layers, copper, lands, plated, holes))
                   for z0, z1 in zip(cuts, cuts[1:]) if z1 - z0 >= 1e-12)
    rects += [(s0, s1, z0, z1, color) for z0, z1, intervals, color in films for s0, s1 in intervals]
    return rects + _edge_strips(line, plated_edges, copper, edge_plating)
