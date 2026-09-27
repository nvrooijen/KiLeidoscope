"""Reference planes: the copper layers above and below a track, and their copper, rasterized.

The engine the return-path analysis (and the DC solver) builds on:

- Stackup: the copper layers top to bottom and the dielectric between neighbours
  (`copper_order`, `copper_neighbours`), exact from the stackup's thicknesses.
- Copper: every piece of copper per layer (zone fills, pads, copper graphics, tracks,
  arcs, via lands: `layer_copper`), rasterized onto one board grid with each pixel
  holding its net's label, 0 for no copper (`rasterize`, `Grid`).
- `ReferencePlanes` keeps those rasters over successive snapshots: a layer is
  rasterized once and then patched only where its copper changed, and it remembers
  where (`changed_since`), so analyses redo only what a change touches.
- Holes: where a plane is broken near a path (`plane_breaks`: voids, slots, split
  channels, another net's copper), and the antipads of a net's own vias and plated
  holes, found in the raster itself (`antipad`): a copper-free region around the hole
  that closes within `MAX_ANTIPAD_NM`. Where an own antipad runs into another net's
  clearance, only the part nearer the own copper counts.
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
MAX_ANTIPAD_NM = 2_000_000  # a copper-free region closing within this of an own via is its antipad
MAX_CHANGES = 64  # changed regions remembered per layer (older ones: redo everything)
MAX_REGIONS = 16  # more changed regions in one update are merged into one

Rect = tuple[int, int, int, int]  # (left, top, right, bottom) nm


@dataclass(frozen=True)
class Neighbour:
    layer: str
    distance_nm: int  # dielectric between the two copper faces


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

    def labels(self, grid: Grid, stats: dict) -> np.ndarray | None:
        if not self.shapes:
            return None
        if self.raw is None:
            self.raw = self._paint(grid, slice(0, grid.height), slice(0, grid.width))
            self.pending = []
            stats["rasterized"] += 1
        elif self.pending:
            rects, self.pending = _merged(self.pending), []
            for rect in rects:
                window = grid.window(rect, 1)
                self.raw[window] = self._paint(grid, *window)
                stats["regions"] += 1
        return self.raw


# --- The engine over successive snapshots ------------------------------------------------

class ReferencePlanes:
    """Stackup, copper rasters and holes of one board over successive snapshots.

        planes = ReferencePlanes()
        planes.update(snapshot)            # the layers whose copper changed; cheap when none did
        labels = planes.labels("In1.Cu")   # (height, width) int16 net labels, or None: no copper
        net = planes.net_of("In1.Cu", label)

    `stats` counts full rasterizations, patched regions and antipads found (for tests
    and timing).
    """

    def __init__(self, pixel_nm: int = PIXEL_NM, max_pixels: int = MAX_PIXELS):
        self.pixel_nm = pixel_nm
        self.max_pixels = max_pixels
        self.snapshot = None
        self.grid = None
        self.order = ()
        self.exact = True  # False: layer distances from evenly spaced heights, not the stackup
        self.neighbours = {}
        self.planes: dict[str, _Plane] = {}
        self.holes = {}  # net -> ((x, y, copper radius, first layer index, last layer index), ...)
        self.partners = {}  # net -> its differential-pair partner net
        self._parts = None
        self._holes_source = None
        self._versions = 0
        self._antipads = {}  # (layer, hole, close holes, nets) -> (stamp, antipad or None)
        self.stats = {"rasterized": 0, "regions": 0, "antipads": 0}

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

    def labels(self, layer: str) -> np.ndarray | None:
        """The layer's net labels on `grid` (0: no copper), or None when it has no copper."""
        plane = self.planes.get(layer)
        return plane.labels(self.grid, self.stats) if plane is not None and self.grid is not None else None

    def net_of(self, layer: str, label: int) -> str | None:
        """The net of a label on `layer`; None for 0 (no copper)."""
        return self.planes[layer].nets[label - 1] if label else None

    def label_of(self, layer: str, net: str) -> int | None:
        plane = self.planes.get(layer)
        return plane.label_of.get(net) if plane is not None else None

    def changed_since(self, layer: str, stamp: int, rect: Rect) -> bool:
        """Did the layer's copper within `rect` change after version `stamp` (a `version`)?"""
        plane = self.planes.get(layer)
        return plane is None or plane.changed_since(stamp, rect)

    def version(self, layer: str) -> int:
        plane = self.planes.get(layer)
        return plane.version if plane is not None else 0

    def plane_breaks(self, layer: str, path, reach_nm: float, plane_nets) -> tuple[Rect, ...]:
        """Where `layer`'s plane is broken within `reach_nm` of the path: every pixel that
        is not copper of `plane_nets` (a void, slot, split channel, or another net's copper
        in it), each hole shown whole but clipped MAX_ANTIPAD_NM beyond the reach, as
        (left, top, right, bottom) rects. Empty without plane nets to compare with."""
        plane, grid = self.planes.get(layer), self.grid
        labels = plane.labels(grid, self.stats) if plane is not None and grid is not None else None
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
        for _ in range(math.ceil(reach_nm / grid.pixel)):
            near = _grow_square(near)
        seeds = near & ~solid
        if not seeds.any():
            return ()
        return _runs(_flood(~solid, seeds), grid, window)

    def antipad(self, layer: str, hole, nets) -> tuple[tuple[slice, slice], np.ndarray, int] | None:
        """The antipad of a via or plated hole (an entry of `holes`) of one of `nets` (a net
        and its pair partner) on `layer`, joined with the antipads of their holes next to it:
        (window, mask of the antipad pixels in it, label of the plane it is cut from). None
        when the copper-free region around the hole does not close within MAX_ANTIPAD_NM
        (a real void, slot or split, not an antipad)."""
        grid = self.grid
        plane = self.planes.get(layer)
        if plane is None or grid is None or plane.labels(grid, self.stats) is None:
            return None
        position = self.order.index(layer) if layer in self.order else -1
        reach = hole[2] + MAX_ANTIPAD_NM
        close = tuple(other for net in nets for other in self.holes.get(net, ())
                      if other != hole and other[3] <= position <= other[4] and
                      abs(other[0] - hole[0]) <= reach and abs(other[1] - hole[1]) <= reach)
        # Around every hole of the group, so a neighbour's antipad is never cut by the window.
        search = _union([(other[0] - other[2] - MAX_ANTIPAD_NM, other[1] - other[2] - MAX_ANTIPAD_NM,
                          other[0] + other[2] + MAX_ANTIPAD_NM, other[1] + other[2] + MAX_ANTIPAD_NM)
                         for other in (hole, *close)])
        key = (layer, hole, close, tuple(nets))
        cached = self._antipads.get(key)
        if cached is not None and not plane.changed_since(cached[0], _grow(search, grid.pixel)):
            return cached[1]
        found = self._find_antipad(plane, (hole, *close), nets, search)
        self._antipads[key] = (plane.version, found)
        self.stats["antipads"] += 1
        return found

    def _find_antipad(self, plane, holes, nets, search):
        grid = self.grid
        raw = plane.labels(grid, self.stats)
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
        return window, region, int(fill)
