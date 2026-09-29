# Vendored from Fill Resistance 1.4.2, fill_resistance/geometry.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Unchanged apart from this header.
"""Plain geometry data model. No kipy imports here.

Everything is int64 nanometers in KiCad board coordinates (y grows down);
z grows from the board top surface downwards through the stackup.
Problem is the complete solver input and doubles as the JSON dump schema,
so the whole pipeline downstream of board_io runs without KiCad.

Schema v2 is multi-layer: per-layer fills at stackup depths, linked by
via/through-pad barrels. v1 dumps (single layer, no vias) still load.
Schema v7 adds PDN terminals (supplies/loads); dumps <= v6 load with
terminals=[] and run the classic two-terminal solve unchanged. v8 adds
the per-terminal `bonded` flag (v7 dumps load with bonded=False).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

JSON_SCHEMA_VERSION = 8


@dataclass(frozen=True)
class Rect:
    x0: int
    y0: int
    x1: int
    y1: int
    layer_name: str

    @classmethod
    def normalized(cls, xa: int, ya: int, xb: int, yb: int, layer_name: str) -> "Rect":
        return cls(min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb), layer_name)

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


@dataclass
class Polygon:
    outline: np.ndarray                       # (N, 2) int64 nm, open ring
    holes: list[np.ndarray] = field(default_factory=list)


@dataclass
class LayerFill:
    layer_name: str
    thickness_nm: int
    z_nm: int                                 # copper center depth from board top
    polygons: list[Polygon]


@dataclass
class SurfaceBuildup:
    """Solder (plus optional added copper) sitting on an outer copper
    layer inside solder-mask openings (zones on F.Mask/B.Mask)."""
    layer_name: str                           # copper layer it sits on
    polygons: list[Polygon]


@dataclass
class TrackSeg:
    """One trace segment: straight ((2, 2) points) or arc ((3, 2)
    start/mid/end points). Kept as centerline + width so the raster can
    decide per run: wide traces are rasterized from their outline,
    traces narrower than TRACK_1D_FACTOR grid cells become exact 1D
    resistor chains along the centerline."""
    layer_name: str
    points: np.ndarray                        # (2|3, 2) int64 nm
    width_nm: int

    def outline(self, tol_nm: float) -> np.ndarray:
        if len(self.points) == 3:
            return arc_band_ring(self.points[0], self.points[1],
                                 self.points[2], self.width_nm, tol_nm)
        return capsule_ring(int(self.points[0][0]), int(self.points[0][1]),
                            int(self.points[1][0]), int(self.points[1][1]),
                            self.width_nm, tol_nm)

    def centerline(self, tol_nm: float) -> np.ndarray:
        """(N, 2) float polyline along the trace center, start to end."""
        if len(self.points) == 3:
            pts = arc_points(self.points[0], self.points[1], self.points[2],
                             tol_nm)
            return np.vstack([pts, self.points[2][None, :]]).astype(float)
        return self.points.astype(float)


@dataclass
class Electrode:
    """One PART of a current-injection terminal: a drawn rectangle, a
    selected pad, or a selected via. A terminal (V+ or V-) is a LIST of
    parts, all merged into one equipotential contact (externally
    bonded). `polygons` (board nm) is the exact copper shape when known
    (pads); None means the rectangle itself is the shape. `contact` =
    'all' or a layer name: which included layers this part touches.

    drill_nm > 0 marks a BARREL contact (selected via or through-hole
    pad): the current physically enters through the plated barrel (the
    lead/wire soldered into the hole), so the contact cells are the
    copper ring at the drill wall, not the whole pad face. `solder`
    additionally models a soldered THT joint: the hole is filled with
    solder and the pad face on the SOLDER side (protrusion_side,
    opposite the component) carries an average-thickness solder coat
    (Problem.solder_thickness_nm over `polygons`) plus the
    protruding-lead cone."""
    rect: Rect                                # bounding box (labels/summary)
    contact: str = "all"
    polygons: list[Polygon] | None = None
    label: str = "rect"
    drill_nm: int = 0                         # >0: barrel contact (slotted
                                              # holes: the slot WIDTH)
    pad_nm: int = 0                           # pad diameter (search bound;
                                              # largest dimension if oblong)
    pad_min_nm: int = 0                       # smallest pad dimension (cone
                                              # taper bound); 0 = pad_nm
    slot_dx_nm: int = 0                       # slotted (oblong) hole: offset
    slot_dy_nm: int = 0                       # from `center` to each end-cap
                                              # center of the slot, board
                                              # frame; (0, 0) = round drill
    center: tuple[int, int] | None = None     # drill center; None = rect center
    barrel_z: tuple[int, int] | None = None   # (z_top, z_bot); None = full stack
    solder: bool = False                      # soldered THT joint (see above)
    protrusion_side: str | None = None        # outer layer where the clipped
                                              # lead protrudes (opposite the
                                              # component): a solder cone
                                              # wraps it there, see
                                              # Problem.tht_protrusion_nm


@dataclass
class Terminal:
    """One PDN-mode terminal: a supply (Thevenin source: open-circuit
    volts v_oc behind r_out_ohm) or a load (prescribed current draw
    i_draw_a). Contact geometry is a list of Electrode parts. In PDN
    mode Problem.terminals replaces electrodes1/electrodes2; supply
    currents are solve OUTCOMES, load draws are prescribed.

    bonded: all the terminal's contact cells are shorted into one
    super-node (an externally bonded lug - a multi-pin package with
    internal metal). The TOTAL current is prescribed as usual, but the
    per-part/per-cell split becomes a solve outcome instead of the
    default per-cell area share (loads) / per-cell Thevenin attachment
    (supplies). The contact face is then equipotential."""
    role: str                                 # "supply" | "load"
    electrodes: list[Electrode]
    label: str = ""                           # display name; "" gets an
                                              # S1/L1 tag at solve time
    i_draw_a: float = 0.0                     # loads: prescribed draw [A]
    r_out_ohm: float = 0.0                    # supplies: Thevenin output
                                              # resistance [ohm]
    v_oc: float | None = None                 # supplies: open-circuit
                                              # volts; None -> the run's
                                              # v_nominal at solve time
    bonded: bool = False                      # short all contact cells
                                              # into one lug (see above)
    component: str = ""                       # display only: the owner
                                              # hint ("U5" / "near U5",
                                              # board_io.component_hints)
    comment: str = ""                         # display only: the user's
                                              # free-text note


@dataclass
class ViaLink:
    """A conductive barrel (via or plated through-hole pad) linking copper
    layers whose z lies within [z_top_nm, z_bot_nm]."""
    x: int
    y: int
    drill_nm: int                             # slotted holes: the slot WIDTH
    z_top_nm: int
    z_bot_nm: int
    kind: str = "via"                         # "via" | "pad"
    pad_nm: int = 0                           # pad/annular diameter; 0 = unknown
                                              # (oblong pads: LARGEST dimension,
                                              # used as a search bound)
    pad_min_nm: int = 0                       # smallest pad dimension (bounds
                                              # the lead-cone taper on oblong
                                              # pads); 0 = same as pad_nm
    slot_dx_nm: int = 0                       # slotted (oblong) hole: offset
    slot_dy_nm: int = 0                       # from (x, y) to each end-cap
                                              # center of the slot, board
                                              # frame; (0, 0) = round drill
    solder_filled: bool = False               # populated THT pad: the hole
                                              # holds lead + solder (in parallel
                                              # with the plating); False for
                                              # vias and DNP footprints
    protrusion_side: str | None = None        # populated THT pad: outer layer
                                              # where the clipped lead tents
                                              # (solder cone), opposite the
                                              # component side

    def spans(self, z_nm: int) -> bool:
        return self.z_top_nm - 1 <= z_nm <= self.z_bot_nm + 1

    def barrel_resistance(self, length_nm: int, rho_ohm_m: float,
                          plating_nm: int,
                          solder_rho_ohm_m: float | None = None,
                          lead_nm: float = 0,
                          lead_rho_ohm_m: float | None = None) -> float:
        """Barrel segment resistance over length_nm: thin-wall annulus of
        plating around the drill (slotted holes: thin wall around the
        stadium-shaped slot). With solder_rho_ohm_m the hole holds a
        soldered THT joint: the component lead (a cylinder of lead_nm
        diameter, resistivity lead_rho_ohm_m) and the solder filling the
        remaining bore conduct in parallel with the plating."""
        ext = 2.0 * math.hypot(self.slot_dx_nm, self.slot_dy_nm) * 1e-9
        wall = math.pi * (self.drill_nm * 1e-9) + 2.0 * ext
        ga = wall * (plating_nm * 1e-9) / rho_ohm_m
                                              # conductance-area [m^2/ohm-m]
        if solder_rho_ohm_m is not None:
            r_core = max(self.drill_nm / 2.0 - plating_nm, 0.0) * 1e-9
            r_lead = min(lead_nm * 1e-9 / 2.0, r_core)
            if lead_rho_ohm_m is not None and r_lead > 0:
                ga += math.pi * r_lead * r_lead / lead_rho_ohm_m
            ga += (math.pi * r_core * r_core + 2.0 * r_core * ext
                   - math.pi * r_lead * r_lead) / solder_rho_ohm_m
        return (length_nm * 1e-9) / ga


@dataclass
class Problem:
    board_path: str
    net_name: str
    rho_ohm_m: float
    plating_nm: int
    layers: list[LayerFill]                   # sorted by z_nm (top first)
    vias: list[ViaLink]
    electrodes1: list[Electrode]              # V+ terminal parts (merged)
    electrodes2: list[Electrode]              # V- terminal parts (merged)
    thickness_source: str = "stackup"
    # PDN mode: non-empty replaces electrodes1/2 entirely (the pipeline
    # rejects a problem carrying both) - N supplies + M loads instead of
    # one driven terminal pair
    terminals: list[Terminal] = field(default_factory=list)
    buildups: list[SurfaceBuildup] = field(default_factory=list)
    solder_thickness_nm: int = 50_000
    solder_rho_ohm_m: float = 1.32e-7
    extra_cu_nm: int = 0
    tracks: list[TrackSeg] = field(default_factory=list)
    vias_capped: bool = True                  # filled+capped vias: thin cap
    cap_plating_nm: int = 15_000              # over outer-layer mouths;
                                              # False = open mouths
    cap_max_drill_nm: int = 500_000           # fab caps only small vias:
                                              # drills above this stay open
                                              # even with vias_capped
    tht_protrusion_nm: int = 1_500_000        # clipped THT lead protrusion:
                                              # a solder cone of this height
                                              # at the drill wall (tapering
                                              # to zero at the pad edge)
                                              # wraps the lead on each solder
                                              # contact's protrusion_side;
                                              # 0 disables the cones
    tht_lead_clearance_nm: int = 250_000      # hole minus lead diameter (fab
                                              # rule): the lead cylinder of
                                              # drill - this conducts inside
                                              # every solder-filled hole
    tht_lead_rho_ohm_m: float = 1.68e-8       # lead material resistivity
                                              # (copper; brass ~6.4e-8,
                                              # copper-clad steel higher)

    @property
    def layer_names(self) -> list[str]:
        return [l.layer_name for l in self.layers]

    def contact_electrodes(self) -> list[Electrode]:
        """Every contact part regardless of mode: classic V+/V- lists
        plus all PDN terminal parts (exactly one group is non-empty in a
        valid problem). Use this wherever per-contact geometry features
        (solder coats, lead cones) are collected, so PDN terminals get
        the same treatment as classic ones."""
        return (self.electrodes1 + self.electrodes2
                + [e for t in self.terminals for e in t.electrodes])

    def sigma_s(self, layer_index: int) -> float:
        """Sheet conductance of one layer [S per square]."""
        return (self.layers[layer_index].thickness_nm * 1e-9) / self.rho_ohm_m

    def copper_bbox(self) -> tuple[int, int, int, int]:
        xs = [p.outline[:, 0] for l in self.layers for p in l.polygons]
        ys = [p.outline[:, 1] for l in self.layers for p in l.polygons]
        tol = 1_000.0
        for seg in self.tracks:
            # exact stroke bbox: centerline extrema + half width (round
            # caps); a chord-tessellated outline undershoots arc and cap
            # extrema by up to its sagitta tolerance
            pts = seg.centerline(tol)
            r = seg.width_nm / 2.0 + tol
            xs.append(np.array([pts[:, 0].min() - r, pts[:, 0].max() + r]))
            ys.append(np.array([pts[:, 1].min() - r, pts[:, 1].max() + r]))
        x = np.concatenate(xs)
        y = np.concatenate(ys)
        return int(x.min()), int(y.min()), int(x.max()), int(y.max())


def contact_solder_buildups(problem: Problem) -> list[str]:
    """Soldered THT-joint contacts: the pad face on the SOLDER side (the
    protrusion side, opposite the component - the component-side face
    stays bare) is covered in solder of average thickness
    solder_thickness_nm. Adds one SurfaceBuildup there for every
    `solder` electrode's pad shape (the buildup machinery intersects
    with actual copper at raster time). Returns the affected layer
    names. Called once when the problem is built."""
    included = {l.layer_name for l in problem.layers}
    touched = []
    for e in problem.contact_electrodes():
        if not e.solder or not e.polygons \
                or e.protrusion_side not in included:
            continue
        problem.buildups.append(
            SurfaceBuildup(layer_name=e.protrusion_side,
                           polygons=list(e.polygons)))
        touched.append(e.protrusion_side)
    return sorted(set(touched))


def slot_distance(xg, yg, dx_nm: int, dy_nm: int):
    """Distance from points (xg, yg) (numpy-broadcastable, coordinates
    RELATIVE to the hole center) to a slotted hole's axis - the segment
    (-dx, -dy)..(+dx, +dy) between the end-cap centers. The slot wall
    sits at distance width/2. Round drills (dx = dy = 0) reduce to the
    plain radius, so callers need no special case."""
    if dx_nm == 0 and dy_nm == 0:
        return np.hypot(xg, yg)
    l2 = float(dx_nm) * dx_nm + float(dy_nm) * dy_nm
    t = np.clip((xg * dx_nm + yg * dy_nm) / l2, -1.0, 1.0)
    return np.hypot(xg - t * dx_nm, yg - t * dy_nm)


def _disc_polygon(x_nm: float, y_nm: float, r_nm: float,
                  n: int = 32) -> Polygon:
    th = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return Polygon(outline=np.round(np.stack(
        [x_nm + r_nm * np.cos(th), y_nm + r_nm * np.sin(th)],
        axis=1)).astype(np.int64))


def _capsule_polygon(x_nm: float, y_nm: float, dx_nm: float, dy_nm: float,
                     r_nm: float, n: int = 16) -> Polygon:
    """Stadium: two half-circle caps of radius r_nm centered at
    (x +- dx, y +- dy), joined by straight flanks."""
    a0 = math.atan2(dy_nm, dx_nm)
    th = np.linspace(-0.5 * math.pi, 0.5 * math.pi, n) + a0
    cap1 = np.stack([x_nm + dx_nm + r_nm * np.cos(th),
                     y_nm + dy_nm + r_nm * np.sin(th)], axis=1)
    cap2 = np.stack([x_nm - dx_nm + r_nm * np.cos(th + math.pi),
                     y_nm - dy_nm + r_nm * np.sin(th + math.pi)], axis=1)
    return Polygon(outline=np.round(np.vstack([cap1, cap2])).astype(np.int64))


def tht_joint_buildups(problem: Problem,
                       shapes: dict | None = None) -> list[str]:
    """Solder coat of the net's populated STITCHING through-hole pads
    (ViaLink kind 'pad' with solder_filled), on the pad's SOLDER side
    (the protrusion side, opposite the component; the component-side
    face stays bare). `shapes` maps (x, y) to the exact pad polygons
    (fetched from KiCad); pads without one fall back to a pad-diameter
    disc. Contact pads are skipped: contact_solder_buildups already
    coats them with the exact pad shape. Returns the affected layer
    names."""
    included = {l.layer_name for l in problem.layers}
    contacts = {e.center for e in problem.contact_electrodes()
                if e.drill_nm > 0 and e.center is not None}
    touched = []
    for v in problem.vias:
        if v.kind != "pad" or not v.solder_filled \
                or (v.x, v.y) in contacts \
                or v.protrusion_side not in included:
            continue
        polys = (shapes or {}).get((v.x, v.y))
        if polys is None:
            if v.pad_nm <= v.drill_nm:
                continue
            # oblong pads: never coat past the pad - a capsule along the
            # slot axis, or the inscribed disc when the axis is unknown
            w = v.pad_min_nm or v.pad_nm
            hl = math.hypot(v.slot_dx_nm, v.slot_dy_nm)
            if hl > 0.0 and v.pad_nm > w:
                s = (v.pad_nm - w) / 2.0 / hl
                polys = [_capsule_polygon(v.x, v.y, v.slot_dx_nm * s,
                                          v.slot_dy_nm * s, w / 2.0)]
            else:
                polys = [_disc_polygon(v.x, v.y, w / 2.0)]
        problem.buildups.append(
            SurfaceBuildup(layer_name=v.protrusion_side,
                           polygons=list(polys)))
        touched.append(v.protrusion_side)
    return sorted(set(touched))


def _arc_params(start, mid, end) -> tuple[float, float, float, float, float] | None:
    """Circle through three points: (cx, cy, r, a0, sweep) with a0 the
    start angle and sweep signed; None if the points are collinear."""
    sx, sy = float(start[0]), float(start[1])
    mx, my = float(mid[0]), float(mid[1])
    ex, ey = float(end[0]), float(end[1])

    d = 2.0 * (sx * (my - ey) + mx * (ey - sy) + ex * (sy - my))
    chord = math.hypot(ex - sx, ey - sy)
    if abs(d) < 1e-9 * max(chord, 1.0):
        return None
    cx = ((sx**2 + sy**2) * (my - ey) + (mx**2 + my**2) * (ey - sy)
          + (ex**2 + ey**2) * (sy - my)) / d
    cy = ((sx**2 + sy**2) * (ex - mx) + (mx**2 + my**2) * (sx - ex)
          + (ex**2 + ey**2) * (mx - sx)) / d
    r = math.hypot(sx - cx, sy - cy)

    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(my - cy, mx - cx)
    a2 = math.atan2(ey - cy, ex - cx)
    two_pi = 2.0 * math.pi
    d01 = (a1 - a0) % two_pi
    d02 = (a2 - a0) % two_pi
    sweep = d02 if d01 <= d02 else d02 - two_pi
    return cx, cy, r, a0, sweep


def _n_arc_segments(sweep_abs: float, r: float, tol_nm: float) -> int:
    """Segments needed to keep the sagitta of each chord <= tol_nm."""
    tol = min(tol_nm, 0.999 * r)
    dtheta_max = 2.0 * math.acos(1.0 - tol / r)
    return max(2, int(math.ceil(sweep_abs / dtheta_max)))


def arc_points(start, mid, end, tol_nm: float) -> np.ndarray:
    """Tessellate a start/mid/end arc into points from start (inclusive)
    to end (exclusive), max sagitta <= tol_nm. Collinear input degrades
    to just the start point (straight segment)."""
    params = _arc_params(start, mid, end)
    if params is None:
        return np.array([[start[0], start[1]]], dtype=np.int64)
    cx, cy, r, a0, sweep = params
    n = _n_arc_segments(abs(sweep), r, tol_nm)
    ks = np.arange(n)
    angs = a0 + sweep * ks / n
    pts = np.stack([cx + r * np.cos(angs), cy + r * np.sin(angs)], axis=1)
    return np.round(pts).astype(np.int64)


def capsule_ring(x1: int, y1: int, x2: int, y2: int, width_nm: int,
                 tol_nm: float) -> np.ndarray:
    """Outline (open ring, int64 nm) of a straight track segment: a
    rectangle with semicircular end caps; a circle for a zero-length
    segment. Cap sagitta <= tol_nm."""
    r = width_nm / 2.0
    dx, dy = float(x2 - x1), float(y2 - y1)
    length = math.hypot(dx, dy)
    n = _n_arc_segments(math.pi, r, tol_nm)
    if length < 1.0:
        angs = np.linspace(0.0, 2.0 * math.pi, 2 * n, endpoint=False)
        pts = np.stack([x1 + r * np.cos(angs), y1 + r * np.sin(angs)],
                       axis=1)
        return np.round(pts).astype(np.int64)
    ux, uy = dx / length, dy / length
    a0 = math.atan2(ux, -uy)                  # angle of the left normal
    ks = np.arange(n + 1)
    cap2 = a0 - ks * math.pi / n              # +normal -> -normal, around end
    cap1 = a0 - (ks + n) * math.pi / n        # -normal -> +normal, around start
    pts = np.concatenate([
        np.stack([x2 + r * np.cos(cap2), y2 + r * np.sin(cap2)], axis=1),
        np.stack([x1 + r * np.cos(cap1), y1 + r * np.sin(cap1)], axis=1),
    ])
    return np.round(pts).astype(np.int64)


def arc_band_ring(start, mid, end, width_nm: int, tol_nm: float) -> np.ndarray:
    """Outline of an arc track: the annular band of the given width
    around the start/mid/end centerline, with semicircular end caps.
    Collinear input degrades to the straight capsule."""
    params = _arc_params(start, mid, end)
    if params is None:
        return capsule_ring(start[0], start[1], end[0], end[1], width_nm,
                            tol_nm)
    cx, cy, r, a0, sweep = params
    w2 = width_nm / 2.0
    router = r + w2
    rinner = max(r - w2, 0.0)
    sgn = 1.0 if sweep >= 0 else -1.0
    a1 = a0 + sweep
    m = _n_arc_segments(abs(sweep), router, tol_nm)
    ncap = _n_arc_segments(math.pi, w2, tol_nm)
    ks = np.arange(m + 1)

    th = a0 + sweep * ks / m                  # outer arc, start -> end
    parts = [np.stack([cx + router * np.cos(th),
                       cy + router * np.sin(th)], axis=1)]
    ex_, ey_ = cx + r * math.cos(a1), cy + r * math.sin(a1)
    ca = a1 + sgn * math.pi * np.arange(1, ncap) / ncap   # end cap, bulges
    parts.append(np.stack([ex_ + w2 * np.cos(ca),        # along exit tangent
                           ey_ + w2 * np.sin(ca)], axis=1))
    if rinner > 0:
        th = a1 - sweep * ks / m              # inner arc, end -> start
        parts.append(np.stack([cx + rinner * np.cos(th),
                               cy + rinner * np.sin(th)], axis=1))
    else:
        parts.append(np.array([[cx, cy]]))    # band swallows the center
    sx_, sy_ = cx + r * math.cos(a0), cy + r * math.sin(a0)
    ca = a0 + math.pi + sgn * math.pi * np.arange(1, ncap) / ncap
    parts.append(np.stack([sx_ + w2 * np.cos(ca),        # start cap, bulges
                           sy_ + w2 * np.sin(ca)], axis=1))  # backwards
    return np.round(np.concatenate(parts)).astype(np.int64)


def linearize_ring(nodes: list, tol_nm: float) -> np.ndarray:
    """nodes: list of ('pt', (x, y)) or ('arc', (start, mid, end)) tuples,
    already in board nm. Returns an (N, 2) int64 open ring."""
    parts = []
    for kind, data in nodes:
        if kind == "pt":
            parts.append(np.array([[data[0], data[1]]], dtype=np.int64))
        elif kind == "arc":
            parts.append(arc_points(data[0], data[1], data[2], tol_nm))
        else:
            raise ValueError(f"unknown polyline node kind: {kind}")
    ring = np.concatenate(parts, axis=0)
    if len(ring) > 1 and (ring[0] == ring[-1]).all():
        ring = ring[:-1]
    return ring


# --- JSON dump / load -------------------------------------------------------

def _poly_to_json(p: Polygon) -> dict:
    return {"outline": p.outline.tolist(), "holes": [h.tolist() for h in p.holes]}


def _poly_from_json(d: dict) -> Polygon:
    return Polygon(outline=np.asarray(d["outline"], dtype=np.int64),
                   holes=[np.asarray(h, dtype=np.int64) for h in d["holes"]])


def _electrode_to_json(e: Electrode) -> dict:
    return {
        "rect": vars(e.rect) | {},
        "contact": e.contact,
        "label": e.label,
        "polygons": (None if e.polygons is None
                     else [_poly_to_json(poly) for poly in e.polygons]),
        "drill_nm": e.drill_nm,
        "pad_nm": e.pad_nm,
        "pad_min_nm": e.pad_min_nm,
        "slot_dx_nm": e.slot_dx_nm,
        "slot_dy_nm": e.slot_dy_nm,
        "center": (None if e.center is None else list(e.center)),
        "barrel_z": (None if e.barrel_z is None else list(e.barrel_z)),
        "solder": e.solder,
        "protrusion_side": e.protrusion_side,
    }


def _electrode_from_json(d: dict) -> Electrode:
    return Electrode(
        rect=_rect_from_json(d["rect"]),
        contact=d.get("contact", "all"),
        label=d.get("label", "rect"),
        polygons=(None if d.get("polygons") is None
                  else [_poly_from_json(pd) for pd in d["polygons"]]),
        drill_nm=int(d.get("drill_nm", 0)),
        pad_nm=int(d.get("pad_nm", 0)),
        pad_min_nm=int(d.get("pad_min_nm", 0)),
        slot_dx_nm=int(d.get("slot_dx_nm", 0)),
        slot_dy_nm=int(d.get("slot_dy_nm", 0)),
        center=(None if d.get("center") is None
                else (int(d["center"][0]), int(d["center"][1]))),
        barrel_z=(None if d.get("barrel_z") is None
                  else (int(d["barrel_z"][0]), int(d["barrel_z"][1]))),
        solder=bool(d.get("solder", False)),
        protrusion_side=d.get("protrusion_side"),
    )


def _terminal_to_json(t: Terminal) -> dict:
    d = {
        "role": t.role,
        "label": t.label,
        "i_draw_a": t.i_draw_a,
        "r_out_ohm": t.r_out_ohm,
        "v_oc": t.v_oc,
        "bonded": t.bonded,
        "electrodes": [_electrode_to_json(e) for e in t.electrodes],
    }
    # display-only metadata, written when present (still schema v8:
    # optional keys, older loaders simply ignore them)
    if t.component:
        d["component"] = t.component
    if t.comment:
        d["comment"] = t.comment
    return d


def _terminal_from_json(d: dict) -> Terminal:
    return Terminal(
        role=d["role"],
        electrodes=[_electrode_from_json(ed) for ed in d["electrodes"]],
        label=d.get("label", ""),
        i_draw_a=float(d.get("i_draw_a", 0.0)),
        r_out_ohm=float(d.get("r_out_ohm", 0.0)),
        v_oc=(None if d.get("v_oc") is None else float(d["v_oc"])),
        bonded=bool(d.get("bonded", False)),      # <= v7: not bonded
        component=str(d.get("component", "")),
        comment=str(d.get("comment", "")),
    )


def problem_to_json(p: Problem) -> dict:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "board_path": p.board_path,
        "net_name": p.net_name,
        "rho_ohm_m": p.rho_ohm_m,
        "plating_nm": p.plating_nm,
        "thickness_source": p.thickness_source,
        "electrodes1": [_electrode_to_json(e) for e in p.electrodes1],
        "electrodes2": [_electrode_to_json(e) for e in p.electrodes2],
        "terminals": [_terminal_to_json(t) for t in p.terminals],
        "layers": [
            {
                "layer_name": l.layer_name,
                "thickness_nm": l.thickness_nm,
                "z_nm": l.z_nm,
                "polygons": [_poly_to_json(poly) for poly in l.polygons],
            }
            for l in p.layers
        ],
        "vias": [vars(v) | {} for v in p.vias],
        "tracks": [
            {"layer_name": s.layer_name, "points": s.points.tolist(),
             "width_nm": s.width_nm}
            for s in p.tracks
        ],
        "buildups": [
            {"layer_name": b.layer_name,
             "polygons": [_poly_to_json(poly) for poly in b.polygons]}
            for b in p.buildups
        ],
        "solder_thickness_nm": p.solder_thickness_nm,
        "solder_rho_ohm_m": p.solder_rho_ohm_m,
        "extra_cu_nm": p.extra_cu_nm,
        "vias_capped": p.vias_capped,
        "cap_plating_nm": p.cap_plating_nm,
        "cap_max_drill_nm": p.cap_max_drill_nm,
        "tht_protrusion_nm": p.tht_protrusion_nm,
        "tht_lead_clearance_nm": p.tht_lead_clearance_nm,
        "tht_lead_rho_ohm_m": p.tht_lead_rho_ohm_m,
    }


def _rect_from_json(rd: dict) -> Rect:
    return Rect(int(rd["x0"]), int(rd["y0"]), int(rd["x1"]), int(rd["y1"]),
                rd["layer_name"])


def problem_from_json(d: dict) -> Problem:
    version = d.get("schema_version", 1)
    if version == 1:
        # v1: single layer, no vias, rect electrodes
        return Problem(
            board_path=d["board_path"],
            net_name=d["net_name"],
            rho_ohm_m=float(d["rho_ohm_m"]),
            plating_nm=18_000,
            layers=[LayerFill(
                layer_name=d["layer_name"],
                thickness_nm=int(d["thickness_nm"]),
                z_nm=0,
                polygons=[_poly_from_json(pd) for pd in d["polygons"]],
            )],
            vias=[],
            electrodes1=[Electrode(rect=_rect_from_json(d["rect1"]))],
            electrodes2=[Electrode(rect=_rect_from_json(d["rect2"]))],
            thickness_source=d.get("thickness_source", "unknown"),
        )
    return Problem(
        board_path=d["board_path"],
        net_name=d["net_name"],
        rho_ohm_m=float(d["rho_ohm_m"]),
        plating_nm=int(d["plating_nm"]),
        layers=[
            LayerFill(
                layer_name=ld["layer_name"],
                thickness_nm=int(ld["thickness_nm"]),
                z_nm=int(ld["z_nm"]),
                polygons=[_poly_from_json(pd) for pd in ld["polygons"]],
            )
            for ld in d["layers"]
        ],
        vias=[
            ViaLink(x=int(vd["x"]), y=int(vd["y"]), drill_nm=int(vd["drill_nm"]),
                    z_top_nm=int(vd["z_top_nm"]), z_bot_nm=int(vd["z_bot_nm"]),
                    kind=vd.get("kind", "via"),
                    pad_nm=int(vd.get("pad_nm", 0)),
                    pad_min_nm=int(vd.get("pad_min_nm", 0)),
                    slot_dx_nm=int(vd.get("slot_dx_nm", 0)),
                    slot_dy_nm=int(vd.get("slot_dy_nm", 0)),
                    # older dumps: every THT pad counted as solder-filled
                    solder_filled=bool(vd.get(
                        "solder_filled", vd.get("kind", "via") == "pad")),
                    protrusion_side=vd.get("protrusion_side"))
            for vd in d["vias"]
        ],
        electrodes1=(
            [_electrode_from_json(ed) for ed in d["electrodes1"]]
            if version >= 3 else [_electrode_from_json(d["electrode1"])]),
        electrodes2=(
            [_electrode_from_json(ed) for ed in d["electrodes2"]]
            if version >= 3 else [_electrode_from_json(d["electrode2"])]),
        # v7: PDN terminals; dumps <= v6 predate them and load classic
        terminals=[_terminal_from_json(td) for td in d.get("terminals", [])],
        thickness_source=d.get("thickness_source", "unknown"),
        buildups=[
            SurfaceBuildup(
                layer_name=bd["layer_name"],
                polygons=[_poly_from_json(pd) for pd in bd["polygons"]])
            for bd in d.get("buildups", [])
        ],
        solder_thickness_nm=int(d.get("solder_thickness_nm", 50_000)),
        solder_rho_ohm_m=float(d.get("solder_rho_ohm_m", 1.32e-7)),
        extra_cu_nm=int(d.get("extra_cu_nm", 0)),
        tracks=[
            TrackSeg(layer_name=td["layer_name"],
                     points=np.asarray(td["points"], dtype=np.int64),
                     width_nm=int(td["width_nm"]))
            for td in d.get("tracks", [])       # <= v4: baked into polygons
        ],
        vias_capped=bool(d.get("vias_capped", True)),
        cap_plating_nm=int(d.get("cap_plating_nm", 15_000)),
        cap_max_drill_nm=int(d.get("cap_max_drill_nm", 500_000)),
        tht_protrusion_nm=int(d.get("tht_protrusion_nm", 1_500_000)),
        tht_lead_clearance_nm=int(d.get("tht_lead_clearance_nm", 250_000)),
        tht_lead_rho_ohm_m=float(d.get("tht_lead_rho_ohm_m", 1.68e-8)),
    )


def save_problem(p: Problem, path: Path) -> None:
    path.write_text(json.dumps(problem_to_json(p)), encoding="utf-8")


def load_problem(path: Path) -> Problem:
    return problem_from_json(json.loads(Path(path).read_text(encoding="utf-8")))
