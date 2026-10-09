"""Tombstone risk of small two-pad SMD parts (chip R/C/L, small diodes), from the board side.

Tombstoning starts when one end of a light part wets before the other and its pull
outweighs the part: the causes are imbalances between the two pads. Everything here is
measured on the board as KiCad has it; nothing comes from the part's 3D model.

Per pad: its copper and paste areas, and how copper of its own net surrounds it, ring by
ring out to `REACH_NM` (a thermal relief's spokes, a trace, a plane). That ring profile
gives the conduction path that draws heat away from the pad. Pure numpy: the copper of a
layer is rasterised once, in strips, by scanline (winding numbers, so overlapping copper
of one net stays copper), and each pad samples only a small window of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import model
from .geometry import sample_arc

REACH_NM = 500_000  # rings measured around each pad, out to this far
CELL_NM = 40_000  # raster cell (a 0.2 mm spoke is 8 cells wide)
MAX_PAD_NM = 2_000_000  # larger pads are not a small chip part's
MAX_PITCH_NM = 4_000_000  # pad centres further apart than a 1210's are not either
STRIP_CELLS = 2_000_000  # cells rasterised at once
CHUNK_PADS = 512  # pad windows sampled at once
COPPER_W_MK = 390.0  # thermal conductivity of copper
DEFAULT_COPPER_NM = 35_000
DEFAULT_BOARD_NM = 1_600_000
PLATING_NM = 25_000  # via wall plating (IPC-6012 class 2 asks 20 um on average)


@dataclass(frozen=True)
class Candidate:
    footprint: model.Footprint
    pads: tuple[model.Pad, model.Pad]
    layer: str  # the copper layer both pads are on


@dataclass(frozen=True)
class PadMeasure:
    pad_id: str
    copper_nm2: float
    paste_nm2: float
    paste_perimeter_nm: float  # the stencil aperture's walls (area ratio)
    length_nm: float  # along the part (centre to centre axis)
    width_nm: float  # across it
    coverage: tuple[float, ...]  # share of each ring (one cell wide) that is the pad's own net
    resistance_k_w: float  # conduction resistance out to REACH_NM through that copper (inf: none)
    vias: int  # vias of the pad's net within REACH_NM (in the pad included)
    open_via_in_pad: bool  # a via in the pad that is neither filled nor capped: solder wicks into it


def candidates(snapshot: model.BoardSnapshot) -> list[Candidate]:
    """Footprints with exactly two SMD pads on one outer layer, small and close together,
    and not marked Do not populate."""
    by_footprint: dict[str, list[model.Pad]] = {}
    for pad in snapshot.pads:
        by_footprint.setdefault(pad.footprint_id, []).append(pad)
    found = []
    for footprint in snapshot.footprints:
        pads = by_footprint.get(footprint.id, [])
        if footprint.dnp or len(pads) != 2 or any(pad.drill for pad in pads):
            continue
        layers = [pad.layers for pad in pads]
        if layers[0] != layers[1] or len(layers[0]) != 1 or layers[0][0] not in ("F.Cu", "B.Cu"):
            continue
        layer = layers[0][0]
        if any(_largest_side(pad, layer) > MAX_PAD_NM for pad in pads):
            continue
        if math.dist(pads[0].pos, pads[1].pos) > MAX_PITCH_NM:
            continue
        found.append(Candidate(footprint, (pads[0], pads[1]), layer))
    return found


def measure(snapshot: model.BoardSnapshot, found: list[Candidate]) -> dict[str, PadMeasure]:
    """Every candidate pad's measure, by pad id."""
    thickness = {entry.name: entry.thickness_nm for entry in snapshot.stackup.layers if entry.type == "copper"}
    results = {}
    for layer in ("F.Cu", "B.Cu"):
        on_layer = [c for c in found if c.layer == layer]
        if not on_layer:
            continue
        pads = [pad for c in on_layer for pad in c.pads]
        partners = [pad for c in on_layer for pad in reversed(c.pads)]
        paste = "F.Paste" if layer == "F.Cu" else "B.Paste"
        frames = _frames(pads, partners, layer)  # cx, cy, ux, uy, half_u, half_v
        coverage = _ring_coverage(snapshot, layer, pads, frames)
        copper_m = (thickness.get(layer) or DEFAULT_COPPER_NM) * 1e-9
        rings = _ring_resistance(coverage, frames[:, 4], frames[:, 5], copper_m)
        copper = _areas([pad.polygons.get(layer, ()) for pad in pads])
        stencil = _areas([pad.polygons.get(paste, ()) for pad in pads])
        walls = _perimeters([pad.polygons.get(paste, ()) for pad in pads])
        vias, open_in_pad, via_conductance = _vias(snapshot, pads, frames, layer, rings)
        with np.errstate(divide="ignore"):
            resistance = 1 / (1 / rings.sum(axis=1) + via_conductance)
        for k, pad in enumerate(pads):
            results[pad.id] = PadMeasure(
                pad.id, float(copper[k]), float(stencil[k]), float(walls[k]),
                float(2 * frames[k, 4]), float(2 * frames[k, 5]),
                tuple(coverage[k].tolist()), float(resistance[k]), int(vias[k]), bool(open_in_pad[k]))
    return results


# --- Pads ---

def _rings(items, outer_only=False):
    """The rings of each item's polygons, flattened: points, ring sizes, each ring's item,
    and whether it is an outer ring (not a hole)."""
    points, sizes, owner, outer = [], [], [], []
    for number, polygons in enumerate(items):
        for polygon in polygons:
            for index, ring in enumerate(polygon[:1] if outer_only else polygon):
                if len(ring) >= 3:
                    points.extend(ring)
                    sizes.append(len(ring))
                    owner.append(number)
                    outer.append(index == 0)
    return (np.asarray(points, np.float64).reshape(-1, 2), np.asarray(sizes, np.int64),
            np.asarray(owner, np.int64), np.asarray(outer, bool))


def _areas(items) -> np.ndarray:
    """Per item, the area of its polygons (outer rings minus holes), nm^2."""
    points, sizes, owner, outer = _rings(items)
    if not len(sizes):
        return np.zeros(len(items))
    first = np.cumsum(sizes) - sizes
    following = np.arange(len(points)) + 1
    following[first + sizes - 1] = first
    a, b = points, points[following]
    area = np.abs(np.add.reduceat(a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1], first)) / 2
    return np.bincount(owner, np.where(outer, area, -area), len(items))


def _perimeters(items) -> np.ndarray:
    """Per item, the length of all its rings (outer and holes), nm."""
    points, sizes, owner, _ = _rings(items)
    if not len(sizes):
        return np.zeros(len(items))
    first = np.cumsum(sizes) - sizes
    following = np.arange(len(points)) + 1
    following[first + sizes - 1] = first
    length = np.hypot(*(points[following] - points).T)
    return np.bincount(np.repeat(owner, sizes), length, len(items))


def _largest_side(pad: model.Pad, layer: str) -> float:
    rings = [np.asarray(polygon[0], np.float64) for polygon in pad.polygons.get(layer, ()) if polygon]
    if not rings:
        return 0.0
    points = np.vstack(rings)
    return float((points.max(axis=0) - points.min(axis=0)).max())


def _frames(pads, partners, layer) -> np.ndarray:
    """Each pad as a rectangle along its part: centre, unit axis towards the partner pad
    (u), and half extents along u and across it (v). Rounded corners and chamfers lie
    inside it: it is the pad's bounding box in the part's own axes."""
    points, sizes, owner, _ = _rings([pad.polygons.get(layer, ()) for pad in pads], outer_only=True)
    axis = (np.asarray([p.pos for p in partners], np.float64) - np.asarray([p.pos for p in pads], np.float64))
    length = np.hypot(axis[:, 0], axis[:, 1])
    u = np.where(length[:, None] > 0, axis / np.maximum(length, 1)[:, None], (1.0, 0.0))
    v = np.column_stack((-u[:, 1], u[:, 0]))
    item = np.repeat(owner, sizes)  # rings come in pad order
    along = (points * u[item]).sum(axis=1)
    across = (points * v[item]).sum(axis=1)
    first = np.searchsorted(item, np.arange(len(pads)))
    low_u, high_u = np.minimum.reduceat(along, first), np.maximum.reduceat(along, first)
    low_v, high_v = np.minimum.reduceat(across, first), np.maximum.reduceat(across, first)
    centre = u * ((low_u + high_u) / 2)[:, None] + v * ((low_v + high_v) / 2)[:, None]
    return np.column_stack((centre, u, (high_u - low_u) / 2, (high_v - low_v) / 2))


def _distance(frames: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Distance from points (per pad: x, y arrays of the same leading size) to each pad's rectangle."""
    cx, cy, ux, uy, hu, hv = (frames[:, i].reshape((-1,) + (1,) * (x.ndim - 1)) for i in range(6))
    dx, dy = x - cx, y - cy
    along, across = np.abs(dx * ux + dy * uy), np.abs(-dx * uy + dy * ux)
    return np.hypot(np.maximum(along - hu, 0.0), np.maximum(across - hv, 0.0))


# --- Copper raster ---

def _copper_edges(snapshot: model.BoardSnapshot, layer: str) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """Every edge of the layer's copper (zones, copper graphics, tracks and arcs as their
    outlines, pads), rings turned so outer rings wind one way and holes the other, with
    the net label of each edge. Copper without a net is left out (it connects nothing)."""
    labels: dict[str, int] = {}
    points, sizes, outer, ring_label = [], [], [], []
    segments, segment_label = [], []

    def label(net):
        return labels.setdefault(net, len(labels) + 1)

    def add(polygons, net):
        if not net:
            return
        for polygon in polygons:
            for index, ring in enumerate(polygon):
                if len(ring) >= 3:
                    points.extend(ring)
                    sizes.append(len(ring))
                    outer.append(index == 0)
                    ring_label.append(label(net))

    for item in (*snapshot.zones, *snapshot.graphics):
        if item.layer == layer:
            add(item.polygons, item.net)
    for pad in snapshot.pads:
        if layer in pad.polygons:
            add(pad.polygons[layer], pad.net)
    for track in snapshot.tracks:
        if track.layer == layer and track.net:
            segments.append((*track.start, *track.end, track.width))
            segment_label.append(label(track.net))
    for via in snapshot.vias:  # its land, where the via ends on this layer
        if layer in (via.layer_top, via.layer_bottom) and via.net:
            segments.append((*via.pos, *via.pos, via.diameter))
            segment_label.append(label(via.net))
    for arc in snapshot.arcs:  # its sampled chords, each a stroke
        if arc.layer == layer and arc.net:
            chord = sample_arc(arc.start, arc.mid, arc.end)
            segments += [(*a, *b, arc.width) for a, b in zip(chord, chord[1:])]
            segment_label += [label(arc.net)] * (len(chord) - 1)
    parts = [_ring_edges(points, sizes, outer, ring_label), _stroke_edges(segments, segment_label)]
    return np.vstack([p[0] for p in parts]), np.concatenate([p[1] for p in parts]), labels


def _ring_edges(points, sizes, outer, ring_label) -> tuple[np.ndarray, np.ndarray]:
    """Closed rings as edges, each ring turned so outer rings have positive area and holes
    negative (turning a ring is swapping the ends of its edges)."""
    if not sizes:
        return np.empty((0, 4)), np.empty(0, np.int64)
    xy = np.asarray(points, np.float64).reshape(-1, 2)
    sizes = np.asarray(sizes, np.int64)
    first = np.cumsum(sizes) - sizes
    following = np.arange(len(xy)) + 1
    following[first + sizes - 1] = first  # each ring's last point closes onto its first
    a, b = xy, xy[following]
    area = np.add.reduceat(a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1], first)
    flip = np.repeat((area > 0) != np.asarray(outer), sizes)
    edges = np.hstack((np.where(flip[:, None], b, a), np.where(flip[:, None], a, b)))
    return edges, np.repeat(np.asarray(ring_label, np.int64), sizes)


def _stroke_edges(segments, segment_label, sides: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """Tracks as stadiums (round ends, `sides` chords per end), all at once, wound positively."""
    if not segments:
        return np.empty((0, 4)), np.empty(0, np.int64)
    rows = np.asarray(segments, np.float64)
    start, end, radius = rows[:, :2], rows[:, 2:4], rows[:, 4:5] / 2
    heading = np.arctan2(end[:, 1] - start[:, 1], end[:, 0] - start[:, 0])[:, None]
    turn = np.linspace(0.0, math.pi, sides + 1)[None, :]
    # Counter-clockwise: round the end from its right side to its left, then the start.
    at_end = end[:, None, :] + radius[:, :, None] * np.stack(
        (np.cos(heading - math.pi / 2 + turn), np.sin(heading - math.pi / 2 + turn)), axis=2)
    at_start = start[:, None, :] + radius[:, :, None] * np.stack(
        (np.cos(heading + math.pi / 2 + turn), np.sin(heading + math.pi / 2 + turn)), axis=2)
    ring = np.concatenate((at_end, at_start), axis=1)  # tracks, 2 * (sides + 1), 2
    edges = np.concatenate((ring, np.roll(ring, -1, axis=1)), axis=2).reshape(-1, 4)
    return edges, np.repeat(np.asarray(segment_label, np.int64), ring.shape[1])


def _strip(edges, owners, x0, y0, columns, first_row, last_row) -> np.ndarray:
    """Net labels (0: no copper) of the cells in rows first_row..last_row-1.

    Each edge adds its winding direction to the first cell whose centre lies right of
    where it crosses a row's centre line; a running sum along the row gives each cell's
    winding number. The same sum weighted by net label, divided by the winding number,
    is the cell's net (copper of two nets never overlaps)."""
    xa, ya, xb, yb = edges.T
    low, high = np.minimum(ya, yb), np.maximum(ya, yb)
    start = np.maximum(np.ceil((low - y0) / CELL_NM - 0.5), first_row).astype(np.int64)
    stop = np.minimum(np.ceil((high - y0) / CELL_NM - 0.5), last_row).astype(np.int64)
    spans = np.maximum(stop - start, 0)
    keep = spans > 0
    rows_total = last_row - first_row
    if not keep.any():
        return np.zeros((rows_total, columns), np.int64)
    start, spans = start[keep], spans[keep]
    xa, ya, xb, yb, owner = xa[keep], ya[keep], xb[keep], yb[keep], owners[keep]
    index = np.repeat(np.arange(len(spans)), spans)
    row = np.repeat(start, spans) + np.arange(len(index)) - np.repeat(np.cumsum(spans) - spans, spans)
    centre = y0 + (row + 0.5) * CELL_NM
    x = xa[index] + (centre - ya[index]) * (xb[index] - xa[index]) / (yb[index] - ya[index])
    column = np.clip(np.ceil((x - x0) / CELL_NM - 0.5), 0, columns).astype(np.int64)
    sign = np.where(yb[index] > ya[index], 1.0, -1.0)
    flat = (row - first_row) * (columns + 1) + column
    size = rows_total * (columns + 1)
    winding = np.cumsum(np.bincount(flat, sign, size).reshape(rows_total, columns + 1), axis=1)[:, :columns]
    weighted = np.cumsum(np.bincount(flat, sign * owner[index], size).reshape(rows_total, columns + 1),
                         axis=1)[:, :columns]
    winding = np.rint(winding)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(winding != 0, np.rint(weighted / winding), 0).astype(np.int64)


def _ring_coverage(snapshot, layer, pads, frames) -> np.ndarray:
    """Per pad, per ring (`CELL_NM` wide, out to `REACH_NM`): the share of the ring's
    cells that is copper of the pad's own net."""
    rings = int(math.ceil(REACH_NM / CELL_NM))
    coverage = np.zeros((len(pads), rings))
    edges, owners, labels = _copper_edges(snapshot, layer)
    if not len(edges):
        return coverage
    half = int(math.ceil((np.hypot(frames[:, 4], frames[:, 5]).max() + REACH_NM) / CELL_NM)) + 1
    centre_x, centre_y = frames[:, 0], frames[:, 1]
    x0 = math.floor((centre_x.min() - (half + 1) * CELL_NM) / CELL_NM) * CELL_NM
    y0 = math.floor((centre_y.min() - (half + 1) * CELL_NM) / CELL_NM) * CELL_NM
    columns = int(math.ceil((centre_x.max() + (half + 1) * CELL_NM - x0) / CELL_NM))
    total_rows = int(math.ceil((centre_y.max() + (half + 1) * CELL_NM - y0) / CELL_NM))
    pad_column = np.floor((centre_x - x0) / CELL_NM).astype(np.int64)
    pad_row = np.floor((centre_y - y0) / CELL_NM).astype(np.int64)
    own = np.array([labels.get(pad.net, -1) if pad.net else -1 for pad in pads])
    offsets = np.arange(-half, half + 1)
    core = max(1, STRIP_CELLS // max(columns, 1) - 2 * half)  # rows a strip's pads may be centred in
    for first in range(0, total_rows, core):
        mine = np.nonzero((pad_row >= first) & (pad_row < first + core))[0]
        if not len(mine):
            continue
        top, bottom = max(0, first - half), min(total_rows, first + core + half + 1)
        labels_strip = _strip(edges, owners, x0, y0, columns, top, bottom)
        for chunk in np.array_split(mine, max(1, math.ceil(len(mine) / CHUNK_PADS))):
            rows = pad_row[chunk, None, None] + offsets[None, :, None] - top
            cols = pad_column[chunk, None, None] + offsets[None, None, :]
            inside = (rows >= 0) & (rows < bottom - top) & (cols >= 0) & (cols < columns)
            found = labels_strip[np.clip(rows, 0, bottom - top - 1), np.clip(cols, 0, columns - 1)]
            same = inside & (found == own[chunk, None, None])
            # Cell centres relative to each pad centre (float32: a few metres in nm is exact enough).
            x = (x0 + (cols + 0.5) * CELL_NM - frames[chunk, 0, None, None]).astype(np.float32)
            y = (y0 + (rows + top + 0.5) * CELL_NM - frames[chunk, 1, None, None]).astype(np.float32)
            distance = _distance(np.column_stack((np.zeros((len(chunk), 2)), frames[chunk, 2:])).astype(np.float32),
                                 x, y)
            ring = np.ceil(distance / CELL_NM).astype(np.int64) - 1  # ring k: k..k+1 cells out, edge included
            counted = (distance > 0) & (ring < rings)
            slot = np.arange(len(chunk))[:, None, None] * rings + np.minimum(ring, rings - 1)
            all_cells = np.bincount(slot[counted], minlength=len(chunk) * rings).reshape(len(chunk), rings)
            copper = np.bincount(slot[counted & same], minlength=len(chunk) * rings).reshape(len(chunk), rings)
            coverage[chunk] = np.where(all_cells > 0, copper / np.maximum(all_cells, 1), np.nan)
    return _fill_gaps(coverage)


def _fill_gaps(coverage: np.ndarray) -> np.ndarray:
    """A ring no cell centre fell in (a pad edge on the grid) takes the ring inside it,
    or for the first ring the next one with cells."""
    for ring in range(1, coverage.shape[1]):
        gap = np.isnan(coverage[:, ring])
        coverage[gap, ring] = coverage[gap, ring - 1]
    for ring in range(coverage.shape[1] - 2, -1, -1):
        gap = np.isnan(coverage[:, ring])
        coverage[gap, ring] = coverage[gap, ring + 1]
    return np.nan_to_num(coverage)


def _ring_resistance(coverage: np.ndarray, half_u: np.ndarray, half_v: np.ndarray, copper_m: float) -> np.ndarray:
    """Per pad and ring, the ring's conduction resistance (K/W): a copper sheet one cell
    long, as wide as the covered share of the ring's perimeter (inf: no copper of the
    pad's net in that ring). In series they are the path out to REACH_NM."""
    distance = (np.arange(coverage.shape[1]) + 0.5) * CELL_NM
    perimeter = 4 * (half_u + half_v)[:, None] + 2 * math.pi * distance[None, :]  # nm
    with np.errstate(divide="ignore"):
        return CELL_NM / (COPPER_W_MK * copper_m * coverage * perimeter)  # the nm cancel


def _depths(snapshot) -> dict[str, float]:
    """Each copper layer's depth (nm, its middle) from the top of the stackup."""
    depth, found = 0.0, {}
    for entry in snapshot.stackup.layers:
        thickness = entry.thickness_nm or 0
        if entry.type == "copper":
            found[entry.name] = depth + thickness / 2
        depth += thickness
    if len(found) >= 2 and depth > 0:
        return found
    return {"F.Cu": 0.0, "B.Cu": float(DEFAULT_BOARD_NM)}  # a stackup without thicknesses


def _vias(snapshot, pads, frames, layer, rings) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per pad: the vias of its net within REACH_NM, whether one in the pad is left open on
    the pad's side (neither filled, capped nor plugged there: solder wicks into it), and
    the conductance (W/K) of the paths through those vias: the rings out to the via, then
    its barrel down to the nearest layer where the net has a pour (else its far end)."""
    count = np.zeros(len(pads), np.int64)
    open_in_pad = np.zeros(len(pads), bool)
    conductance = np.zeros(len(pads))
    depth = _depths(snapshot)
    poured = {(zone.net, zone.layer) for zone in snapshot.zones}
    out_to = np.cumsum(rings, axis=1)  # series resistance out to the end of each ring
    by_net: dict[str, list[model.Via]] = {}
    for via in snapshot.vias:
        if layer in (via.layer_top, via.layer_bottom) and via.net:
            by_net.setdefault(via.net, []).append(via)
    plug = model.PROTECTION.index("plug_front" if layer == "F.Cu" else "plug_back")
    fill, cap = model.PROTECTION.index("fill"), model.PROTECTION.index("cap")
    for k, pad in enumerate(pads):
        vias = by_net.get(pad.net, [])
        if not vias:
            continue
        xy = np.asarray([via.pos for via in vias], np.float64)
        gap = _distance(frames[k:k + 1], xy[None, :, 0], xy[None, :, 1])[0]
        for via, centre_gap in zip(vias, gap):
            edge_gap = centre_gap - via.diameter / 2
            if edge_gap > REACH_NM:
                continue
            count[k] += 1
            if centre_gap == 0 and via.drill and not any(via.protection[i] == 1 for i in (fill, cap, plug)):
                open_in_pad[k] = True
            ring = int(max(edge_gap, 0) // CELL_NM)
            path = out_to[k, ring - 1] if ring > 0 else 0.0
            path += _barrel(via, layer, pad.net, depth, poured)
            if math.isfinite(path) and path > 0:
                conductance[k] += 1 / path
    return count, open_in_pad, conductance


def _barrel(via, layer, net, depth, poured) -> float:
    """A via barrel's conduction resistance (K/W) from `layer` to the nearest layer it
    reaches where its net has a pour, else to its far end."""
    ends = [depth.get(via.layer_top), depth.get(via.layer_bottom)]
    if None in ends or layer not in depth or not via.drill:
        return math.inf
    low, high = min(ends), max(ends)
    here = depth[layer]
    planes = [abs(z - here) for name, z in depth.items()
              if name != layer and low <= z <= high and (net, name) in poured]
    length = min(planes) if planes else max(abs(low - here), abs(high - here))
    wall = math.pi * via.drill * PLATING_NM * 1e-18  # m^2
    return length * 1e-9 / (COPPER_W_MK * wall) if length > 0 else math.inf


# --- Risk ---
# After _dev/docs/tombstone-science.md section c (2026-10-05): each part takes the worst of
# its factor levels, two or more raised factors compound, and the package's size shifts
# the result. [S] marks a threshold with measured support, [G] a reasoned guess.

LOW, MEDIUM, HIGH = 0, 1, 2
LEVELS = ("LOW", "MEDIUM", "HIGH")
DEFAULT_STENCIL_NM = 120_000  # the panel's default; KiCad stores no stencil thickness
LOCAL_W_K = 1e-3  # a pad's own coupling without copper (FR4 under it, paste, termination) [G]
CAPACITOR_PREFIXES = ("C", "L", "FB")  # taller parts with heavier ends: most tombstones are caps [S]


@dataclass(frozen=True)
class Package:
    name: str
    pitch_nm: int  # pad centre to centre in KiCad's library (IPC-7351 nominal, R and C averaged)
    pad_nm: tuple[int, int]  # nominal pad: along the part, across it
    length_nm: int  # EIA body length
    termination_nm: int
    susceptible: int  # how readily the size tombstones (science c.2)
    stencil_limit_nm: int | None = None  # thicker stencils overprint it (science F9) [G]


PACKAGES = (
    Package("01005", 500_000, (400_000, 300_000), 400_000, 100_000, HIGH, 80_000),
    Package("0201", 640_000, (460_000, 400_000), 600_000, 150_000, HIGH, 100_000),
    Package("0402", 990_000, (550_000, 630_000), 1_000_000, 250_000, HIGH, 125_000),
    Package("0603", 1_600_000, (850_000, 950_000), 1_600_000, 350_000, MEDIUM),
    Package("0805", 1_860_000, (1_010_000, 1_430_000), 2_000_000, 500_000, LOW),
    Package("1206", 2_940_000, (1_140_000, 1_780_000), 3_200_000, 500_000, LOW),
)
SMALL = ("01005", "0201", "0402")  # below 0603: IPC-7351's tighter toe goals


@dataclass(frozen=True)
class Factor:
    key: str  # F1..F9, as in the science notes
    level: int
    text: str


@dataclass(frozen=True)
class Finding:
    reference: str
    level: str  # "MEDIUM" or "HIGH"
    package: str  # inferred size, or "" when the pads match none
    message: str
    factors: tuple[str, ...]  # every raised factor, worst first
    where: model.Point
    items: tuple[str, ...]  # KiCad ids to select


def package_of(candidate: Candidate, measures: dict[str, PadMeasure]) -> Package | None:
    """The chip size whose KiCad footprint the pads match best (pitch, then pad width);
    None when no size is within about 30 %."""
    a, b = (measures[pad.id] for pad in candidate.pads)
    pitch = math.dist(*(pad.pos for pad in candidate.pads))
    width = (a.width_nm + b.width_nm) / 2
    if pitch <= 0 or width <= 0:
        return None
    scored = sorted((abs(math.log(pitch / p.pitch_nm)) + 0.5 * abs(math.log(width / p.pad_nm[1])), index)
                    for index, p in enumerate(PACKAGES))
    return PACKAGES[scored[0][1]] if scored[0][0] < 0.3 else None


def factors(candidate: Candidate, measures: dict[str, PadMeasure], package: Package | None,
            stencil_nm: int = DEFAULT_STENCIL_NM) -> list[Factor]:
    """Every factor of one part above LOW, worst first."""
    pads = candidate.pads
    a, b = (measures[pad.id] for pad in pads)
    name = [f"pad {pad.number or k + 1}" for k, pad in enumerate(pads)]
    tiny = package is not None and package.name in ("01005", "0201")
    found = []

    def add(key, level, text):
        if level > LOW:
            found.append(Factor(key, level, text))

    # F1 open via in pad: ~8-9x the rate, worst in one pad only (Liu 2006); capped: none (Lee 2012) [S]
    open_vias = [m.open_via_in_pad for m in (a, b)]
    if all(open_vias):
        add("F1", HIGH if tiny else MEDIUM, "open vias in both pads: solder drains into them")
    elif any(open_vias):
        add("F1", HIGH, f"open via in {name[open_vias.index(True)]} only: solder drains into it")
    # F2 heat drawn away: plane vs relief vs trace (science c.5) [G]
    drain = [(1 / m.resistance_k_w if math.isfinite(m.resistance_k_w) else 0.0) + LOCAL_W_K for m in (a, b)]
    slow = int(drain[1] > drain[0])
    ratio = drain[slow] / drain[1 - slow]
    add("F2", HIGH if ratio > 5 else MEDIUM if ratio > 2 else LOW,
        f"{name[slow]} ({pads[slow].net or 'no net'}) draws heat away {ratio:.1f}x as fast: "
        f"{name[1 - slow]} melts first")
    # F3 wettable copper, F4 paste: the same bands [G] (Suntron: 25 % counts as imbalance)
    add("F3", *_ratio_level(a.copper_nm2, b.copper_nm2, name, "copper"))
    if (a.paste_nm2 > 0) != (b.paste_nm2 > 0):
        add("F4", HIGH, f"paste on {name[0 if a.paste_nm2 > 0 else 1]} only")
    elif a.paste_nm2 > 0:
        add("F4", *_ratio_level(a.paste_nm2, b.paste_nm2, name, "paste"))
    # F5 stencil area ratio: 0.66 prints reliably (IPC-7525), below 0.5 barely [S]
    ratios = [m.paste_nm2 / (m.paste_perimeter_nm * stencil_nm) for m in (a, b) if m.paste_perimeter_nm > 0]
    if ratios:
        worst = min(ratios)
        add("F5", HIGH if worst < 0.5 else MEDIUM if worst < 0.66 else LOW,
            f"stencil area ratio {worst:.2f} at {stencil_nm / 1000:.0f} um: paste releases unevenly")
    if package is None:
        return sorted(found, key=lambda f: -f.level)
    pitch = math.dist(*(pad.pos for pad in pads))
    # F6 toe beyond the part's end, against the nominal footprint's [G]
    toes = [pitch / 2 + m.length_nm / 2 - package.length_nm / 2 for m in (a, b)]
    nominal_toe = package.pitch_nm / 2 + package.pad_nm[0] / 2 - package.length_nm / 2
    uneven = abs(toes[0] - toes[1])
    excess = max(toes) - nominal_toe
    step = 50_000 if package.name in SMALL else 100_000
    toe_level = max(HIGH if uneven > 100_000 else MEDIUM if uneven > 50_000 else LOW,
                    HIGH if excess > 3 * step else MEDIUM if excess > step else LOW)
    add("F6", toe_level, f"pads reach {max(toes) / 1000:.0f} um beyond the part's end "
                         f"(nominal {nominal_toe / 1000:.0f} um, ends differ by {uneven / 1000:.0f} um)")
    # F7 pad size against nominal: 01005 at 120 % ~1.6x, 130 % ~3.5x (Liu 2006) [S]
    size = max(a.copper_nm2, b.copper_nm2) / (package.pad_nm[0] * package.pad_nm[1])
    add("F7", HIGH if size > 1.25 else MEDIUM if size > 1.10 else LOW,
        f"pads {size * 100:.0f} % of a nominal {package.name} pad")
    # F8 inner gap: 0201 below 0.25 mm gave none (Wang 2002) [S]; elsewhere [G]. The pads
    # start past the inside of the part's metal ends (MEDIUM), or under half an end is left
    # over a pad (HIGH).
    gap = pitch - (a.length_nm + b.length_nm) / 2
    if gap > package.length_nm - package.termination_nm:
        add("F8", HIGH, f"pad gap {gap / 1000:.0f} um: under half of each metal end is over its pad")
    elif gap > package.length_nm - 2 * package.termination_nm and not (package.name == "0201" and gap <= 250_000):
        add("F8", MEDIUM, f"pad gap {gap / 1000:.0f} um: the pads start past the inside of the metal ends")
    # F9 stencil thickness for the size [G]
    if package.stencil_limit_nm is not None and stencil_nm > package.stencil_limit_nm:
        heavy = stencil_nm > package.stencil_limit_nm + 25_000 or stencil_nm >= 150_000
        add("F9", HIGH if heavy else MEDIUM,
            f"{stencil_nm / 1000:.0f} um stencil is thick for {package.name} "
            f"(up to {package.stencil_limit_nm / 1000:.0f} um)")
    return sorted(found, key=lambda f: -f.level)


def _ratio_level(first: float, second: float, name, what) -> tuple[int, str]:
    big = int(second > first)
    ratio = max(first, second) / max(min(first, second), 1.0)
    level = HIGH if ratio > 1.30 else MEDIUM if ratio > 1.10 else LOW
    return level, f"{name[big]} has {ratio:.2f}x the {what} of the other"


def risk(candidate: Candidate, found: list[Factor], package: Package | None) -> int:
    """The part's level (science c.4): the worst factor; two raised factors make MEDIUM
    HIGH; a less susceptible size lowers it a step (on a 0603, an open via or a heat
    imbalance at HIGH stays). Capacitors and inductors on a 0603 count as small [G]."""
    if not found:
        return LOW
    level = max(f.level for f in found)
    if level == MEDIUM and len(found) >= 2:
        level = HIGH
    if package is not None:
        susceptible = package.susceptible
    else:
        pitch = math.dist(*(pad.pos for pad in candidate.pads))
        susceptible = HIGH if pitch < 1_300_000 else MEDIUM if pitch < 1_750_000 else LOW
    if susceptible == MEDIUM and candidate.footprint.reference.rstrip("0123456789?").upper() in CAPACITOR_PREFIXES:
        susceptible = HIGH
    if susceptible == MEDIUM and any(f.key in ("F1", "F2") and f.level == HIGH for f in found):
        return level
    return max(level - (susceptible < HIGH), LOW)


def check(snapshot: model.BoardSnapshot, stencil_nm: int = DEFAULT_STENCIL_NM) -> tuple[int, list[Finding]]:
    """How many parts were checked, and every one at MEDIUM or HIGH risk, HIGH first."""
    found = candidates(snapshot)
    measures = measure(snapshot, found)
    findings = []
    for candidate in found:
        package = package_of(candidate, measures)
        raised = factors(candidate, measures, package, stencil_nm)
        level = risk(candidate, raised, package)
        if level == LOW:
            continue
        reference = candidate.footprint.reference
        findings.append(Finding(
            reference, LEVELS[level], package.name if package else "",
            f"{reference}: {raised[0].text}", tuple(f"{f.key} {LEVELS[f.level]}: {f.text}" for f in raised),
            candidate.footprint.pos, (candidate.footprint.id,)))
    return len(found), sorted(findings, key=lambda f: (f.level != "HIGH", f.reference))


def report(snapshot: model.BoardSnapshot, stencil_nm: int = DEFAULT_STENCIL_NM) -> dict:
    """What Blender's panel shows (JSON-ready): how many parts were checked, and the findings."""
    checked, findings = check(snapshot, stencil_nm)
    return {"checked": checked, "stencil_nm": stencil_nm, "findings": [model.to_jsonable(f) for f in findings]}
