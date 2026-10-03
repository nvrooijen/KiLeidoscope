"""Which copper layers each via has an annular ring on (KiCad's "Annular rings").

A via keeps a ring on every layer it spans (KiCad's default), on its start and end
layers only, or only where it connects: KiCad flashes a layer when copper of the via's
net touches the via there (a track, a zone, a pad, a copper shape). The connection test
here is geometric: that copper reaches the via's land. Pure numpy over the snapshot's
integer nanometres.
"""

from __future__ import annotations

import numpy as np

from . import model

CONNECTING = (model.RINGS_CONNECTED, model.RINGS_ENDS_AND_CONNECTED)  # rings that follow the via's connections
MOST_ROWS = 4096  # the finest `_Edges` lists a net's polygon edges by


def copper_order(snapshot: model.BoardSnapshot) -> list[str]:
    """The board's copper layers, top to bottom (the stackup's order)."""
    names = [entry.name for entry in snapshot.stackup.layers if entry.type == "copper"]
    return names or ["F.Cu", "B.Cu"]


def ringed_masks(snapshot: model.BoardSnapshot, order: list[str]) -> np.ndarray:
    """Per via, a bitmask over `order`: bit i set where the via has a ring on order[i]."""
    index = {name: i for i, name in enumerate(order)}
    copper = None  # (layer, net) -> what can touch a via there; built on first need
    masks = np.zeros(len(snapshot.vias), np.int64)
    for k, via in enumerate(snapshot.vias):
        ends = [index[name] for name in (via.layer_top, via.layer_bottom) if name in index]
        if len(ends) < 2:
            continue
        first, last = min(ends), max(ends)
        spanned = range(first, last + 1)
        if via.rings == model.RINGS_ALL:
            layers = set(spanned)
        elif via.rings == model.RINGS_ENDS:
            layers = {first, last}
        else:
            if copper is None:
                copper = _copper_by_net(snapshot)
            layers = {i for i in spanned if _touches(via, copper.get((order[i], via.net)))}
            if via.rings == model.RINGS_ENDS_AND_CONNECTED:
                layers |= {first, last}
        masks[k] = sum(1 << i for i in layers)
    return masks


class _Copper:
    """One net's copper on one layer: its track segments, and its polygons (zone fills,
    pads, shapes), which become `_Edges` only when a via first asks for them."""

    def __init__(self):
        self.segments = []  # x0, y0, x1, y1, half width
        self.polygons = []
        self._edges = None

    def segment_rows(self):
        if isinstance(self.segments, list):
            self.segments = np.asarray(self.segments, np.float64).reshape(-1, 5)
        return self.segments

    def edges(self):
        if self._edges is None:
            self._edges = _Edges(self.polygons)
        return self._edges


def _copper_by_net(snapshot):
    """(layer, net) -> `_Copper`, for the nets of the vias whose rings follow their
    connections (copper without a net connects nothing, and no other net is asked for)."""
    nets = {via.net for via in snapshot.vias if via.rings in CONNECTING and via.net}
    found = {}

    def entry(layer, net):
        return found.setdefault((layer, net), _Copper())

    for track in snapshot.tracks:
        if track.net in nets:
            entry(track.layer, track.net).segments.append((*track.start, *track.end, track.width / 2))
    for arc in snapshot.arcs:  # its chords through the mid point: close enough to touch a via
        if arc.net in nets:
            for a, b in ((arc.start, arc.mid), (arc.mid, arc.end)):
                entry(arc.layer, arc.net).segments.append((*a, *b, arc.width / 2))
    for item in (*snapshot.zones, *snapshot.graphics):
        if item.net in nets:
            entry(item.layer, item.net).polygons += item.polygons
    for pad in snapshot.pads:
        if pad.net in nets:
            for layer, polygons in pad.polygons.items():
                entry(layer, pad.net).polygons += polygons
    return found


def _touches(via: model.Via, copper) -> bool:
    """Copper of the via's net reaches its land: a segment within its half width, or a
    polygon over the via's centre or within its radius."""
    if copper is None or not via.net:
        return False
    x, y = via.pos
    radius = via.diameter / 2
    segments = copper.segment_rows()
    if len(segments) and (_segment_distance(segments[:, :4], x, y) <= radius + segments[:, 4]).any():
        return True
    return bool(copper.polygons) and copper.edges().touch(x, y, radius)


class _Edges:
    """Every edge of a set of polygons (rings closed; a polygon's holes are its later
    rings), listed by the rows of the board it passes through.

    A via is tested against the edges in its own rows only, not against every edge of
    every polygon: a pour with an antipad for each foreign via has thousands of edges,
    and testing them all for every via of the net took seconds on a few hundred vias.
    The rows only narrow the search down. Each edge found is still tested exactly, so
    the answer does not depend on where the rows fall.
    """

    def __init__(self, polygons):
        edges, owner = [np.empty((0, 4))], [np.empty(0, np.int64)]
        for number, polygon in enumerate(polygons):
            rings = [np.asarray(ring, np.float64).reshape(-1, 2) for ring in polygon]
            if not rings or len(rings[0]) < 3:
                continue
            for ring in rings:
                edges.append(np.hstack((ring, np.roll(ring, -1, axis=0))))
                owner.append(np.full(len(ring), number, np.int64))
        self.edges, self.owner = np.vstack(edges), np.concatenate(owner)  # x0, y0, x1, y1; its polygon
        count = len(self.edges)
        low = np.minimum(self.edges[:, 1], self.edges[:, 3])
        high = np.maximum(self.edges[:, 1], self.edges[:, 3])
        self.bottom = float(low.min()) if count else 0.0
        self.rows = max(1, min(MOST_ROWS, int(np.sqrt(count))))
        self.height = max((float(high.max()) - self.bottom) / self.rows, 1.0) if count else 1.0
        first, last = self._row(low), self._row(high)
        spans = last - first + 1  # an edge is listed in every row it passes through
        edge = np.repeat(np.arange(count), spans)
        row = np.repeat(first, spans) + np.arange(len(edge)) - np.repeat(np.cumsum(spans) - spans, spans)
        by_row = np.argsort(row, kind="stable")
        self.listed = edge[by_row]
        self.starts = np.searchsorted(row[by_row], np.arange(self.rows + 1))

    def _row(self, y):
        """The row of a height; a height beyond the polygons counts as the nearest row."""
        return np.clip(np.floor((np.asarray(y, np.float64) - self.bottom) / self.height), 0,
                       self.rows - 1).astype(np.int64)

    def _between(self, y_low, y_high):
        """Every edge that reaches into the heights `y_low`..`y_high` (and some that do not)."""
        return self.listed[self.starts[int(self._row(y_low))]:self.starts[int(self._row(y_high)) + 1]]

    def touch(self, x, y, radius) -> bool:
        """A polygon lies over (x, y) (even-odd over its outer ring and holes), or an edge
        comes within `radius` of it."""
        level = self._between(y, y)
        x0, y0, x1, y1 = self.edges[level].T
        straddle = (y0 > y) != (y1 > y)
        if straddle.any():
            x0, y0, x1, y1 = x0[straddle], y0[straddle], x1[straddle], y1[straddle]
            right = x < x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if (np.bincount(self.owner[level][straddle][right]) % 2 == 1).any():
                return True
        near = self.edges[self._between(y - radius, y + radius)]
        return bool(len(near) and (_segment_distance(near, x, y) <= radius).any())


def _segment_distance(segments, x, y):
    """Distance from (x, y) to each segment (x0, y0, x1, y1)."""
    a, b = segments[:, :2], segments[:, 2:4]
    ab = b - a
    length = np.maximum((ab * ab).sum(axis=1), 1e-12)
    t = np.clip(((np.array((x, y)) - a) * ab).sum(axis=1) / length, 0.0, 1.0)
    nearest = a + ab * t[:, None]
    return np.hypot(nearest[:, 0] - x, nearest[:, 1] - y)
