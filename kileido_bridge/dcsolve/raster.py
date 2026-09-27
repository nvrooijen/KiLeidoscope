# Vendored from Fill Resistance 1.4.2, fill_resistance/raster.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted: _paint_ring rasterizes with KiLeidoscope's numpy scanline filler
# (kileido_bridge.reference.rasterize, pixel-centre and even-odd like the original's
# PIL fill plus matplotlib edge test) instead of PIL and matplotlib; the prints stay.
"""Rasterization of the fill polygons onto a shared multi-layer grid, and
electrode mask construction.

Grid convention: layer l, row i, col j maps to the cell center
    x = x0_nm + (j + 0.5) * h_nm
    y = y0_nm + (i + 0.5) * h_nm
in KiCad board coordinates (y grows down). Row 0 is the minimum-y row,
the TOP of the board as drawn in the editor; plots use origin='upper'.
All layers share the same frame, so cell (i, j) is vertically aligned
across layers (via links connect equal (i, j) on different layers).

Connectivity restriction lives in solver.py: it needs the via edges.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from ..reference import Grid, rasterize
from . import config
from .errors import ElectrodeError, GridSizeError
from .geometry import Electrode, Problem, Rect, slot_distance

# 4-connectivity: matches the in-plane 5-point stencil of the solver
_STRUCT4 = ndimage.generate_binary_structure(2, 1)


@dataclass
class RasterStack:
    masks: np.ndarray         # bool (L, ny, nx), True = copper
    x0_nm: float              # grid origin (outer corner of cell [., 0, 0])
    y0_nm: float
    h_nm: float
    layer_names: list[str]
    buildup: np.ndarray | None = None   # bool (L, ny, nx): solder buildup
                                        # (mask opening ∩ copper)
    chain: np.ndarray | None = None     # bool (L, ny, nx): cells that are
                                        # copper only through a 1D trace chain
    chain_edges: tuple | None = None    # (a, b, g_dc, layer, dl_m) arrays:
                                        # explicit DC conductances and link
                                        # lengths of the chain links
    thick_scale: np.ndarray | None = None   # float (L, ny, nx): per-cell
                                            # copper-thickness factor (via
                                            # mouths: cap-thin or partially
                                            # drilled cells; folded-in cone
                                            # and plug extras); None = all 1
    t_extra_nm: np.ndarray | None = None    # float (L, ny, nx): additive
                                            # conduction-equivalent copper
                                            # (lead cones + hole plugs),
                                            # folded into thick_scale at the
                                            # end of rasterize_stack
    plug: np.ndarray | None = None      # bool (L, ny, nx): solder-filled THT
                                        # hole mouths (lead + solder plug,
                                        # drawn on the raster map)
    mesh: np.ndarray | None = None      # bool (L, ny, nx): adaptive leaf
                                        # boundaries (drawn on the raster map)

    @property
    def nlayers(self) -> int:
        return self.masks.shape[0]

    @property
    def shape2d(self) -> tuple[int, int]:
        return self.masks.shape[1:]

    def cell_centers(self, i0: int, i1: int, j0: int, j1: int):
        xs = self.x0_nm + (np.arange(j0, j1) + 0.5) * self.h_nm
        ys = self.y0_nm + (np.arange(i0, i1) + 0.5) * self.h_nm
        return np.meshgrid(xs, ys)

    def cell_of(self, x_nm: float, y_nm: float) -> tuple[int, int] | None:
        """(i, j) of the cell containing the point, or None if outside."""
        ny, nx = self.shape2d
        j = math.floor((x_nm - self.x0_nm) / self.h_nm)
        i = math.floor((y_nm - self.y0_nm) / self.h_nm)
        if 0 <= i < ny and 0 <= j < nx:
            return i, j
        return None

    def extent_mm(self) -> tuple[float, float, float, float]:
        """imshow extent (left, right, bottom, top) for origin='upper',
        y axis in board orientation (increasing downward)."""
        ny, nx = self.shape2d
        return (
            self.x0_nm * 1e-6,
            (self.x0_nm + nx * self.h_nm) * 1e-6,
            (self.y0_nm + ny * self.h_nm) * 1e-6,
            self.y0_nm * 1e-6,
        )


def choose_cell_size(bbox_nm: tuple[int, int, int, int], nlayers: int) -> float:
    """Pick the cell size h [nm]; TARGET_CELLS counts TOTAL cells across
    all layers. Raise if the grid would exceed HARD_MAX_CELLS."""
    x0, y0, x1, y1 = bbox_nm
    w, ht = float(x1 - x0), float(y1 - y0)
    if w <= 0 or ht <= 0:
        raise GridSizeError("Copper geometry has a degenerate bounding box.")

    if config.CELL_UM_OVERRIDE is not None:
        if config.CELL_UM_OVERRIDE <= 0:
            raise GridSizeError(
                f"Cell size must be positive "
                f"(got {config.CELL_UM_OVERRIDE:g} um)."
            )
        h = config.CELL_UM_OVERRIDE * 1000.0
    else:
        # the adaptive grid decouples unknowns from the fine cell count,
        # so its auto sizing affords a larger fine-cell budget (finer h)
        target = (config.TARGET_CELLS_ADAPTIVE if config.ADAPTIVE_CELLS
                  else config.TARGET_CELLS)
        h = math.sqrt(w * ht * nlayers / target)
        h = min(max(h, config.MIN_CELL_UM * 1000.0), config.MAX_CELL_UM * 1000.0)

    ncells = math.ceil(w / h) * math.ceil(ht / h) * nlayers
    if ncells > config.HARD_MAX_CELLS:
        raise GridSizeError(
            f"Grid would need ~{ncells / 1e6:.1f} M cells over {nlayers} "
            f"layer(s) at cell size {h / 1000:.0f} um (limit "
            f"{config.HARD_MAX_CELLS / 1e6:.0f} M). Raise MAX_CELL_UM / "
            f"CELL_UM_OVERRIDE in config.py, deselect layers, or measure a "
            f"smaller region."
        )
    return h


def _paint_ring(stack: RasterStack, ring: np.ndarray, value: bool,
                target: np.ndarray) -> None:
    """Set target (2D) cells whose center lies inside ring to `value`,
    working only within the ring's bbox. KiLeidoscope's scanline filler
    (reference.rasterize): a pure center-in-polygon pass, as the
    original's PIL fill with its exact matplotlib test along the edge."""
    ny, nx = stack.shape2d
    h = stack.h_nm
    j0 = max(0, int((ring[:, 0].min() - stack.x0_nm) / h) - 1)
    j1 = min(nx, int((ring[:, 0].max() - stack.x0_nm) / h) + 2)
    i0 = max(0, int((ring[:, 1].min() - stack.y0_nm) / h) - 1)
    i1 = min(ny, int((ring[:, 1].max() - stack.y0_nm) / h) + 2)
    if i0 >= i1 or j0 >= j1:
        return
    w, ht = j1 - j0, i1 - i0

    # cell (i, j) center <-> pixel (j - j0, i - i0)
    window = Grid(stack.x0_nm + j0 * h, stack.y0_nm + i0 * h, h, w, ht)
    inside = rasterize(window, [(1, (ring,))]) > 0

    sub = target[i0:i1, j0:j1]
    sub[inside] = value


def rasterize_stack(problem: Problem, h_nm: float) -> RasterStack:
    """Rasterize every included layer onto one shared frame."""
    x0, y0, x1, y1 = problem.copper_bbox()
    m = config.MARGIN_CELLS
    nx = math.ceil((x1 - x0) / h_nm) + 2 * m
    ny = math.ceil((y1 - y0) / h_nm) + 2 * m
    stack = RasterStack(
        masks=np.zeros((len(problem.layers), ny, nx), dtype=bool),
        x0_nm=x0 - m * h_nm,
        y0_nm=y0 - m * h_nm,
        h_nm=h_nm,
        layer_names=problem.layer_names,
    )
    for li, layer in enumerate(problem.layers):
        for poly in layer.polygons:
            if poly.holes:
                pmask = np.zeros((ny, nx), dtype=bool)
                _paint_ring(stack, poly.outline, True, pmask)
                for hole in poly.holes:
                    _paint_ring(stack, hole, False, pmask)
                stack.masks[li] |= pmask
            else:
                # hole-less (e.g. a track outline): paint the layer mask
                # directly, skipping the full-frame temp
                _paint_ring(stack, poly.outline, True, stack.masks[li])

    # via ring/pad copper BEFORE tracks, so 1D chains see it as regular
    # copper; drill mouths AFTER tracks, so drills go through trace copper
    _paint_via_rings(stack, problem)

    # traces: wide ones are rasterized from their outline, sub-resolution
    # ones become exact 1D resistor chains along their centerline
    index = {name: li for li, name in enumerate(stack.layer_names)}
    narrow = []
    for seg in problem.tracks:
        li = index.get(seg.layer_name)
        if li is None:
            continue
        if seg.width_nm >= config.TRACK_1D_FACTOR * h_nm:
            _paint_ring(stack, seg.outline(config.ARC_TOL_FRACTION * h_nm),
                        True, stack.masks[li])
        else:
            narrow.append((li, seg))
    if narrow:
        n_links = _build_chains(stack, problem, narrow)
        print(f"{len(narrow)} trace(s) narrower than "
              f"{config.TRACK_1D_FACTOR:g} cells modeled as 1D resistor "
              f"chains ({n_links} links)")

    _apply_via_mouths(stack, problem)

    if problem.buildups:
        stack.buildup = np.zeros_like(stack.masks)
        index = {name: li for li, name in enumerate(stack.layer_names)}
        for b in problem.buildups:
            li = index.get(b.layer_name)
            if li is None:
                continue
            for poly in b.polygons:
                pmask = np.zeros((ny, nx), dtype=bool)
                _paint_ring(stack, poly.outline, True, pmask)
                for hole in poly.holes:
                    _paint_ring(stack, hole, False, pmask)
                stack.buildup[li] |= pmask
        stack.buildup &= stack.masks        # solder wets exposed copper only

    _paint_lead_fillets(stack, problem)

    if stack.t_extra_nm is not None:
        # cones + plugs are ADDITIVE conduction-equivalent copper; fold
        # them into the multiplicative per-cell scale once (multiplying
        # per contribution would overstate cells carrying both)
        if stack.thick_scale is None:
            stack.thick_scale = np.ones(stack.masks.shape)
        for li, layer in enumerate(problem.layers):
            stack.thick_scale[li] *= np.where(
                stack.masks[li],
                1.0 + stack.t_extra_nm[li] / layer.thickness_nm, 1.0)
    return stack


def _paint_lead_fillets(stack: RasterStack, problem: Problem) -> None:
    """Protruding THT leads (barrel contacts AND the net's populated
    stitching through-hole pads): the clipped lead sticks
    tht_protrusion_nm out of the hole on the side opposite the
    component, wrapped by a solder cone - full protrusion height at
    the drill wall, tapering linearly to zero at the pad edge. Modeled
    as extra conduction-equivalent copper (stack.t_extra_nm, ADDITIVE
    with the hole plug, folded into thick_scale by rasterize_stack): the
    tall solder column next to the wall pulls those cells to lead
    potential (equivalent to extending the barrel wall vertically), the
    taper carries the radial spreading. At f > 0 the factor multiplies
    the skin-corrected sheet conductance, like the via mouths
    (approximation)."""
    H = problem.tht_protrusion_nm
    if H <= 0:
        return
    ny, nx = stack.shape2d
    h = stack.h_nm
    index = {name: li for li, name in enumerate(stack.layer_names)}

    # one cone per joint: contact electrodes first (exact data), then the
    # net's populated stitching THT pads, skipping the contacts' barrels
    jobs = []
    seen = set()
    for e in problem.contact_electrodes():
        if e.drill_nm <= 0:
            continue
        if e.center is not None:
            x, y = e.center
        else:
            x = (e.rect.x0 + e.rect.x1) / 2.0
            y = (e.rect.y0 + e.rect.y1) / 2.0
        seen.add((int(x), int(y)))
        if e.solder and e.protrusion_side:
            # oblong pads: taper from the (slot) wall to the inscribed
            # dimension (conservative)
            jobs.append((x, y, e.drill_nm, e.pad_min_nm or e.pad_nm,
                         e.protrusion_side, e.slot_dx_nm, e.slot_dy_nm))
    for v in problem.vias:
        if v.kind == "pad" and v.solder_filled and v.protrusion_side \
                and (v.x, v.y) not in seen:
            jobs.append((v.x, v.y, v.drill_nm, v.pad_min_nm or v.pad_nm,
                         v.protrusion_side, v.slot_dx_nm, v.slot_dy_nm))

    for x, y, drill_nm, pad_nm, side, sdx, sdy in jobs:
        li = index.get(side)
        if li is None or pad_nm <= drill_nm:
            continue
        ra, rb = drill_nm / 2.0, pad_nm / 2.0
        ex, ey = rb + abs(sdx), rb + abs(sdy)
        j0 = max(0, math.floor((x - ex - stack.x0_nm) / h))
        j1 = min(nx, math.floor((x + ex - stack.x0_nm) / h) + 1)
        i0 = max(0, math.floor((y - ey - stack.y0_nm) / h))
        i1 = min(ny, math.floor((y + ey - stack.y0_nm) / h) + 1)
        if i0 >= i1 or j0 >= j1:
            continue
        xs = stack.x0_nm + (np.arange(j0, j1) + 0.5) * h - x
        ys = stack.y0_nm + (np.arange(i0, i1) + 0.5) * h - y
        r = slot_distance(xs[None, :], ys[:, None], sdx, sdy)
        t_sn = H * np.clip((rb - r) / (rb - ra), 0.0, 1.0)
        t_eq = t_sn * (problem.rho_ohm_m / problem.solder_rho_ohm_m)
        if stack.t_extra_nm is None:
            stack.t_extra_nm = np.zeros(stack.masks.shape)
        m = stack.masks[li, i0:i1, j0:j1]
        stack.t_extra_nm[li, i0:i1, j0:j1] += np.where(m, t_eq, 0.0)


def _via_span(problem: Problem, via) -> list[int]:
    return [li for li, layer in enumerate(problem.layers)
            if via.spans(layer.z_nm)]


def _paint_via_rings(stack: RasterStack, problem: Problem) -> None:
    """Annular-ring / via-pad copper: a full-thickness disc of the pad
    diameter on every layer the barrel spans (kind='via' only - THT pad
    copper stays outside the model). The drill mouth re-opens the disc
    center in _apply_via_mouths."""
    ny, nx = stack.shape2d
    h = stack.h_nm
    for via in problem.vias:
        if via.kind != "via" or via.pad_nm <= 0:
            continue
        r = via.pad_nm / 2.0
        j0 = max(0, math.floor((via.x - r - stack.x0_nm) / h))
        j1 = min(nx, math.floor((via.x + r - stack.x0_nm) / h) + 1)
        i0 = max(0, math.floor((via.y - r - stack.y0_nm) / h))
        i1 = min(ny, math.floor((via.y + r - stack.y0_nm) / h) + 1)
        if i0 >= i1 or j0 >= j1:
            continue
        xs = stack.x0_nm + (np.arange(j0, j1) + 0.5) * h - via.x
        ys = stack.y0_nm + (np.arange(i0, i1) + 0.5) * h - via.y
        disc = (ys[:, None] ** 2 + xs[None, :] ** 2) <= r * r
        for li in _via_span(problem, via):
            stack.masks[li, i0:i1, j0:j1] |= disc


def _apply_via_mouths(stack: RasterStack, problem: Problem) -> None:
    """Drill-mouth treatment, area-weighted per cell (4x4 supersampling):
    capped vias carry a cap_plating-thin copper cap over the mouth on the
    OUTER layers, uncapped vias (and inner layers either way) get an open
    hole. The fab caps only small vias: drills above cap_max_drill_nm
    stay open even with vias_capped. THT pad mouths: populated pads are
    solder-filled - the mouth keeps its copper and additionally carries
    the PLUG (the component lead plus the solder filling the bore) as
    in-plane conduction-equivalent copper of the FULL hole depth on
    EVERY spanned layer (the pin continues beyond both mouths, so each
    layer sees the whole plug cross-section); the joint is then
    side-symmetric except for the solder: the solder-side coat and cone
    come on top, additively (see _paint_lead_fillets).
    DNP pad holes are cut open on every layer. Fully swallowed cells
    leave the mask; partially covered cells keep a thickness-scaled
    sheet conductance via stack.thick_scale."""
    ny, nx = stack.shape2d
    h = stack.h_nm
    outer = {li for li, n in enumerate(stack.layer_names)
             if n in ("F.Cu", "B.Cu")}
    sub = (np.arange(4) + 0.5) / 4.0
    for via in problem.vias:
        if via.drill_nm <= 0:
            continue
        plugged = via.kind == "pad" and via.solder_filled
        r = via.drill_nm / 2.0
        ex, ey = r + abs(via.slot_dx_nm), r + abs(via.slot_dy_nm)
        j0 = max(0, math.floor((via.x - ex - stack.x0_nm) / h))
        j1 = min(nx, math.floor((via.x + ex - stack.x0_nm) / h) + 1)
        i0 = max(0, math.floor((via.y - ey - stack.y0_nm) / h))
        i1 = min(ny, math.floor((via.y + ey - stack.y0_nm) / h) + 1)
        if i0 >= i1 or j0 >= j1:
            continue
        xs = stack.x0_nm + (np.arange(j0, j1)[:, None] + sub[None, :]) * h \
            - via.x
        ys = stack.y0_nm + (np.arange(i0, i1)[:, None] + sub[None, :]) * h \
            - via.y
        cov = (slot_distance(xs[None, :, None, :], ys[:, None, :, None],
                             via.slot_dx_nm, via.slot_dy_nm)
               <= r).mean(axis=(2, 3))
        if not (cov > 0).any():
            continue                            # mouth far smaller than h
        span = _via_span(problem, via)

        if plugged:
            # lead cylinder + solder bore: the pin continues beyond BOTH
            # mouths (component body / clipped stickout), so every
            # spanned layer sees the FULL plug depth for lateral
            # spreading - no per-layer split
            r_lead = max(via.drill_nm - problem.tht_lead_clearance_nm,
                         0) / 2.0
            cov_lead = (np.hypot(xs[None, :, None, :],
                                 ys[:, None, :, None])
                        <= r_lead).mean(axis=(2, 3))
            t_sn = problem.rho_ohm_m / problem.solder_rho_ohm_m
            t_pb = problem.rho_ohm_m / problem.tht_lead_rho_ohm_m
            depth = max(float(via.z_bot_nm - via.z_top_nm), 0.0)
            t_eq = depth * (cov_lead * t_pb + (cov - cov_lead) * t_sn)
            if stack.t_extra_nm is None:
                stack.t_extra_nm = np.zeros(stack.masks.shape)
            if stack.plug is None:
                stack.plug = np.zeros_like(stack.masks)
            for li in span:
                m = stack.masks[li, i0:i1, j0:j1]
                stack.t_extra_nm[li, i0:i1, j0:j1] += np.where(m, t_eq, 0.0)
                stack.plug[li, i0:i1, j0:j1] |= m & (cov > 0.5)
            continue

        if stack.thick_scale is None:
            stack.thick_scale = np.ones(stack.masks.shape)
        for li in span:
            if via.kind == "via" and problem.vias_capped and li in outer \
                    and via.drill_nm <= problem.cap_max_drill_nm:
                ratio = min(problem.cap_plating_nm
                            / problem.layers[li].thickness_nm, 1.0)
            else:
                ratio = 0.0                 # open hole (also DNP THT holes)
            s = 1.0 - cov * (1.0 - ratio)
            gone = s <= 1e-9
            stack.masks[li, i0:i1, j0:j1] &= ~gone
            stack.thick_scale[li, i0:i1, j0:j1] *= np.where(gone, 1.0, s)


def _build_chains(stack: RasterStack, problem: Problem,
                  narrow: list) -> int:
    """Sub-resolution traces as 1D resistor chains: mark the cells their
    centerline crosses as copper and record one explicit conductance per
    pair of consecutive cells, allocating the trace's TRUE arc length to
    each link (a diagonal trace is not staircase-inflated). Links whose
    cells are already regular copper AND face-adjacent are skipped there
    (the trace merges into the pour: union, not sum). Returns the number
    of links."""
    L, ny, nx = stack.masks.shape
    plane = ny * nx
    h = stack.h_nm
    regular = stack.masks.copy()
    chain = np.zeros_like(stack.masks)
    aa, bb, gg, ll, dd = [], [], [], [], []
    for li, seg in narrow:
        pts = seg.centerline(0.2 * h)
        d = np.hypot(*np.diff(pts, axis=0).T)
        s = np.concatenate([[0.0], np.cumsum(d)])
        length = float(s[-1])
        n_samp = max(2, int(math.ceil(length / (h / 3.0))) + 1)
        ss = np.linspace(0.0, length, n_samp)
        xs = np.interp(ss, s, pts[:, 0])
        ys = np.interp(ss, s, pts[:, 1])
        jj = np.floor((xs - stack.x0_nm) / h).astype(np.int64)
        ii = np.floor((ys - stack.y0_nm) / h).astype(np.int64)
        jj = np.clip(jj, 0, nx - 1)             # bbox includes all tracks;
        ii = np.clip(ii, 0, ny - 1)             # clip only guards rounding
        first = np.concatenate(
            [[True], (ii[1:] != ii[:-1]) | (jj[1:] != jj[:-1])])
        ci, cj, cs = ii[first], jj[first], ss[first]
        chain[li, ci, cj] = True
        g0 = (seg.width_nm * 1e-9
              * problem.layers[li].thickness_nm * 1e-9 / problem.rho_ohm_m)
        for k in range(len(ci) - 1):
            dl = (cs[k + 1] - cs[k]) * 1e-9
            if dl <= 0:
                continue
            adj4 = abs(int(ci[k + 1] - ci[k])) + abs(int(cj[k + 1] - cj[k])) == 1
            if adj4 and regular[li, ci[k], cj[k]] \
                    and regular[li, ci[k + 1], cj[k + 1]]:
                continue                        # pour conducts here already
            aa.append(li * plane + int(ci[k]) * nx + int(cj[k]))
            bb.append(li * plane + int(ci[k + 1]) * nx + int(cj[k + 1]))
            gg.append(g0 / dl)
            ll.append(li)
            dd.append(dl)
    stack.chain = chain & ~regular
    stack.masks |= stack.chain
    stack.chain_edges = (np.asarray(aa, dtype=np.int64),
                         np.asarray(bb, dtype=np.int64),
                         np.asarray(gg, dtype=float),
                         np.asarray(ll, dtype=np.int64),
                         np.asarray(dd, dtype=float))
    return len(aa)


def _rect_cells(stack: RasterStack, rect: Rect) -> np.ndarray:
    """Bool (ny, nx) mask of cells whose center lies inside the rectangle."""
    ny, nx = stack.shape2d
    h = stack.h_nm
    out = np.zeros((ny, nx), dtype=bool)
    j0 = max(0, int(math.ceil((rect.x0 - stack.x0_nm) / h - 0.5)))
    j1 = min(nx, int(math.floor((rect.x1 - stack.x0_nm) / h - 0.5)) + 1)
    i0 = max(0, int(math.ceil((rect.y0 - stack.y0_nm) / h - 0.5)))
    i1 = min(ny, int(math.floor((rect.y1 - stack.y0_nm) / h - 0.5)) + 1)
    if i0 < i1 and j0 < j1:
        out[i0:i1, j0:j1] = True
    return out


def _electrode_cells2d(stack: RasterStack, e: Electrode) -> np.ndarray:
    """2D footprint of the electrode shape (pad polygons or rectangle)."""
    if e.polygons:
        cells = np.zeros(stack.shape2d, dtype=bool)
        for poly in e.polygons:
            pm = np.zeros(stack.shape2d, dtype=bool)
            _paint_ring(stack, poly.outline, True, pm)
            for hole in poly.holes:
                _paint_ring(stack, hole, False, pm)
            cells |= pm
        if not cells.any():
            # shape smaller than one grid cell (small pad): use the cell
            # containing its center
            r = e.rect
            c = stack.cell_of((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
            if c is not None:
                cells[c] = True
        return cells
    return _rect_cells(stack, e.rect)


def _barrel_ring2d(stack: RasterStack, e: Electrode,
                   mask2d: np.ndarray) -> np.ndarray:
    """Contact cells of a barrel electrode on one layer: the copper ring
    at the drill wall (cell centers within one cell of radius drill/2;
    slotted holes: within one cell of the stadium-shaped slot wall),
    where the lead/wire soldered into the hole actually meets the layer.
    If rasterization or an antipad leaves no copper there, fall back to
    the nearest copper ring within the pad footprint (+1 cell of slop) -
    the same search bound as the solver's barrel attachment."""
    ny, nx = stack.shape2d
    h = stack.h_nm
    if e.center is not None:
        x, y = e.center
    else:
        x = (e.rect.x0 + e.rect.x1) / 2.0
        y = (e.rect.y0 + e.rect.y1) / 2.0
    r = e.drill_nm / 2.0
    rw = max(e.pad_nm, e.drill_nm + 300_000) / 2.0 + h
    ex, ey = rw + abs(e.slot_dx_nm), rw + abs(e.slot_dy_nm)
    out = np.zeros((ny, nx), dtype=bool)
    j0 = max(0, math.floor((x - ex - stack.x0_nm) / h))
    j1 = min(nx, math.floor((x + ex - stack.x0_nm) / h) + 1)
    i0 = max(0, math.floor((y - ey - stack.y0_nm) / h))
    i1 = min(ny, math.floor((y + ey - stack.y0_nm) / h) + 1)
    if i0 >= i1 or j0 >= j1:
        return out
    xs = stack.x0_nm + (np.arange(j0, j1) + 0.5) * h - x
    ys = stack.y0_nm + (np.arange(i0, i1) + 0.5) * h - y
    d = slot_distance(xs[None, :], ys[:, None], e.slot_dx_nm, e.slot_dy_nm)
    m = mask2d[i0:i1, j0:j1]
    ring = m & (np.abs(d - r) <= h)
    if not ring.any():
        dc = np.where(m & (d <= rw), d, np.inf)
        dmin = dc.min()
        if np.isfinite(dmin):
            ring = dc <= dmin + h              # e.g. thermal-spoke tips
    out[i0:i1, j0:j1] = ring
    return out


def _part_mask3d(stack: RasterStack, problem: Problem,
                 el: Electrode) -> np.ndarray:
    """(L, ny, nx) contact cells of one electrode part: the barrel-wall
    ring on every spanned layer for via/THT-pad contacts, else the
    part's shape ∩ copper on its contact layer(s)."""
    part = np.zeros_like(stack.masks)
    if el.drill_nm > 0:
        for li, name in enumerate(stack.layer_names):
            if el.contact not in ("all", name):
                continue
            if el.barrel_z is not None:
                z = problem.layers[li].z_nm
                if not (el.barrel_z[0] - 1 <= z <= el.barrel_z[1] + 1):
                    continue
            part[li] = _barrel_ring2d(stack, el, stack.masks[li])
        return part
    cells2d = _electrode_cells2d(stack, el)
    for li, name in enumerate(stack.layer_names):
        if el.contact in ("all", name):
            part[li] = cells2d & stack.masks[li]
    return part


def electrode_masks(stack: RasterStack, problem: Problem
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Terminal mask = OR over its parts; part = shape ∩ copper on the
    part's contact layer(s), or the barrel-wall ring for via/THT-pad
    contacts (current enters through the soldered barrel, not the pad
    face). contact 'all' = every included layer (bolted lug / through
    pad); a layer name = that layer only. Every part must individually
    land on copper (clear feedback). V+/V- must not overlap; touching is
    checked later, only for the equipotential contact model."""
    def build(parts: list[Electrode], which: str) -> np.ndarray:
        e = np.zeros_like(stack.masks)
        for el in parts:
            part = _part_mask3d(stack, problem, el)
            if not part.any():
                where = ("near its barrel (drill-wall ring / pad footprint)"
                         if el.drill_nm > 0 else
                         "(or is smaller than one grid cell)")
                raise ElectrodeError(
                    f"A {which} contact part ({el.label}) does not overlap "
                    f"any copper of the selected fill on contact layer(s) "
                    f"'{el.contact}' {where}."
                )
            e |= part
        if not e.any():
            raise ElectrodeError(f"The {which} terminal has no contact parts.")
        return e

    e1 = build(problem.electrodes1, "V+")
    e2 = build(problem.electrodes2, "V-")

    if (e1 & e2).any():
        raise ElectrodeError(
            "The V+ and V- contact areas overlap on the copper grid. "
            "Move them apart."
        )
    return e1, e2


def terminal_masks(stack: RasterStack, problem: Problem) -> list:
    """PDN mode: one (L, ny, nx) contact mask per problem.terminals
    entry, same order. Same part semantics as electrode_masks (every
    part must land on copper); additionally NO two terminals may share
    a cell - each cell's injection/attachment must belong to exactly one
    terminal or the currents would be ill-defined."""
    out = []
    for t in problem.terminals:
        m = np.zeros_like(stack.masks)
        for el in t.electrodes:
            part = _part_mask3d(stack, problem, el)
            if not part.any():
                where = ("near its barrel (drill-wall ring / pad footprint)"
                         if el.drill_nm > 0 else
                         "(or is smaller than one grid cell)")
                raise ElectrodeError(
                    f"{t.role} '{t.label}': contact part ({el.label}) does "
                    f"not overlap any copper of the selected fill on "
                    f"contact layer(s) '{el.contact}' {where}."
                )
            m |= part
        out.append(m)
    for i, ti in enumerate(problem.terminals):
        for j in range(i + 1, len(problem.terminals)):
            if (out[i] & out[j]).any():
                tj = problem.terminals[j]
                raise ElectrodeError(
                    f"The contact areas of {ti.role} '{ti.label}' and "
                    f"{tj.role} '{tj.label}' overlap on the copper grid. "
                    f"Move them apart."
                )
    return out


def terminal_partition(stack: RasterStack, problem: Problem) -> list:
    """PDN mode: per-part cell masks for each terminal, as a list (one
    entry per terminal) of [(label, mask3d), ...]. Within one terminal
    overlapping parts keep the first-wins attribution of
    electrode_partition, so part currents sum to the terminal current."""
    out = []
    for t in problem.terminals:
        parts = []
        claimed = np.zeros_like(stack.masks)
        for el in t.electrodes:
            m = _part_mask3d(stack, problem, el)
            m &= ~claimed
            claimed |= m
            parts.append((el.label, m))
        out.append(parts)
    return out


def electrode_partition(stack: RasterStack, problem: Problem
                        ) -> tuple[list, list]:
    """Per-part cell masks for both terminals, as [(label, mask3d), ...].
    Cells covered by several overlapping parts are attributed to the
    FIRST part (first-wins partition), so part currents sum exactly to
    the terminal current."""
    def build(parts: list[Electrode]) -> list:
        out = []
        claimed = np.zeros_like(stack.masks)
        for el in parts:
            m = _part_mask3d(stack, problem, el)
            m &= ~claimed
            claimed |= m
            out.append((el.label, m))
        return out

    return build(problem.electrodes1), build(problem.electrodes2)


def electrodes_touch(stack: RasterStack, e1: np.ndarray,
                     e2: np.ndarray) -> str | None:
    """Layer name where the terminals are 4-adjacent, or None."""
    for li in range(stack.nlayers):
        if (ndimage.binary_dilation(e1[li], structure=_STRUCT4) & e2[li]).any():
            return stack.layer_names[li]
    return None
