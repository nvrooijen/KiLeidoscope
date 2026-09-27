"""Reference-plane lookup: which copper lies above and below each track, and where it is missing.

For every track and arc (arcs as their sampled polyline) this finds the nearest
copper layers above and below it in the stackup, the dielectric between them, and
along the item where solid copper (zone fills) covers its footprint widened by
`MARGIN_HEIGHTS` dielectric heights on each side.

Zone fills are rasterized per layer onto one board grid, each pixel holding the
index of the plane's net (0: no copper). A reference layer is then eroded by the
margin: a pixel keeps its net only when the square of `margin` around it is all
that one net. So along the item, a single lookup per sample on a few lines across
its width answers "is the widened footprint on one solid plane here", and a change
of net across a split shows as a gap. The erosion square covers at least the
margin in every direction (up to 1.41x along diagonals).

Everything is cached for the live loop: a layer is rasterized again only when its
zone fills change, and an item is looked up again only when it, its reference
layers or the vias of its net change.

A signal via's own antipad (its clearance hole in the plane) would read as a void
at every layer change, so samples within `ANTIPAD_CLEARANCE_NM` (plus the margin)
of a via or plated hole of the item's own net, or its differential-pair partner's,
take the coverage of the copper next to them.
"""

import math
from dataclasses import dataclass

import numpy as np

from . import model, protocol
from .geometry import sample_arc
from .selection import diff_pair_partner

PIXEL_NM = 50_000  # raster pitch: under a 0.1 mm track's half-width
MAX_PIXELS = 4_000_000  # per layer; larger boards get a coarser pitch
MARGIN_HEIGHTS = 3.0  # copper needed beside the track, in dielectric heights
ANTIPAD_CLEARANCE_NM = 500_000  # assumed plane clearance around an own-net via or hole (KiCad's zone default)

Span = tuple[int, int]  # (start, end) nm along an item from its start


@dataclass(frozen=True)
class Cover:
    """One reference layer seen from an item."""
    layer: str
    distance_nm: int  # dielectric between the two copper faces
    margin_nm: int  # copper needed beside the footprint, as rasterized
    covered_fraction: float  # of the item's length
    gaps: tuple[Span, ...]  # uncovered spans
    planes: tuple[tuple[int, int, str | None], ...]  # covered spans and the plane's net (None: not known)


@dataclass(frozen=True)
class SegmentReference:
    """The reference planes of one track or arc."""
    id: str
    layer: str
    net: str
    width: int
    path: tuple[model.Point, ...]  # centreline; an arc as sampled for display
    length_nm: int
    above: Cover | None  # None: no copper layer on that side
    below: Cover | None

    @property
    def covers(self) -> tuple[Cover, ...]:
        return tuple(cover for cover in (self.above, self.below) if cover is not None)

    @property
    def primary(self) -> Cover | None:
        """The side with the most copper under the item, the nearer one on a tie; None
        when neither side has any."""
        found = [cover for cover in self.covers if cover.covered_fraction > 0]
        return max(found, key=lambda cover: (cover.covered_fraction, -cover.distance_nm), default=None)

    def point_at(self, distance_nm: float) -> model.Point:
        return _along(self.path, distance_nm)

    def subpath(self, start_nm: float, end_nm: float) -> tuple[model.Point, ...]:
        """The centreline between two distances along it."""
        points, walked = [_along(self.path, start_nm)], 0.0
        for a, b in zip(self.path, self.path[1:]):
            walked += math.dist(a, b)
            if start_nm < walked < end_nm:
                points.append(b)
        points.append(_along(self.path, end_nm))
        return tuple(points)


@dataclass(frozen=True)
class Neighbour:
    layer: str
    distance_nm: int


def _along(path, distance_nm):
    walked = 0.0
    for a, b in zip(path, path[1:]):
        length = math.dist(a, b)
        if length and walked + length >= distance_nm:
            t = max(0.0, (distance_nm - walked) / length)
            return round(a[0] + t * (b[0] - a[0])), round(a[1] + t * (b[1] - a[1]))
        walked += length
    return path[-1]


# --- Stackup ---------------------------------------------------------------------------

def copper_order(snapshot: model.BoardSnapshot) -> tuple[tuple[str, ...], tuple[int, ...], bool]:
    """(copper layers top first, dielectric between each neighbouring pair, exact).

    Exact from the stackup's thicknesses; otherwise the evenly spaced heights the
    viewer shows (protocol.layer_heights_nm), and `exact` is False.
    """
    stack = snapshot.stackup.layers
    copper = [index for index, entry in enumerate(stack) if entry.type == "copper"]
    names = tuple(stack[index].name for index in copper)
    used = protocol.copper_layers(snapshot)
    if copper and used <= set(names) and all(stack[index].thickness_nm for index in range(copper[0], copper[-1] + 1)):
        gaps = tuple(sum(stack[k].thickness_nm for k in range(a + 1, b)) for a, b in zip(copper, copper[1:]))
        return names, gaps, True
    heights, _, _ = protocol.layer_heights_nm(snapshot)
    names = tuple(sorted(heights, key=lambda name: -heights[name]))
    return names, tuple(heights[a] - heights[b] for a, b in zip(names, names[1:])), False


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


# --- Rasters ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Grid:
    """Pixel (column c, row r) has its centre at (x0 + (c + 0.5) * pixel, y0 + (r + 0.5) * pixel)."""
    x0: int
    y0: int
    pixel: int
    width: int
    height: int


def board_grid(snapshot: model.BoardSnapshot, pixel_nm: int = PIXEL_NM, max_pixels: int = MAX_PIXELS) -> Grid | None:
    """One grid over the board outline (or, without one, the zone fills and tracks)."""
    points = [p for polygon in snapshot.outline.polygons for ring in polygon for p in ring]
    if not points:
        points = [p for zone in snapshot.zones for polygon in zone.polygons for ring in polygon for p in ring]
        points += [p for track in (*snapshot.tracks, *snapshot.arcs) for p in (track.start, track.end)]
    if not points:
        return None
    xs, ys = zip(*points)
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    pixel = max(pixel_nm, math.ceil(math.sqrt((right - left) * (bottom - top) / max_pixels)))
    return Grid(left - pixel, top - pixel, pixel,
                (right - left) // pixel + 3, (bottom - top) // pixel + 3)


def rasterize(grid: Grid, polygons: list[tuple[int, model.Polygon]]) -> np.ndarray:
    """(height, width) int16 labels: each (label, polygon) painted by pixel centre,
    even-odd within a polygon (outer ring and holes), later labels over earlier ones."""
    labels = np.zeros((grid.height, grid.width), np.int16)
    by_label: dict[int, list[model.Polygon]] = {}
    for label, polygon in polygons:
        by_label.setdefault(label, []).append(polygon)
    for label, group in by_label.items():
        covered = _fill(grid, group)
        if covered is not None:
            labels[covered] = label
    return labels


def _fill(grid: Grid, polygons: list[model.Polygon]) -> np.ndarray | None:
    """Scanline fill at pixel centres, every row's crossings found at once."""
    starts, ends, owners = [], [], []
    for index, polygon in enumerate(polygons):
        for ring in polygon:
            if len(ring) >= 3:
                points = np.asarray(ring, np.float64)
                starts.append(points)
                ends.append(np.roll(points, -1, axis=0))
                owners.append(np.full(len(points), index))
    if not starts:
        return None
    width, height = grid.width, grid.height
    a = (np.concatenate(starts) - (grid.x0, grid.y0)) / grid.pixel - 0.5  # pixel centres at integers
    b = (np.concatenate(ends) - (grid.x0, grid.y0)) / grid.pixel - 0.5
    owner = np.concatenate(owners)
    low = np.clip(np.ceil(np.minimum(a[:, 1], b[:, 1])), 0, height).astype(np.int64)  # rows low <= r < high
    high = np.clip(np.ceil(np.maximum(a[:, 1], b[:, 1])), 0, height).astype(np.int64)
    count = high - low
    if not count.sum():
        return None
    edge = np.repeat(np.arange(len(count)), count)
    row = low[edge] + np.arange(len(edge)) - np.repeat(np.cumsum(count) - count, count)
    slope = (b[edge, 0] - a[edge, 0]) / (b[edge, 1] - a[edge, 1])  # count > 0: never horizontal
    x = a[edge, 0] + (row - a[edge, 1]) * slope
    order = np.lexsort((x, row, owner[edge]))  # each (polygon, row) has an even number of crossings
    x, row = x[order], row[order]
    left = np.clip(np.ceil(x[0::2]), 0, width).astype(np.int64)
    right = np.clip(np.ceil(x[1::2]), 0, width).astype(np.int64)
    rows = row[0::2]
    size = height * (width + 1)
    delta = (np.bincount(rows * (width + 1) + left, minlength=size) -
             np.bincount(rows * (width + 1) + right, minlength=size))
    return np.cumsum(delta.reshape(height, width + 1), axis=1)[:, :width] > 0


def erode(labels: np.ndarray, radius: int) -> np.ndarray:
    """Keep a pixel's label only where the (2 radius + 1) square around it is that one
    nonzero label: separable, one cumulative count of label changes per axis."""
    if radius <= 0:
        return labels
    for axis in (1, 0):
        padded = np.pad(labels, [(radius, radius) if a == axis else (0, 0) for a in (0, 1)])
        moved = np.moveaxis(padded, axis, -1)
        breaks = np.ones(moved.shape, np.int32)
        breaks[..., 1:] = (moved[..., 1:] != moved[..., :-1]) | (moved[..., 1:] == 0)
        count = np.cumsum(breaks, axis=-1)
        size = labels.shape[axis]
        uniform = (count[..., 2 * radius:2 * radius + size] == count[..., :size]) & (moved[..., :size] != 0)
        labels = np.moveaxis(np.where(uniform, moved[..., :size], 0).astype(np.int16), -1, axis)
    return labels


class _Plane:
    """One layer's zone fills, rasterized when first needed."""

    def __init__(self, zones: tuple[model.ZoneFill, ...], version: int):
        self.zones = zones
        self.version = version
        self.nets = tuple(sorted({zone.net for zone in zones}))  # label = index + 1
        self.labels = None
        self.eroded = {}

    def raw_labels(self, grid: Grid, stats: dict) -> np.ndarray | None:
        if not self.zones:
            return None
        if self.labels is None:
            label = {net: index + 1 for index, net in enumerate(self.nets)}
            self.labels = rasterize(grid, [(label[zone.net], polygon) for zone in self.zones
                                           for polygon in zone.polygons])
            stats["rasterized"] += 1
        return self.labels

    def eroded_labels(self, grid: Grid, radius: int, stats: dict) -> np.ndarray | None:
        labels = self.raw_labels(grid, stats)
        if labels is None:
            return None
        if radius not in self.eroded:
            self.eroded[radius] = erode(labels, radius)
            stats["eroded"] += 1
        return self.eroded[radius]


# --- Lookup ----------------------------------------------------------------------------

class ReferencePlanes:
    """Reference-plane lookup over successive snapshots of one board.

        planes = ReferencePlanes()
        planes.update(snapshot)           # cheap when little changed
        found = planes.segments({ids})    # {id: SegmentReference}; all tracks and arcs without ids

    `stats` counts rasterized layers, erosions and looked-up items (for tests and timing).
    """

    def __init__(self, pixel_nm: int = PIXEL_NM, margin_heights: float = MARGIN_HEIGHTS,
                 max_pixels: int = MAX_PIXELS):
        self.pixel_nm = pixel_nm
        self.margin_heights = margin_heights
        self.max_pixels = max_pixels
        self.snapshot = None
        self.grid = None
        self.order = ()
        self.exact = True  # False: layer distances from evenly spaced heights, not the stackup
        self.neighbours = {}
        self.planes: dict[str, _Plane] = {}
        self.items = {}  # id -> track or arc
        self.holes = {}  # net -> ((x, y, radius, top index, bottom index), ...)
        self.partners = {}  # net -> its differential-pair partner net
        self._holes_source = None
        self._versions = 0
        self._results = {}  # id -> (key, SegmentReference)
        self.stats = {"rasterized": 0, "eroded": 0, "computed": 0}

    def update(self, snapshot: model.BoardSnapshot) -> frozenset[str]:
        """Take a new snapshot; the layers whose zone fills changed."""
        if snapshot is self.snapshot:
            return frozenset()
        self.snapshot = snapshot
        order, gaps, self.exact = copper_order(snapshot)
        if order != self.order:
            self.order = order
            self.planes.clear()
        self.neighbours = _neighbours(order, gaps)
        grid = board_grid(snapshot, self.pixel_nm, self.max_pixels)
        if grid != self.grid:
            self.grid = grid
            self.planes.clear()
        zones: dict[str, list[model.ZoneFill]] = {}
        for zone in snapshot.zones:
            zones.setdefault(zone.layer, []).append(zone)
        changed = set()
        for layer in set(self.planes) | set(zones):
            fills = tuple(zones.get(layer, ()))
            plane = self.planes.get(layer)
            if plane is None or plane.zones != fills:  # the reader keeps unchanged items: mostly `is`
                self._versions += 1
                self.planes[layer] = _Plane(fills, self._versions)
                changed.add(layer)
        for layer in order:
            if layer not in self.planes:
                self._versions += 1
                self.planes[layer] = _Plane((), self._versions)
        self.items = {item.id: item for item in (*snapshot.tracks, *snapshot.arcs)}
        nets = {item.net for item in (*snapshot.tracks, *snapshot.arcs, *snapshot.vias)}
        self.partners = {net: partner for net in nets if (partner := diff_pair_partner(net, nets))}
        self._update_holes(snapshot)
        return frozenset(changed)

    def _update_holes(self, snapshot):
        """Own-net vias and plated holes, whose antipads are excused (module docstring)."""
        source = (snapshot.vias, snapshot.pads, self.order)
        if source == self._holes_source:
            return
        self._holes_source = source
        index = {name: position for position, name in enumerate(self.order)}
        last = len(self.order) - 1
        holes: dict[str, list] = {}
        for via in snapshot.vias:
            top, bottom = sorted((index.get(via.layer_top, 0), index.get(via.layer_bottom, last)))
            holes.setdefault(via.net, []).append(
                (*via.pos, via.diameter / 2 + ANTIPAD_CLEARANCE_NM, top, bottom))
        for pad in snapshot.pads:
            if not pad.drill or min(pad.drill) <= 0 or not pad.net:
                continue
            reach = max((math.dist(pad.pos, p) for polygons in pad.polygons.values()
                         for polygon in polygons for ring in polygon for p in ring), default=max(pad.drill) / 2)
            holes.setdefault(pad.net, []).append((*pad.pos, reach + ANTIPAD_CLEARANCE_NM, 0, last))
        self.holes = {net: tuple(found) for net, found in holes.items()}

    def nets_near(self, layer: str, path, reach_nm: float) -> tuple[str, ...]:
        """Nets of the zone fills on `layer` within `reach_nm` of the path's bounding box."""
        plane, grid = self.planes.get(layer), self.grid
        labels = plane.raw_labels(grid, self.stats) if plane is not None and grid is not None else None
        if labels is None:
            return ()
        xs, ys = zip(*path)
        columns = [math.floor((value - grid.x0) / grid.pixel) for value in (min(xs) - reach_nm, max(xs) + reach_nm)]
        rows = [math.floor((value - grid.y0) / grid.pixel) for value in (min(ys) - reach_nm, max(ys) + reach_nm)]
        window = labels[max(0, rows[0]):max(0, rows[1] + 1), max(0, columns[0]):max(0, columns[1] + 1)]
        return tuple(plane.nets[label - 1] for label in np.unique(window).tolist() if label)

    def radius_px(self, distance_nm: int) -> int:
        return math.ceil(self.margin_heights * distance_nm / self.grid.pixel) if self.grid else 0

    def segments(self, ids=None) -> dict[str, SegmentReference]:
        """Look up these track and arc ids (unknown ids are skipped), or all of them."""
        wanted = self.items.keys() if ids is None else [i for i in ids if i in self.items]
        found, stale = {}, []
        for item_id in wanted:
            item = self.items[item_id]
            key = self._key(item)
            cached = self._results.get(item_id)
            if cached is not None and cached[0] == key:
                found[item_id] = cached[1]
            else:
                stale.append((item, key))
        by_layer: dict[str, list] = {}
        for item, key in stale:
            by_layer.setdefault(item.layer, []).append((item, key))
        for layer, group in by_layer.items():
            for (item, key), result in zip(group, self._look_up(layer, [item for item, _ in group])):
                self._results[item.id] = (key, result)
                found[item.id] = result
        self.stats["computed"] += len(stale)
        if len(self._results) > 2 * len(self.items) + 1024:  # forget deleted items now and then
            self._results = {k: v for k, v in self._results.items() if k in self.items}
        return found

    def _key(self, item):
        """Everything an item's lookup depends on (compared, not hashed)."""
        sides = []
        for neighbour in self.neighbours.get(item.layer, (None, None)):
            if neighbour is None:
                sides.append(None)
            else:
                plane = self.planes.get(neighbour.layer)
                sides.append((neighbour, plane.version if plane else 0))
        partner = self.partners.get(item.net)
        return item, self.grid, tuple(sides), self.holes.get(item.net), self.holes.get(partner)

    def _look_up(self, layer: str, items: list) -> list[SegmentReference]:
        paths = [_path(item) for item in items]
        lengths = [sum(math.dist(a, b) for a, b in zip(path, path[1:])) for path in paths]
        covers = []
        for neighbour in self.neighbours.get(layer, (None, None)):
            if neighbour is None or self.grid is None:
                covers.append([None] * len(items))
            else:
                covers.append(self._covers(neighbour, items, paths, lengths))
        return [SegmentReference(item.id, item.layer, item.net, item.width, path, round(length), above, below)
                for item, path, length, above, below in zip(items, paths, lengths, *covers)]

    def _covers(self, neighbour: Neighbour, items, paths, lengths) -> list[Cover]:
        """Cover of one reference layer for items of one signal layer, sampled together."""
        grid = self.grid
        radius = self.radius_px(neighbour.distance_nm)
        margin_nm = radius * grid.pixel
        plane = self.planes.get(neighbour.layer)
        labels = plane.eroded_labels(grid, radius, self.stats) if plane is not None else None
        if labels is None:  # no copper at all on that layer
            return [Cover(neighbour.layer, neighbour.distance_nm, margin_nm, 0.0,
                          ((0, round(length)),) if length else (), ()) for length in lengths]
        samples = _Samples(paths, lengths, [item.width for item in items], grid.pixel, margin_nm)
        found = samples.labels(grid, labels)
        excused = self._excused(neighbour.layer, items, samples, margin_nm)
        found[excused] = -1
        found = _fill_excused(found, samples.item)
        return _covers_from_runs(neighbour, margin_nm, plane.nets, samples, found, lengths)

    def _excused(self, layer, items, samples, margin_nm) -> np.ndarray:
        """Samples near an antipad of the item's own net or its pair partner's."""
        excused = np.zeros(len(samples.item), bool)
        position = self.order.index(layer) if layer in self.order else -1
        by_net: dict[str, list[int]] = {}
        for index, item in enumerate(items):
            by_net.setdefault(item.net, []).append(index)
        for net, indices in by_net.items():
            holes = [hole for owner in (net, self.partners.get(net)) for hole in self.holes.get(owner, ())
                     if hole[3] <= position <= hole[4]]
            if not net or not holes:
                continue
            chosen = np.flatnonzero(np.isin(samples.item, indices))
            points = samples.points[chosen]
            holes = np.asarray(holes, np.float64)
            extra = margin_nm * math.sqrt(2) + samples.half_width[samples.item[chosen]]
            # Only holes within reach of these samples at all, in blocks (a ground net has hundreds).
            low, high = points.min(axis=0) - extra.max(), points.max(axis=0) + extra.max()
            holes = holes[np.all((holes[:, :2] + holes[:, 2:3] >= low) & (holes[:, :2] - holes[:, 2:3] <= high),
                                 axis=1)]
            block = max(1, 1_000_000 // max(1, len(holes)))
            for first in range(0, len(chosen) if len(holes) else 0, block):
                part = slice(first, first + block)
                distance = np.hypot(points[part, None, 0] - holes[None, :, 0],
                                    points[part, None, 1] - holes[None, :, 1])
                excused[chosen[part]] = (distance < holes[None, :, 2] + extra[part, None]).any(axis=1)
        return excused


def _path(item) -> tuple[model.Point, ...]:
    if isinstance(item, model.Arc):
        return sample_arc(item.start, item.mid, item.end)
    return (item.start, item.end)


class _Samples:
    """Points every `step` nm along many polylines at once, each with lines across the
    item's width close enough that their eroded squares leave no gap between them."""

    def __init__(self, paths, lengths, widths, step, margin_nm):
        count = len(paths)
        lengths = np.asarray(lengths, np.float64)
        per_item = np.where(lengths > 0, np.ceil(lengths / step).astype(np.int64) + 1, 1)
        self.item = np.repeat(np.arange(count), per_item)
        first = np.cumsum(per_item) - per_item
        k = np.arange(len(self.item)) - first[self.item]
        spacing = np.where(per_item > 1, lengths / np.maximum(per_item - 1, 1), 0.0)
        self.distance = k * spacing[self.item]
        self.spacing = spacing
        # Sub-segments of every path, laid end to end with a gap of 1 nm between items.
        starts = np.concatenate([np.asarray(path[:-1] or path, np.float64).reshape(-1, 2) for path in paths])
        ends = np.concatenate([np.asarray(path[1:] or path, np.float64).reshape(-1, 2) for path in paths])
        owner = np.repeat(np.arange(count), [max(1, len(path) - 1) for path in paths])
        piece = np.hypot(*(ends - starts).T)
        walked = np.concatenate([[0.0], np.cumsum(piece)])[:-1]
        own_first = np.searchsorted(owner, np.arange(count))  # each item's first piece
        piece_start = walked - walked[own_first[owner]]  # along its own path
        offset = np.concatenate([[0.0], np.cumsum(lengths + 1)])[:-1]
        which = np.searchsorted(offset[owner] + piece_start, offset[self.item] + self.distance, side="right") - 1
        which = np.maximum(which, own_first[self.item])
        span = np.maximum(piece[which], 1e-9)
        along = np.clip((self.distance - piece_start[which]) / span, 0, 1)
        direction = (ends - starts)[which] / span[:, None]
        self.points = starts[which] + along[:, None] * (ends - starts)[which]
        self.normal = np.stack((-direction[:, 1], direction[:, 0]), axis=1)
        self.half_width = np.asarray(widths, np.float64) / 2
        # Lines across: the squares (side 2 margin) around neighbouring lines must touch.
        lines = np.ceil(2 * self.half_width / max(2 * margin_nm, step)).astype(np.int64) + 1
        self.across = _across(lines, int(lines.max(initial=1))) * self.half_width[:, None]  # (items, lines) nm

    def labels(self, grid: Grid, labels: np.ndarray) -> np.ndarray:
        """Per sample: the eroded label all its lines agree on, else 0."""
        offsets = self.across[self.item]  # (samples, lines)
        x = self.points[:, 0, None] + offsets * self.normal[:, 0, None]
        y = self.points[:, 1, None] + offsets * self.normal[:, 1, None]
        column = np.floor((x - grid.x0) / grid.pixel).astype(np.int64)
        row = np.floor((y - grid.y0) / grid.pixel).astype(np.int64)
        inside = (column >= 0) & (column < grid.width) & (row >= 0) & (row < grid.height)
        found = np.where(inside, labels[np.clip(row, 0, grid.height - 1), np.clip(column, 0, grid.width - 1)], 0)
        agree = (found == found[:, :1]).all(axis=1)
        return np.where(agree, found[:, 0], 0).astype(np.int32)


def _across(lines: np.ndarray, most: int) -> np.ndarray:
    """(items, most) fractions -1..1 of the half-width; an item with fewer lines repeats its centre."""
    table = np.zeros((len(lines), most))
    for count in np.unique(lines):
        if count > 1:
            table[lines == count, :count] = np.linspace(-1, 1, count)
    return table


def _fill_excused(found: np.ndarray, item: np.ndarray) -> np.ndarray:
    """Excused samples (-1) take the nearest earlier sample's label of the same item,
    else the nearest later one's; an item excused throughout keeps -1 (not known)."""
    size = len(found)
    index = np.arange(size)
    earlier = np.maximum.accumulate(np.where(found != -1, index, -1))
    use = (found == -1) & (earlier >= 0)
    use[use] &= item[earlier[use]] == item[use]
    found[use] = found[earlier[use]]
    later = np.minimum.accumulate(np.where(found != -1, index, size)[::-1])[::-1]
    use = (found == -1) & (later < size)
    use[use] &= item[later[use]] == item[use]
    found[use] = found[later[use]]
    return found


def _covers_from_runs(neighbour, margin_nm, nets, samples: _Samples, found, lengths) -> list[Cover]:
    """Runs of equal labels become gaps (0) and plane spans, per item."""
    item = samples.item
    boundary = np.flatnonzero((found[1:] != found[:-1]) | (item[1:] != item[:-1])) + 1
    starts = np.concatenate([[0], boundary])
    stops = np.concatenate([boundary, [len(found)]]) - 1
    half = samples.spacing[item[starts]] / 2
    begin = np.maximum(0, samples.distance[starts] - half)
    end = np.minimum(np.asarray(lengths)[item[starts]], samples.distance[stops] + half)
    gaps = [[] for _ in lengths]
    planes = [[] for _ in lengths]
    for owner, label, a, b in zip(item[starts].tolist(), found[starts].tolist(), begin.tolist(), end.tolist()):
        span = (round(a), round(b))
        if label == 0:
            gaps[owner].append(span)
        else:
            planes[owner].append((*span, nets[label - 1] if label > 0 else None))
    result = []
    for index, length in enumerate(lengths):
        uncovered = sum(b - a for a, b in gaps[index])
        fraction = 1.0 - uncovered / length if length else float(not gaps[index])
        result.append(Cover(neighbour.layer, neighbour.distance_nm, margin_nm, max(0.0, min(1.0, fraction)),
                            tuple(gaps[index]), tuple(planes[index])))
    return result
