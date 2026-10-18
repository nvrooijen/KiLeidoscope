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

The delay is KiCad's: one number per layer, from the net's tuning profile
(delays.py), plus each end pad's pad-to-die delay (a KiCad pad property): the start
pad's before the first copper, so it offsets Δt along the whole pair; the end pad's after
the last, so it is in the end-to-end skew but at no point of the copper. A pair without a
profile, with one whose time-domain tuning is off, or with copper on a layer its profile
has no delay for, is listed but not measured (`Unprofiled`).

Each via or plated hole a route passes through is kept (`Crossing`: position,
layers, barrel length, delay) for return-path and via-stub analyses to reuse.
"""

import math
import os
import time
from dataclasses import dataclass, field, replace

import numpy as np

from . import channels, delays, model, protocol

SAMPLES = 400  # per side at most; longer routes get a longer step
MIN_STEP_NM = 50_000
BUDGET_S = 0.08  # per poll; pairs left over follow on the next polls



@dataclass(frozen=True)
class Settings:
    tolerance_ps: float = 1.0
    min_length_nm: float = 5_000_000
    follow_series: bool = True
    flipped: frozenset = frozenset()  # channel keys walked from their other end

    @classmethod
    def from_request(cls, header: dict) -> "Settings":
        """Blender's `phase_settings` frame; missing or bad values keep the defaults."""
        default = cls()
        try:
            tolerance = max(0.0, float(header.get("tolerance_ps", default.tolerance_ps)))
            length = max(0.0, float(header.get("min_length_mm", default.min_length_nm / 1e6))) * 1e6
        except (TypeError, ValueError):
            tolerance, length = default.tolerance_ps, default.min_length_nm
        flipped = header.get("flipped", ())
        return cls(tolerance, length, bool(header.get("follow_series", True)),
                   frozenset(str(key) for key in flipped) if isinstance(flipped, (list, tuple)) else frozenset())

    def as_json(self) -> dict:
        return {"tolerance_ps": self.tolerance_ps, "min_length_mm": self.min_length_nm / 1e6,
                "follow_series": self.follow_series, "flipped": sorted(self.flipped)}


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
class Unprofiled:
    """A pair KiCad has no delay for: no tuning profile on a net's netclass, a profile with
    time-domain tuning off, or copper on a layer the profile has no delay for. Listed, not
    measured; `reason` says which."""
    channel: channels.Channel
    reason: str
    net_ids: tuple = ()


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
    net_ids: tuple = ()  # every item on the pair's nets (stubs and leftovers too), for Blender's highlight
    # (k, 2) where each Δt reading sits: along P's copper and along N's (nm), so a tool
    # comparing with a measurement along the copper reads it at the same two places, and
    # each side's delay from the start there (ps; their difference is dt_ps).
    matched_nm: np.ndarray | None = field(default=None, repr=False)
    delays_ps: np.ndarray | None = field(default=None, repr=False)

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
    die: tuple  # (start, end) pad-to-die delays (ps); the start's is in `delay`, the end's is added to the total


def _walk(route: channels.Route, side: str, model_: delays.DelayModel, heights: dict[str, int]) -> _Walk:
    xyz, along, delay, items, widths, nets = [], [], [], [], [], []
    crossings, sources = [], set()
    # KiCad's pad-to-die delay of the two end pads, as KiCad adds it to a net's total: the
    # start pad's is spent before the first copper, the end pad's after the last.
    pads = {pad.id: pad for pad in route.pads}
    die = tuple(pads[end.item_id].die_delay_ps if end.item_id in pads else 0.0 for end in (route.start, route.end))
    for end, ps in zip((route.start, route.end), die):
        if ps:
            sources.add(f"pad-to-die delay of {end.label}")
    s, t = 0.0, die[0]

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
            barrel = model_.barrel(piece.net, piece.layer, piece.to_layer, piece.span, piece.length_nm)
            if barrel is None:
                raise ValueError(f"no delay for {piece.net}'s via")  # _unprofiled catches it first
            ps, source = barrel
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
        if unit is None:
            raise ValueError(f"no delay for {piece.net} on {piece.layer}")  # _unprofiled catches it first
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
                 np.asarray(widths, np.float64), nets, crossings, sources, die)


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
            heights: dict[str, int], settings: Settings) -> PairPhase:
    flipped = channel.key in settings.flipped
    p, n = _orient(p, n, flipped)
    walks = (_walk(p, "P", model_, heights), _walk(n, "N", model_, heights))
    step = max(MIN_STEP_NM, max(walk.along[-1] for walk in walks) / SAMPLES)
    (a, t_p, items_p, s_a), (b, t_n, items_n, s_b) = (_resample(walk, step) for walk in walks)
    i, j = _align(a, b)
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
    excursions = tuple(
        Excursion(tuple(float(v) for v in centre[first]), float(along[first]), float(along[last] - along[first]),
                  float(dt[first + int(np.abs(dt[first:last + 1]).argmax())]),
                  next((item for item in items_p[i[first]:] if item), ""),
                  tuple(dict.fromkeys(item for item in (*items_p[i[first]:i[last] + 1], *items_n[j[first]:j[last] + 1])
                                      if item)))
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
    return PairPhase(channel, p, n, flipped, centre, dt, along,
                     float(walks[0].delay[-1] + walks[0].die[1]), float(walks[1].delay[-1] + walks[1].die[1]),
                     width, excursions, crossings,
                     tuple(sorted(walks[0].sources | walks[1].sources)), tuple(warnings), traces, pads,
                     matched_nm=matched, delays_ps=reached)


def analyze_board(snapshot: model.BoardSnapshot, model_: delays.DelayModel | None = None,
                  settings: Settings = Settings()) -> tuple[list[PairPhase], list[str]]:
    """Every pair on the board at once (tests and tools): (results, warnings)."""
    heights = protocol.layer_heights_nm(snapshot)[0]
    model_ = model_ or delays.ProfileDelays()
    by_net = channels.items_by_net(snapshot)
    references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
    results, warnings = [], []
    for channel in channels.find_channels(snapshot, settings.follow_series):
        result, warning = _analyze_channel(channel, by_net, heights, references, model_, settings)
        if result is not None:
            results.append(result)
        if warning:
            warnings.append(warning)
    return results, warnings


def _unprofiled(routes, model_: delays.DelayModel) -> str:
    """Why KiCad has no delay for these routes ("" when it has one for every piece): a net
    without a tuning profile, a profile with time-domain tuning off (KiCad then charges 0 ps
    to everything on it), or the layers its profile has no delay for."""
    pieces = [piece for route in routes if route is not None for piece in route.pieces]
    bare = sorted({piece.net for piece in pieces if piece.net and not model_.profile(piece.net)})
    if bare:
        return "no tuning profile in KiCad for " + ", ".join(bare)
    off = sorted({model_.profile(piece.net) for piece in pieces if piece.net and not model_.time_domain(piece.net)})
    if off:
        return "time-domain tuning is off in KiCad for tuning profile " + ", ".join(repr(name) for name in off)
    missing = sorted({piece.layer for piece in pieces if piece.kind != "barrel" and piece.net
                      and model_.track(piece.net, piece.layer, piece.width) is None})
    if missing:
        names = sorted({model_.profile(piece.net) for piece in pieces if piece.net})
        return f"tuning profile {', '.join(repr(name) for name in names)} has no delay for {', '.join(missing)}"
    return ""


def _analyze_channel(channel, by_net, heights, references, model_, settings):
    p_items, n_items = channels.combined(by_net, channel.p_nets), channels.combined(by_net, channel.n_nets)
    items = channels.combined(by_net, channel.nets)
    net_ids = tuple(item.id for kind in (items.tracks, items.arcs, items.vias, items.pads) for item in kind)
    touching = channels.contacts(p_items, n_items, heights)
    routes = channels.pair_routes(p_items, n_items, heights, references, channel.p_links, channel.n_links)
    reason = _unprofiled(routes, model_)
    if touching:
        measured = analyze(channel, *routes, model_, heights, settings) if None not in routes and not reason else None
        return Shorted(channel, tuple(touching), net_ids, measured), ""
    if routes[0] is None or routes[1] is None:
        return None, f"{channel.name}: no routed path between two pads yet"
    if reason:
        return Unprofiled(channel, reason[0].upper() + reason[1:], net_ids), ""
    result = analyze(channel, *routes, model_, heights, settings)
    return replace(result, net_ids=net_ids), ""


# --- Live: recompute only pairs whose copper changed ---------------------------------------

def _pad_key(pad: model.Pad):
    return pad.id, pad.net, pad.pos, pad.number, pad.footprint_id, pad.layers, pad.die_delay_ps


class PhaseTracker:
    """The bridge's dynamic-phase state: settings from Blender, tuning profiles from the
    project file, and one result per channel, recomputed when its copper,
    the delay inputs or its settings change, within BUDGET_S per poll."""

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.settings = Settings()
        self.results: dict[str, PairPhase] = {}
        self.frames: dict[str, bytes] = {}  # key -> latest phase_pair frame; replaced whole (resync reads it)
        self.list_frame = b""
        self.warnings: list[str] = []
        self._channel_warnings: dict[str, str] = {}  # key -> why it has no result
        self.project_file = ""
        self.netclasses: dict[str, tuple[str, str]] = {}  # net -> (netclass, tuning profile) from KiCad
        self._fingerprints: dict[str, int] = {}
        self._pending = 0  # channels still waiting for their turn
        self._netclass_tries = None
        self._parts = None  # the snapshot's tuples last seen
        self._channels: list[channels.Channel] = []
        self._by_net = {}
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

    def set_project(self, project_file: str):
        """The .kicad_pro the tuning profiles come from ("": none, so no pair is measured)."""
        self.project_file = project_file

    def wanted_nets(self) -> set[str]:
        return {net for channel in self._channels for net in channel.nets}

    def _file_stamp(self) -> tuple:
        stamps = []
        for path in (self.project_file,):
            try:
                info = os.stat(path) if path else None
                stamps.append((path, info.st_mtime_ns, info.st_size) if info else (path,))
            except OSError:
                stamps.append((path,))
        return tuple(stamps)

    def _delay_model(self, snapshot: model.BoardSnapshot) -> bool:
        """Rebuild the delay model when the stackup, the project file or the netclasses change;
        True when it was rebuilt."""
        files = self._file_stamp()
        if self._inputs_stamp is not None and files[0] != self._inputs_stamp[0][0]:
            self.netclasses = {}  # the project changed: netclass assignments may have too
            self._netclass_tries = None
        stamp = (files, snapshot.stackup, tuple(sorted(self.netclasses.items())))
        if stamp == self._inputs_stamp and self._model is not None:
            return False
        self._inputs_stamp = stamp
        self._heights = protocol.layer_heights_nm(snapshot)[0]
        project = delays.read_project(self.project_file)
        nets = self.wanted_nets()
        from_project = delays.net_profiles_from_project(
            project, nets, {net: netclass for net, (netclass, _) in self.netclasses.items()})
        net_profiles = {net: self.netclasses.get(net, ("", ""))[1] or from_project.get(net, "") for net in nets}
        profiles = delays.tuning_profiles(project)
        height = delays.via_height(project)
        self._model = delays.ProfileDelays(profiles, net_profiles, height)
        self._model_key = repr((sorted(self._heights.items()), sorted(profiles.items()), sorted(net_profiles.items()),
                                height))
        return True

    def update(self, snapshot: model.BoardSnapshot, revision: int, read_netclasses=None) -> list[bytes]:
        """Frames for the pairs that changed since the last call (and the pair list when
        it changed). `read_netclasses(nets)` asks KiCad for pair nets' netclasses.
        Cheap when nothing changed: the snapshot's item tuples are the reader's own
        until an edit replaces them."""
        started = self.clock()
        parts = (snapshot.tracks, snapshot.arcs, snapshot.vias, snapshot.pads, snapshot.footprints,
                 snapshot.stackup)
        stale = self._pending or self._parts is None or any(a is not b for a, b in zip(parts, self._parts))
        if stale:
            self._channels = channels.find_channels(snapshot, self.settings.follow_series)
            self._by_net = channels.items_by_net(snapshot)
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
                                                                 self._model, self.settings)
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
                     self.settings.min_length_nm, self._model_key))

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


def pair_frame(result: PairPhase | Shorted | Unprofiled, revision: int) -> bytes:
    """One pair for Blender (protocol.phase_pair_message)."""
    if isinstance(result, Shorted):
        return _shorted_frame(result, revision)
    if isinstance(result, Unprofiled):  # listed, nothing to draw
        return protocol.phase_pair_message({
            "key": result.channel.key, "name": result.channel.name,
            "p_nets": list(result.channel.p_nets), "n_nets": list(result.channel.n_nets),
            "parts": [link.reference for link in (*result.channel.p_links, *result.channel.n_links)],
            "unprofiled": result.reason, "ids": list(result.net_ids), "net_ids": list(result.net_ids),
            "warnings": [result.reason]}, {}, revision)
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
                        "length_mm": e.length_nm / 1e6, "peak_ps": e.peak_ps,
                        "id": e.item_id, "ids": list(e.item_ids)}
                       for e in result.excursions],
        "branches": [{"side": side, "label": branch.terminal.label, "id": branch.terminal.item_id,
                      "at_mm": branch.at_nm / 1e6, "length_mm": branch.length_nm / 1e6}
                     for side, route in (("P", p), ("N", n)) for branch in route.branches],
        "loops": [{"side": side, "x": loop.pos[0], "y": loop.pos[1], "layer": loop.layer, "ids": list(loop.item_ids),
                   "length_mm": loop.length_nm / 1e6}
                  for side, route in (("P", p), ("N", n)) for loop in route.loops],
        "sources": list(result.sources), "warnings": list(result.warnings),
        "ids": list(dict.fromkeys(p.item_ids + n.item_ids)),
        "pads": list(result.pads),
        "net_ids": list(result.net_ids),
        "vias": [{"side": crossing.side.lower(), "id": crossing.item_id, "x": crossing.pos[0], "y": crossing.pos[1],
                  "diameter": crossing.diameter_nm, "drill": crossing.drill_nm,
                  "layers": [crossing.from_layer, crossing.to_layer],
                  "t": [crossing.reached_ps, crossing.reached_ps + crossing.delay_ps], "lead": crossing.lead_ps}
                 for crossing in result.crossings],
    }
    traces = {side: (trace.xyz_nm, trace.delay_ps, trace.lead_ps, trace.width_nm)
              for side, trace in zip("pn", result.traces)}
    centreline = {"along": result.along_nm, "dt": result.dt_ps}  # Δt along the pair: the chart export
    return protocol.phase_pair_message(info, traces, revision, centreline)
