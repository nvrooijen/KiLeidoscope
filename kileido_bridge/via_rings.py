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


def _copper_by_net(snapshot):
    """(layer, net) -> {"segments": (n, 5) x0, y0, x1, y1, half width, "polygons": [polygon]},
    for named nets only (copper without a net connects nothing)."""
    found = {}

    def entry(layer, net):
        return found.setdefault((layer, net), {"segments": [], "polygons": []})

    for track in snapshot.tracks:
        if track.net:
            entry(track.layer, track.net)["segments"].append((*track.start, *track.end, track.width / 2))
    for arc in snapshot.arcs:  # its chords through the mid point: close enough to touch a via
        if arc.net:
            for a, b in ((arc.start, arc.mid), (arc.mid, arc.end)):
                entry(arc.layer, arc.net)["segments"].append((*a, *b, arc.width / 2))
    for item in (*snapshot.zones, *snapshot.graphics):
        if item.net:
            entry(item.layer, item.net)["polygons"] += item.polygons
    for pad in snapshot.pads:
        if pad.net:
            for layer, polygons in pad.polygons.items():
                entry(layer, pad.net)["polygons"] += polygons
    return {key: {"segments": np.asarray(value["segments"], np.float64).reshape(-1, 5),
                  "polygons": value["polygons"]} for key, value in found.items()}


def _touches(via: model.Via, copper) -> bool:
    """Copper of the via's net reaches its land: a segment within its half width, or a
    polygon over the via's centre or within its radius."""
    if copper is None or not via.net:
        return False
    x, y = via.pos
    radius = via.diameter / 2
    segments = copper["segments"]
    if len(segments) and (_segment_distance(segments[:, :4], x, y) <= radius + segments[:, 4]).any():
        return True
    for polygon in copper["polygons"]:
        rings = [np.asarray(ring, np.float64).reshape(-1, 2) for ring in polygon]
        if not rings or len(rings[0]) < 3:
            continue
        low, high = rings[0].min(axis=0), rings[0].max(axis=0)
        if x < low[0] - radius or x > high[0] + radius or y < low[1] - radius or y > high[1] + radius:
            continue
        if _inside(rings, x, y):
            return True
        edges = np.vstack([np.hstack((ring, np.roll(ring, -1, axis=0))) for ring in rings])
        if (_segment_distance(edges, x, y) <= radius).any():
            return True
    return False


def _segment_distance(segments, x, y):
    """Distance from (x, y) to each segment (x0, y0, x1, y1)."""
    a, b = segments[:, :2], segments[:, 2:4]
    ab = b - a
    length = np.maximum((ab * ab).sum(axis=1), 1e-12)
    t = np.clip(((np.array((x, y)) - a) * ab).sum(axis=1) / length, 0.0, 1.0)
    nearest = a + ab * t[:, None]
    return np.hypot(nearest[:, 0] - x, nearest[:, 1] - y)


def _inside(rings, x, y) -> bool:
    """Even-odd over the outer ring and its holes."""
    crossings = 0
    for ring in rings:
        x0, y0 = ring[:, 0], ring[:, 1]
        x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
        straddle = (y0 > y) != (y1 > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            cross_x = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
        crossings += int((straddle & (x < cross_x)).sum())
    return crossings % 2 == 1
