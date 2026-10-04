"""Flex mode's fold, without bpy: where every point of the flat board goes when it folds.

The bridge's flex report (kileido_bridge.flex_checks.report) gives the flat regions, joined
by bends in a tree, and each bend's chord, child side and curved width. Here they become
world metres (Blender's frame: KiCad's y flipped, B.Cu at z = 0).

A bend folds around its chord. Its curved strip, `width` wide on the flex's middle plane
and centred on the chord, rolls onto a cylinder whose radius is width / angle: the strip
keeps its width as it folds, so a half fold is a gentler curve. Everything past the strip
(the child region and all that hangs off it) turns with the strip's far edge as one piece.
A positive angle folds the child towards the top (F.Cu) side.

A twist (`kind` "twist") turns instead of rolling: its strip, `width` long along the tail,
turns about the twist's drawn line (through `pivot`, along `normal`) in proportion to how
far along it a point lies, and what lies past it turns by the whole angle. A positive
angle turns right-handed about `normal`, which points along the tail to the child side.

A cone (`kind` "cone"; its strip a wedge of angle `alpha` between its sides, from `ray`
round `spin` towards the child) rolls onto a cone with its tip at `pivot`. Its handle's
angle t is how much further than `alpha` it turns about the cone's axis: psi = alpha + |t|,
with half angle beta at the tip from psi * sin(beta) = alpha, so the wedge keeps its angle
(flat: psi = alpha, the axis upright, no fold at all). A wedge point first turns back onto
the first side (about `spin`), then by its share of psi about the axis.

A wrap (a cone with `closed`: the bridge's wrap) has no chord: it bends its region (its
`parent`) all over, a point at angle gamma round its tip from the first side turning just
as a cone strip's point does, gamma unclipped, so board past the second side runs on round
the cone; once past a full turn it moves `width` towards the bottom, under the start. What
hangs off the region by another bend turns as one piece with the cone where that bend
joins it: at that bend's gamma (`wrap`, per point).

A dome reaches here as one bend per finger (`handle`: the report's bend they belong to,
`ratio`: their share of its handle's angle), each a bend whose strip starts at its chord
(its origin half a width past it) and is cut only within its finger (`local`).

The handles (`Plan.handles`) are the report's bends: one per bend, twist, cone and wrap,
one per dome for all its fingers. Angles per bend follow from them (`bend_angles`).

Points are tagged once (`tags`), from their flat position: `mask` has bit b set for every
bend b whose child side they are on (and a wrap's for its region and all that hangs off
it), `zone` is the bend whose strip they are in (-1: none), `wrap` the gamma a wrap turns
them by.
`fold` then moves them, the deepest bends first, so each bend turns what hangs off it
after that has folded itself. fold.py's Geometry Nodes group does the same on the GPU side
of Blender; this module is its reference and tests it.
"""

import math
from typing import NamedTuple

import numpy as np

MIN_ANGLE = 1e-6  # radians: flatter is flat (the Geometry Nodes group keeps the radius finite with it)
RAMP_M = 50e-6  # the slope from rigid board down to the thin flex, at their boundary
TWIST_PITCH_M = 0.5e-3  # a twist's strip is also cut along the tail this far apart
PIECE_M = 1e-3  # a bend's or twist's strip is cut at least this often, so a wide radius stays smooth
NEAR_ZONE_M = 2e-3  # board this close outside a flex zone may still be flex (see `flexness`)
UP = np.array((0.0, 0.0, 1.0))


class Bend(NamedTuple):
    origin: np.ndarray  # the chord's middle, on the flex's middle plane
    axis: np.ndarray  # along the chord
    normal: np.ndarray  # in the board's plane, towards the child side
    length: float  # of the chord
    width: float  # of the curved strip
    target: float  # radians, signed: the fold KiCad's text asks for
    step: int
    parent: int
    child: int
    kind: str = "bend"  # or "twist", "cone"
    pivot: np.ndarray = None  # a twist's: a point on the line it turns about; a cone's: its tip
    ray: np.ndarray = None  # a cone's: from its tip along its first side (the parent's)
    spin: np.ndarray = None  # a cone's: the board's normal turning `ray` towards its second side
    alpha: float = 0.0  # a cone's: the angle between its sides
    area: np.ndarray = None  # a bend drawn as its area: the shape drawn, world xy
    handle: int = 0  # the report's bend it belongs to: its handle
    ratio: float = 1.0  # its angle over its handle's
    closed: bool = False  # a wrap
    hinge: np.ndarray = None  # where it joins its parent: its chord's middle (a dome finger's origin is past it)
    local: bool = False  # curves from its chord into its child only, cut only there (a dome's finger,
    # or anything hanging off a wrap: the wrap curves the parent's side)
    finger: bool = False  # one of a dome's fingers (they share the dome's handle)


class Plan(NamedTuple):
    bends: list
    regions: list  # world xy rings, (n, 2)
    masks: np.ndarray  # per region: the bends it hangs off (bit b: bend b)
    order: list  # bend indices, deepest first
    zones: list  # flex zones, world xy rings
    transitions: list  # flex zone edges that border rigid board: ((x0, y0), (x1, y1))
    z_mid: float  # the flex's middle plane
    z_range: tuple  # the thin flex: (bottom, top), coverlay included
    handles: list = None  # per report bend: its bends' indices
    parents: list = None  # per region: the region it hangs off (None: a root)
    joins: list = None  # per region: the bend joining it to its parent (None: a root)
    wrapped: dict = None  # region -> the wrap bending it or what it hangs off
    attach: dict = None  # region -> the bend it (or what it hangs off) joins a wrapped region by
    ribbons: dict = None  # the first bend of a bend and twist sharing board -> their Ribbon


class Ribbon(NamedTuple):
    """A bend and a twist whose strips share a stretch of tail, folded as one (`ribbon`).

    Along the tail (s, from `centre` along `along`: the twist's line) the stretch from
    `start` to `end` is cut into `pieces` where either strip starts or stops. In each the
    tail turns at steady rates, about the bend's axis (`along` x up) by the bend's angle over
    its width and about `along` by the twist's angle over its length: a helix, a plain arc
    or a plain twist. Both handles turn it; `first` hangs off the other's parent side."""
    first: int  # the member nearer the root: its index stands for the whole ribbon
    second: int  # the member hanging off it
    bend: int
    twist: int
    centre: np.ndarray  # on the twist's line, on the flex's middle plane: s = 0
    along: np.ndarray  # the twist's line, towards the child
    start: float
    end: float
    join: float  # where `second` joins its parent: what lies on that parent (but off the strips) turns as there
    pieces: tuple  # (s at its start, length, bend turns it, twist turns it)


def world_xy(points_nm, origin_nm) -> np.ndarray:
    points = np.asarray(points_nm, dtype=np.float64).reshape(-1, 2)
    out = (points - np.asarray(origin_nm, dtype=np.float64)) * 1e-9
    out[:, 1] *= -1
    return out


def flex_span(layers, heights, thickness):
    """(bottom, top) z of the flex's copper: heights are copper tops, B.Cu's bottom at 0."""
    top = heights[layers[0]] if layers[0] != "B.Cu" else thickness.get("B.Cu", 0.0)
    last = layers[-1]
    bottom = 0.0 if last == "B.Cu" else heights[last] - thickness.get(last, 0.0)
    return bottom, top


def plan(report, origin_nm, heights, thickness):
    """The fold plan for a flex report, or None without flex layers or anything flex (zones,
    bends). Zones without bends still give the thin flex."""
    if not report or not report.get("layers") or not (report.get("bends") or report.get("zones")):
        return None
    if any(layer not in heights for layer in report["layers"]):
        return None
    bottom, top = flex_span(report["layers"], heights, thickness)
    coverlay = report["coverlay_nm"] * 1e-9
    z_mid = (bottom + top) / 2
    under = top - bottom + 2 * coverlay + UNDER_GAP_M  # how far a wrap's overrun passes under its start
    regions = [world_xy(region["ring"], origin_nm) for region in report["regions"]]
    bends, handles = [], []
    for handle, entry in enumerate(report["bends"]):
        members = []
        if entry.get("kind") == "dome":
            fingers = [finger for finger in entry.get("fingers", ()) if finger.get("child") is not None]
            widest = max((abs(finger["angle_deg"]) for finger in fingers), default=0.0) or 1.0
            for finger in fingers:
                members.append(len(bends))
                bends.append(_bend(dict(entry, start=finger["start"], end=finger["end"], side=finger["side"],
                                        width_nm=finger["width_nm"], angle_deg=finger["angle_deg"],
                                        child=finger["child"], kind="bend"),
                                   origin_nm, z_mid, under, handle, abs(finger["angle_deg"]) / widest, True, True))
        else:
            members.append(len(bends))
            bends.append(_bend(entry, origin_nm, z_mid, under, handle))
        handles.append(members)
    parents = [region["parent"] for region in report["regions"]]
    joins = [None] * len(regions)
    for index, bend in enumerate(bends):
        if bend.child is not None:
            joins[bend.child] = index
    masks, depths = np.zeros(len(regions), dtype=mask_dtype(len(bends))), [0] * len(regions)
    for index in range(len(regions)):
        here = index
        while here is not None and parents[here] is not None and joins[here] is not None:
            masks[index] |= 1 << joins[here]
            depths[index] += 1
            here = parents[here]
    wrapped, attach = {}, {}
    for index, bend in enumerate(bends):
        if not bend.closed or bend.parent is None:
            continue
        for region in range(len(regions)):
            path, here = [], region
            while here is not None and here != bend.parent:
                path.append(here)
                here = parents[here]
            if here == bend.parent:  # the wrapped region, or hangs off it
                masks[region] |= 1 << index
                wrapped[region] = index
                if path:
                    attach[region] = joins[path[-1]]
    for index, bend in enumerate(bends):  # off a wrap: curving on its own side only
        if not bend.closed and not bend.local and bend.kind == "bend" and bend.parent in wrapped:
            bends[index] = bend._replace(origin=bend.hinge + bend.normal * bend.width / 2, local=True)

    def depth(index):
        bend = bends[index]
        if bend.closed:
            return depths[bend.parent] + 0.5 if bend.parent is not None else 0  # after what hangs off its region
        return depths[bend.child] if bend.child is not None else 0
    order = sorted(range(len(bends)), key=lambda b: -depth(b))
    zones = [world_xy(zone, origin_nm) for zone in report.get("zones", ())]
    ribbons = {}
    for pair in report.get("ribbons", ()):
        found = _ribbon(bends, [handles[handle][0] for handle in pair])
        if found is not None:
            ribbons[found.first] = found
    return Plan(bends, regions, masks, order, zones, _transitions(zones, regions), z_mid,
                (bottom - coverlay, top + coverlay), handles, parents, joins, wrapped, attach, ribbons)


def mask_dtype(count):
    """What holds a mask of `count` bends: int64 up to 62 (a dome's fingers count one each),
    else Python ints (numpy object arrays), as wide as needed."""
    return np.int64 if count <= 62 else object


def _ribbon(bends, members):
    """The Ribbon of a bend and a twist the bridge paired (`report["ribbons"]`), or None."""
    first, second = members if bends[members[1]].parent == bends[members[0]].child else members[::-1]
    bend, twist = (first, second) if bends[first].kind == "bend" else (second, first)
    if bends[bend].kind != "bend" or bends[twist].kind != "twist":
        return None
    centre, along = bends[twist].pivot, bends[twist].normal

    def span(index):
        middle = float((bends[index].origin - centre) @ along)
        return middle - bends[index].width / 2, middle + bends[index].width / 2
    (b0, b1), (t0, t1) = span(bend), span(twist)
    start, end = min(b0, t0), max(b1, t1)
    cuts = sorted({start, end, b0, b1, t0, t1})
    pieces = tuple((a, b - a, b0 <= (a + b) / 2 <= b1, t0 <= (a + b) / 2 <= t1)
                   for a, b in zip(cuts, cuts[1:]) if b - a > 1e-9)
    join = float((bends[second].hinge - centre) @ along)
    return Ribbon(first, second, bend, twist, centre, along, start, end, join, pieces)


UNDER_GAP_M = 20e-6  # between a wrap's overrun and the start it passes under


def _bend(entry, origin_nm, z_mid, under, handle, ratio=1.0, local=False, finger=False):
    """One bend of the plan from the report's entry (or a dome finger's, `local`: its strip
    starts at its chord)."""
    start, end = world_xy([entry["start"], entry["end"]], origin_nm)
    axis = np.append(end - start, 0.0)
    length = float(np.linalg.norm(axis)) or 1e-9
    axis /= length
    # KiCad's (-dy, dx) is world (dy, dx) with y flipped back: the side turns too.
    kicad = np.array(entry["end"], dtype=np.float64) - np.array(entry["start"], dtype=np.float64)
    normal = np.array((-kicad[1], kicad[0]), dtype=np.float64) * (entry["side"] or 1)
    normal = np.array((normal[0], -normal[1], 0.0)) / (np.linalg.norm(normal) or 1.0)
    middle = (start + end) / 2
    hinge = np.array((middle[0], middle[1], z_mid))
    width = entry["width_nm"] * 1e-9
    origin = hinge + normal * width / 2 if local else hinge
    kind = entry.get("kind", "bend")
    target = math.radians(entry["angle_deg"])
    pivot = ray = spin = None
    alpha = 0.0
    closed = bool(entry.get("closed"))
    if kind == "twist":
        pivot = np.append(world_xy([entry["pivot"]], origin_nm)[0], z_mid)
    elif kind == "cone":
        pivot = np.append(world_xy([entry["apex"]], origin_nm)[0], z_mid)
        middles = [world_xy(side, origin_nm).mean(axis=0) for side in entry["sides"]]
        rays = [np.append(middle - pivot[:2], 0.0) / np.linalg.norm(middle - pivot[:2]) for middle in middles]
        ray = rays[0]
        alpha = float(entry["alpha"])
        if closed:  # the bridge orders a wrap's sides to sweep x to y in KiCad: clockwise here
            spin, width = -UP, under
        else:
            spin = np.cross(rays[0], rays[1])
            spin /= np.linalg.norm(spin)
        target = math.copysign(float(entry["psi"]) - alpha, target)  # the handle's angle: past flat
    area = world_xy(entry["area"], origin_nm) if entry.get("area") else None
    return Bend(origin, axis, normal, length, width, target, int(entry.get("step", 1)),
                entry["parent"], entry["child"], kind, pivot, ray, spin, alpha, area, handle,
                ratio, closed, hinge, local, finger)


# Stiffener materials by the words in their KiCad text: how each is drawn. The text is
# the only source (KiCad has no stiffener material); an unknown one is drawn as FR4 and
# the panel says so.
STIFFENER_WORDS = (("metal", ("stainless", "steel", "sus", "alumin", "metal")),
                   ("polyimide", ("polyimide", "kapton")),
                   ("fr4", ("fr4", "fr-4", "glass", "epoxy")))
STIFFENER_LOOKS = {"metal": "metal", "polyimide": "polyimide", "fr4": "FR4"}


def stiffener_look(material):
    """(how a stiffener of `material` is drawn: "metal", "polyimide" or "fr4", known?)."""
    words = material.casefold()
    for look, names in STIFFENER_WORDS:
        if any(name in words for name in names) or (look == "polyimide" and words.split() == ["pi"]):
            return look, True
    return "fr4", False


# --- Tags -----------------------------------------------------------------------------------

def inside(points, ring) -> np.ndarray:
    """Even-odd point in polygon for (n, 2) points against an (m, 2) ring."""
    x, y = points[:, 0:1], points[:, 1:2]
    x1, y1 = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
    crosses = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        at = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
    return np.count_nonzero(crosses & (x < at), axis=1) % 2 == 1


def edge_distance(points, segments) -> np.ndarray:
    """Distance from (n, 2) points to the nearest of (m, 2, 2) segments; inf without any."""
    if not len(segments):
        return np.full(len(points), np.inf)
    a, b = segments[:, 0], segments[:, 1]
    span = b - a
    lengths = np.maximum(np.einsum("ij,ij->i", span, span), 1e-30)
    relative = points[:, None, :] - a[None]
    t = np.clip(np.einsum("nmj,mj->nm", relative, span) / lengths, 0.0, 1.0)
    nearest = a[None] + t[..., None] * span[None]
    return np.min(np.linalg.norm(points[:, None, :] - nearest, axis=2), axis=1)


def _ring_segments(ring):
    return np.stack((ring, np.roll(ring, -1, axis=0)), axis=1)


def _transitions(zones, regions):
    """Flex zone edges with board on both sides: where rigid board meets the flex."""
    found = []
    probe = 20e-6
    for zone in zones:
        for a, b in _ring_segments(zone):
            span = b - a
            length = float(np.hypot(*span))
            if length < 1e-9:
                continue
            middle, across = (a + b) / 2, np.array((-span[1], span[0])) / length
            sides = np.array((middle + across * probe, middle - across * probe))
            on_board = np.zeros(2, dtype=bool)
            for ring in regions:
                on_board |= inside(sides, ring)
            if on_board.all():  # oriented with the zone on its left (flexness' signed side)
                flex_left = inside((middle + across * probe)[None], zone)[0]
                found.append((tuple(a), tuple(b)) if flex_left else (tuple(b), tuple(a)))
    return found


def regions_of(xy, regions) -> np.ndarray:
    """The region each point is in; points off every region take the nearest one."""
    found = np.full(len(xy), -1, dtype=np.int64)
    for index, ring in enumerate(regions):
        found[(found < 0) & inside(xy, ring)] = index
    lost = found < 0
    if lost.any():
        distances = np.stack([edge_distance(xy[lost], _ring_segments(ring)) for ring in regions], axis=1)
        found[lost] = np.argmin(distances, axis=1)
    return found


def wrap_gamma(bend, xy):
    """How far round a wrap's tip from its first side flat points lie: past its second side
    up to half way round the gap, before its first side negative."""
    relative = np.asarray(xy, dtype=np.float64).reshape(-1, 2) - bend.pivot[:2]
    across = np.cross(bend.spin, bend.ray)
    gamma = np.mod(np.arctan2(relative @ across[:2], relative @ bend.ray[:2]), 2 * math.pi)
    return np.where(gamma > bend.alpha + (2 * math.pi - bend.alpha) / 2, gamma - 2 * math.pi, gamma)


def _joined_gamma(fold_plan, index):
    """The gamma of a wrap at which bend `index` joins the region it wraps."""
    bend = fold_plan.bends[index]
    return float(wrap_gamma(fold_plan.bends[fold_plan.wrapped[bend.parent]], bend.hinge[:2])[0])


def tags(points, fold_plan):
    """(mask, zone, wrap) per point, from its flat position (see the module's docstring)."""
    xy = points[:, :2]
    region = regions_of(xy, fold_plan.regions)
    mask = fold_plan.masks[region].copy()
    zone = np.full(len(points), -1, dtype=np.int64)
    wrap = np.zeros(len(points))
    wrapped = fold_plan.wrapped or {}
    for index, bend in enumerate(fold_plan.bends):
        if bend.closed and bend.parent is not None:  # all of its region; what hangs off it as it joins
            here = region == bend.parent
            zone[here] = index
            wrap[here] = wrap_gamma(bend, xy[here])
            for hanging, joint in (fold_plan.attach or {}).items():
                if wrapped.get(hanging) == index:
                    wrap[region == hanging] = _joined_gamma(fold_plan, joint)
    for index, bend in enumerate(fold_plan.bends):
        if bend.closed:
            continue
        if bend.kind == "cone":  # its wedge, where it is drawn
            strip = inside(xy, bend.area) & ((region == bend.parent) | (region == bend.child))
        else:
            relative = xy - bend.origin[:2]
            u, v = relative @ bend.axis[:2], relative @ bend.normal[:2]
            strip = (np.abs(v) <= bend.width / 2) & (np.abs(u) <= bend.length / 2 + bend.width) & \
                ((region == bend.child) | ((region == bend.parent) & (not bend.local)))  # a finger: only past it
        zone[strip] = index
        mask[strip] = fold_plan.masks[bend.parent]
        if bend.parent in wrapped:  # on a wrapped region, or hanging off one: turns as its joint does
            wrap[strip] = _joined_gamma(fold_plan, (fold_plan.attach or {}).get(bend.parent, index))
    for first, ribbon in (fold_plan.ribbons or {}).items():  # either strip: the ribbon's, hanging off its root side
        either = (zone == first) | (zone == ribbon.second)
        zone[either] = first
        mask[either] = fold_plan.masks[fold_plan.bends[first].parent]
    return mask, zone, wrap


def thin(points, fold_plan, body_range):
    """Flat points with the flex zones' z squeezed into the thin flex: the board's top and
    what lies on it (mask, silkscreen) onto the flex's top, its bottom onto the flex's
    bottom, anything between clamped. A RAMP_M slope joins it to the rigid board."""
    share = flexness(points[:, :2], fold_plan)
    in_zone = share > 0
    if not in_zone.any():
        return points
    share = share[in_zone]
    z = points[in_zone, 2]
    out = points.copy()
    out[in_zone, 2] = z + share * (squeeze(z, fold_plan, body_range) - z)
    return out


def squeeze(z, fold_plan, body_range):
    """Heights on the flex (`thin`): the board's top and above onto the flex's top, its
    bottom and below onto the flex's bottom, anything between clamped into the flex."""
    z = np.asarray(z, dtype=np.float64)
    low, high = fold_plan.z_range
    body_low, body_high = body_range
    squeezed = np.clip(z, low, high)
    squeezed = np.where(z >= body_high - 1e-6, high + (z - body_high), squeezed)
    return np.where(z <= body_low + 1e-6, low + (z - body_low), squeezed)


def flexness(xy, fold_plan):
    """Per point: 1 in the flex, 0 on rigid board, between on the ramp at their boundary.

    What decides is the side of the rigid board's edge (the nearest transition) a point is
    on, not only the drawn zone: board just outside the zone but past that edge is flex
    too, as the web of an outline's rounded inside corner where a tail leaves the board."""
    share = np.zeros(len(xy))
    if not fold_plan.zones:
        return share
    in_zone, near = np.zeros(len(xy), dtype=bool), np.zeros(len(xy), dtype=bool)
    for zone in fold_plan.zones:
        distance = edge_distance(xy, _ring_segments(zone))
        here = inside(xy, zone) | (distance < 1e-7)
        in_zone |= here
        near |= here | (distance < NEAR_ZONE_M)
    if not fold_plan.transitions:
        share[in_zone] = 1.0
        return share
    if near.any():
        points = xy[near]
        segments = np.array(fold_plan.transitions, dtype=np.float64).reshape(-1, 2, 2)
        a, span = segments[:, 0], segments[:, 1] - segments[:, 0]
        lengths = np.hypot(span[:, 0], span[:, 1])
        relative = points[:, None, :] - a[None]
        t = np.clip(np.einsum("nmj,mj->nm", relative, span) / lengths ** 2, 0.0, 1.0)
        reach = np.linalg.norm(relative - t[..., None] * span[None], axis=2)
        nearest = np.argmin(reach, axis=1)
        rows = np.arange(len(points))
        # Signed distance to the nearest edge's line: positive on its flex side (its left).
        side = (span[nearest, 0] * relative[rows, nearest, 1]
                - span[nearest, 1] * relative[rows, nearest, 0]) / lengths[nearest]
        share[near] = np.clip(side / RAMP_M, 0.0, 1.0)
    return share


def cut_planes(fold_plan, per_degree=10.0):
    """(point, normal) planes the flat meshes are cut along: across every bend's strip, so it
    can curve, and along the flex's edges with rigid board, for the step down to thin flex."""
    planes = []
    for bend in fold_plan.bends:
        if bend.closed:  # all round its tip: each plane through it cuts both ways, so half a turn of them
            step = math.radians(per_degree / 2) * bend.alpha / (bend.alpha + abs(bend.target))
            for k in range(math.ceil(math.pi / step)):
                generator = rotate(bend.ray[None], np.zeros(3), bend.spin, k * step)[0]
                planes.append((bend.pivot.copy(), np.cross(bend.spin, generator)))
            continue
        if bend.kind == "cone":  # a fan of planes through its tip, one every few degrees of its turn
            count = max(8, math.ceil(math.degrees(bend.alpha + abs(bend.target)) / (per_degree / 2)))
            for k in range(count + 1):
                generator = rotate(bend.ray[None], np.zeros(3), bend.spin, bend.alpha * k / count)[0]
                planes.append((bend.pivot.copy(), np.cross(bend.spin, generator)))
            continue
        twist = bend.kind == "twist"
        # A twisted strip is no cylinder: finer across it, and cut along it too (TWIST_PITCH_M).
        count = max(12 if twist else 6, math.ceil(abs(math.degrees(bend.target)) / (per_degree / 3 if twist
                                                                                      else per_degree)),
                    math.ceil(bend.width / PIECE_M))
        limit = (fold_plan.regions[bend.child],) if bend.local else ()  # a dome's finger: only across it
        for k in range(count + 1):
            planes.append((bend.origin + bend.normal * (-bend.width / 2 + bend.width * k / count), bend.normal,
                           *limit))
        if twist:
            lanes = max(4, math.ceil(bend.length / TWIST_PITCH_M))
            planes += [(bend.origin + bend.axis * (-bend.length / 2 + bend.length * k / lanes), bend.axis)
                       for k in range(1, lanes)]
    for a, b in fold_plan.transitions:
        span = np.array(b) - np.array(a)
        across = np.array((-span[1], span[0], 0.0)) / np.hypot(*span)
        for offset in (0.0, RAMP_M, -RAMP_M):
            planes.append((np.array((a[0], a[1], 0.0)) + across * offset, across))
    return planes


# --- The fold -------------------------------------------------------------------------------

def _local(bend, points):
    relative = points - bend.origin
    return relative @ bend.axis, relative @ bend.normal, relative @ UP


def _world(bend, u, v, h):
    return bend.origin + np.outer(u, bend.axis) + np.outer(v, bend.normal) + np.outer(h, UP)


def _radius(bend, angle):
    turn = max(abs(angle), MIN_ANGLE)
    return turn, bend.width / turn, (1.0 if angle >= 0 else -1.0)


def rotate(points, centre, axis, angles):
    """Points turned right-handed about the line through `centre` along unit `axis`, by
    `angles` (one, or one per point): Rodrigues' formula."""
    relative = np.asarray(points, dtype=np.float64) - centre
    angles = np.broadcast_to(np.asarray(angles, dtype=np.float64), (len(relative),))[:, None]
    cosine, sine = np.cos(angles), np.sin(angles)
    return (centre + relative * cosine + np.cross(axis, relative) * sine
            + np.outer(relative @ axis, axis) * (1 - cosine))


def strip(bend, points, angle):
    """Points of the curved strip, from their flat positions, rolled onto the cylinder (or
    for a twist, turned by their share of its angle)."""
    if abs(angle) < MIN_ANGLE:
        return np.array(points, dtype=np.float64)
    if bend.kind == "twist":
        _, v, _ = _local(bend, points)
        share = np.clip((v + bend.width / 2) / bend.width, 0.0, 1.0)
        return rotate(points, bend.pivot, bend.normal, angle * share)
    if bend.kind == "cone":
        relative = np.asarray(points, dtype=np.float64) - bend.pivot
        across = np.cross(bend.spin, bend.ray)
        gamma = np.clip(np.arctan2(relative @ across, relative @ bend.ray), 0.0, bend.alpha)
        axis, psi, turn = _cone(bend, angle)
        return rotate(rotate(points, bend.pivot, bend.spin, -gamma), bend.pivot, axis, turn * gamma * psi / bend.alpha)
    u, v, h = _local(bend, points)
    turn, radius, sign = _radius(bend, angle)
    phi = (np.clip(v, -bend.width / 2, bend.width / 2) + bend.width / 2) / radius
    across = -bend.width / 2 + (radius - sign * h) * np.sin(phi)
    up = sign * radius * (1 - np.cos(phi)) + h * np.cos(phi)
    return _world(bend, u, across, up)


def rigid(bend, points, angle):
    """Points past the strip (wherever earlier folds put them), turned with its far edge."""
    if abs(angle) < MIN_ANGLE:
        return np.array(points, dtype=np.float64)
    if bend.kind == "twist":
        return rotate(points, bend.pivot, bend.normal, angle)
    if bend.kind == "cone":
        axis, psi, turn = _cone(bend, angle)
        return rotate(rotate(points, bend.pivot, bend.spin, -bend.alpha), bend.pivot, axis, turn * psi)
    u, v, h = _local(bend, points)
    turn, radius, sign = _radius(bend, angle)
    beyond = v - bend.width / 2
    across = -bend.width / 2 + (radius - sign * h) * math.sin(turn) + beyond * math.cos(turn)
    up = sign * radius * (1 - math.cos(turn)) + h * math.cos(turn) + sign * beyond * math.sin(turn)
    return _world(bend, u, across, up)


RIBBON_TURN = 1e-6  # radians a metre along its line: a ribbon piece always turns a little, so its axis is defined


def ribbon(ribbon_, points, s, bend_angle, twist_angle, bends):
    """Points of a Ribbon's stretch (from their flat positions) or hanging off it (wherever
    earlier folds put them), each turned as the tail is at its `s`: the pieces after s left
    as they are, the one it is in up to s, those before it whole, last to first. A piece
    turns about `w` = its rates (rad/m) for its length d: a point first steps back along
    the tail by d onto the piece's start, then turns about w through the start by |w| d,
    and the start itself moves along the helix: the integral of the turning line over d."""
    kappa = bend_angle / bends[ribbon_.bend].width
    tau = twist_angle / bends[ribbon_.twist].width
    across = np.cross(ribbon_.along, UP)  # turning the tail about it lifts it towards the top
    out = np.array(points, dtype=np.float64)
    s = np.asarray(s, dtype=np.float64)
    for start, length, bending, twisting in reversed(ribbon_.pieces):
        rates = across * (kappa if bending else 0.0) + ribbon_.along * ((tau if twisting else 0.0) + RIBBON_TURN)
        speed = float(np.linalg.norm(rates))
        axis = rates / speed
        travel = np.clip(s - start, 0.0, length)
        theta = speed * travel
        parallel = (ribbon_.along @ axis) * axis
        square = ribbon_.along - parallel
        helix = (np.outer(travel, parallel) + np.outer(np.sin(theta) / speed, square)
                 + np.outer((1 - np.cos(theta)) / speed, np.cross(axis, square)))
        out = rotate(out - np.outer(travel, ribbon_.along), ribbon_.centre + start * ribbon_.along, axis, theta) + helix
    return out


def _cone(bend, angle):
    """(axis, psi, turn sign) of a cone at its handle's `angle` (see the module)."""
    psi = bend.alpha + abs(angle)
    sine = min(1.0, bend.alpha / psi)
    towards = UP if angle >= 0 else -UP  # which face it folds towards
    axis = bend.ray * math.sqrt(1.0 - sine * sine) + towards * sine
    return axis / np.linalg.norm(axis), psi, (1.0 if bend.spin @ towards > 0 else -1.0)


WRAP_RAMP = 0.05  # radians round the cone over which a wrap's overrun steps under its start


def wrapped(bend, points, gamma, angle):
    """Points of a wrap's region (or hanging off it), each turned as the cone turns the
    flat point at its `gamma`: back onto the first side, then round the axis; past a full
    turn (either way), `width` towards the bottom first (under the other end)."""
    if abs(angle) < MIN_ANGLE:
        return np.array(points, dtype=np.float64)
    axis, psi, turn = _cone(bend, angle)
    gamma = np.broadcast_to(np.asarray(gamma, dtype=np.float64), (len(points),))
    round_ = gamma * psi / bend.alpha
    past = np.clip((round_ - 2 * math.pi) / WRAP_RAMP, 0.0, 1.0) + np.clip(-round_ / WRAP_RAMP, 0.0, 1.0)
    under = past * bend.width
    moved = np.asarray(points, dtype=np.float64) - np.outer(under, UP)
    return rotate(rotate(moved, bend.pivot, bend.spin, -gamma), bend.pivot, axis, turn * round_)


def fold(points, mask, zone, angles, fold_plan, wrap=None):
    """Folded positions of flat `points` (n, 3) with their `tags`, each bend at `angles`."""
    out = np.array(points, dtype=np.float64)
    wrap = np.zeros(len(out)) if wrap is None else np.asarray(wrap, dtype=np.float64)
    ribbons = fold_plan.ribbons or {}
    seconds = {ribbon.second for ribbon in ribbons.values()}
    for index in fold_plan.order:
        bend = fold_plan.bends[index]
        beyond = (mask >> index) & 1 == 1
        if index in seconds:  # folded with its ribbon's first
            continue
        if index in ribbons:
            ribbon_ = ribbons[index]
            in_strip = zone == index
            past = (mask >> ribbon_.second) & 1 == 1  # past both
            flat = np.asarray(points, dtype=np.float64)
            s = np.where(in_strip, np.clip((flat - ribbon_.centre) @ ribbon_.along, ribbon_.start, ribbon_.end),
                         np.where(past, ribbon_.end, ribbon_.join))
            moving = in_strip | beyond
            if moving.any():
                start = np.where(in_strip[:, None], flat, out)
                out[moving] = ribbon(ribbon_, start[moving], s[moving], angles[ribbon_.bend], angles[ribbon_.twist],
                                     fold_plan.bends)
            continue
        if bend.closed:
            if beyond.any():
                out[beyond] = wrapped(bend, out[beyond], wrap[beyond], angles[index])
            continue
        if beyond.any():
            out[beyond] = rigid(bend, out[beyond], angles[index])
        in_strip = zone == index
        if in_strip.any():
            out[in_strip] = strip(bend, np.asarray(points, dtype=np.float64)[in_strip], angles[index])
    return out


def _frames(fold_plan, angles, points, mask, zone, wrap):
    """(n, 4, 4) world transforms of small pieces of board at flat `points`, folded with
    their tags: all in one fold."""
    step = 1e-4
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    count = len(points)
    basis = (points[:, None, :] + np.vstack((np.zeros(3), np.eye(3) * step))[None]).reshape(-1, 3)
    moved = fold(basis, np.repeat(mask, 4), np.repeat(zone, 4), angles, fold_plan,
                 np.repeat(wrap, 4)).reshape(count, 4, 3)
    x = moved[:, 1] - moved[:, 0]
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    y = moved[:, 2] - moved[:, 0]
    y -= x * np.einsum("ij,ij->i", x, y)[:, None]
    y /= np.linalg.norm(y, axis=1, keepdims=True)
    matrices = np.tile(np.eye(4), (count, 1, 1))
    matrices[:, :3, 0], matrices[:, :3, 1], matrices[:, :3, 2] = x, y, np.cross(x, y)
    matrices[:, :3, 3] = moved[:, 0] - np.einsum("nij,nj->ni", matrices[:, :3, :3], points)
    return matrices


def _frame(fold_plan, angles, point, mask, zone, wrap):
    return _frames(fold_plan, angles, point, np.array([mask]), np.array([zone]), np.array([wrap]))[0]


def point_matrices(points, angles, fold_plan):
    """The 4x4 world transforms that fold what sits at flat `points` (parts' origins): each
    its region's, or where it lies in a strip or on a wrap, the board's there."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    mask, zone, wrap = tags(points, fold_plan)
    return _frames(fold_plan, angles, points, mask, zone, wrap)


def point_matrix(point, angles, fold_plan):
    return point_matrices(point, angles, fold_plan)[0]


def region_matrix(region, angles, fold_plan):
    """The 4x4 world transform that folds everything rigidly in `region` (components)."""
    basis = np.array(((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)), dtype=np.float64)
    count = len(basis)
    moved = fold(basis, np.full(count, fold_plan.masks[region]), np.full(count, -1), angles, fold_plan)
    matrix = np.eye(4)
    matrix[:3, 3] = moved[0]
    matrix[:3, :3] = (moved[1:] - moved[0]).T
    return matrix


def hinge_matrix(index, angles, fold_plan):
    """The 4x4 world transform of where bend `index` joins its parent, as folded (its handle)."""
    bend = fold_plan.bends[index]
    if bend.parent is None:
        return np.eye(4)
    wrapped_by = (fold_plan.wrapped or {}).get(bend.parent)
    if wrapped_by is None or bend.closed:
        return region_matrix(bend.parent, angles, fold_plan)
    gamma = _joined_gamma(fold_plan, (fold_plan.attach or {}).get(bend.parent, index))
    return _frame(fold_plan, angles, bend.hinge, fold_plan.masks[bend.parent], -1, gamma)


def bend_angles(fold_plan, handle_angles):
    """Each bend's angle from its handle's."""
    return [handle_angles[bend.handle] * bend.ratio for bend in fold_plan.bends]


def handle_targets(fold_plan):
    """Each handle's angle when folded as KiCad says."""
    return _handle_angles(fold_plan, [bend.target for bend in fold_plan.bends])


def handle_angles_at(fold_plan, progress, steps=None):
    """Each handle's angle at `progress` (`angles_at`)."""
    return _handle_angles(fold_plan, angles_at(fold_plan, progress, steps))


def _handle_angles(fold_plan, angles):
    """Each handle's angle from its bends' (`bend_angles` the other way). A bend of ratio 0
    (a dome's finger with no angle) stays flat whatever its handle does, so it says nothing."""
    out = []
    for members in fold_plan.handles:
        turning = next((k for k in members if fold_plan.bends[k].ratio), None)
        out.append(angles[turning] / fold_plan.bends[turning].ratio if turning is not None else 0.0)
    return out


def steps(fold_plan):
    """The folding sequence: [(step number, [bend indices])], in order."""
    numbers = sorted({bend.step for bend in fold_plan.bends})
    return [(number, [k for k, bend in enumerate(fold_plan.bends) if bend.step == number]) for number in numbers]


def handle_name(fold_plan, handle):
    """A handle as the panel names it: "Bend 3", "Dome 2" (the report's numbering)."""
    bend = fold_plan.bends[fold_plan.handles[handle][0]]
    kind = "dome" if bend.finger else ("wrap" if bend.closed else bend.kind)
    return f"{kind.capitalize()} {handle + 1}"


def pieces(fold_plan, points, zone):
    """Per point: the piece of board it belongs to while folding, ("strip", bend) in a bend's
    strip, else ("region", region index)."""
    region = regions_of(points[:, :2], fold_plan.regions)
    return [("strip", int(z)) if z >= 0 else ("region", int(r)) for z, r in zip(zone, region)]


def face_pieces(fold_plan, points, zone, faces):
    """Per face (a tuple of point indices): ("strip", bend) when every corner is tagged
    for that strip, else ("region", r) of its centre. A corner on the cut between a strip
    and a region carries the strip's tag; the region's face beside it is still the region's."""
    if not len(faces):
        return []
    centres = np.array([points[list(face), :2].mean(axis=0) for face in faces])
    regions = regions_of(centres, fold_plan.regions)
    out = []
    for face, region in zip(faces, regions):
        zones = zone[list(face)]
        out.append(("strip", int(zones[0])) if zones[0] >= 0 and (zones == zones[0]).all()
                   else ("region", int(region)))
    return out


def apart(fold_plan, first, second):
    """Two pieces that may not touch: not the same, nor a strip and a region it joins. The
    regions either side of a bend meet only through its strip: a flap folded back onto the
    board runs into it."""
    if first == second:
        return False
    joined = set()
    wrapped = fold_plan.wrapped or {}
    for index, bend in enumerate(fold_plan.bends):
        pairs = [(("strip", index), ("region", bend.parent)), (("strip", index), ("region", bend.child))]
        if not bend.closed and bend.parent in wrapped and wrapped[bend.parent] != index:
            wrap = ("strip", wrapped[bend.parent])  # its parent is all wrap: it joins that
            pairs += [(("strip", index), wrap), (("region", bend.child), wrap)]
        if index in (fold_plan.ribbons or {}):  # its strips are one, and reach the far region too
            pairs.append((("strip", index), ("region", fold_plan.bends[fold_plan.ribbons[index].second].child)))
        for a, b in pairs:
            joined |= {(a, b), (b, a)}
    return (first, second) not in joined


def piece_name(fold_plan, piece):
    """A piece as the panel names it: "bend 2", "the part past bend 2", "the board"."""
    kind, index = piece
    if kind == "strip":
        bend = fold_plan.bends[index]
        name = handle_name(fold_plan, bend.handle).lower() if fold_plan.handles else f"bend {index + 1}"
        if bend.finger:
            name += f" (finger {fold_plan.handles[bend.handle].index(index) + 1})"
        return name
    joint = next((k for k, bend in enumerate(fold_plan.bends) if bend.child == index), None)
    if joint is None:
        return "the board"
    return f"the part past {piece_name(fold_plan, ('strip', joint))}"


def angles_at(fold_plan, progress, steps=None):
    """Each bend's angle at `progress` (0 flat, 1 folded) through the step sequence: the
    steps fold one after another, each over an equal share of the progress."""
    order = sorted({bend.step for bend in fold_plan.bends}) if steps is None else steps
    if not order:
        return []
    share = 1.0 / len(order)
    out = []
    for bend in fold_plan.bends:
        start = order.index(bend.step) * share
        out.append(bend.target * min(1.0, max(0.0, (progress - start) / share)))
    return out
