"""Planes and return paths on a board snapshot, for findings.

The stackup gives each copper layer its nearest copper layers above and below, with the
dielectric between (`copper_neighbours`). A plane is one net's zone fill on one layer
(`plane`): polygons as rings, the outer ring first, then its holes. A signal sees the
plane with its own antipads filled (`plane_under`): a hole around a via or plated hole of
the signal's net, or its differential-pair partner's, that closes within
`MAX_ANTIPAD_NM` of it is that via's clearance, not a void. `coverage` cuts a track's
centre line where it crosses the plane's edges and tests each piece for copper under it;
`edges_near` gives the plane's edges around a gap as polylines; `islands` the fill pieces
of a net and the vias, plated holes, pads and tracks that tie them across layers;
`return_vias` where a signal via's return current can change layer. A via's or plated
hole's barrel (`barrel`) runs over the layers of its span and is used where its net meets
it; the rest past the last used layer is a stub, its length from the stackup
(`barrel_length`). A `Line` is a closed ring followed along (a pour's or the board's
outline): `stitches` are the vias of a net along it, `gaps` the stretches between them.
Exact geometry in numpy (segment crossings, even-odd tests):
no rasters and no shapely, the bridge runs on KiCad's Python. Values are nanometres in
KiCad's board frame (y down), as in `model`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from . import model, protocol
from .board_index import (BoardIndex, _inside, _point_segments, copper_rank, gap, has_copper, on_layer, polygon_area,
                          shape_of)
from .geometry import sample_arc
from .model import Point
from .selection import diff_pair_partner

MAX_ANTIPAD_NM = 2_000_000  # a hole closing within this of a via of the signal's own net is its antipad
TOUCH_NM = 5_000  # copper this close is joined: a fill meets a via or pad at its edge
MIN_STRETCH_NM = 1_000  # shorter pieces of a centre line are rounding where it crosses an edge
JOIN_NM = 1_000  # stretch ends on this grid are one point (`joined`)
_BLOCK = 1 << 20  # point/segment pairs computed at once (memory stays small)

Rings = tuple[np.ndarray, ...]  # one polygon: outer ring, then its holes, each (k, 2)


# stackup

@dataclass(frozen=True)
class Neighbour:
    layer: str
    distance_nm: int  # dielectric between the two copper faces


def copper_order(snapshot: model.BoardSnapshot) -> tuple[tuple[str, ...], tuple[int, ...], bool]:
    """(copper layers top first, dielectric between each neighbouring pair, exact).
    Exact from the stackup's thicknesses; otherwise the layers evenly spread over the
    board thickness the viewer shows (`protocol.layer_heights_nm`), and `exact` is False."""
    stack = snapshot.stackup.layers
    copper = [i for i, entry in enumerate(stack) if entry.type == "copper"]
    names = tuple(stack[i].name for i in copper)
    used = protocol.copper_layers(snapshot)
    if copper and used <= set(names) and all(stack[i].thickness_nm for i in range(copper[0], copper[-1] + 1)):
        gaps = tuple(sum(stack[k].thickness_nm for k in range(a + 1, b)) for a, b in zip(copper, copper[1:]))
        return names, gaps, True
    names = tuple(sorted((name for name in used if copper_rank(name) is not None), key=copper_rank))
    _, thickness, _ = protocol.layer_heights_nm(snapshot)
    return names, (thickness // max(1, len(names) - 1),) * max(0, len(names) - 1), False


def copper_neighbours(snapshot: model.BoardSnapshot) -> dict[str, tuple[Neighbour | None, Neighbour | None]]:
    """(above, below) nearest copper layer of each copper layer, None at the outside."""
    names, gaps, _ = copper_order(snapshot)
    return _neighbours(names, gaps)


def _neighbours(names, gaps):
    result = {}
    for index, name in enumerate(names):
        above = Neighbour(names[index - 1], gaps[index - 1]) if index else None
        below = Neighbour(names[index + 1], gaps[index]) if index + 1 < len(names) else None
        result[name] = (above, below)
    return result


def next_to(snapshot: model.BoardSnapshot, layer: str) -> tuple[str, ...]:
    """The copper layers right above and below `layer` (a plane's signal layers)."""
    return tuple(side.layer for side in copper_neighbours(snapshot).get(layer, ()) if side)


# plane copper

def _rings(polygon) -> Rings:
    if not polygon or len(polygon[0]) < 3:
        return ()
    return tuple(np.asarray(ring, dtype=float) for ring in polygon if len(ring) >= 3)


def plane(index: BoardIndex, net: str, layer: str) -> tuple[Rings, ...]:
    """The zone fills of `net` on `layer`, one polygon per fill piece."""
    return tuple(rings for zone in index.net_items(net, layer) if isinstance(zone, model.ZoneFill)
                 for rings in map(_rings, zone.polygons) if rings)


def outline(snapshot: model.BoardSnapshot) -> tuple[Rings, ...]:
    """The board outline's polygons."""
    return tuple(rings for polygon in snapshot.outline.polygons for rings in (_rings(polygon),) if rings)


def largest(polygons: tuple[Rings, ...]) -> Rings:
    """The polygon whose outer ring holds the most area."""
    return max(polygons, key=lambda rings: polygon_area(rings[:1]))


def signal_nets(index: BoardIndex, nets) -> frozenset[str]:
    """`nets` with their differential-pair partners (KiCad's naming rule)."""
    return frozenset(name for net in nets if net for name in (net, diff_pair_partner(net, index.nets)) if name)


def own_holes(index: BoardIndex, nets, layer: str) -> np.ndarray:
    """Positions (n, 2) of the vias of `nets` through `layer` and of their plated holes."""
    points = [record.pos for net in nets for record in index.net_items(net)
              if (isinstance(record, model.Via) and on_layer(record, layer)) or
              (isinstance(record, model.Pad) and record.drill and min(record.drill) > 0)]
    return np.array(points, dtype=float).reshape(-1, 2)


def fill_antipads(polygons: tuple[Rings, ...], holes: np.ndarray) -> tuple[Rings, ...]:
    """The polygons without the holes that are antipads of `holes`: a hole ring around
    one of them whose every corner lies within `MAX_ANTIPAD_NM` of one it holds. A void
    that runs further (a slot, a split) stays."""
    if not len(holes):
        return polygons
    result = []
    for rings in polygons:
        kept = [rings[0]]
        for ring in rings[1:]:
            low, high = ring.min(axis=0), ring.max(axis=0)
            held = holes[np.all((holes >= low) & (holes <= high), axis=1)]
            held = held[_inside(held, (ring,))] if len(held) else held
            if len(held):
                reach = np.hypot(*(ring[:, None, :] - held[None, :, :]).transpose(2, 0, 1)).min(axis=1)
                if reach.max() <= MAX_ANTIPAD_NM:
                    continue
            kept.append(ring)
        result.append(tuple(kept))
    return tuple(result)


def plane_under(index: BoardIndex, net: str, layer: str, records) -> tuple[Rings, ...]:
    """The plane as the records' signals see it: `net`'s fill on `layer` with the
    antipads of their nets and pair partners filled."""
    nets = signal_nets(index, {getattr(record, "net", "") for record in records})
    return fill_antipads(plane(index, net, layer), own_holes(index, nets, layer))


def _edges(polygons) -> np.ndarray:
    """Every ring edge of the polygons (n, 4: x1, y1, x2, y2)."""
    parts = [np.hstack([ring, np.roll(ring, -1, axis=0)]) for rings in polygons for ring in rings]
    return np.vstack(parts) if parts else np.zeros((0, 4))


def covered(points: np.ndarray, polygons: tuple[Rings, ...]) -> np.ndarray:
    """Which points (P, 2) lie in plane copper: in any polygon, even-odd within each."""
    inside = np.zeros(len(points), dtype=bool)
    for rings in polygons:
        low, high = rings[0].min(axis=0), rings[0].max(axis=0)
        pick = np.flatnonzero(~inside & np.all((points >= low) & (points <= high), axis=1))
        if len(pick):
            inside[pick] = _in_rings(points[pick], rings)
    return inside


def _in_rings(points: np.ndarray, rings: Rings) -> np.ndarray:
    """Even-odd test of points (P, 2) against one polygon's rings. A ring only sees the
    points level with it and left of its right end: a point's crossings lie to its right."""
    inside = np.zeros(len(points), dtype=bool)
    for ring in rings:
        low, high = ring.min(axis=0), ring.max(axis=0)
        pick = np.flatnonzero((points[:, 1] >= low[1]) & (points[:, 1] <= high[1]) & (points[:, 0] <= high[0]))
        if len(pick):
            inside[pick] ^= _inside(points[pick], (ring,))
    return inside


# coverage of a centre line

@dataclass(frozen=True)
class Stretch:
    """A straight piece of a track's centre line, with plane copper under it or not."""
    id: str
    layer: str
    width: int
    start: Point
    end: Point
    covered: bool

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def middle(self) -> Point:
        return ((self.start[0] + self.end[0]) // 2, (self.start[1] + self.end[1]) // 2)


def centre_lines(record) -> list[tuple[Point, Point]]:
    """A track's centre line as one segment, an arc's as its sampled chords."""
    if isinstance(record, model.Arc):
        points = sample_arc(record.start, record.mid, record.end)
        return list(zip(points, points[1:])) or [(record.start, record.end)]
    return [(record.start, record.end)]


def on_centre_line(record, point) -> tuple[Point, tuple[float, float]]:
    """The spot of a track's or arc's centre line nearest `point`, and the line's
    direction there (a unit vector, from its start towards its end)."""
    best = None
    for a, b in centre_lines(record):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = min(1.0, max(0.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2)) if length2 else 0.0
        spot = (a[0] + t * dx, a[1] + t * dy)
        apart = math.dist(spot, point)
        if best is None or apart < best[0]:
            best = (apart, spot, (dx, dy))
    _, spot, (dx, dy) = best
    length = math.hypot(dx, dy) or 1.0
    return _round(spot), (dx / length, dy / length)


def _cuts(a: Point, b: Point, edges: np.ndarray) -> np.ndarray:
    """Where the segment a-b crosses or touches `edges`, as fractions along it (edges
    lying on it give none: the pieces either side are tested instead)."""
    (ax, ay), (bx, by) = a, b
    edges = edges[(np.maximum(edges[:, 0], edges[:, 2]) >= min(ax, bx)) &
                  (np.minimum(edges[:, 0], edges[:, 2]) <= max(ax, bx)) &
                  (np.maximum(edges[:, 1], edges[:, 3]) >= min(ay, by)) &
                  (np.minimum(edges[:, 1], edges[:, 3]) <= max(ay, by))]
    rx, ry = bx - ax, by - ay
    sx, sy = edges[:, 2] - edges[:, 0], edges[:, 3] - edges[:, 1]
    qx, qy = edges[:, 0] - ax, edges[:, 1] - ay
    across = rx * sy - ry * sx
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (qx * sy - qy * sx) / across
        u = (qx * ry - qy * rx) / across
    return t[(across != 0) & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)]


def coverage(records, polygons: tuple[Rings, ...]) -> list[Stretch]:
    """The tracks and arcs of `records` cut into stretches with and without plane copper
    under their centre lines, in order along each. A piece shorter than `MIN_STRETCH_NM`
    takes the state of the piece before it (at a line's start, the one after)."""
    edges = _edges(polygons)
    lines, middles = [], []  # per line: record and its pieces (start, end); every piece's middle
    for record in records:
        for a, b in centre_lines(record):
            if a == b:
                continue
            cuts = np.unique(np.concatenate([[0.0, 1.0], _cuts(a, b, edges)]))
            ends = [(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])) for t in cuts]
            lines.append((record, list(zip(ends, ends[1:]))))
            middles += [((start[0] + end[0]) / 2, (start[1] + end[1]) / 2) for start, end in lines[-1][1]]
    inside = iter(covered(np.array(middles, dtype=float).reshape(-1, 2), polygons).tolist())
    stretches: list[Stretch] = []
    for record, pieces in lines:
        states = [next(inside) for _ in pieces]
        tiny = [math.dist(start, end) < MIN_STRETCH_NM for start, end in pieces]
        for i in range(1, len(pieces)):  # a tiny piece takes the state of the one before it
            if tiny[i]:
                states[i] = states[i - 1]
        lead = next((i for i, small in enumerate(tiny) if not small), 0)
        for i in range(lead - 1, -1, -1):  # tiny pieces at the start: the one after
            states[i] = states[i + 1]
        start = 0
        for i in range(1, len(pieces) + 1):
            if i == len(pieces) or states[i] != states[start]:
                stretches.append(Stretch(record.id, record.layer, int(record.width), _round(pieces[start][0]),
                                         _round(pieces[i - 1][1]), states[start]))
                start = i
    return [stretch for stretch in stretches if stretch.start != stretch.end]


def _round(point) -> Point:
    return (int(round(point[0])), int(round(point[1])))


def joined(stretches) -> list[tuple[float, list[Stretch]]]:
    """Stretches joined where one ends where another starts on the same layer (on the
    same JOIN_NM grid point), with their total length, longest first: the gaps along a route."""
    union = _Union(len(stretches))
    ends: dict[tuple, int] = {}
    for i, stretch in enumerate(stretches):
        for x, y in (stretch.start, stretch.end):
            key = (round(x / JOIN_NM), round(y / JOIN_NM), stretch.layer)
            if key in ends:
                union.join(ends[key], i)
            else:
                ends[key] = i
    groups: dict[int, list[Stretch]] = defaultdict(list)
    for i, stretch in enumerate(stretches):
        groups[union.find(i)].append(stretch)
    found = [(sum(stretch.length for stretch in group), group) for group in groups.values()]
    return sorted(found, key=lambda item: -item[0])


# plane edges

def _clip(ring: np.ndarray, box) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Each edge of a ring clipped to a box (Liang-Barsky): (kept, t0, t1) along it."""
    a, d = ring, np.roll(ring, -1, axis=0) - ring
    t0, t1 = np.zeros(len(a)), np.ones(len(a))
    kept = np.ones(len(a), dtype=bool)
    for p, q in ((-d[:, 0], a[:, 0] - box[0]), (d[:, 0], box[2] - a[:, 0]),
                 (-d[:, 1], a[:, 1] - box[1]), (d[:, 1], box[3] - a[:, 1])):
        with np.errstate(divide="ignore", invalid="ignore"):
            t = q / p
        kept &= ~((p == 0) & (q < 0))
        t0 = np.where(p < 0, np.maximum(t0, t), t0)
        t1 = np.where(p > 0, np.minimum(t1, t), t1)
    return kept & (t0 <= t1), t0, t1


def _merged(boxes: list[list[float]]) -> list[list[float]]:
    """Boxes that overlap joined into one, until none do."""
    boxes = [list(box) for box in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes


def edges_near(polygons: tuple[Rings, ...], boxes, reach: float) -> list[list[Point]]:
    """The polygons' edges within the boxes (x0, y0, x1, y1) grown by `reach`, as
    polylines: what of the plane's outline and holes lies around a gap."""
    lines: list[list[Point]] = []
    for box in _merged([[box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach] for box in boxes]):
        for rings in polygons:
            for ring in rings:
                low, high = ring.min(axis=0), ring.max(axis=0)
                if low[0] > box[2] or high[0] < box[0] or low[1] > box[3] or high[1] < box[1]:
                    continue
                lines += _polylines(ring, box)
    return lines


def _polylines(ring: np.ndarray, box) -> list[list[Point]]:
    kept, t0, t1 = _clip(ring, box)
    if not kept.any():
        return []
    d = np.roll(ring, -1, axis=0) - ring
    goes_on = kept & np.roll(kept, 1) & (t0 == 0) & (np.roll(t1, 1) == 1)  # edge i continues edge i-1
    if goes_on.all():  # the whole ring is in the box
        return [[_round(point) for point in (*ring, ring[0])]]
    first = int(np.flatnonzero(~goes_on)[0])
    lines: list[list[Point]] = []
    for i in np.roll(np.arange(len(ring)), -first):
        if not kept[i]:
            continue
        end = _round(ring[i] + t1[i] * d[i])
        if goes_on[i] and lines:
            lines[-1].append(end)
        else:
            lines.append([_round(ring[i] + t0[i] * d[i]), end])
    return lines


# vias

def plated(record) -> bool:
    """A pad with a plated hole: a barrel through the board."""
    return isinstance(record, model.Pad) and bool(record.drill) and min(record.drill) > 0 and has_copper(record)


def barrel(index: BoardIndex, record) -> tuple[str, ...]:
    """The copper layers a via's barrel passes (its span), or a plated hole's (every copper
    layer), top to bottom."""
    return index.layers_of(record) if isinstance(record, model.Via) else index.copper_layers


def via_layers(index: BoardIndex, via) -> tuple[str, ...]:
    """The copper layers where a via (or plated hole) meets other copper of its net
    (tracks, arcs, pads, zones, shapes, a via stacked on it), top to bottom: the layers it
    carries its signal between. A plated hole meets it only where it has a land."""
    used = []
    for layer in barrel(index, via):
        others = [record for record in index.net_items(via.net, layer) if record is not via] if via.net else []
        if not others:
            continue
        if isinstance(via, model.Via):
            met = index.nearest(via.pos, others, layer, max_distance=via.diameter / 2) is not None
        else:
            found = index.closest((via,), others, layer) if layer in via.polygons else None
            met = found is not None and found[0].distance <= TOUCH_NM
        if met:
            used.append(layer)
    return tuple(used)


def reference_at(index: BoardIndex, layer: str, point: Point, nets) -> tuple[str, str] | None:
    """(net, layer) of the plane a signal on `layer` returns on at `point`: fill of a net
    not in `nets` there (their antipads filled), on the nearest copper layer above or
    below (the upper one on a tie). None when neither has any there."""
    sides = sorted((side for side in copper_neighbours(index.snapshot).get(layer, ()) if side),
                   key=lambda side: (side.distance_nm, copper_rank(side.layer)))
    spot = np.array([point], dtype=float)
    for side in sides:
        fills = sorted({record.net for record in index.layer_items(side.layer)
                        if isinstance(record, model.ZoneFill) and record.net and record.net not in nets})
        holes = own_holes(index, nets, side.layer)
        for net in fills:
            if covered(spot, fill_antipads(plane(index, net, side.layer), holes))[0]:
                return net, side.layer
    return None


def reference_across(index: BoardIndex, layer: str, a, b, nets) -> tuple[tuple | None, tuple | None, Point] | None:
    """(plane at `a`, plane at `b`, where the line a-b crosses that plane's edge, the
    crossing nearest its middle) for a signal on `layer` (`reference_at`; None for no
    plane), or None when both ends see the same plane."""
    a, b = _round(a), _round(b)
    before, after = reference_at(index, layer, a, nets), reference_at(index, layer, b, nets)
    if before == after:
        return None
    net, plane_layer = before or after
    edges = _edges(fill_antipads(plane(index, net, plane_layer), own_holes(index, nets, plane_layer)))
    cuts = _cuts(a, b, edges)
    t = float(cuts[np.argmin(np.abs(cuts - 0.5))]) if len(cuts) else 0.5
    return before, after, _round((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))


def return_vias(index: BoardIndex, nets, layers, point: Point, skip=None) -> list[tuple[float, object]]:
    """Vias of `nets` through every layer of `layers`, and plated holes of `nets` (they
    pass every layer), as (centre distance from `point`, record), nearest first."""
    found = []
    for net in sorted(nets):
        for record in index.net_items(net):
            if record is skip:
                continue
            if (isinstance(record, model.Via) and all(on_layer(record, layer) for layer in layers)) or \
                    (isinstance(record, model.Pad) and record.drill and min(record.drill) > 0):
                found.append((math.dist(point, record.pos), record))
    return sorted(found, key=lambda item: (item[0], item[1].id))


# stubs

@dataclass(frozen=True)
class Stub:
    """A barrel's use: the layers it spans and those where its net meets it (top to
    bottom, empty when none), and the unused part past the last used layer as (from the
    used layer, to the barrel's end), empty without one. `other` is a second, shorter
    stub at the other end, when the barrel is used only in its middle."""
    span: tuple[str, ...]
    used: tuple[str, ...]
    stub: tuple[str, ...]
    length_nm: int
    other: tuple[str, ...]
    other_nm: int
    exact: bool  # lengths from the stackup's thicknesses, not the board's split evenly


def barrel_length(snapshot: model.BoardSnapshot, start: str, end: str) -> tuple[int, bool] | None:
    """(length, exact) of a barrel from the face of copper layer `start` towards `end` to
    the far face of `end`: the dielectric and copper between, `end`'s copper included.
    Exact from the stackup's thicknesses; else the board's thickness from a complete
    stackup split evenly between its copper layers; None when the stackup gives neither."""
    if start == end:
        return 0, True
    stack = snapshot.stackup.layers
    names = [entry.name for entry in stack]
    if start in names and end in names:
        a, b = names.index(start), names.index(end)
        step = 1 if b > a else -1
        span = [stack[k] for k in range(a + step, b + step, step)]
        if all(entry.thickness_nm for entry in span):
            return sum(entry.thickness_nm for entry in span), True
    if "F.Cu" in names and "B.Cu" in names:
        whole = stack[names.index("F.Cu"):names.index("B.Cu") + 1]
        layers, _, _ = copper_order(snapshot)
        if all(entry.thickness_nm for entry in whole) and start in layers and end in layers and len(layers) > 1:
            board = sum(entry.thickness_nm for entry in whole)
            return round(abs(layers.index(end) - layers.index(start)) * board / (len(layers) - 1)), False
    return None


def stub(index: BoardIndex, record) -> Stub | None:
    """The stub of a via or plated hole: its barrel past the last layer where its net
    meets it, to the barrel's end (the whole barrel when its net meets it nowhere). With
    stubs at both ends the longer one is `stub`. None when a stub needs a length the
    stackup cannot give (`barrel_length`)."""
    span = barrel(index, record)
    used = via_layers(index, record)
    if not span:
        return Stub(span, used, (), 0, (), 0, True)
    ends = [(span[0], span[-1])] if not used else \
        [(used[-1], span[-1]), (used[0], span[0])]  # below the last used layer, above the first
    found = []
    for start, end in ends:
        if start == end:
            continue
        length = barrel_length(index.snapshot, start, end)
        if length is None:
            return None
        found.append(((start, end), *length))
    found.sort(key=lambda item: -item[1])
    first = found[0] if found else ((), 0, True)
    second = found[1] if len(found) > 1 else ((), 0, True)
    return Stub(span, (used[0], used[-1]) if used else (), first[0], first[1], second[0], second[1],
                first[2] and second[2])


# lines followed

@dataclass(frozen=True)
class Line:
    """A closed ring followed along (a pour's or the board's outline): its corners and the
    length along it to each from the first; the last entry of `along` is the way round."""
    corners: np.ndarray  # (k, 2)
    along: np.ndarray  # (k + 1,)

    @staticmethod
    def of(ring) -> "Line":
        corners = np.asarray(ring, dtype=float)
        steps = np.hypot(*(np.roll(corners, -1, axis=0) - corners).T)
        return Line(corners, np.concatenate([[0.0], np.cumsum(steps)]))

    @property
    def length(self) -> float:
        return float(self.along[-1])

    def at(self, s: float) -> Point:
        """The point `s` along the line (round past the start when longer)."""
        return _round(self._at(s)[0])

    def _at(self, s: float) -> tuple[np.ndarray, np.ndarray]:
        """(point, direction): on a corner, the direction halfway between its two edges."""
        s = s % self.length if self.length else 0.0
        k = min(int(np.searchsorted(self.along, s, side="right")) - 1, len(self.corners) - 1)
        a, b = self.corners[k], self.corners[(k + 1) % len(self.corners)]
        step = self.along[k + 1] - self.along[k]
        direction = (b - a) / (step or 1.0)
        if s - self.along[k] < 1.0:  # on corner k
            before = a - self.corners[k - 1]
            direction = direction + before / (float(np.hypot(*before)) or 1.0)
        return a + (b - a) * ((s - self.along[k]) / step if step else 0.0), direction

    def stretch(self, start: float, size: float) -> list[Point]:
        """The line from `start` along it for `size` (at most once round), as a polyline
        through the corners it passes."""
        corners = np.concatenate([self.along[:-1], self.along[:-1] + self.length])
        passed = np.flatnonzero((corners > start) & (corners < start + size))
        return [self.at(start), *(_round(self.corners[k % len(self.corners)]) for k in passed),
                self.at(start + size)]

    def project(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(distance from each point (P, 2) to the line, the length along it to its nearest spot)."""
        edges = np.hstack([self.corners, np.roll(self.corners, -1, axis=0)])
        distance, along = np.zeros(len(points)), np.zeros(len(points))
        step = max(1, _BLOCK // max(1, len(edges)))
        for start in range(0, len(points), step):
            pick = slice(start, start + step)
            gaps, cx, cy = _point_segments(points[pick, 0], points[pick, 1], edges)
            k = np.argmin(gaps, axis=1)
            rows = np.arange(len(k))
            distance[pick] = gaps[rows, k]
            along[pick] = self.along[k] + np.hypot(cx[rows, k] - edges[k, 0], cy[rows, k] - edges[k, 1])
        return distance, along


def stitches(index: BoardIndex, net: str, rings: Rings, line: Line, reach: float,
             layer: str | None = None) -> list[tuple[float, float, model.Via]]:
    """The vias of `net` whose centre lies within `reach` of `line`, as (length along it,
    distance from it, via), in order along it. With `layer` (a pour): vias through it whose land lies in or
    touches the polygon `rings`' copper there. Without (the board's edge): vias inside the
    polygon `rings`."""
    vias = [record for record in index.net_items(net) if isinstance(record, model.Via) and
            (layer is None or on_layer(record, layer))]
    if not vias:
        return []
    points = np.array([via.pos for via in vias], dtype=float)
    distance, along = line.project(points)
    near = np.flatnonzero(distance <= reach)
    if layer is None:
        near = near[_in_rings(points[near], rings)] if len(near) else near
    elif len(near):
        radii = np.array([vias[k].diameter / 2 + TOUCH_NM for k in near])
        near = near[_touching(points[near], radii, rings)]
    found = [(float(along[k]), float(distance[k]), vias[k]) for k in near]
    return sorted(found, key=lambda item: (item[0], item[2].id))


def gaps(positions: list[float], length: float) -> list[tuple[float, float]]:
    """(start, length along the line) of each stretch of a closed line between neighbouring
    positions (in order), the last one round past the start; the whole line without any."""
    if not positions:
        return [(0.0, length)]
    ends = [*positions[1:], positions[0] + length]
    return [(start, end - start) for start, end in zip(positions, ends)]


# islands

@dataclass(frozen=True)
class Island:
    """One fill piece of a net. `group` numbers the pieces that vias, pads, tracks (and
    overlapping fills) tie together, across layers; the group with the most area is
    the main plane. `tied`: in the main group; `main`: its largest piece on this layer."""
    layer: str
    zone: str  # zone id
    rings: Rings
    area_nm2: float
    group: int
    tied: bool
    main: bool


def _touching(points: np.ndarray, radii: np.ndarray, rings: Rings) -> np.ndarray:
    """Which discs (centre, radius) lie in a polygon's copper or reach its edge."""
    low, high = rings[0].min(axis=0), rings[0].max(axis=0)
    near = np.flatnonzero(np.all((points >= low - radii[:, None]) & (points <= high + radii[:, None]), axis=1))
    found = np.zeros(len(points), dtype=bool)
    if not len(near):
        return found
    found[near] = _in_rings(points[near], rings)
    rest = near[~found[near]]
    if len(rest):
        edges = _edges((rings,))
        step = max(1, _BLOCK // len(edges))
        for start in range(0, len(rest), step):
            pick = rest[start:start + step]
            distance, _, _ = _point_segments(points[pick, 0], points[pick, 1], edges)
            found[pick] = distance.min(axis=1) <= radii[pick]
    return found


def _overlap(box_a, box_b, slack: float = 0.0) -> bool:
    return box_a[0] <= box_b[2] + slack and box_b[0] <= box_a[2] + slack and \
        box_a[1] <= box_b[3] + slack and box_b[1] <= box_a[3] + slack


def _tracks_touching(records, rings: Rings) -> np.ndarray:
    """Which tracks and arcs have copper in or at a polygon's copper: an end's disc touches
    it, the centre line crosses one of its edges, or a corner of it lies in the copper."""
    found = np.zeros(len(records), dtype=bool)
    if not records:
        return found
    ends = np.array([end for record in records for end in (record.start, record.end)], dtype=float)
    reach = np.repeat([record.width / 2 + TOUCH_NM for record in records], 2)
    found |= _touching(ends, reach, rings).reshape(-1, 2).any(axis=1)
    edges = _edges((rings,))
    corners = edges[:, :2]
    for k in np.flatnonzero(~found):
        record = records[k]
        for a, b in centre_lines(record):
            if len(_cuts(a, b, edges)):
                found[k] = True
                break
            low = np.minimum(a, b) - record.width / 2 - TOUCH_NM
            high = np.maximum(a, b) + record.width / 2 + TOUCH_NM
            close = corners[np.all((corners >= low) & (corners <= high), axis=1)]
            if len(close):
                distance, _, _ = _point_segments(close[:, 0], close[:, 1], np.array([[*a, *b]], dtype=float))
                if distance.min() <= record.width / 2 + TOUCH_NM:
                    found[k] = True
                    break
    return found


def _join_touching(union: "_Union", items) -> None:
    """Join the (node, shape) items whose copper touches, swept along x by their boxes."""
    items = sorted(items, key=lambda item: item[1].bbox[0])
    for at, (node, copper) in enumerate(items):
        for other, near in items[at + 1:]:
            if near.bbox[0] > copper.bbox[2] + TOUCH_NM:
                break
            if union.find(node) != union.find(other) and _overlap(copper.bbox, near.bbox, TOUCH_NM) and \
                    gap(copper, near).distance <= TOUCH_NM:
                union.join(node, other)


def islands(index: BoardIndex, net: str) -> list[Island]:
    """Every fill piece of `net` on every layer, grouped by what ties them: a via joins
    the pieces its land touches on each layer of its span, a pad (a plated hole on each of
    its layers) the pieces its copper touches, fills of two zones that overlap on a layer
    join there, and a track or arc of the net joins the pieces its copper touches. Vias,
    pads, tracks and arcs whose copper touches join each other too, so a piece tied to
    another only by a track, or by a track between two vias, counts as tied."""
    pieces = [(zone, polygon, rings) for zone in index.net_items(net) if isinstance(zone, model.ZoneFill)
              for polygon in zone.polygons for rings in (_rings(polygon),) if rings]
    if not pieces:
        return []
    vias = [record for record in index.net_items(net) if isinstance(record, model.Via)]
    holes = [record for record in index.net_items(net) if isinstance(record, model.Pad)]
    tracks = [record for record in index.net_items(net) if isinstance(record, (model.Track, model.Arc))]
    union = _Union(len(pieces) + len(vias) + len(holes) + len(tracks))
    boxes = [(*rings[0].min(axis=0), *rings[0].max(axis=0)) for _, _, rings in pieces]
    shapes = {}

    def shape(i):
        if i not in shapes:
            zone, polygon, _ = pieces[i]
            shapes[i] = shape_of(model.ZoneFill(zone.id, zone.net, zone.layer, (polygon,)))
        return shapes[i]

    by_layer: dict[str, list[int]] = defaultdict(list)
    for i, (zone, _, _) in enumerate(pieces):
        by_layer[zone.layer].append(i)
    for layer, numbers in by_layer.items():
        through = [k for k, via in enumerate(vias) if on_layer(via, layer)]
        if through:
            points = np.array([vias[k].pos for k in through], dtype=float)
            radii = np.array([vias[k].diameter / 2 + TOUCH_NM for k in through])
            for i in numbers:
                for k in np.flatnonzero(_touching(points, radii, pieces[i][2])):
                    union.join(i, len(pieces) + through[k])
        for k, hole in enumerate(holes):
            if layer not in hole.polygons:
                continue
            copper = index.shape(hole, layer)
            for i in numbers:
                if _overlap(boxes[i], copper.bbox, TOUCH_NM) and gap(copper, shape(i)).distance <= TOUCH_NM:
                    union.join(i, len(pieces) + len(vias) + k)
        for at, i in enumerate(numbers):
            for j in numbers[at + 1:]:
                if pieces[i][0].id != pieces[j][0].id and _overlap(boxes[i], boxes[j], TOUCH_NM) and \
                        gap(shape(i), shape(j)).distance <= TOUCH_NM:
                    union.join(i, j)
        first = len(pieces) + len(vias) + len(holes)
        on = [k for k, record in enumerate(tracks) if record.layer == layer]
        for i in numbers:
            near = [k for k in on if _overlap(boxes[i], index.shape(tracks[k]).bbox, TOUCH_NM)]
            for k, touches in zip(near, _tracks_touching([tracks[k] for k in near], pieces[i][2])):
                if touches:
                    union.join(i, first + k)
    layers = {layer for record in (*vias, *holes, *tracks) for layer in index.layers_of(record)}
    for layer in sorted(layers, key=copper_rank):  # vias, pads, tracks and arcs touching on a layer
        items = [(len(pieces) + k, index.shape(via, layer)) for k, via in enumerate(vias) if on_layer(via, layer)]
        items += [(len(pieces) + len(vias) + k, index.shape(pad, layer)) for k, pad in enumerate(holes)
                  if layer in pad.polygons]
        items += [(len(pieces) + len(vias) + len(holes) + k, index.shape(record)) for k, record in enumerate(tracks)
                  if record.layer == layer]
        _join_touching(union, [(node, copper) for node, copper in items if len(copper.segments)])
    areas = [polygon_area(rings) for _, _, rings in pieces]
    totals: dict[int, float] = defaultdict(float)
    for i, area in enumerate(areas):
        totals[union.find(i)] += area
    main_group = max(totals, key=lambda root: totals[root]) if totals else None
    group_of = {root: n for n, root in enumerate(dict.fromkeys(union.find(i) for i in range(len(pieces))), 1)}
    largest = {}
    for i, (zone, _, _) in enumerate(pieces):
        if union.find(i) == main_group and (zone.layer not in largest or areas[i] > areas[largest[zone.layer]]):
            largest[zone.layer] = i
    return [Island(zone.layer, zone.id, rings, areas[i], group_of[union.find(i)], union.find(i) == main_group,
                   largest.get(zone.layer) == i)
            for i, (zone, _, rings) in enumerate(pieces)]


class _Union:
    """Union-find over numbered items."""

    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def join(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)
