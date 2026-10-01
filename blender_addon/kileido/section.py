"""A board's cross section along a vertical cut plane, as flat rectangles.

Pure numpy, no bpy: cut.py reads the board from Blender and draws what this returns.
The cut runs through `origin` (board XY, metres) with `across` the plane's horizontal
normal; a point's place along the cut is `s`. The section is built in horizontal slabs:
in each, every stretch of the cut is laminate, copper, via plating, plug, or open (not
drawn). Nothing is drawn twice, so nothing overlaps.

What a micrograph shows, in sRGB: bare copper (cut metal never has the finish), pale
cured core, a darker prepreg, and a few named laminates.
"""

import numpy as np

COPPER = (184 / 255, 115 / 255, 50 / 255)  # KiCad's bare copper
CORE = (0.89, 0.83, 0.62)
PREPREG = (0.80, 0.72, 0.46)
RESIN = (0.60, 0.64, 0.50)  # epoxy plugging a via
LAMINATES = {"polyimide": (0.80, 0.50, 0.05), "ptfe": (0.94, 0.94, 0.90), "rogers": (0.92, 0.90, 0.84)}
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


# --- The stackup ------------------------------------------------------------------------------

def _laminate_color(saved):
    material = str((saved or {}).get("material") or "").casefold()
    for name, color in LAMINATES.items():
        if name in material:
            return color
    return PREPREG if (saved or {}).get("type") == "prepreg" else CORE


def stack_layout(heights, thickness, stack, saved):
    """(copper, bands): each copper layer's z range, and the laminate between the outer
    copper as (z0, z1, colour) bands, from the bottom up.

    `heights` puts B.Cu's bottom at 0 and every other layer's top at its height (the
    bridge's layer_heights_nm). Between two copper layers, the stack's dielectrics share
    the gap by their thickness; `saved` (the saved board's types, top first) colours
    them when it matches the stack. Inner copper's own z range takes the colour of the
    laminate below it: that is what fills between its traces.
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
    bands.sort()
    return copper, bands


# --- The section ------------------------------------------------------------------------------

def cross_section(line, outline, layers, copper, bands, vias=None, pad_drills=None,
                  plating=25e-6, plug=None):
    """Rectangles (s0, s1, z0, z1, sRGB colour), bottom up.

    outline: (a, b, item) edges of the board outline (even-odd: cutouts are holes).
    layers: {layer name: interval array of its copper along the cut}, already crossed.
    copper, bands: from `stack_layout`.
    vias: dict of arrays xy, diameter, drill, and per via the names `top` and `bottom`
    of the layers it joins (lands on those two, a barrel between).
    pad_drills: rows (x, y, width, height, angle, plated), through the whole board.
    plating: barrel wall thickness. plug: None (open), "RESIN" or "COPPER".
    """
    inside = rings(line, *outline) if outline is not None else EMPTY
    laminate_z = (min(b[0] for b in bands), max(b[1] for b in bands)) if bands else (0.0, 0.0)
    holes = []  # (z0, z1, full interval, bore interval, plugged)
    lands = {}  # layer -> land intervals
    if vias is not None and len(vias["xy"]):
        xy = np.asarray(vias["xy"], np.float64).reshape(-1, 2)
        near = np.abs(line.d(xy)) < np.maximum(vias["diameter"], vias["drill"]) / 2
        for index in np.flatnonzero(near):
            top, bottom = vias["top"][index], vias["bottom"][index]
            if top not in copper or bottom not in copper:
                continue
            centre = xy[index:index + 1]
            outer = vias["drill"][index] / 2
            full = discs(line, centre, outer)
            bore = discs(line, centre, max(outer - plating, 0.0))
            z0, z1 = min(copper[bottom][0], copper[top][0]), max(copper[bottom][1], copper[top][1])
            holes.append((z0, z1, full, bore, plug))
            for name in {top, bottom}:
                lands.setdefault(name, []).append(discs(line, centre, vias["diameter"][index] / 2))
    if pad_drills is not None and len(pad_drills):
        rows = np.asarray(pad_drills, np.float64).reshape(-1, 6)
        whole = (min(z[0] for z in copper.values()), max(z[1] for z in copper.values()))
        for row in rows:
            full = drills(line, row[None, :])
            if not len(full):
                continue
            shrink = plating if row[5] else 0.0
            bore = drills(line, np.array([[row[0], row[1], max(row[2] - 2 * shrink, 0), max(row[3] - 2 * shrink, 0),
                                           row[4], row[5]]]))
            holes.append((*whole, full, bore, None))
    lands = {name: merge(np.vstack(found)) for name, found in lands.items()}

    cuts = {z for band in bands for z in band[:2]} | {z for zr in copper.values() for z in zr}
    cuts |= {z for hole in holes for z in hole[:2]}
    cuts = sorted(cuts)
    growing, done = {}, []  # a stretch drawn in the slab below grows up instead of stacking
    for z0, z1 in zip(cuts, cuts[1:]):
        if z1 - z0 < 1e-12:
            continue
        middle = (z0 + z1) / 2
        metal = [layers.get(name, EMPTY) for name, (c0, c1) in copper.items() if c0 <= middle < c1]
        metal += [lands[name] for name, (c0, c1) in copper.items() if c0 <= middle < c1 and name in lands]
        metal = merge(np.vstack(metal)) if metal else EMPTY
        here = [hole for hole in holes if hole[0] <= middle < hole[1]]
        drilled = merge(np.vstack([hole[2] for hole in here])) if here else EMPTY
        bores = merge(np.vstack([hole[3] for hole in here])) if here else EMPTY
        plugged = merge(np.vstack([hole[3] for hole in here if hole[4]])) if any(h[4] for h in here) else EMPTY
        plug_color = COPPER if any(h[4] == "COPPER" for h in here) else RESIN
        walls = subtract(drilled, bores)
        laminate = inside if laminate_z[0] <= middle < laminate_z[1] else EMPTY
        band = next((color for b0, b1, color in bands if b0 <= middle < b1), CORE)
        for intervals, color in ((subtract(subtract(laminate, metal), drilled), band),
                                 (subtract(metal, drilled), COPPER),
                                 (walls, COPPER),
                                 (plugged, plug_color)):
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
