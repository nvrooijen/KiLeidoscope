"""Dynamic phase of differential pairs: how far P and N are apart in time along the route.

KiCad 10's time-domain tuning compares the total delay of P and N end to end. A
pair can match there and still run out of phase in between: a bend near one end
compensated only at the far end leaves the pair partly common-mode over
everything between. So both sides are walked from the same end and their
positions matched along the pair: Δt(s) = t_P − t_N, the delay difference
accumulated up to each point, is what a ribbon along the pair shows. Δt < 0: P is
ahead there (its edge arrives first); Δt > 0: N is ahead.

Measure and show, don't judge: no target impedances or interface standards. The
only thresholds are the user's, and they only place markers: where |Δt| stays
above `tolerance_ps` for at least `min_length_nm`, a marker at the start.

Matching positions: each side is sampled evenly along its copper (barrels
included, so a via is a vertical stretch), and the two sample sequences are
aligned by dynamic time warping on 3D distance (`_align`). Coupled stretches pair
up side by side; where one side meanders or breaks out, the other side's position
waits. Each matched sample is then projected onto the other side's copper, so Δt
does not depend on the sampling step. The centreline is the midpoint of each match.

Each via or plated hole a route passes through is kept (`Crossing`: position,
layers, barrel length, delay) for return-path and via-stub analyses to reuse.
"""

import math
import os
import time
from dataclasses import dataclass, field, replace

import numpy as np

from . import board_text, channels, delays, model, modes, protocol

SAMPLES = 400  # per side at most; longer routes get a longer step
COUPLE_STEP_NM = 50_000  # the gap to the partner, and so the coupled unit delay, every 50 um
COUPLED_WITHIN_H = 3.0  # the partner this close (edge to edge, in dielectric heights): a coupled line
GROUNDED_WITHIN_H = 2.0  # a pour this close: a grounded coplanar line
GROUND_REACH_NM = 2_000_000  # pour edges farther than this are not looked at (past 10 dielectric heights)
MIN_STEP_NM = 50_000
BUDGET_S = 0.08  # per poll; pairs left over follow on the next polls

# Model limits: where the centreline rule is known to be off. A full-wave study (2026-10-16,
# _dev/docs/bend-meander-findings.md) found on one pair geometry (0.2 mm traces, 0.25 mm gap,
# 0.12 mm of laminate) that a coupled arc or corner makes 8.5–8.8 % MORE skew than its length
# difference (two radii and a four-corner turn: a property of the pair's coupling, not of the
# radius) and that a tight meander of a side running alone (arcs of 1.25 track widths, legs
# 1.5 widths apart) makes 17–20 % LESS than its extra length (one geometry, three meshes);
# the odd-mode delay on straights was right to 0.1 %. Δt is not corrected: `PairPhase.band_ps`
# shows that known error round it, each limit applied to the Δt its own features made. A
# looser meander gets none: nothing was measured there.
BEND_EXCESS = (0.0, 0.09)  # a coupled arc or corner: this much more skew than its length difference
TIGHT_MEANDER = (-0.20, -0.10)  # a tight meander of a lone side: this much less than its extra length makes
TIGHT_WIDTHS = 3.0  # a corner radius, or the spacing of a side's own legs, under this many track widths is tight
SHARP_RAD = math.radians(30)  # a kink this large at one vertex is a sharp corner; a sampled arc kinks less per vertex
TURN_RAD = math.radians(0.1)  # a smaller kink is two collinear tracks
BAND_NOTE = "known model error at tight features (one full-wave study, 2026-10-16)"  # the chart's legend and metadata


@dataclass(frozen=True)
class Settings:
    tolerance_ps: float = 1.0
    min_length_nm: float = 5_000_000
    follow_series: bool = True
    flipped: frozenset = frozenset()  # channel keys walked from their other end
    rise_ps: float = 25.0  # the signal's edge (10–90 %): how fast skew fades from the traces as it becomes common mode

    @classmethod
    def from_request(cls, header: dict) -> "Settings":
        """Blender's `phase_settings` frame; missing or bad values keep the defaults."""
        default = cls()
        try:
            tolerance = max(0.0, float(header.get("tolerance_ps", default.tolerance_ps)))
            length = max(0.0, float(header.get("min_length_mm", default.min_length_nm / 1e6))) * 1e6
        except (TypeError, ValueError):
            tolerance, length = default.tolerance_ps, default.min_length_nm
        try:
            rise = min(1000.0, max(1.0, float(header.get("rise_ps", default.rise_ps))))
        except (TypeError, ValueError):
            rise = default.rise_ps
        flipped = header.get("flipped", ())
        return cls(tolerance, length, bool(header.get("follow_series", True)),
                   frozenset(str(key) for key in flipped) if isinstance(flipped, (list, tuple)) else frozenset(),
                   rise)

    def as_json(self) -> dict:
        return {"tolerance_ps": self.tolerance_ps, "min_length_mm": self.min_length_nm / 1e6,
                "follow_series": self.follow_series, "flipped": sorted(self.flipped), "rise_ps": self.rise_ps}


@dataclass(frozen=True)
class Crossing:
    """A via or plated hole on a route: kept for return-path and via-stub analyses."""
    side: str  # "P" or "N"
    item_id: str
    net: str
    pos: model.Point
    from_layer: str
    to_layer: str
    span: tuple[str, str]  # the via's own layers
    length_nm: float  # barrel between from_layer and to_layer
    delay_ps: float
    at_nm: float  # along its side's route
    diameter_nm: int = 0  # the via's land
    drill_nm: int = 0  # and its hole
    reached_ps: float = 0.0  # delay from the start terminal to where the barrel begins
    lead_ps: float = 0.0  # how far this side is ahead of its partner there


@dataclass(frozen=True)
class Excursion:
    pos: tuple[float, float, float]  # nm, where it starts
    at_nm: float  # along the centreline
    length_nm: float
    peak_ps: float  # signed, the largest |Δt| of the stretch
    item_id: str  # the P track there
    item_ids: tuple[str, ...] = ()  # the copper of both sides along the stretch, in walking order
    # ps the pair's even (common) mode falls behind its odd (differential) mode over the
    # stretch (delays.DelayModel.mode_lag summed along both sides' copper, 0 on stripline):
    # the common mode made where the excursion starts arrives this much after the edge
    # where it ends, so a compensation there cancels it only if this is small against the
    # signal's rise time.
    mode_lag_ps: float = 0.0


@dataclass(frozen=True)
class Trace:
    """One side's copper as Blender draws it: its path in walking order (the route's
    own corners plus the alignment samples between them), and at each point the delay
    from the start and how far this side is ahead of its partner there."""
    xyz_nm: np.ndarray = field(repr=False)  # (v, 3)
    delay_ps: np.ndarray = field(repr=False)  # (v,) from the start terminal
    lead_ps: np.ndarray = field(repr=False)  # (v,) > 0: this side's edge arrives first
    width_nm: np.ndarray = field(repr=False)  # (v,) of the track there


@dataclass(frozen=True)
class Shorted:
    """A pair whose P copper touches its N copper: no differential signal, so no phase or
    numbers; where they touch, for a marker and the panel. When both sides still route
    from end to end, `measured` walks them, so the pulses can run from the start terminal
    up to the short (pair_frame cuts them there)."""
    channel: channels.Channel
    contacts: tuple[channels.Contact, ...]
    net_ids: tuple = ()
    measured: "PairPhase | None" = None


@dataclass(frozen=True)
class PairPhase:
    channel: channels.Channel
    p: channels.Route  # both walked from the same end
    n: channels.Route
    flipped: bool
    centre_nm: np.ndarray = field(repr=False)  # (k, 3) x, y, z
    dt_ps: np.ndarray = field(repr=False)  # (k,) t_P - t_N up to each centre point
    along_nm: np.ndarray = field(repr=False)  # (k,) along the centreline
    delay_p_ps: float = 0.0
    delay_n_ps: float = 0.0
    width_nm: float = 0.0  # P to N across the pair, centre to centre, plus a track width
    excursions: tuple[Excursion, ...] = ()
    crossings: tuple[Crossing, ...] = ()
    sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    traces: tuple = ()  # (P Trace, N Trace)
    pads: tuple = ()  # dicts for the frame: see _pads
    lines: tuple = ()  # ({"layer", "kind", "mm"}, ...): what kind of line the route is, longest first
    net_ids: tuple = ()  # every item on the pair's nets (stubs and leftovers too), for Blender's highlight
    # (k, 2) where each Δt reading sits: along P's copper and along N's (nm), so a tool
    # comparing with a measurement along the copper reads it at the same two places.
    matched_nm: np.ndarray | None = field(default=None, repr=False)
    # (k, 2) each side's delay from the start at its matched reading (ps; their difference
    # is dt_ps), and (k,) ps the pair's even mode has fallen behind its odd mode by each
    # reading (the mean of the two sides' `_Walk.lag`): what a tool needs to rebuild the two
    # traces' signals and predict what a probe on either reads once skew has made common mode.
    delays_ps: np.ndarray | None = field(default=None, repr=False)
    lag_ps: np.ndarray | None = field(default=None, repr=False)
    # (k,) ps: Δt as a probe on each trace would read it (modes.apparent_dt, for the settings'
    # rise time): the geometric Δt where skew is made, fading as its common mode falls behind.
    read_dt_ps: np.ndarray | None = field(default=None, repr=False)
    # (k, 2) ps, below and above dt_ps: the known model error at tight features by each
    # reading, from the model limits at the top of this module (BEND_EXCESS on the Δt the
    # pair's coupled arcs and corners made, TIGHT_MEANDER on the Δt a side's tight meanders
    # made; see _band). (0, 0) on a straight pair. Δt itself is not corrected.
    band_ps: np.ndarray | None = field(default=None, repr=False)

    @property
    def skew_ps(self) -> float:
        return self.delay_p_ps - self.delay_n_ps

    @property
    def max_abs_ps(self) -> float:
        return float(np.abs(self.dt_ps).max()) if len(self.dt_ps) else 0.0


# --- One side: where it runs and how much delay it has gathered ---------------------------

@dataclass
class _Walk:
    xyz: np.ndarray  # (v, 3) nm
    along: np.ndarray  # (v,) nm of copper
    delay: np.ndarray  # (v,) ps
    items: list  # (v,) item id of the piece each vertex ends ("" for a hop across a pad)
    widths: np.ndarray  # (v,) track width there (a barrel or hop keeps the last one)
    nets: list  # (v,) net of the piece each vertex ends
    crossings: list
    sources: set
    lines: dict = field(default_factory=dict)  # (layer, kind) -> nm of track (from _couple)
    lag: np.ndarray | None = None  # (v,) ps the even mode has fallen behind the odd mode by each vertex (from _couple)


def _walk(route: channels.Route, side: str, model_: delays.DelayModel, heights: dict[str, int]) -> _Walk:
    xyz, along, delay, items, widths, nets = [], [], [], [], [], []
    crossings, sources = [], set()
    s = t = 0.0

    def add(point, layer_z, item_id, width=0, net=""):
        xyz.append((point[0], point[1], layer_z))
        along.append(s)
        delay.append(t)
        items.append(item_id)
        widths.append(width or (widths[-1] if widths else 0))
        nets.append(net)

    for piece in route.pieces:
        z = heights.get(piece.layer, 0)
        if piece.kind == "barrel":
            ps, source = model_.barrel(piece.net, piece.layer, piece.to_layer, piece.span, piece.length_nm)
            sources.add(source)
            crossings.append(Crossing(side, piece.item_id, piece.net, piece.points[0], piece.layer,
                                      piece.to_layer, piece.span, piece.length_nm, ps, s, piece.diameter,
                                      piece.drill, t))
            if not xyz:
                add(piece.points[0], z, piece.item_id)
            s += piece.length_nm
            t += ps
            add(piece.points[-1], heights.get(piece.to_layer, 0), piece.item_id)
            continue
        unit = model_.track(piece.net, piece.layer, piece.width)
        sources.add(unit.source)
        points = piece.points
        chords = [math.dist(a, b) for a, b in zip(points, points[1:])]
        scale = piece.length_nm / sum(chords) if sum(chords) else 0.0
        if not xyz or (xyz[-1][0], xyz[-1][1]) != tuple(points[0]):
            add(points[0], z, piece.item_id, piece.width, piece.net)  # also where a barrel ended at the via centre
        for point, chord in zip(points[1:], chords):
            s += chord * scale
            t += chord * scale * unit.ps_per_mm / 1e6
            add(point, z, piece.item_id, piece.width, piece.net)
    if not xyz:
        add(route.start.pos, heights.get(route.start.layer, 0), "")
    first = next((width for width in widths if width), 0)
    widths = [width or first for width in widths]  # a route that starts through a via
    return _Walk(np.asarray(xyz, np.float64), np.asarray(along), np.asarray(delay), items,
                 np.asarray(widths, np.float64), nets, crossings, sources)


def ground_edges(snapshot: model.BoardSnapshot) -> dict[str, tuple[np.ndarray, tuple[str, ...]]]:
    """Per copper layer, every zone fill's edges ((m, 2, 2) nm) and the net of each."""
    found: dict[str, tuple[list, list]] = {}
    for zone in snapshot.zones:
        segments, nets = found.setdefault(zone.layer, ([], []))
        for polygon in zone.polygons:
            for ring in polygon:
                points = np.asarray(ring, np.float64).reshape(-1, 2)
                if len(points) > 1:
                    segments.append(np.stack((points, np.roll(points, -1, axis=0)), axis=1))
                    nets.extend([zone.net] * len(points))
    return {layer: (np.concatenate(segments) if segments else np.zeros((0, 2, 2)), tuple(nets))
            for layer, (segments, nets) in found.items()}


def _ground_distance(points: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Distance from each point (x, y) to the nearest of `edges`, up to GROUND_REACH_NM
    (inf beyond: a pour that far changes nothing). Points go in groups of 32, each
    against only the edges within reach of its own box."""
    best = np.full(len(points), np.inf)
    if not len(edges):
        return best
    lows, highs = edges.min(axis=1), edges.max(axis=1)
    for first in range(0, len(points), 32):
        group = points[first:first + 32]
        low, high = group.min(axis=0) - GROUND_REACH_NM, group.max(axis=0) + GROUND_REACH_NM
        near = edges[(highs >= low).all(axis=1) & (lows <= high).all(axis=1)]
        if not len(near):
            continue
        start, span = near[:, 0], near[:, 1] - near[:, 0]
        squared = np.maximum((span * span).sum(axis=1), 1e-12)
        relative = group[:, None, :] - start[None]
        t = np.clip((relative * span[None]).sum(axis=2) / squared[None], 0.0, 1.0)
        distance = np.hypot(*(relative - t[..., None] * span[None]).transpose(2, 0, 1)).min(axis=1)
        best[first:first + 32] = np.where(distance <= GROUND_REACH_NM, distance, np.inf)
    return best


def _between_samples(where: np.ndarray, at: np.ndarray, values: np.ndarray) -> np.ndarray:
    """`values` (one per sample, at `at` along the copper, inf: no pour within reach) at
    `where`: linear between two finite samples, else the nearer sample's, so a stretch
    without a pour stays without one rather than borrowing a pour from either end."""
    right = np.clip(np.searchsorted(at, where), 1, len(at) - 1)
    left = right - 1
    a, b = values[left], values[right]
    span = at[right] - at[left]
    t = np.clip((where - at[left]) / np.where(span > 0, span, 1.0), 0.0, 1.0)
    both = np.isfinite(a) & np.isfinite(b)
    linear = np.where(both, a, 0.0) + t * (np.where(both, b, 0.0) - np.where(both, a, 0.0))
    return np.where(both, linear, np.where(t < 0.5, a, b))


def _couple(walk: _Walk, samples: tuple[np.ndarray, np.ndarray], partner: np.ndarray, model_: delays.DelayModel,
            heights: dict[str, int], grounds: dict | None = None, own_nets=()) -> _Walk:
    """The walk's delays again with the pair's coupling: along each track stretch, the
    unit delay at the gap to the partner there (the odd mode where they run close, the
    lone line where one meanders or breaks out). A tuning profile's number holds at the
    profile's own gap and is scaled away from it (delays.StackupDelays._coupling); an inner
    layer gives the same delay either way; a barrel, or a hop across a pad, keeps its own.

    `samples` (along, xyz) are this side's alignment samples: the gap is measured at
    each (to the partner's nearest sample, centre to centre) and interpolated between;
    so is the distance to the nearest pour of another net on the sample's layer
    (`grounds`, from ground_edges), for a grounded coplanar waveguide.

    Along the same stretches the even mode's lag behind the odd mode (model_.mode_lag,
    ps/mm at the gap there) is added up into `lag`: how far a common-mode pulse has
    fallen behind the differential edge by each vertex."""
    total = walk.along[-1]
    if total <= 0 or len(partner) < 2:
        return walk
    at, points = samples
    nearest = np.sqrt(((points[:, None, :] - partner[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
    layer_at = {height: layer for layer, height in heights.items()}
    ground = np.full(len(points), np.inf)
    for height in np.unique(points[:, 2]):
        edges, nets = (grounds or {}).get(layer_at.get(int(round(height))), (np.zeros((0, 2, 2)), ()))
        if len(edges):
            edges = edges[np.array([net not in own_nets for net in nets])]
        on = points[:, 2] == height
        ground[on] = _ground_distance(points[on, :2], edges)
    cuts = np.union1d(walk.along, np.linspace(0.0, total, max(2, int(total / COUPLE_STEP_NM) + 1)))
    delay = np.interp(cuts, walk.along, walk.delay)
    middle = (cuts[:-1] + cuts[1:]) / 2
    vertex = np.clip(np.searchsorted(walk.along, middle), 1, len(walk.along) - 1)
    track = (walk.xyz[vertex, 2] == walk.xyz[vertex - 1, 2]) & np.array([bool(walk.items[v]) for v in vertex])
    ratio = np.ones(len(middle))
    lag = np.zeros(len(middle))  # ps/mm the even mode falls behind the odd mode on each cut
    if track.any():
        gaps = np.interp(middle[track], at, nearest)
        pours = _between_samples(middle[track], at, ground)
        lone, cache = {}, {}
        for index, gap, pour in zip(np.flatnonzero(track), gaps, pours):
            v = vertex[index]
            width, layer = int(walk.widths[v]), layer_at.get(int(round(walk.xyz[v, 2])))
            if layer is None:
                continue
            side = round(max(pour - width / 2, 0.0) / 10_000) * 10_000 if np.isfinite(pour) else None  # 10 um
            key = (walk.nets[v], layer, width, round(max(gap - width, 0.0) / 2_000) * 2_000, side)  # the gap: 2 um
            if key not in cache:
                alone = lone.get(key[:3])
                if alone is None:
                    alone = lone[key[:3]] = model_.track(walk.nets[v], layer, width).ps_per_mm
                cache[key] = (model_.track(walk.nets[v], layer, width, key[3], side).ps_per_mm / alone if alone else 1.0,
                              model_.mode_lag(walk.nets[v], layer, width, key[3], side))
            ratio[index], lag[index] = cache[key]
    lines = {}
    if track.any():
        spans = np.diff(cuts)[track]
        for index, span, gap, pour in zip(np.flatnonzero(track), spans, gaps, pours):
            v = vertex[index]
            layer = layer_at.get(int(round(walk.xyz[v, 2])))
            kind, h = model_.structure(layer) if layer is not None else ("", 0.0)
            if not kind:
                continue
            h = h or 1.0
            if kind == "microstrip" and pour - walk.widths[v] / 2 < GROUNDED_WITHIN_H * h:
                kind = "grounded coplanar"
            if gap - walk.widths[v] < COUPLED_WITHIN_H * h:
                kind = "coupled " + kind
            lines[(layer, kind)] = lines.get((layer, kind), 0.0) + float(span)
    coupled = np.concatenate(([delay[0]], delay[0] + np.cumsum(np.diff(delay) * ratio)))
    behind = np.concatenate(([0.0], np.cumsum(np.diff(cuts) / 1e6 * lag)))
    crossings = [replace(crossing, reached_ps=float(np.interp(crossing.at_nm, cuts, coupled)))
                 for crossing in walk.crossings]
    return replace(walk, delay=np.interp(walk.along, cuts, coupled), crossings=crossings, lines=lines,
                   lag=np.interp(walk.along, cuts, behind))


def _resample(walk: _Walk, step: float) -> tuple[np.ndarray, np.ndarray, list, np.ndarray]:
    """Points, delays and item ids every `step` nm along the copper, ends included, and
    where along the copper each sample is."""
    total = walk.along[-1]
    count = max(2, int(math.ceil(total / step)) + 1)
    s = np.linspace(0.0, total, count)
    xyz = np.column_stack([np.interp(s, walk.along, walk.xyz[:, axis]) for axis in range(3)])
    t = np.interp(s, walk.along, walk.delay)
    vertex = np.clip(np.searchsorted(walk.along, s, side="left"), 0, len(walk.items) - 1)
    return xyz, t, [walk.items[index] for index in vertex], s


def _trace(walk: _Walk, s: np.ndarray, lead: np.ndarray) -> Trace:
    """The side's own vertices (its corners stay sharp) with the samples between them;
    the lead is known at the samples and interpolated along the copper."""
    extra = s[~np.isin(s, walk.along)]
    along = np.concatenate((walk.along, extra))
    order = np.argsort(along, kind="stable")  # a jump (one position, two points) keeps its order
    between = np.column_stack([np.interp(extra, walk.along, walk.xyz[:, axis]) for axis in range(3)])
    xyz = np.vstack((walk.xyz, between.reshape(-1, 3)))[order]
    delay = np.concatenate((walk.delay, np.interp(extra, walk.along, walk.delay)))[order]
    after = np.clip(np.searchsorted(walk.along, extra), 0, len(walk.widths) - 1)
    width = np.concatenate((walk.widths, walk.widths[after]))[order]
    return Trace(xyz, delay, np.interp(along[order], s, lead), width)


def _per_sample(index: np.ndarray, values: np.ndarray, count: int) -> np.ndarray:
    """The mean of `values` over each sample's matches (every sample has at least one)."""
    return np.bincount(index, values, count) / np.maximum(np.bincount(index, minlength=count), 1)


def _align(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dynamic time warping of two point sequences, both from their first to their last
    point: index pairs (i, j), each step advancing i, j or both.

    A pair's cost is its distance beyond each point's nearest point on the other side,
    so running side by side costs nothing whatever the pitch; a step advancing both
    counts twice (the symmetric pattern), so waiting out the other side's detour
    costs no more than walking past it offset. The table is filled one anti-diagonal
    (i + j = k) at a time, each stored on its own with rows from `low[k]`: slices of
    the two before it, no fancy indexing (~4x faster at 400 points a side)."""
    n, m = len(a), len(b)
    distance = np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2))
    nearest = (distance.min(axis=1)[:, None] + distance.min(axis=0)[None, :]) / 2
    flipped = np.maximum(distance - nearest, 0.0)[:, ::-1]  # anti-diagonals become diagonals
    low = [max(0, k - m + 1) for k in range(n + m - 1)]
    high = [min(k, n - 1) for k in range(n + m - 1)]
    totals, moves = [np.array([flipped[0, m - 1]])], [np.zeros(1, np.int8)]
    for k in range(1, n + m - 1):
        cost = flipped.diagonal(m - 1 - k)  # rows low[k]..high[k]
        options = np.full((3, len(cost)), np.inf)  # 0 both, 1 i only, 2 j only
        for option, back, shift, weight in ((0, 2, 1, 2.0), (1, 1, 1, 1.0), (2, 1, 0, 1.0)):
            if k < back:
                continue
            first = max(low[k], low[k - back] + shift)
            last = min(high[k], high[k - back] + shift)
            if first <= last:
                source = totals[k - back][first - shift - low[k - back]:last - shift - low[k - back] + 1]
                here = slice(first - low[k], last - low[k] + 1)
                options[option, here] = source + weight * cost[here]
        choice = options.argmin(axis=0)
        totals.append(options[choice, np.arange(len(cost))])
        moves.append(choice.astype(np.int8))
    path_i, path_j = [n - 1], [m - 1]
    i, j = n - 1, m - 1
    while i or j:
        step = moves[i + j][i - low[i + j]]
        i, j = (i - 1, j - 1) if step == 0 else (i - 1, j) if step == 1 else (i, j - 1)
        path_i.append(i)
        path_j.append(j)
    return np.asarray(path_i[::-1]), np.asarray(path_j[::-1])


def _nearby(points: np.ndarray, line: np.ndarray, index: np.ndarray) -> np.ndarray:
    """Fractional index on the polyline `line` nearest each point, on the two
    segments either side of its matched sample `index`."""
    best = index.astype(np.float64)
    gap = np.full(len(points), np.inf)
    last = len(line) - 1
    for first, second in ((np.maximum(index - 1, 0), index), (index, np.minimum(index + 1, last))):
        start, span = line[first], line[second] - line[first]
        squared = (span * span).sum(axis=1)
        t = np.clip(((points - start) * span).sum(axis=1) / np.maximum(squared, 1e-12), 0.0, 1.0)
        distance = np.linalg.norm(start + t[:, None] * span - points, axis=1)
        closer = distance < gap
        best = np.where(closer, first + t * (second - first), best)
        gap = np.where(closer, distance, gap)
    return best


def _at(line: np.ndarray, fraction: np.ndarray) -> np.ndarray:
    """Points of a polyline at fractional indices."""
    whole = np.minimum(np.floor(fraction).astype(int), len(line) - 1)
    after = np.minimum(whole + 1, len(line) - 1)
    return line[whole] + (fraction - whole)[:, None] * (line[after] - line[whole])


def _pads(route: channels.Route, trace: Trace, side: str, heights: dict[str, int]) -> list[dict]:
    """Each pad the route starts, ends or runs through, on the layer the route meets it:
    its outline (nm), and the delay and lead of the route's nearest point, so Blender
    tints it and a pulse lights it as it passes."""
    layer_at = {height: layer for layer, height in heights.items()}
    found = []
    for pad in route.pads:
        near = int(np.argmin(np.hypot(*(trace.xyz_nm[:, :2] - np.asarray(pad.pos, np.float64)).T)))
        layer = layer_at.get(int(round(trace.xyz_nm[near, 2])))
        polygons = pad.polygons.get(layer) if layer else None
        if not polygons:
            continue  # no copper shape on that layer (an old dump)
        found.append({"side": side, "id": pad.id, "layer": layer,
                      "rings": [[int(value) for point in polygon[0] for value in point] for polygon in polygons],
                      "t": float(trace.delay_ps[near]), "lead": float(trace.lead_ps[near])})
    return found


# --- Model limits: where the centreline rule is known to be off ----------------------------

def _corners(walk: _Walk) -> tuple[np.ndarray, np.ndarray]:
    """Per vertex: how far the copper turns there in the layer's plane (radians, unsigned;
    0 at a barrel's ends and between collinear tracks) and the corner's radius (nm): a
    sampled arc's from its chords, 0 at a sharp corner (a kink of SHARP_RAD or more at one
    vertex: KiCad's arcs are sampled finer than that), inf where the copper does not turn."""
    turn, radius = np.zeros(len(walk.xyz)), np.full(len(walk.xyz), np.inf)
    if len(walk.xyz) < 3:
        return turn, radius
    step = np.diff(walk.xyz[:, :2], axis=0)
    length = np.hypot(step[:, 0], step[:, 1])
    flat = (length > 0) & (np.diff(walk.xyz[:, 2]) == 0)
    angle = np.abs(np.arctan2(step[:-1, 0] * step[1:, 1] - step[:-1, 1] * step[1:, 0],
                              (step[:-1] * step[1:]).sum(axis=1)))
    angle[~(flat[:-1] & flat[1:]) | (angle < TURN_RAD)] = 0.0
    turn[1:-1] = angle
    with np.errstate(divide="ignore", invalid="ignore"):
        chord = np.minimum(length[:-1], length[1:]) / (2 * np.tan(angle / 2))
    radius[1:-1] = np.where(angle == 0, np.inf, np.where(angle >= SHARP_RAD, 0.0, chord))
    return turn, radius


def _turned(along: np.ndarray, turn: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The turn added up along the copper, each vertex's kink spread from the middle of the
    piece before it to the middle of the piece after it (so a sampled arc turns evenly
    along its length): (where, total) for np.interp."""
    at = np.flatnonzero(turn > 0)
    if not len(at):
        return np.array([0.0]), np.array([0.0])
    first, last = (along[at - 1] + along[at]) / 2, (along[at] + along[at + 1]) / 2
    rate = turn[at] / np.maximum(last - first, 1.0)
    where, change = np.concatenate((first, last)), np.concatenate((rate, -rate))
    order = np.argsort(where, kind="stable")
    where, slope = where[order], np.cumsum(change[order])
    return where, np.concatenate(([0.0], np.cumsum(slope[:-1] * np.diff(where))))


def _own_legs(s: np.ndarray, xyz: np.ndarray, width: float) -> np.ndarray:
    """Per sample: whether the side's own copper comes back within TIGHT_WIDTHS widths of it
    (edge to edge) the long way round: a meander's facing legs, not the next sample."""
    apart = np.sqrt(((xyz[:, None, :] - xyz[None, :, :]) ** 2).sum(axis=2))
    around = np.abs(s[:, None] - s[None, :])
    return ((apart - width < TIGHT_WIDTHS * width) & (around > 1.4 * apart) & (around > 0)).any(axis=1)


def _meanders(walk: _Walk, s: np.ndarray, xyz: np.ndarray, partner: np.ndarray, pitch: float,
              track_nm: float) -> list[tuple[float, float, tuple[float, float]]]:
    """This side's meanders: each a stretch of its copper [from, to] (nm) and the limit that
    applies to the Δt it made. A meander is where the side parts from its partner by more
    than a track width beyond the pair's pitch (its alignment samples `s`, `xyz` against the
    partner's samples), reaching back and forward over short pieces (chords of an arc,
    legs under TIGHT_WIDTHS widths) to the straight the feature leaves and the one it
    rejoins. Tight when a corner in it has a radius under TIGHT_WIDTHS widths or its own
    legs come that close; a looser meander, or a straight that merely parts (a fan-out),
    gets no limit (and none of a coupled bend either)."""
    width = float(np.median(walk.widths[walk.widths > 0])) if (walk.widths > 0).any() else track_nm
    nearest = np.sqrt(((xyz[:, None, :] - partner[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
    departed = nearest - pitch > track_nm
    if not departed.any():
        return []
    turn, radius = _corners(walk)
    along = walk.along
    piece = np.diff(along)
    short = TIGHT_WIDTHS * width
    close = _own_legs(s, xyz, width)
    edges = np.diff(np.concatenate(([0], departed.astype(np.int8), [0])))
    stretches = []
    for first, last in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1):
        v0 = max(int(np.searchsorted(along, s[first], side="right")) - 1, 0)
        while v0 > 0 and piece[v0 - 1] < short:
            v0 -= 1
        v1 = min(int(np.searchsorted(along, s[last], side="left")), len(along) - 1)
        while v1 < len(along) - 1 and piece[v1] < short:
            v1 += 1
        low, high = float(along[v0]), float(along[v1])
        if stretches and low <= stretches[-1][1]:
            low = stretches.pop()[0]
        stretches.append((low, high))
    found = []
    for low, high in stretches:
        at = (along >= low) & (along <= high)
        tight = bool(((turn[at] > 0) & (radius[at] < short)).any() or close[(s >= low) & (s <= high)].any())
        found.append((low, high, TIGHT_MEANDER if tight else (0.0, 0.0)))
    return found


def _band(walks: tuple, samples: tuple, matched: np.ndarray, p_at: np.ndarray, n_at: np.ndarray, dt: np.ndarray,
          model_: delays.DelayModel, heights: dict[str, int], track_nm: float) -> np.ndarray:
    """(k, 2) ps: the known model error at tight features, below and above Δt by each reading.

    Each step between readings is classed by what made its Δt increment: a meander of one
    side (_meanders: the steps whose reading on that side falls in it), else a coupled bend
    (the pair coupled by the delay model's own criterion, COUPLED_WITHIN_H, and either side
    turning there). The increments of each class are added up like Δt itself and the
    class's limit applied to the sum: the errors are systematic (a turn the other way takes
    its share back), so the band widens through a feature and only a compensating feature
    of the same kind closes it. Straights, vias and width changes add nothing."""
    count = len(dt)
    band = np.zeros((count, 2))
    if count < 2 or track_nm <= 0:
        return band
    apart = np.linalg.norm(p_at - n_at, axis=1)
    pitch = float(np.median(apart))
    turned, meanders = [], []
    for side, (walk, (s, xyz)) in enumerate(zip(walks, samples)):
        where, total = _turned(walk.along, _corners(walk)[0])
        turned.append(lambda x, where=where, total=total: np.interp(x, where, total))
        meanders.append(_meanders(walk, s, xyz, samples[1 - side][1], pitch, track_nm))
    layer_at = {height: layer for layer, height in heights.items()}
    heights_at = np.array([model_.structure(layer_at.get(int(round(z)), ""))[1] for z in p_at[:, 2]])
    coupled = (heights_at > 0) & (apart - track_nm < COUPLED_WITHIN_H * heights_at)
    turning = sum(np.diff(turned[side](matched[:, side])) for side in range(2)) > TURN_RAD
    factor = np.zeros((count - 1, 2))  # each step's (low, high) limit on its own Δt increment
    factor[coupled[:-1] & coupled[1:] & turning] = BEND_EXCESS
    rank = {(0.0, 0.0): 0, TIGHT_MEANDER: 1}  # a step in two sides' meanders: the tighter
    for side in range(2):
        s = samples[side][0]
        margin = float(s[1] - s[0]) if len(s) > 1 else 0.0  # the matching may stumble a sample before a feature
        for low, high, limit in sorted(meanders[side], key=lambda found: rank[found[2]]):
            inside = (matched[:, side] >= low - margin) & (matched[:, side] <= high + margin)
            factor[inside[:-1] | inside[1:]] = limit
    # One unknown factor per limit, within its range: the Δt its steps made is added up and
    # the range applied to the sum; the band is the sum of the limits' ranges.
    for limit in (BEND_EXCESS, TIGHT_MEANDER):
        on = np.all(factor == limit, axis=1)
        if on.any():
            made = np.concatenate(([0.0], np.cumsum(np.diff(dt) * on)))
            edges_ps = np.column_stack((limit[0] * made, limit[1] * made))
            band[:, 0] += edges_ps.min(axis=1)
            band[:, 1] += edges_ps.max(axis=1)
    return band


def find_excursions(along: np.ndarray, dt: np.ndarray, tolerance_ps: float, min_length_nm: float):
    """(first index, last index) of every stretch where |Δt| > tolerance for at least min_length."""
    over = np.abs(dt) > tolerance_ps
    edges = np.diff(np.concatenate(([0], over.astype(np.int8), [0])))
    starts, stops = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1
    return [(int(a), int(b)) for a, b in zip(starts, stops) if along[b] - along[a] >= min_length_nm]


# --- A pair -------------------------------------------------------------------------------

def _orient(p: channels.Route, n: channels.Route, flipped: bool) -> tuple[channels.Route, channels.Route]:
    """N walked from P's start; the start is the end whose label sorts first (stable
    through edits), or the other one when flipped."""
    same = math.dist(p.start.pos, n.start.pos) + math.dist(p.end.pos, n.end.pos)
    crossed = math.dist(p.start.pos, n.end.pos) + math.dist(p.end.pos, n.start.pos)
    if crossed < same:
        n = n.reversed()
    if (p.end.label, p.end.pos) < (p.start.label, p.start.pos):
        p, n = p.reversed(), n.reversed()
    if flipped:
        p, n = p.reversed(), n.reversed()
    return p, n


def end_label(p: channels.Terminal, n: channels.Terminal) -> str:
    """One end of a pair: "U1.A5/A6" on one part, else "U1.A5/U2.3"."""
    p_ref, _, p_pin = p.label.partition(".")
    n_ref, _, n_pin = n.label.partition(".")
    if p_ref == n_ref and p_pin and n_pin:
        return f"{p_ref}.{p_pin}/{n_pin}"
    return p.label if p.label == n.label else f"{p.label}/{n.label}"


def analyze(channel: channels.Channel, p: channels.Route, n: channels.Route, model_: delays.DelayModel,
            heights: dict[str, int], settings: Settings, grounds: dict | None = None) -> PairPhase:
    flipped = channel.key in settings.flipped
    p, n = _orient(p, n, flipped)
    walks = (_walk(p, "P", model_, heights), _walk(n, "N", model_, heights))
    step = max(MIN_STEP_NM, max(walk.along[-1] for walk in walks) / SAMPLES)
    (a, t_p, items_p, s_a), (b, t_n, items_n, s_b) = (_resample(walk, step) for walk in walks)
    i, j = _align(a, b)
    walks = tuple(_couple(walk, samples, partner, model_, heights, grounds, channel.nets)
                  for walk, samples, partner in ((walks[0], (s_a, a), b), (walks[1], (s_b, b), a)))
    t_p, t_n = np.interp(s_a, walks[0].along, walks[0].delay), np.interp(s_b, walks[1].along, walks[1].delay)
    # Matched samples are up to half a step apart along the pair; the side that moved
    # keeps its sample and the other is projected onto its own nearby copper, so a
    # plateau of Δt is exact, not quantized to the step (~0.3 ps at 0.12 mm).
    waiting_p = np.concatenate(([False], np.diff(i) == 0))  # N advanced alone: P waits
    f_a, f_b = _nearby(b[j], a, i), _nearby(a[i], b, j)
    dt = np.where(waiting_p, np.interp(f_a, np.arange(len(a)), t_p) - t_n[j],
                  t_p[i] - np.interp(f_b, np.arange(len(b)), t_n))
    matched = np.column_stack((np.where(waiting_p, np.interp(f_a, np.arange(len(a)), s_a), s_a[i]),
                               np.where(waiting_p, s_b[j], np.interp(f_b, np.arange(len(b)), s_b))))
    reached = np.column_stack((np.where(waiting_p, np.interp(f_a, np.arange(len(a)), t_p), t_p[i]),
                               np.where(waiting_p, t_n[j], np.interp(f_b, np.arange(len(b)), t_n))))
    p_at = np.where(waiting_p[:, None], _at(a, f_a), a[i])  # the two matched points of each reading
    n_at = np.where(waiting_p[:, None], b[j], _at(b, f_b))
    centre = (p_at + n_at) / 2
    along = np.concatenate(([0.0], np.cumsum(np.linalg.norm(np.diff(centre, axis=0), axis=1))))
    widths = [piece.width for piece in (*p.pieces, *n.pieces) if piece.kind == "track"]
    track = float(np.median(widths)) if widths else 0.0
    width = float(np.median(np.hypot(*(a[i, :2] - b[j, :2]).T))) + track
    band = _band(walks, ((s_a, a), (s_b, b)), matched, p_at, n_at, dt, model_, heights, track)
    # The even mode's lag behind the odd mode at each reading, along P's copper and along N's.
    behind = [np.interp(matched[:, column], walk.along, walk.lag) if walk.lag is not None else np.zeros(len(matched))
              for column, walk in enumerate(walks)]
    excursions = tuple(
        Excursion(tuple(float(v) for v in centre[first]), float(along[first]), float(along[last] - along[first]),
                  float(dt[first + int(np.abs(dt[first:last + 1]).argmax())]),
                  next((item for item in items_p[i[first]:] if item), ""),
                  tuple(dict.fromkeys(item for item in (*items_p[i[first]:i[last] + 1], *items_n[j[first]:j[last] + 1])
                                      if item)),
                  mode_lag_ps=float(sum(side[last] - side[first] for side in behind) / 2))  # both sides: their mean
        for first, last in find_excursions(along, dt, settings.tolerance_ps, settings.min_length_nm))
    warnings = []
    parts = [{terminal.label.partition(".")[0] for terminal in (route.start, route.end) if "." in terminal.label}
             for route in (p, n)]
    if len(parts[0]) == len(parts[1]) == 2 and parts[0] != parts[1]:
        warnings.append("P and N end on different parts")
    for side, route in (("P", p), ("N", n)):
        if not route.complete:
            warnings.append(f"{side} side is not fully routed")
        if route.branches:
            warnings.append(f"{side} side has {len(route.branches)} branch" + ("es" if len(route.branches) > 1 else ""))
        for loop in route.loops:
            warnings.append(f"{side} side has a loop: {loop.length_nm / 1e6:.1f} mm of copper beside the route")
    leads = (-_per_sample(i, dt, len(a)), _per_sample(j, dt, len(b)))  # Δt < 0: P ahead
    traces = (_trace(walks[0], s_a, leads[0]), _trace(walks[1], s_b, leads[1]))
    crossings = tuple(replace(crossing, lead_ps=float(np.interp(crossing.at_nm, s, lead)))
                      for walk, s, lead in zip(walks, (s_a, s_b), leads) for crossing in walk.crossings)
    pads = tuple(_pads(p, traces[0], "p", heights) + _pads(n, traces[1], "n", heights))
    lengths = {}
    for walk in walks:
        for key, length in walk.lines.items():
            lengths[key] = lengths.get(key, 0.0) + length / 2  # both sides: their mean
    lines = tuple({"layer": layer, "kind": kind, "mm": length / 1e6}
                  for (layer, kind), length in sorted(lengths.items(), key=lambda entry: -entry[1]))
    lag = (behind[0] + behind[1]) / 2
    return PairPhase(channel, p, n, flipped, centre, dt, along, float(walks[0].delay[-1]), float(walks[1].delay[-1]),
                     width, excursions, crossings,
                     tuple(sorted(walks[0].sources | walks[1].sources)), tuple(warnings), traces, pads, lines,
                     matched_nm=matched, delays_ps=reached, lag_ps=lag,
                     read_dt_ps=modes.apparent_dt(reached, lag, settings.rise_ps), band_ps=band)


def analyze_board(snapshot: model.BoardSnapshot, model_: delays.DelayModel | None = None,
                  settings: Settings = Settings()) -> tuple[list[PairPhase], list[str]]:
    """Every pair on the board at once (tests and tools): (results, warnings)."""
    heights = protocol.layer_heights_nm(snapshot)[0]
    model_ = model_ or delays.StackupDelays(snapshot.stackup.layers, heights)
    by_net = channels.items_by_net(snapshot)
    references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
    grounds = ground_edges(snapshot)
    results, warnings = [], []
    for channel in channels.find_channels(snapshot, settings.follow_series):
        result, warning = _analyze_channel(channel, by_net, heights, references, model_, settings, grounds)
        if result is not None:
            results.append(result)
        if warning:
            warnings.append(warning)
    return results, warnings


def _analyze_channel(channel, by_net, heights, references, model_, settings, grounds=None):
    p_items, n_items = channels.combined(by_net, channel.p_nets), channels.combined(by_net, channel.n_nets)
    items = channels.combined(by_net, channel.nets)
    net_ids = tuple(item.id for kind in (items.tracks, items.arcs, items.vias, items.pads) for item in kind)
    touching = channels.contacts(p_items, n_items, heights)
    routes = channels.pair_routes(p_items, n_items, heights, references, channel.p_links, channel.n_links)
    if touching:
        measured = analyze(channel, *routes, model_, heights, settings, grounds) if None not in routes else None
        return Shorted(channel, tuple(touching), net_ids, measured), ""
    if routes[0] is None or routes[1] is None:
        return None, f"{channel.name}: no routed path between two pads yet"
    result = analyze(channel, *routes, model_, heights, settings, grounds)
    return replace(result, net_ids=net_ids), ""


# --- Live: recompute only pairs whose copper changed ---------------------------------------

def _pad_key(pad: model.Pad):
    return pad.id, pad.net, pad.pos, pad.number, pad.footprint_id, pad.layers


class PhaseTracker:
    """The bridge's dynamic-phase state: settings from Blender, delay inputs from the
    board and project files, and one result per channel, recomputed when its copper,
    the delay inputs or its settings change, within BUDGET_S per poll."""

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.settings = Settings()
        self.results: dict[str, PairPhase] = {}
        self.frames: dict[str, bytes] = {}  # key -> latest phase_pair frame; replaced whole (resync reads it)
        self.list_frame = b""
        self.warnings: list[str] = []
        self._channel_warnings: dict[str, str] = {}  # key -> why it has no result
        self.board_file = ""
        self.project_file = ""
        self.netclasses: dict[str, tuple[str, str]] = {}  # net -> (netclass, tuning profile) from KiCad
        self._fingerprints: dict[str, int] = {}
        self._pending = 0  # channels still waiting for their turn
        self._netclass_tries = None
        self._parts = None  # the snapshot's tuples last seen
        self._channels: list[channels.Channel] = []
        self._by_net = {}
        self._grounds = {}  # layer -> (pour edges, their nets), from the snapshot's zone fills
        self._grounds_of = None  # the zones tuple they came from
        self._inputs_stamp = None
        self._model = None
        self._model_key = ""
        self._heights = {}
        self._listed = None
        self._error = ""  # the last `failed` message sent

    def reset(self):
        self.__init__(self.clock)

    def configure(self, request: dict):
        settings = Settings.from_request(request)
        if settings != self.settings:
            self.settings = settings
            self._parts = None  # channels depend on follow_series; results on the rest

    def set_files(self, board_file: str, project_file: str):
        self.board_file, self.project_file = board_file, project_file

    def wanted_nets(self) -> set[str]:
        return {net for channel in self._channels for net in channel.nets}

    def _file_stamp(self) -> tuple:
        stamps = []
        for path in (self.board_file, self.project_file):
            try:
                info = os.stat(path) if path else None
                stamps.append((path, info.st_mtime_ns, info.st_size) if info else (path,))
            except OSError:
                stamps.append((path,))
        return tuple(stamps)

    def _delay_model(self, snapshot: model.BoardSnapshot) -> bool:
        """Rebuild the delay model when the stackup, the files or the netclasses change;
        True when it was rebuilt."""
        files = self._file_stamp()
        if self._inputs_stamp is not None and files[1] != self._inputs_stamp[0][1]:
            self.netclasses = {}  # the project changed: netclass assignments may have too
            self._netclass_tries = None
        stamp = (files, snapshot.stackup, tuple(sorted(self.netclasses.items())))
        if stamp == self._inputs_stamp and self._model is not None:
            return False
        self._inputs_stamp = stamp
        self._heights = protocol.layer_heights_nm(snapshot)[0]
        text = ""
        if self.board_file:
            try:
                with open(self.board_file, encoding="utf-8") as handle:
                    text = handle.read()
            except (OSError, UnicodeDecodeError):
                pass
        stackup = board_text.stackup_layers(text) or snapshot.stackup.layers
        project = delays.read_project(self.project_file)
        nets = self.wanted_nets()
        from_project = delays.net_profiles_from_project(
            project, nets, {net: netclass for net, (netclass, _) in self.netclasses.items()})
        net_profiles = {net: self.netclasses.get(net, ("", ""))[1] or from_project.get(net, "") for net in nets}
        profiles = delays.tuning_profiles(project)
        self._model = delays.StackupDelays(stackup, self._heights, profiles, net_profiles)
        self._model_key = repr((stackup, sorted(self._heights.items()), sorted(profiles.items()),
                                sorted(net_profiles.items())))
        return True

    def update(self, snapshot: model.BoardSnapshot, revision: int, read_netclasses=None) -> list[bytes]:
        """Frames for the pairs that changed since the last call (and the pair list when
        it changed). `read_netclasses(nets)` asks KiCad for pair nets' netclasses.
        Cheap when nothing changed: the snapshot's item tuples are the reader's own
        until an edit replaces them."""
        started = self.clock()
        parts = (snapshot.tracks, snapshot.arcs, snapshot.vias, snapshot.pads, snapshot.footprints,
                 snapshot.stackup, snapshot.zones)
        stale = self._pending or self._parts is None or any(a is not b for a, b in zip(parts, self._parts))
        if stale:
            self._channels = channels.find_channels(snapshot, self.settings.follow_series)
            self._by_net = channels.items_by_net(snapshot)
            if self._grounds_of is not snapshot.zones:
                self._grounds, self._grounds_of = ground_edges(snapshot), snapshot.zones
            self._parts = parts  # only once its channels are known: a failure above is tried again
        missing = self.wanted_nets() - self.netclasses.keys()
        if missing and read_netclasses is not None and self._netclass_tries != missing:
            try:
                found = read_netclasses(missing)
            except Exception:
                found = {}  # an older KiCad: the project file's netclass patterns stand in
            if found is not None:  # None: KiCad busy, asked again on a later poll
                self._netclass_tries = missing  # asked once per set of nets: KiCad may not know them all
                self.netclasses.update(found)
        if not self._delay_model(snapshot) and not stale:
            return []
        references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
        frames, results, encoded, fingerprints = [], dict(self.results), dict(self.frames), {}
        warnings = dict(self._channel_warnings)
        pending = 0
        for channel in self._channels:
            fingerprint = self._fingerprint(channel, references)
            if self._fingerprints.get(channel.key) == fingerprint:
                fingerprints[channel.key] = fingerprint
                continue
            if self.clock() - started > BUDGET_S:
                pending += 1  # its old result (if any) stays until its turn
                continue
            fingerprints[channel.key] = fingerprint
            try:
                result, warnings[channel.key] = _analyze_channel(channel, self._by_net, self._heights, references,
                                                                 self._model, self.settings, self._grounds)
                frame = pair_frame(result, revision) if result is not None else b""
            except Exception as exc:  # this pair only: the others are still measured, it is tried again when it changes
                result, warnings[channel.key] = None, f"{channel.name}: not measured ({type(exc).__name__}: {exc})"
            if result is None:
                results.pop(channel.key, None)
                encoded.pop(channel.key, None)
                continue
            results[channel.key] = result
            encoded[channel.key] = frame
            frames.append(frame)
        current = {channel.key for channel in self._channels}
        keys = [channel.key for channel in self._channels if channel.key in results]
        self.results = {key: results[key] for key in keys}
        self.frames = {key: encoded[key] for key in keys}  # a new dict: a resync may be reading the old
        self._fingerprints, self._pending = fingerprints, pending
        self._channel_warnings = {key: text for key, text in warnings.items() if key in current and text}
        self.warnings = sorted(self._channel_warnings.values())
        listed = (tuple(keys), self.settings, tuple(self.warnings), pending)
        if listed != self._listed:
            self._listed = listed
            self.list_frame = protocol.phase_list_message(keys, self.settings.as_json(), self.warnings, pending,
                                                          revision)
            frames.insert(0, self.list_frame)
        self._error = ""
        return frames

    def failed(self, exc: Exception, revision: int) -> list[bytes]:
        """`update` raised outside any one pair: the viewer drops every pair and the panel
        says why (sent once, not every poll). Every pair is measured again, and the list
        resent, on the next update that gets that far."""
        message = f"Dynamic phase stopped: {type(exc).__name__}: {exc}"
        self.results, self.frames, self._fingerprints, self._listed = {}, {}, {}, None
        if message == self._error:
            return []
        self._error = message
        self.list_frame = protocol.phase_list_message([], self.settings.as_json(), [message], 0, revision)
        return [self.list_frame]

    def _fingerprint(self, channel: channels.Channel, references) -> int:
        """Everything a channel's result depends on (a process-local hash)."""
        items = channels.combined(self._by_net, channel.nets)
        return hash((items.tracks, items.arcs, items.vias, tuple(_pad_key(pad) for pad in items.pads),
                     tuple(sorted({references.get(pad.footprint_id, "") for pad in items.pads})),
                     channel, channel.key in self.settings.flipped, self.settings.tolerance_ps,
                     self.settings.min_length_nm, self.settings.rise_ps, self._model_key, id(self._grounds_of)))

    def snapshot_frames(self) -> list[bytes]:
        """Everything for a viewer that (re)connected: the list, then every pair."""
        frames = self.frames
        return ([self.list_frame] if self.list_frame else []) + list(frames.values())

    def measure_all(self, snapshot: model.BoardSnapshot) -> tuple[list, list[str]]:
        """Every pair measured now, whole and at once (the DRC column's Dynamic phase
        button): (results, warnings) as `analyze_board`, with the delay model and settings
        the tracker has. It changes nothing in the tracker: the worker may be polling."""
        return analyze_board(snapshot, self._model, self.settings)


def _end(p_end: channels.Terminal, n_end: channels.Terminal) -> dict:
    """One end of the pair for the frame, marked between its P and N terminals."""
    return {"label": end_label(p_end, n_end), "p": p_end.label, "n": n_end.label,
            "ids": [p_end.item_id, n_end.item_id], "layer": p_end.layer,
            "x": round((p_end.pos[0] + n_end.pos[0]) / 2), "y": round((p_end.pos[1] + n_end.pos[1]) / 2)}


def _until(trace: Trace, pos) -> tuple[Trace, float]:
    """The trace from its start to its point nearest `pos` (x, y nm), with no lead (a
    shorted pair has no phase), and the delay there: where a short stops the pulses."""
    near = int(np.argmin(np.hypot(*(trace.xyz_nm[:, :2] - np.asarray(pos, np.float64)).T)))
    keep = slice(0, near + 1)
    return (Trace(trace.xyz_nm[keep], trace.delay_ps[keep], np.zeros(near + 1), trace.width_nm[keep]),
            float(trace.delay_ps[near]))


def _shorted_frame(result: Shorted, revision: int) -> bytes:
    """A shorted pair: where P and N touch; with its routes, the traces from the start
    terminal up to the first short along P, for the pulses."""
    info = {
        "key": result.channel.key, "name": result.channel.name,
        "p_nets": list(result.channel.p_nets), "n_nets": list(result.channel.n_nets),
        "parts": [link.reference for link in (*result.channel.p_links, *result.channel.n_links)],
        "shorts": [{"x": contact.pos[0], "y": contact.pos[1], "layer": contact.layer,
                    "ids": [contact.p_id, contact.n_id]} for contact in result.contacts],
        "ids": list(result.net_ids), "net_ids": list(result.net_ids),
        "warnings": [f"P and N are shorted ({len(result.contacts)} place" + ("s)" if len(result.contacts) > 1 else ")")]}
    measured = result.measured
    if measured is None or len(measured.traces) != 2:
        return protocol.phase_pair_message(info, {}, revision)
    first = min(result.contacts, key=lambda contact: _until(measured.traces[0], contact.pos)[1])
    cut = {side: _until(trace, first.pos) for side, trace in zip("pn", measured.traces)}
    info.update({
        "start": _end(measured.p.start, measured.n.start), "flipped": measured.flipped,
        "delay_p_ps": cut["p"][1], "delay_n_ps": cut["n"][1],
        "vias": [{"side": crossing.side.lower(), "id": crossing.item_id, "x": crossing.pos[0], "y": crossing.pos[1],
                  "diameter": crossing.diameter_nm, "drill": crossing.drill_nm,
                  "layers": [crossing.from_layer, crossing.to_layer],
                  "t": [crossing.reached_ps, crossing.reached_ps + crossing.delay_ps], "lead": 0.0}
                 for crossing in measured.crossings if crossing.reached_ps <= cut[crossing.side.lower()][1]],
        "pads": [dict(pad, lead=0.0) for pad in measured.pads if pad["t"] <= cut[pad["side"]][1]]})
    traces = {side: (trace.xyz_nm, trace.delay_ps, trace.lead_ps, trace.width_nm) for side, (trace, _) in cut.items()}
    return protocol.phase_pair_message(info, traces, revision)


def pair_frame(result: PairPhase | Shorted, revision: int) -> bytes:
    """One pair for Blender (protocol.phase_pair_message)."""
    if isinstance(result, Shorted):
        return _shorted_frame(result, revision)
    p, n = result.p, result.n

    info = {
        "key": result.channel.key, "name": result.channel.name,
        "p_nets": list(result.channel.p_nets), "n_nets": list(result.channel.n_nets),
        "parts": [link.reference for link in (*result.channel.p_links, *result.channel.n_links)],
        "start": _end(p.start, n.start), "end": _end(p.end, n.end),
        "delay_p_ps": result.delay_p_ps, "delay_n_ps": result.delay_n_ps, "skew_ps": result.skew_ps,
        "max_ps": result.max_abs_ps, "length_p_mm": p.length_nm / 1e6, "length_n_mm": n.length_nm / 1e6,
        "width_nm": result.width_nm, "flipped": result.flipped,
        "excursions": [{"x": e.pos[0], "y": e.pos[1], "z": e.pos[2], "at_mm": e.at_nm / 1e6,
                        "length_mm": e.length_nm / 1e6, "peak_ps": e.peak_ps, "mode_lag_ps": e.mode_lag_ps,
                        "id": e.item_id, "ids": list(e.item_ids)}
                       for e in result.excursions],
        "branches": [{"side": side, "label": branch.terminal.label, "id": branch.terminal.item_id,
                      "at_mm": branch.at_nm / 1e6, "length_mm": branch.length_nm / 1e6}
                     for side, route in (("P", p), ("N", n)) for branch in route.branches],
        "loops": [{"side": side, "x": loop.pos[0], "y": loop.pos[1], "layer": loop.layer, "ids": list(loop.item_ids),
                   "length_mm": loop.length_nm / 1e6}
                  for side, route in (("P", p), ("N", n)) for loop in route.loops],
        "sources": list(result.sources), "warnings": list(result.warnings),
        "band_note": BAND_NOTE,  # what the band round Δt in the centreline arrays means
        "ids": list(dict.fromkeys(p.item_ids + n.item_ids)),
        "pads": list(result.pads),
        "lines": list(result.lines),
        "net_ids": list(result.net_ids),
        "vias": [{"side": crossing.side.lower(), "id": crossing.item_id, "x": crossing.pos[0], "y": crossing.pos[1],
                  "diameter": crossing.diameter_nm, "drill": crossing.drill_nm,
                  "layers": [crossing.from_layer, crossing.to_layer],
                  "t": [crossing.reached_ps, crossing.reached_ps + crossing.delay_ps], "lead": crossing.lead_ps}
                 for crossing in result.crossings],
    }
    traces = {side: (trace.xyz_nm, trace.delay_ps, trace.lead_ps, trace.width_nm)
              for side, trace in zip("pn", result.traces)}
    centreline = {"along": result.along_nm, "dt": result.dt_ps,  # Δt along the pair: the chart export
                  "dt_read": result.read_dt_ps if result.read_dt_ps is not None else result.dt_ps}  # as a probe reads it
    if result.band_ps is not None:  # the model limits, below and above dt
        centreline["band_lo"], centreline["band_hi"] = result.band_ps[:, 0], result.band_ps[:, 1]
    return protocol.phase_pair_message(info, traces, revision, centreline)
