"""Lookups and distances on a board snapshot, for findings.

Indexes by reference, pad, net, layer and item id replace linear scans. Shapes turn
records into line segments with a half width (tracks, arcs, vias, the outline) and
filled polygons (pads, zones, copper graphics, footprint boxes), so one set of distance
functions answers both "which item is nearest this point" and "where are two items
closest". A part measures as the copper of its pads (`BoardIndex.shape`), not its box,
which takes in its texts; only a part with no copper pads measures as its box. Values
are nanometres in KiCad's board frame (y down), as in `model`.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from . import model
from .geometry import sample_arc
from .model import Point

SHORT_ID = 8  # a uuid's last 8 hex name it: legacy uuids (00000000-…-00005a222dbd) share every prefix
_BLOCK = 1 << 20  # point/segment pairs computed at once (memory stays small)
_INNER = re.compile(r"In(\d+)\.Cu")

Record = object  # any copper record of `model`, a Footprint, or the Outline


def copper_rank(layer: str) -> int | None:
    """Position of a copper layer from the top (F.Cu 0, In1.Cu 1, ..., B.Cu last); None otherwise."""
    if layer == "F.Cu":
        return 0
    if layer == "B.Cu":
        return 1_000
    match = _INNER.fullmatch(layer)
    return int(match.group(1)) if match else None


def on_layer(record: Record, layer: str) -> bool:
    """Whether a copper record has copper on `layer`. A via counts on every layer of its
    drill span (its barrel), annular ring or not; a plain hole (no copper) on none."""
    if isinstance(record, model.Via):
        rank, top, bottom = copper_rank(layer), copper_rank(record.layer_top), copper_rank(record.layer_bottom)
        return None not in (rank, top, bottom) and min(top, bottom) <= rank <= max(top, bottom)
    if isinstance(record, model.Pad):
        return layer in record.polygons and layer not in model.PASTE_LAYERS
    return getattr(record, "layer", None) == layer


def has_copper(record: Record) -> bool:
    """False for a non-plated hole: a pad with no copper polygons."""
    return not isinstance(record, model.Pad) or any(layer not in model.PASTE_LAYERS for layer in record.polygons)


def top_layer(record: Record) -> str:
    """The layer a record is drawn or measured on when none is named: a via's top, a
    pad's F.Cu when it has copper there (else its first copper layer, else the layer its
    hole rides on), a part's side, Edge.Cuts for the outline, else the record's layer."""
    if isinstance(record, model.Via):
        return record.layer_top
    if isinstance(record, model.Pad):
        copper = [layer for layer in record.polygons if layer not in model.PASTE_LAYERS]
        layers = copper or record.layers
        return "F.Cu" if "F.Cu" in layers or not layers else layers[0]
    if isinstance(record, model.Footprint):
        return "B.Cu" if record.side == "bottom" else "F.Cu"
    if isinstance(record, model.Outline):
        return "Edge.Cuts"
    return getattr(record, "layer", "")


@dataclass(frozen=True)
class Shape:
    """Segments (n, 4: x1, y1, x2, y2) each widened by its radius, plus filled polygons
    (rings as (k, 2) arrays, outer ring first). A point is a segment of zero length.
    `probes` holds one point per line and per ring: with no crossing and no touch, a
    line or ring is wholly inside another shape's area or wholly outside it."""
    segments: np.ndarray
    radii: np.ndarray
    areas: tuple[tuple[np.ndarray, ...], ...]
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1, radii included
    probes: np.ndarray


def _shape(lines: list[tuple[float, float, float, float]], radius: float,
           polygons=(), outline_only: bool = False) -> Shape:
    """`lines` widened by `radius`; `polygons` filled, unless `outline_only` (edges, no area)."""
    rings = [tuple(np.asarray(ring, dtype=float) for ring in polygon if len(ring) >= 2) for polygon in polygons]
    rings = [polygon for polygon in rings if polygon]
    edges = [np.hstack([ring, np.roll(ring, -1, axis=0)]) for polygon in rings for ring in polygon]
    parts = ([np.asarray(lines, dtype=float).reshape(-1, 4)] if lines else []) + edges
    segments = np.vstack(parts) if parts else np.zeros((0, 4))
    radii = np.concatenate([np.full(len(lines), float(radius)), np.zeros(len(segments) - len(lines))])
    if len(segments):
        grow = radii[:, None]
        low = np.minimum(segments[:, :2], segments[:, 2:]) - grow
        high = np.maximum(segments[:, :2], segments[:, 2:]) + grow
        bbox = (*low.min(axis=0), *high.max(axis=0))
    else:
        bbox = (math.inf, math.inf, -math.inf, -math.inf)
    probes = [segments[:len(lines), :2]] + [ring[:1] for polygon in rings for ring in polygon]
    return Shape(segments, radii, () if outline_only else tuple(rings), tuple(float(v) for v in bbox),
                 np.vstack(probes) if probes else np.zeros((0, 2)))


def shape_of(record: Record, layer: str | None = None) -> Shape:
    """The copper (or, for a footprint, its box; for the outline, its edges) of a record.
    A pad on `layer` uses that layer's polygons; without a layer, all its copper layers."""
    if isinstance(record, model.Track):
        return _shape([(*record.start, *record.end)], record.width / 2)
    if isinstance(record, model.Arc):
        points = sample_arc(record.start, record.mid, record.end)
        return _shape([(*a, *b) for a, b in zip(points, points[1:])] or [(*record.start, *record.end)],
                      record.width / 2)
    if isinstance(record, model.Via):
        return _shape([(*record.pos, *record.pos)], record.diameter / 2)
    if isinstance(record, model.Pad):
        hole = _shape([(*record.pos, *record.pos)], min(record.drill) / 2 if record.drill else 0)
        if layer is not None and layer in record.layers and layer not in record.polygons:
            return hole  # a drilled pad on a layer where it has no copper (model.Pad.layers): its hole
        layers = [layer] if layer in record.layers and layer in record.polygons else \
            [name for name in record.polygons if name not in model.PASTE_LAYERS]
        polygons = [polygon for name in layers for polygon in record.polygons.get(name, ())]
        if polygons:
            return _shape([], 0, polygons)
        return hole
    if isinstance(record, (model.ZoneFill, model.CopperGraphic)):
        return _shape([], 0, record.polygons)
    if isinstance(record, model.Footprint):
        if record.bbox_nm is None:
            return _shape([(*record.pos, *record.pos)], 0)
        x, y, width, height = record.bbox_nm
        return _shape([], 0, [(((x, y), (x + width, y), (x + width, y + height), (x, y + height)),)])
    if isinstance(record, model.Outline):
        return _shape([], 0, record.polygons, outline_only=True)
    raise TypeError(f"no shape for {type(record).__name__}")


def union(shapes) -> Shape:
    """Several shapes as one: their segments, areas and probes together."""
    shapes = [shape for shape in shapes if len(shape.segments)]
    if not shapes:
        return _shape([], 0)
    boxes = np.array([shape.bbox for shape in shapes])
    return Shape(np.vstack([shape.segments for shape in shapes]), np.concatenate([shape.radii for shape in shapes]),
                 tuple(area for shape in shapes for area in shape.areas),
                 (float(boxes[:, 0].min()), float(boxes[:, 1].min()), float(boxes[:, 2].max()), float(boxes[:, 3].max())),
                 np.vstack([shape.probes for shape in shapes]))


def centre(record: Record) -> Point:
    """A representative point: a track's middle, an arc's mid point, a pad's or via's or
    footprint's position, the middle of a zone's box."""
    if isinstance(record, model.Track):
        return ((record.start[0] + record.end[0]) // 2, (record.start[1] + record.end[1]) // 2)
    if isinstance(record, model.Arc):
        return record.mid
    if isinstance(record, (model.Via, model.Pad, model.Footprint)):
        return record.pos
    x0, y0, x1, y1 = shape_of(record).bbox
    return (round((x0 + x1) / 2), round((y0 + y1) / 2))


@dataclass(frozen=True)
class Gap:
    """Copper edge to copper edge: `distance` in nm (0 when touching or overlapping), and
    the closest point on each side (the same point when they touch)."""
    distance: float
    a: Point
    b: Point


def _point_segments(px: np.ndarray, py: np.ndarray, segments: np.ndarray):
    """Distances (P, S) from points to segments, and the closest points on the segments."""
    ax, ay, bx, by = (segments[:, i][None, :] for i in range(4))
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = ((px[:, None] - ax) * dx + (py[:, None] - ay) * dy) / np.where(length2 == 0, 1, length2)
    t = np.clip(np.where(length2 == 0, 0, t), 0, 1)
    cx, cy = ax + t * dx, ay + t * dy
    return np.hypot(px[:, None] - cx, py[:, None] - cy), cx, cy


def _inside(points: np.ndarray, polygon: tuple[np.ndarray, ...]) -> np.ndarray:
    """Even-odd test of points (P, 2) against one polygon with holes."""
    inside = np.zeros(len(points), dtype=bool)
    for ring in polygon:
        x1, y1 = ring[:, 0][None, :], ring[:, 1][None, :]
        x2, y2 = np.roll(ring[:, 0], -1)[None, :], np.roll(ring[:, 1], -1)[None, :]
        step = max(1, _BLOCK // max(1, len(ring)))
        for start in range(0, len(points), step):
            px, py = points[start:start + step, 0][:, None], points[start:start + step, 1][:, None]
            straddle = (y1 > py) != (y2 > py)
            with np.errstate(divide="ignore", invalid="ignore"):
                cross_x = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            inside[start:start + step] ^= (np.count_nonzero(straddle & (px < cross_x), axis=1) % 2).astype(bool)
    return inside


def _ends(segments: np.ndarray, radii: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Both ends of every segment, with their segment's radius."""
    return np.vstack([segments[:, :2], segments[:, 2:]]), np.concatenate([radii, radii])


def _boxes(segments: np.ndarray, radii: np.ndarray) -> np.ndarray:
    """Copper box (x0, y0, x1, y1) of every segment."""
    grow = radii[:, None]
    return np.hstack([np.minimum(segments[:, :2], segments[:, 2:]) - grow,
                      np.maximum(segments[:, :2], segments[:, 2:]) + grow])


def _box_gaps(boxes: np.ndarray, box) -> np.ndarray:
    """Distance from each box to one box: never more than the copper inside them."""
    dx = np.maximum(np.maximum(box[0] - boxes[:, 2], boxes[:, 0] - box[2]), 0)
    dy = np.maximum(np.maximum(box[1] - boxes[:, 3], boxes[:, 1] - box[3]), 0)
    return np.hypot(dx, dy)


def _enclosing(boxes: np.ndarray):
    return (boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max())


def _point_in_areas(points: np.ndarray, shape: Shape) -> np.ndarray | None:
    """The first of `points` inside a filled polygon of `shape`, or None."""
    for polygon in shape.areas:
        hits = np.flatnonzero(_inside(points, polygon))
        if len(hits):
            return points[hits[0]]
    return None


def _ends_to_segments(points: np.ndarray, point_radii: np.ndarray, segments: np.ndarray, radii: np.ndarray):
    """(edge gap, point index, closest point on the segments, that segment's radius) of the
    closest point/segment pair, radii subtracted; None when either side is empty."""
    if not len(points) or not len(segments):
        return None
    best = None
    step = max(1, _BLOCK // len(segments))
    for start in range(0, len(points), step):
        px, py = points[start:start + step, 0], points[start:start + step, 1]
        distance, cx, cy = _point_segments(px, py, segments)
        edge = distance - point_radii[start:start + step, None] - radii[None, :]
        i, j = np.unravel_index(np.argmin(edge), edge.shape)
        if best is None or edge[i, j] < best[0]:
            best = (float(edge[i, j]), start + i, np.array([cx[i, j], cy[i, j]]), float(radii[j]))
    return best


_SAMPLE = 64  # ends measured in full to find an upper bound before narrowing


def _upper_bound(a: Shape, b: Shape) -> float:
    """An edge gap that some pair really has, so the closest pair is no farther apart:
    the ends of `a` nearest `b`'s box, measured against all of `b`, both ways, plus a
    thinned end-to-end pass that finds the close region when the boxes overlap."""
    bound = math.inf
    for one, other in ((a, b), (b, a)):
        ends, radii = _ends(one.segments, one.radii)
        pick = np.argsort(_box_gaps(np.hstack([ends, ends]) + np.c_[-radii, -radii, radii, radii],
                                    other.bbox), kind="stable")[:_SAMPLE]
        result = _ends_to_segments(ends[pick], radii[pick], other.segments, other.radii)
        if result:
            bound = min(bound, result[0])
    ends_a, radii_a = _ends(a.segments, a.radii)
    ends_b, radii_b = _ends(b.segments, b.radii)
    thin_a = slice(None, None, max(1, len(ends_a) // 512))
    thin_b = slice(None, None, max(1, len(ends_b) // 512))
    apart = (np.hypot(*(ends_a[thin_a, None, :] - ends_b[None, thin_b, :]).transpose(2, 0, 1))
             - radii_a[thin_a, None] - radii_b[None, thin_b])
    return max(0.0, min(bound, float(apart.min())))


def _point(xy) -> Point:
    return (int(round(float(xy[0]))), int(round(float(xy[1]))))


def gap(a: Shape, b: Shape) -> Gap:
    """Closest approach of two shapes' copper. Two segment sets come closest at an end of
    one of them unless they cross, so ends against segments both ways, crossings and
    "inside a filled polygon" together cover every case. Only segment pairs whose boxes
    lie within a gap some pair really has are measured, so large zones stay fast."""
    if not len(a.segments) or not len(b.segments):
        raise ValueError("gap between empty shapes")
    for inner, outer in ((a, b), (b, a)):
        hit = _point_in_areas(inner.probes, outer) if outer.areas and len(inner.probes) else None
        if hit is not None:
            return Gap(0.0, _point(hit), _point(hit))
    limit = _upper_bound(a, b)
    boxes_a, boxes_b = _boxes(a.segments, a.radii), _boxes(b.segments, b.radii)
    keep_a = np.flatnonzero(_box_gaps(boxes_a, b.bbox) <= limit + 1.0)  # cheap: one box per side first
    keep_b = np.flatnonzero(_box_gaps(boxes_b, _enclosing(boxes_a[keep_a])) <= limit + 1.0)
    keep_a = keep_a[_box_gaps(boxes_a[keep_a], _enclosing(boxes_b[keep_b])) <= limit + 1.0]
    best = None  # (edge, start point, its radius, nearest point, its radius, start is on b)
    for i, j in _pairs(boxes_a[keep_a], boxes_b[keep_b], limit):
        i, j = keep_a[i], keep_b[j]
        sa, ra, sb, rb = a.segments[i], a.radii[i], b.segments[j], b.radii[j]
        crossing = _crossings(sa, sb)
        if crossing is not None:
            return Gap(0.0, _point(crossing), _point(crossing))
        for ends, end_radii, segments, radii, on_b in ((sa[:, :2], ra, sb, rb, False), (sa[:, 2:], ra, sb, rb, False),
                                                       (sb[:, :2], rb, sa, ra, True), (sb[:, 2:], rb, sa, ra, True)):
            distance, cx, cy = _point_segment_pairs(ends, segments)
            edge = distance - end_radii - radii
            k = int(np.argmin(edge))
            if best is None or edge[k] < best[0]:
                best = (float(edge[k]), ends[k], float(end_radii[k]), np.array([cx[k], cy[k]]), float(radii[k]), on_b)
    edge, start, start_radius, near, near_radius, swapped = best
    axis = near - start
    length = float(np.hypot(*axis))
    unit = axis / length if length else np.zeros(2)
    if edge <= 0:  # the middle of where the two coppers overlap along the axis
        low, high = max(-start_radius, length - near_radius), min(start_radius, length + near_radius)
        here = there = start + unit * min(max((low + high) / 2, 0.0), length)
    else:
        here, there = start + unit * start_radius, near - unit * near_radius
    if swapped:
        here, there = there, here
    return Gap(max(0.0, edge), _point(here), _point(there))


def _pairs(boxes_a: np.ndarray, boxes_b: np.ndarray, limit: float):
    """Index arrays (i into a, j into b) of segment pairs whose copper boxes lie within
    `limit`, in blocks of at most `_BLOCK` pairs. The closest pair is always among them."""
    step = max(1, _BLOCK // max(1, len(boxes_b)))
    limit2 = (limit + 1.0) ** 2  # 1 nm slack: the pair that set `limit` must never round out
    pending_i, pending_j = [], []
    count = 0
    for start in range(0, len(boxes_a), step):
        block = boxes_a[start:start + step]
        dx = np.maximum(np.maximum(boxes_b[None, :, 0] - block[:, None, 2], block[:, None, 0] - boxes_b[None, :, 2]), 0)
        dy = np.maximum(np.maximum(boxes_b[None, :, 1] - block[:, None, 3], block[:, None, 1] - boxes_b[None, :, 3]), 0)
        i, j = np.nonzero(dx * dx + dy * dy <= limit2)
        pending_i.append(i + start)
        pending_j.append(j)
        count += len(i)
        if count >= _BLOCK:
            yield np.concatenate(pending_i), np.concatenate(pending_j)
            pending_i, pending_j, count = [], [], 0
    if count:
        yield np.concatenate(pending_i), np.concatenate(pending_j)


def _point_segment_pairs(points: np.ndarray, segments: np.ndarray):
    """Distance from points[k] to segments[k], and the closest points on the segments."""
    ax, ay, bx, by = segments.T
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = ((points[:, 0] - ax) * dx + (points[:, 1] - ay) * dy) / np.where(length2 == 0, 1, length2)
    t = np.clip(np.where(length2 == 0, 0, t), 0, 1)
    cx, cy = ax + t * dx, ay + t * dy
    return np.hypot(points[:, 0] - cx, points[:, 1] - cy), cx, cy


def _crossings(a: np.ndarray, b: np.ndarray) -> np.ndarray | None:
    """Where a[k] properly crosses b[k] for the first such k, or None."""
    def side(seg, px, py):
        return np.sign((seg[:, 2] - seg[:, 0]) * (py - seg[:, 1]) - (seg[:, 3] - seg[:, 1]) * (px - seg[:, 0]))
    crosses = ((side(a, b[:, 0], b[:, 1]) * side(a, b[:, 2], b[:, 3]) < 0) &
               (side(b, a[:, 0], a[:, 1]) * side(b, a[:, 2], a[:, 3]) < 0))
    hits = np.flatnonzero(crosses)
    if not len(hits):
        return None
    k = hits[0]
    p, r = a[k, :2], a[k, 2:] - a[k, :2]
    q, s = b[k, :2], b[k, 2:] - b[k, :2]
    t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / (r[0] * s[1] - r[1] * s[0])
    return p + t * r


def point_gap(shape: Shape, point: Point) -> Gap:
    """From a point to a shape's copper edge (0 inside it); `a` is the point, `b` the copper."""
    p = np.array([point], dtype=float)
    if _point_in_areas(p, shape) is not None:
        return Gap(0.0, point, point)
    result = _ends_to_segments(p, np.zeros(1), shape.segments, shape.radii)
    if result is None:
        raise ValueError("gap to an empty shape")
    edge, _, near, near_radius = result
    axis = near - p[0]
    length = float(np.hypot(*axis))
    if edge <= 0:
        return Gap(0.0, point, point)
    return Gap(edge, point, _point(near - axis / length * near_radius))


def _bbox_gap(box_a, box_b) -> float:
    dx = max(box_a[0] - box_b[2], box_b[0] - box_a[2], 0.0)
    dy = max(box_a[1] - box_b[3], box_b[1] - box_a[3], 0.0)
    return math.hypot(dx, dy)


@dataclass(frozen=True)
class Near:
    record: Record
    gap: Gap  # `a` the query side, `b` on the record


class BoardIndex:
    """One snapshot's items by id, short id, reference, pad, net and layer, with cached shapes."""

    def __init__(self, snapshot: model.BoardSnapshot):
        self.snapshot = snapshot
        self.copper: tuple[Record, ...] = tuple(
            record for record in (*snapshot.tracks, *snapshot.arcs, *snapshot.vias, *snapshot.pads,
                                  *snapshot.zones, *snapshot.graphics) if has_copper(record))
        self._by_id: dict[str, list[Record]] = defaultdict(list)
        self._by_short: dict[str, set[str]] = defaultdict(set)
        self._by_reference: dict[str, list[model.Footprint]] = defaultdict(list)
        self._footprints: dict[str, model.Footprint] = {footprint.id: footprint for footprint in snapshot.footprints}
        self._pads: dict[tuple[str, str], list[model.Pad]] = defaultdict(list)
        self._copper_pads: dict[str, list[model.Pad]] = defaultdict(list)  # footprint id -> its copper pads
        self._by_net: dict[str, list[Record]] = defaultdict(list)
        self._shapes: dict[tuple, Shape] = {}
        self._layers: tuple[str, ...] | None = None
        holes = (pad for pad in snapshot.pads if not has_copper(pad))  # copper pads are in `copper`
        for record in (*self.copper, *holes, *snapshot.footprints):
            key = record.id.lower()
            self._by_id[key].append(record)
            self._by_short[key[-SHORT_ID:]].add(key)
        reference_of = {}
        for footprint in snapshot.footprints:
            self._by_reference[footprint.reference].append(footprint)
            reference_of[footprint.id] = footprint.reference
        for pad in snapshot.pads:
            if pad.footprint_id in reference_of:
                self._pads[(reference_of[pad.footprint_id], pad.number)].append(pad)
            if has_copper(pad):
                self._copper_pads[pad.footprint_id].append(pad)
        for record in self.copper:
            if record.net:
                self._by_net[record.net].append(record)

    @property
    def copper_layers(self) -> tuple[str, ...]:
        """The board's copper layers top to bottom: every layer an item has copper on."""
        if self._layers is None:
            names = set()
            for record in self.copper:
                if isinstance(record, model.Via):
                    names.update((record.layer_top, record.layer_bottom))
                elif isinstance(record, model.Pad):
                    names.update(record.layers)
                elif getattr(record, "layer", ""):
                    names.add(record.layer)
            self._layers = tuple(sorted((name for name in names if copper_rank(name) is not None), key=copper_rank))
        return self._layers

    def layers_of(self, record: Record) -> tuple[str, ...]:
        """The copper layers a record has copper on (a via: every layer of its span)."""
        if isinstance(record, model.Via):
            return tuple(layer for layer in self.copper_layers if on_layer(record, layer))
        if isinstance(record, model.Pad):
            return tuple(record.layers)
        layer = getattr(record, "layer", "")
        return (layer,) if copper_rank(layer or "") is not None else ()

    @property
    def nets(self) -> frozenset[str]:
        return frozenset(self._by_net)

    def footprints(self, reference: str) -> tuple[model.Footprint, ...]:
        return tuple(self._by_reference.get(reference, ()))

    def footprint_of(self, pad: model.Pad) -> model.Footprint | None:
        """The part a pad belongs to; None for a pad whose part the snapshot lacks."""
        return self._footprints.get(pad.footprint_id)

    def pads(self, reference: str, number: str) -> tuple[model.Pad, ...]:
        """Every pad with this number on the part: KiCad allows repeats (thermal pads)."""
        return tuple(self._pads.get((reference, number), ()))

    def net_items(self, net: str, layer: str | None = None) -> tuple[Record, ...]:
        items = self._by_net.get(net, ())
        return tuple(item for item in items if layer is None or on_layer(item, layer))

    def layer_items(self, layer: str) -> tuple[Record, ...]:
        return tuple(item for item in self.copper if on_layer(item, layer))

    def ids_ending(self, text: str) -> tuple[str, ...]:
        """Distinct item ids equal to `text` or ending in it (at least the last 8 hex)."""
        key = text.strip().lower()
        if key in self._by_id:
            return (key,)
        if len(key) < SHORT_ID:
            return ()
        return tuple(sorted(item for item in self._by_short.get(key[-SHORT_ID:], ()) if item.endswith(key)))

    def records(self, item_id: str) -> tuple[Record, ...]:
        """Every record with this id (a zone has one per copper layer)."""
        return tuple(self._by_id.get(item_id.lower(), ()))

    def shape(self, record: Record, layer: str | None = None) -> Shape:
        """The shape distances are measured on: `shape_of`, except a part, which is the
        copper of its pads (those on `layer` when it has any there; its box, which takes
        in its texts, only when it has no copper pads at all)."""
        if isinstance(record, model.Outline):
            return shape_of(record)
        key = (id(record), layer)  # records live as long as the snapshot this index holds
        shape = self._shapes.get(key)
        if shape is None:
            pads = self._copper_pads.get(record.id, ()) if isinstance(record, model.Footprint) else ()
            if pads:
                on = [pad for pad in pads if layer is not None and on_layer(pad, layer)]
                shape = union(self.shape(pad, layer if on else None) for pad in (on or pads))
            else:
                shape = shape_of(record, layer)
            self._shapes[key] = shape
        return shape

    def nearest(self, point: Point, candidates, layer: str | None = None,
                max_distance: float | None = None) -> Near | None:
        """The candidate whose copper is nearest `point` (within `max_distance`), checked
        in order of box distance so far-away items are never measured."""
        box = (point[0], point[1], point[0], point[1])
        shapes = [(record, self.shape(record, layer)) for record in candidates]
        order = sorted(((_bbox_gap(shape.bbox, box), i) for i, (_, shape) in enumerate(shapes)
                        if len(shape.segments)), key=lambda item: item[0])
        best = None
        for lower, i in order:
            limit = best.gap.distance if best else max_distance
            if limit is not None and lower > limit:
                break
            record, shape = shapes[i]
            result = point_gap(shape, point)
            if (best is None or result.distance < best.gap.distance) and \
                    (max_distance is None or result.distance <= max_distance):
                best = Near(record, result)
        return best

    def closest(self, items_a, items_b, layer: str | None = None) -> tuple[Gap, Record, Record] | None:
        """Where any of `items_a` comes closest to any of `items_b` (edge to edge)."""
        shapes_a = [(record, self.shape(record, layer)) for record in items_a]
        shapes_b = [(record, self.shape(record, layer)) for record in items_b]
        pairs = sorted(((_bbox_gap(sa.bbox, sb.bbox), ra, sa, rb, sb)
                        for ra, sa in shapes_a if len(sa.segments)
                        for rb, sb in shapes_b if len(sb.segments)), key=lambda item: item[0])
        best = None
        for lower, ra, sa, rb, sb in pairs:
            if best is not None and lower > best[0].distance:
                break
            result = gap(sa, sb)
            if best is None or result.distance < best[0].distance:
                best = (result, ra, rb)
        return best
