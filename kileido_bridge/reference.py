"""Reference-plane lookup: which copper lies above and below each track, and where it is missing.

For every track and arc (arcs as their sampled polyline) this finds the nearest
copper layers above and below it in the stackup, the dielectric between them, and
along the item where solid copper covers its footprint widened by `MARGIN_HEIGHTS`
dielectric heights on each side.

All copper on a layer counts: zone fills, pads, copper graphics, tracks, arcs and
via lands, rasterized per layer onto one board grid with each pixel holding its
net's label (0: no copper). A reference layer is then eroded by the margin: a pixel
keeps its net only when the square of `margin` around it is all that one net. So
narrow copper (another signal's track) drops out while planes and wide pours stay,
a single lookup per sample on a few lines across the item's width answers "is the
widened footprint on one solid plane here", and a change of net across a split
shows as a gap. The erosion square covers at least the margin in every direction
(up to 1.41x along diagonals).

A signal via's own antipad (its clearance hole in the plane) would read as a void
at every layer change. Around each via and plated hole of the item's net, or its
differential-pair partner's, the copper-free region is found in the raster itself:
when it closes within `MAX_ANTIPAD_NM` of the hole, the item is looked up as if it
were filled with the plane around it. A void that runs further (a split, a slot,
the plane's edge) stays a void.

Everything is cached for the live loop: a layer's raster is updated only where its
copper changed, and an item is looked up again only when it, the vias of its net,
or the copper near it on a reference layer changed.
"""

import math
from collections import Counter
from dataclasses import dataclass

import numpy as np

from . import model, protocol
from .geometry import circle_ring, polyline_strokes, sample_arc, stroke
from .selection import diff_pair_partner

PIXEL_NM = 50_000  # raster pitch: under a 0.1 mm track's half-width
MAX_PIXELS = 4_000_000  # per layer; larger boards get a coarser pitch
MARGIN_HEIGHTS = 3.0  # copper needed beside the track, in dielectric heights
MAX_ANTIPAD_NM = 2_000_000  # a copper-free region closing within this of an own via is its antipad
MAX_CHANGES = 64  # changed regions remembered per layer (older ones: look up everything again)
MAX_REGIONS = 16  # more changed regions in one update are merged into one

Span = tuple[int, int]  # (start, end) nm along an item from its start
Rect = tuple[int, int, int, int]  # (left, top, right, bottom) nm


@dataclass(frozen=True)
class Cover:
    """One reference layer seen from an item."""
    layer: str
    distance_nm: int  # dielectric between the two copper faces
    margin_nm: int  # copper needed beside the footprint, as rasterized
    covered_fraction: float  # of the item's length
    gaps: tuple[Span, ...]  # uncovered spans
    planes: tuple[tuple[int, int, str], ...]  # covered spans and the net of the copper there


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
    def references(self) -> tuple[Cover, ...]:
        """The sides with any solid copper along the item (both, for most striplines)."""
        return tuple(cover for cover in self.covers if cover.covered_fraction > 0)

    @property
    def primary(self) -> Cover | None:
        """The side with the most copper along the item, the nearer one on a tie; None
        when neither side has any."""
        return max(self.references, key=lambda cover: (cover.covered_fraction, -cover.distance_nm), default=None)

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


def _span(order_index: dict[str, int], top: str, bottom: str) -> tuple[int, int]:
    """(first, last) stackup index a via or hole joins; unknown layers mean the outside."""
    return tuple(sorted((order_index.get(top, 0), order_index.get(bottom, len(order_index) - 1))))


# --- Copper per layer ------------------------------------------------------------------

def layer_copper(snapshot: model.BoardSnapshot, order: tuple[str, ...]) -> dict[str, dict]:
    """{layer: {key: (item, net)}}: every piece of copper on every copper layer."""
    index = {name: position for position, name in enumerate(order)}
    layers: dict[str, dict] = {name: {} for name in order}
    seen = Counter()  # graphic ids may repeat (one entry per shape of a footprint)

    def add(layer, key, item, net):
        if layer in layers:
            layers[layer][key] = (item, net)

    for zone in snapshot.zones:
        add(zone.layer, ("zone", zone.id), zone, zone.net)
    for graphic in snapshot.graphics:
        seen[graphic.id] += 1
        add(graphic.layer, ("graphic", graphic.id, seen[graphic.id]), graphic, graphic.net)
    for item in (*snapshot.tracks, *snapshot.arcs):
        add(item.layer, ("track", item.id), item, item.net)
    for pad in snapshot.pads:
        for layer in pad.polygons:
            add(layer, ("pad", pad.id), pad, pad.net)
    for via in snapshot.vias:
        first, last = _span(index, via.layer_top, via.layer_bottom)
        for layer in order[first:last + 1]:
            add(layer, ("via", via.id), via, via.net)
    return layers


def _polygons(item, layer) -> tuple[model.Polygon, ...]:
    if isinstance(item, (model.ZoneFill, model.CopperGraphic)):
        return item.polygons
    if isinstance(item, model.Track):
        return (stroke(item.start, item.end, item.width),)
    if isinstance(item, model.Arc):
        return polyline_strokes(sample_arc(item.start, item.mid, item.end), item.width)
    if isinstance(item, model.Pad):
        return item.polygons.get(layer, ())
    return ((circle_ring(item.pos, item.diameter / 2),),)  # a via land


def _bounds(polygons) -> Rect | None:
    points = [p for polygon in polygons for ring in polygon for p in ring]
    if not points:
        return None
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(a: Rect, b: Rect) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _grow(rect: Rect, by: float) -> Rect:
    return rect[0] - by, rect[1] - by, rect[2] + by, rect[3] + by


def _union(rects) -> Rect:
    left, top, right, bottom = zip(*rects)
    return min(left), min(top), max(right), max(bottom)


def _merged(rects: list[Rect]) -> list[Rect]:
    """Overlapping rects joined (an edited item's old and new place are usually one)."""
    if len(rects) > MAX_REGIONS:
        return [_union(rects)]
    merged = []
    for rect in rects:
        while (other := next((m for m in merged if _overlap(m, rect)), None)) is not None:
            merged.remove(other)
            rect = _union((rect, other))
        merged.append(rect)
    return merged


# --- Rasters ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Grid:
    """Pixel (column c, row r) has its centre at (x0 + (c + 0.5) * pixel, y0 + (r + 0.5) * pixel)."""
    x0: int
    y0: int
    pixel: int
    width: int
    height: int

    def window(self, rect: Rect, pad_px: int = 0) -> tuple[slice, slice]:
        """The (rows, columns) of pixels touching `rect`, grown by `pad_px`, clipped."""
        left = max(0, math.floor((rect[0] - self.x0) / self.pixel) - pad_px)
        top = max(0, math.floor((rect[1] - self.y0) / self.pixel) - pad_px)
        right = min(self.width, math.floor((rect[2] - self.x0) / self.pixel) + 1 + pad_px)
        bottom = min(self.height, math.floor((rect[3] - self.y0) / self.pixel) + 1 + pad_px)
        return slice(top, max(top, bottom)), slice(left, max(left, right))

    def sub(self, rows: slice, columns: slice) -> "Grid":
        return Grid(self.x0 + columns.start * self.pixel, self.y0 + rows.start * self.pixel, self.pixel,
                    columns.stop - columns.start, rows.stop - rows.start)

    def rect(self, rows: slice, columns: slice) -> Rect:
        return (self.x0 + columns.start * self.pixel, self.y0 + rows.start * self.pixel,
                self.x0 + columns.stop * self.pixel, self.y0 + rows.stop * self.pixel)


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
    even-odd within a polygon (outer ring and holes). Where copper of two labels
    overlaps (only a DRC error does that), the span that starts later wins."""
    labels = np.zeros((grid.height, grid.width), np.int16)
    starts, ends, owners = [], [], []
    for index, (_, polygon) in enumerate(polygons):
        for ring in polygon:
            if len(ring) >= 3:
                points = np.asarray(ring, np.float64)
                starts.append(points)
                ends.append(np.roll(points, -1, axis=0))
                owners.append(np.full(len(points), index))
    if not starts:
        return labels
    width, height = grid.width, grid.height
    a = (np.concatenate(starts) - (grid.x0, grid.y0)) / grid.pixel - 0.5  # pixel centres at integers
    b = (np.concatenate(ends) - (grid.x0, grid.y0)) / grid.pixel - 0.5
    owner = np.concatenate(owners)
    low = np.clip(np.ceil(np.minimum(a[:, 1], b[:, 1])), 0, height).astype(np.int64)  # rows low <= r < high
    high = np.clip(np.ceil(np.maximum(a[:, 1], b[:, 1])), 0, height).astype(np.int64)
    count = high - low
    if not count.sum():
        return labels
    edge = np.repeat(np.arange(len(count)), count)
    row = low[edge] + np.arange(len(edge)) - np.repeat(np.cumsum(count) - count, count)
    slope = (b[edge, 0] - a[edge, 0]) / (b[edge, 1] - a[edge, 1])  # count > 0: never horizontal
    x = a[edge, 0] + (row - a[edge, 1]) * slope
    order = np.lexsort((x, row, owner[edge]))  # each (polygon, row) has an even number of crossings
    x, row, polygon = x[order], row[order], owner[edge][order]
    left = np.clip(np.ceil(x[0::2]), 0, width).astype(np.int64)
    right = np.clip(np.ceil(x[1::2]), 0, width).astype(np.int64)
    rows, span_label = row[0::2], np.array([label for label, _ in polygons], np.int16)[polygon[0::2]]
    keep = right > left
    rows, left, right, span_label = rows[keep], left[keep], right[keep], span_label[keep]
    size = height * (width + 1)
    first = rows * (width + 1) + left
    covered = np.cumsum((np.bincount(first, minlength=size) -
                         np.bincount(rows * (width + 1) + right, minlength=size)).reshape(height, width + 1),
                        axis=1)[:, :width] > 0
    # Each covered pixel takes the label of the last span started at or before it in its row.
    start_label = np.zeros(size, np.int16)
    start_label[first] = span_label
    marker = np.zeros(size, np.int64)
    marker[first] = first + 1
    latest = np.maximum.accumulate(marker).reshape(height, width + 1)[:, :width]
    labels[covered] = start_label[latest[covered] - 1]
    return labels


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


def _grown(window: tuple[slice, slice], by: int, shape) -> tuple[slice, slice]:
    rows, columns = window
    return (slice(max(0, rows.start - by), min(shape[0], rows.stop + by)),
            slice(max(0, columns.start - by), min(shape[1], columns.stop + by)))


def _inner(outer: tuple[slice, slice], inner: tuple[slice, slice]) -> tuple[slice, slice]:
    """`inner` in the coordinates of an array cut out at `outer`."""
    return (slice(inner[0].start - outer[0].start, inner[0].stop - outer[0].start),
            slice(inner[1].start - outer[1].start, inner[1].stop - outer[1].start))


def _erode_window(labels: np.ndarray, window, radius: int) -> tuple[tuple[slice, slice], np.ndarray]:
    """(target, eroded labels there) for everything within `radius` of `window`: the
    erosion reads `radius` further, so the target is exact (the grid's edge is real)."""
    target = _grown(window, radius, labels.shape)
    source = _grown(target, radius, labels.shape)
    return target, erode(labels[source], radius)[_inner(source, target)]


def _flood(allowed: np.ndarray, seeds: np.ndarray) -> np.ndarray:
    """The 4-connected region of `allowed` pixels around the `seeds` mask."""
    region = seeds & allowed
    while True:
        grown = region.copy()
        grown[1:] |= region[:-1]
        grown[:-1] |= region[1:]
        grown[:, 1:] |= region[:, :-1]
        grown[:, :-1] |= region[:, 1:]
        grown &= allowed
        if np.array_equal(grown, region):
            return region
        region = grown


def _grow_square(mask: np.ndarray) -> np.ndarray:
    """`mask` grown by one pixel in all eight directions."""
    rows = mask.copy()
    rows[1:] |= mask[:-1]
    rows[:-1] |= mask[1:]
    grown = rows.copy()
    grown[:, 1:] |= rows[:, :-1]
    grown[:, :-1] |= rows[:, 1:]
    return grown


def _steps(sources: np.ndarray) -> np.ndarray:
    """Per pixel, the square rings (8-connected steps) to the nearest `sources` pixel."""
    distance = np.where(sources, 0, np.iinfo(np.int32).max).astype(np.int32)
    reached, step = sources, 0
    while True:
        grown = _grow_square(reached)
        new = grown & ~reached
        if not new.any():
            return distance
        step += 1
        distance[new] = step
        reached = grown


def _runs(mask: np.ndarray, grid: Grid, window: tuple[slice, slice]) -> tuple[Rect, ...]:
    """A window's mask as rects: row runs, joined over rows where they repeat."""
    edges = np.diff(np.pad(mask, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    rows, starts = np.nonzero(edges == 1)
    _, stops = np.nonzero(edges == -1)
    rects, last = [], {}  # (start, stop) -> index of the rect that reached the row before
    for row, start, stop in zip(rows.tolist(), starts.tolist(), stops.tolist()):
        index = last.get((start, stop))
        if index is not None and rects[index][3] == row:
            rects[index][3] = row + 1
        else:
            last[(start, stop)] = len(rects)
            rects.append([start, row, stop, row + 1])
    x0 = grid.x0 + window[1].start * grid.pixel
    y0 = grid.y0 + window[0].start * grid.pixel
    return tuple((x0 + left * grid.pixel, y0 + top * grid.pixel, x0 + right * grid.pixel, y0 + bottom * grid.pixel)
                 for left, top, right, bottom in rects)


class _Plane:
    """One layer's copper, rasterized when first needed and then patched where it changes."""

    def __init__(self, version: int):
        self.shapes = {}  # key -> (item, label, polygons, bounds)
        self.nets = []  # label - 1 -> net
        self.label_of = {}
        self.raw = None
        self.eroded = {}  # radius -> labels
        self.pending = []  # changed rects not yet in the rasters
        self.version = version
        self.forgotten = version  # changes up to this version are no longer listed
        self.changes = []  # (version, rect)

    def label(self, net: str) -> int:
        if net not in self.label_of:
            self.nets.append(net)
            self.label_of[net] = len(self.nets)
        return self.label_of[net]

    def update(self, copper: dict, layer: str, version: int) -> bool:
        """Take this layer's copper ({key: (item, net)}); True when anything changed."""
        dirty = [self.shapes.pop(key)[3] for key in self.shapes.keys() - copper.keys()]
        for key, (item, net) in copper.items():
            old = self.shapes.get(key)
            if old is not None and (old[0] is item or old[0] == item):
                continue
            polygons = _polygons(item, layer)
            bounds = _bounds(polygons)
            if old is not None:
                dirty.append(old[3])
                del self.shapes[key]
            if bounds is not None:
                self.shapes[key] = (item, self.label(net), polygons, bounds)
                dirty.append(bounds)
        if not dirty:
            return False
        rects = _merged(dirty)
        self.version = version
        self.changes += [(version, rect) for rect in rects]
        if len(self.changes) > MAX_CHANGES:
            self.forgotten = self.changes[-MAX_CHANGES - 1][0]
            self.changes = self.changes[-MAX_CHANGES:]
        self.pending += rects
        return True

    def changed_since(self, stamp: int, rect: Rect) -> bool:
        """Did copper within `rect` change after version `stamp`?"""
        if stamp >= self.version:
            return False
        if stamp < self.forgotten:
            return True
        return any(version > stamp and _overlap(changed, rect) for version, changed in self.changes)

    def _paint(self, grid: Grid, rows: slice, columns: slice) -> np.ndarray:
        area = grid.rect(rows, columns)
        return rasterize(grid.sub(rows, columns), [(label, polygon) for _, label, polygons, bounds
                                                   in self.shapes.values() if _overlap(bounds, area)
                                                   for polygon in polygons])

    def raw_labels(self, grid: Grid, stats: dict) -> np.ndarray | None:
        if not self.shapes:
            return None
        if self.raw is None:
            self.raw = self._paint(grid, slice(0, grid.height), slice(0, grid.width))
            self.eroded, self.pending = {}, []
            stats["rasterized"] += 1
        elif self.pending:
            rects, self.pending = _merged(self.pending), []
            for rect in rects:
                window = grid.window(rect, 1)
                self.raw[window] = self._paint(grid, *window)
                for radius, eroded in self.eroded.items():
                    target, labels = _erode_window(self.raw, window, radius)
                    eroded[target] = labels
                stats["regions"] += 1
        return self.raw

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

    `stats` counts full rasterizations, updated regions, erosions, antipads found
    and looked-up items (for tests and timing).
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
        self.holes = {}  # net -> ((x, y, copper radius, first layer index, last layer index), ...)
        self.partners = {}  # net -> its differential-pair partner net
        self._parts = None
        self._holes_source = None
        self._versions = 0
        self._results = {}  # id -> (key, stamps, SegmentReference)
        self._antipads = {}  # (layer, hole, nets, radius) -> (stamp, patch or None)
        self.stats = {"rasterized": 0, "regions": 0, "eroded": 0, "antipads": 0, "computed": 0}

    def update(self, snapshot: model.BoardSnapshot) -> frozenset[str]:
        """Take a new snapshot; the layers whose copper changed."""
        if snapshot is self.snapshot:
            return frozenset()
        self.snapshot = snapshot
        order, gaps, self.exact = copper_order(snapshot)
        grid = board_grid(snapshot, self.pixel_nm, self.max_pixels)
        if order != self.order or grid != self.grid:
            self.order, self.grid = order, grid
            self.planes.clear()
            self._antipads.clear()
            self._parts = None
        self.neighbours = _neighbours(order, gaps)
        changed = set()
        # The reader keeps unchanged parts from poll to poll: compare those first.
        parts = (snapshot.tracks, snapshot.arcs, snapshot.vias, snapshot.pads, snapshot.zones, snapshot.graphics)
        if parts != self._parts:
            self._parts = parts
            for layer, copper in layer_copper(snapshot, order).items():
                if layer not in self.planes:
                    self._versions += 1
                    self.planes[layer] = _Plane(self._versions)
                if self.planes[layer].update(copper, layer, self._versions + 1):
                    self._versions += 1
                    changed.add(layer)
        self.items = {item.id: item for item in (*snapshot.tracks, *snapshot.arcs)}
        nets = {item.net for item in (*snapshot.tracks, *snapshot.arcs, *snapshot.vias)}
        self.partners = {net: partner for net in nets if (partner := diff_pair_partner(net, nets))}
        self._update_holes(snapshot)
        return frozenset(changed)

    def _update_holes(self, snapshot):
        """Vias and plated holes by net: where antipads are looked for (module docstring)."""
        source = (snapshot.vias, snapshot.pads, self.order)
        if source == self._holes_source:
            return
        self._holes_source = source
        index = {name: position for position, name in enumerate(self.order)}
        holes: dict[str, list] = {}
        for via in snapshot.vias:
            holes.setdefault(via.net, []).append((*via.pos, via.diameter / 2,
                                                  *_span(index, via.layer_top, via.layer_bottom)))
        for pad in snapshot.pads:
            if not pad.drill or min(pad.drill) <= 0 or not pad.net:
                continue
            reach = max((math.dist(pad.pos, p) for polygons in pad.polygons.values()
                         for polygon in polygons for ring in polygon for p in ring), default=max(pad.drill) / 2)
            holes.setdefault(pad.net, []).append((*pad.pos, reach, 0, len(self.order) - 1))
        self.holes = {net: tuple(found) for net, found in holes.items()}

    def nets_near(self, layer: str, path, reach_nm: float, margin_nm: int) -> tuple[str, ...]:
        """Nets of plane-sized copper on `layer` (what survives `margin_nm`, a Cover's
        margin) within `reach_nm` of the path's bounding box."""
        plane, grid = self.planes.get(layer), self.grid
        if plane is None or grid is None:
            return ()
        radius = margin_nm // grid.pixel
        labels = plane.eroded_labels(grid, radius, self.stats)
        if labels is None:
            return ()
        xs, ys = zip(*path)
        window = grid.window((min(xs), min(ys), max(xs), max(ys)), math.ceil(reach_nm / grid.pixel) + radius + 1)
        return tuple(sorted(plane.nets[label - 1] for label in np.unique(labels[window]).tolist() if label))

    def plane_breaks(self, layer: str, path, reach_nm: float, plane_nets) -> tuple[Rect, ...]:
        """Where `layer`'s plane is broken within `reach_nm` of the path: every pixel that
        is not copper of `plane_nets` (a void, slot, split channel, or another net's copper
        in it), each hole shown whole but clipped MAX_ANTIPAD_NM beyond the reach, as
        (left, top, right, bottom) rects. Empty without plane nets to compare with."""
        plane, grid = self.planes.get(layer), self.grid
        labels = plane.raw_labels(grid, self.stats) if plane is not None and grid is not None else None
        solid_labels = [plane.label_of[net] for net in plane_nets if plane is not None and net in plane.label_of]
        if labels is None or not solid_labels:
            return ()
        xs, ys = zip(*path)
        window = grid.window(_grow((min(xs), min(ys), max(xs), max(ys)), reach_nm + MAX_ANTIPAD_NM))
        solid = np.isin(labels[window], solid_labels)
        near = np.zeros(solid.shape, bool)
        for a, b in zip(path, path[1:] or path):  # a point per pixel along the path
            count = max(2, math.ceil(math.dist(a, b) / grid.pixel) + 1)
            columns = np.floor((np.linspace(a[0], b[0], count) - grid.x0) / grid.pixel).astype(np.int64)
            rows = np.floor((np.linspace(a[1], b[1], count) - grid.y0) / grid.pixel).astype(np.int64)
            columns -= window[1].start
            rows -= window[0].start
            inside = (rows >= 0) & (rows < near.shape[0]) & (columns >= 0) & (columns < near.shape[1])
            near[rows[inside], columns[inside]] = True
        for _ in range(math.ceil(reach_nm / grid.pixel)):  # the same square the erosion reads
            near = _grow_square(near)
        seeds = near & ~solid
        if not seeds.any():
            return ()
        return _runs(_flood(~solid, seeds), grid, window)

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
            if cached is not None and cached[0] == key and not self._near_changes(item, cached[1]):
                self._results[item_id] = (key, self._stamps(item), cached[2])
                found[item_id] = cached[2]
            else:
                stale.append((item, key))
        by_layer: dict[str, list] = {}
        for item, key in stale:
            by_layer.setdefault(item.layer, []).append((item, key))
        for layer, group in by_layer.items():
            for (item, key), result in zip(group, self._look_up(layer, [item for item, _ in group])):
                self._results[item.id] = (key, self._stamps(item), result)
                found[item.id] = result
        self.stats["computed"] += len(stale)
        if len(self._results) > 2 * len(self.items) + 1024:  # forget deleted items now and then
            self._results = {k: v for k, v in self._results.items() if k in self.items}
        return found

    def _key(self, item):
        """What an item's lookup depends on besides the copper near it (compared, not hashed)."""
        partner = self.partners.get(item.net)
        return (item, self.grid, self.neighbours.get(item.layer), self.holes.get(item.net),
                self.holes.get(partner))

    def _stamps(self, item):
        return tuple(self.planes[n.layer].version if n and n.layer in self.planes else None
                     for n in self.neighbours.get(item.layer, (None, None)))

    def _near_changes(self, item, stamps) -> bool:
        """Did copper change on a reference layer close enough to change this item's lookup
        (its widened footprint, or an antipad of a via next to it)?"""
        path = (item.start, item.end) if isinstance(item, model.Track) else (item.start, item.mid, item.end)
        xs, ys = zip(*path)
        for neighbour, stamp in zip(self.neighbours.get(item.layer, (None, None)), stamps):
            plane = self.planes.get(neighbour.layer) if neighbour else None
            if plane is None or stamp is None:
                continue
            reach = (item.width / 2 + 2 * (self.radius_px(neighbour.distance_nm) + 2) * self.grid.pixel +
                     2 * MAX_ANTIPAD_NM)
            if plane.changed_since(stamp, _grow((min(xs), min(ys), max(xs), max(ys)), reach)):
                return True
        return False

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
        found = samples.line_labels(grid, labels)
        self._fill_antipads(neighbour.layer, plane, radius, items, samples, found)
        agree = (found == found[:, :1]).all(axis=1)
        found = np.where(agree, found[:, 0], 0).astype(np.int32)
        return _covers_from_runs(neighbour, margin_nm, plane.nets, samples, found, lengths)

    def _fill_antipads(self, layer, plane, radius, items, samples, found):
        """Samples near an antipad of the item's own net or its pair partner's read the
        plane as if the antipad were filled (module docstring)."""
        position = self.order.index(layer)
        by_net: dict[str, list[int]] = {}
        for index, item in enumerate(items):
            by_net.setdefault(item.net, []).append(index)
        for net, indices in by_net.items():
            partner = self.partners.get(net)
            holes = [hole for owner in (net, partner) for hole in self.holes.get(owner, ())
                     if hole[3] <= position <= hole[4]]
            if not net or not holes:
                continue
            chosen = np.flatnonzero(np.isin(samples.item, indices))
            points = samples.points[chosen]
            reach = MAX_ANTIPAD_NM + (radius + 2) * self.grid.pixel + samples.half_width.max()
            low, high = points.min(axis=0) - reach, points.max(axis=0) + reach
            for hole in holes:
                if not (low[0] - hole[2] <= hole[0] <= high[0] + hole[2] and
                        low[1] - hole[2] <= hole[1] <= high[1] + hole[2]):
                    continue
                # Antipads next to each other (a pair's two vias) are filled together.
                reach = hole[2] + MAX_ANTIPAD_NM
                close = tuple(other for other in holes
                              if abs(other[0] - hole[0]) <= reach and abs(other[1] - hole[1]) <= reach)
                patch = self._antipad(layer, plane, hole, close, (net, partner), radius, samples.half_width.max())
                if patch is not None:
                    samples.relabel(self.grid, found, chosen, *patch)

    def _antipad(self, layer, plane, hole, close, nets, radius, half_width):
        """(window, eroded labels there) with the antipads of the `close` holes filled;
        None when none of them closes within MAX_ANTIPAD_NM of `hole`."""
        grid = self.grid
        key = (layer, hole, close, nets, radius)
        # Around every hole of the group, so a neighbour's antipad is never cut by the window.
        search = _union([(other[0] - other[2] - MAX_ANTIPAD_NM, other[1] - other[2] - MAX_ANTIPAD_NM,
                          other[0] + other[2] + MAX_ANTIPAD_NM, other[1] + other[2] + MAX_ANTIPAD_NM)
                         for other in (hole, *close)])
        extra = 2 * radius + math.ceil(half_width / grid.pixel) + 2
        cached = self._antipads.get(key)
        if cached is not None and not plane.changed_since(cached[0], _grow(search, (extra + radius) * grid.pixel)):
            return cached[1]
        patch = self._find_antipad(plane, close, nets, radius, search, extra)
        self._antipads[key] = (plane.version, patch)
        self.stats["antipads"] += 1
        return patch

    def _find_antipad(self, plane, holes, nets, radius, search, extra):
        grid = self.grid
        raw = plane.raw_labels(grid, self.stats)
        window = grid.window(search)
        area = raw[window]
        own = [plane.label_of[net] for net in nets if net in plane.label_of]
        allowed = (area == 0) | np.isin(area, own)
        region = np.zeros_like(allowed)
        centres = np.zeros_like(allowed)
        for hole in holes:
            seed = (math.floor((hole[1] - grid.y0) / grid.pixel) - window[0].start,
                    math.floor((hole[0] - grid.x0) / grid.pixel) - window[1].start)
            if not (0 <= seed[0] < area.shape[0] and 0 <= seed[1] < area.shape[1]) or not allowed[seed] or region[seed]:
                continue
            centres[seed] = True
            found = _flood(allowed, centres & ~region)
            if not (found[0].any() or found[-1].any() or found[:, 0].any() or found[:, -1].any()):
                region |= found  # closed: an antipad; one running past the window is a real void
        if not region.any():
            return None
        edge = _grow_square(region) & ~region
        around = area[edge]
        if not len(around):
            return None
        fill = np.bincount(around).argmax()  # the plane the antipads are cut from
        foreign = edge & (area != fill)  # another net's copper inside the same hole
        if foreign.any():
            # Holes that run together: keep only what lies nearer the own copper than
            # the other net's, so its clearance is not filled with the own antipad.
            own_copper = region & (np.isin(area, own) | centres)
            region &= _steps(own_copper) <= _steps(foreign)
        rows, columns = np.nonzero(region)
        inside = (slice(window[0].start + rows.min(), window[0].start + rows.max() + 1),
                  slice(window[1].start + columns.min(), window[1].start + columns.max() + 1))
        target = _grown(inside, extra, raw.shape)
        source = _grown(target, radius, raw.shape)
        patched = raw[source].copy()
        patched[rows + window[0].start - source[0].start, columns + window[1].start - source[1].start] = fill
        return grid.sub(*target), erode(patched, radius)[_inner(source, target)]


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
        across = _across(lines, int(lines.max(initial=1))) * self.half_width[:, None]  # (items, lines) nm
        offsets = across[self.item]  # (samples, lines)
        self.x = self.points[:, 0, None] + offsets * self.normal[:, 0, None]
        self.y = self.points[:, 1, None] + offsets * self.normal[:, 1, None]

    def line_labels(self, grid: Grid, labels: np.ndarray) -> np.ndarray:
        """(samples, lines) labels under every line point."""
        column = np.floor((self.x - grid.x0) / grid.pixel).astype(np.int64)
        row = np.floor((self.y - grid.y0) / grid.pixel).astype(np.int64)
        inside = (column >= 0) & (column < grid.width) & (row >= 0) & (row < grid.height)
        return np.where(inside, labels[np.clip(row, 0, grid.height - 1), np.clip(column, 0, grid.width - 1)], 0)

    def relabel(self, grid: Grid, found: np.ndarray, chosen: np.ndarray, window: Grid, labels: np.ndarray):
        """Read the chosen samples' line points inside `window` from `labels` instead."""
        column = np.floor((self.x[chosen] - window.x0) / grid.pixel).astype(np.int64)
        row = np.floor((self.y[chosen] - window.y0) / grid.pixel).astype(np.int64)
        inside = (column >= 0) & (column < window.width) & (row >= 0) & (row < window.height)
        if inside.any():
            part = found[chosen]
            part[inside] = labels[row[inside], column[inside]]
            found[chosen] = part


def _across(lines: np.ndarray, most: int) -> np.ndarray:
    """(items, most) fractions -1..1 of the half-width; an item with fewer lines repeats its centre."""
    table = np.zeros((len(lines), most))
    for count in np.unique(lines):
        if count > 1:
            table[lines == count, :count] = np.linspace(-1, 1, count)
    return table


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
            planes[owner].append((*span, nets[label - 1]))
    result = []
    for index, length in enumerate(lengths):
        uncovered = sum(b - a for a, b in gaps[index])
        fraction = 1.0 - uncovered / length if length else float(not gaps[index])
        result.append(Cover(neighbour.layer, neighbour.distance_nm, margin_nm, max(0.0, min(1.0, fraction)),
                            tuple(gaps[index]), tuple(planes[index])))
    return result
