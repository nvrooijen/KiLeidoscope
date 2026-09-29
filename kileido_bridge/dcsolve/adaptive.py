# Vendored from Fill Resistance 1.4.2, fill_resistance/adaptive.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Unchanged apart from this header.
"""Adaptive-grid solve path with deferred-correction interface fluxes.

Maps the fully rasterized problem onto per-layer balanced quadtree leaf
graphs (quadtree.py), solves with the production assembly/AMG, and
expands every field back to the fine grid, so plots, reports and dumps
are unchanged.

Enabled via config.ADAPTIVE_CELLS (dialog checkbox "adaptive cells").
Every fine cell that carries anything non-uniform - electrodes, 1D
trace-chain cells, solder buildup, via-mouth thickness scaling - is
pinned at the fine size (keep_fine), so all coarser leaves have the
plain layer conductance and the leaf system reduces EXACTLY to the
production system wherever the grid is fine. The minimum element size
is the grid cell size itself; ADAPTIVE_MAX_CELL_UM caps the coarsest
leaf.

ACCURACY: raw two-point fluxes across coarse-fine faces miss the
tangential potential gradient (different-size neighbors have laterally
offset centers), biasing R low by ~0.5-2%. Deferred correction fixes
this: after the first solve, per-leaf gradients are reconstructed by
least squares over face neighbors and the known tangential term
g * delta * Gt moves to the right-hand side of a re-solve
(ADAPTIVE_CORRECTION_PASSES, default 1). The matrix is unchanged, so
the LU factorization / AMG hierarchy is reused, and the corrected
currents satisfy KCL exactly (the power-balance identity holds).
Measured residual bias after one pass: < 0.03% on both the narrow-strip
worst case and feature-dense plates.
"""
from __future__ import annotations

import time

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph

from . import config, progress, quadtree, skin
from . import solver as sv
from .errors import ConnectivityError
from .geometry import Problem
from .raster import RasterStack


def _max_block(h_nm: float) -> int:
    mb = 1
    while mb * 2 * h_nm <= config.ADAPTIVE_MAX_CELL_UM * 1000.0:
        mb *= 2
    return mb


def _nodes_of_cells(grids, offs, li: int, cells2d: np.ndarray) -> np.ndarray:
    """Node ids of the (copper) fine cells selected by a 2D bool mask."""
    ids = grids[li].id_grid[cells2d]
    ids = ids[ids >= 0].astype(np.int64)
    return offs[li] + np.unique(ids)


def _leaf_gradients(N: int, a: np.ndarray, b: np.ndarray, cx: np.ndarray,
                    cy: np.ndarray, V: np.ndarray):
    """Per-node least-squares gradient from face-neighbor differences
    (both endpoints accumulate the same symmetric products)."""
    dx = cx[b] - cx[a]
    dy = cy[b] - cy[a]
    dv = V[b] - V[a]
    Sxx = np.zeros(N)
    Sxy = np.zeros(N)
    Syy = np.zeros(N)
    Sxv = np.zeros(N)
    Syv = np.zeros(N)
    for acc, val in ((Sxx, dx * dx), (Sxy, dx * dy), (Syy, dy * dy),
                     (Sxv, dx * dv), (Syv, dy * dv)):
        np.add.at(acc, a, val)
        np.add.at(acc, b, val)
    det = Sxx * Syy - Sxy ** 2
    ok = det > 1e-12
    safe = np.where(ok, det, 1.0)
    gx = np.where(ok, (Syy * Sxv - Sxy * Syv) / safe, 0.0)
    gy = np.where(ok, (Sxx * Syv - Sxy * Sxv) / safe, 0.0)
    return gx, gy


def _leaf_graph(problem: Problem, stack: RasterStack, sigmas: list,
                via_factor: float, sigma_buildup: float,
                keep_extra: np.ndarray):
    """Per-layer quadtree leaf graphs + their edge set, shared by the
    classic and PDN adaptive solves (pure code motion out of
    run_solve_adaptive). keep_extra: feature cells the caller pins at
    the fine size (classic: e1|e2; PDN: the OR of every terminal's
    contact mask, so contact nodes stay 1:1 with cells and per-node
    injection equals per-cell); chain / buildup / thickness-scaled /
    barrel-attachment cells are pinned here on top. Returns (grids,
    offs, N, edges, e_delta, e_axis, e_layer, cxg, cyg, teq_leaves);
    the dead-barrel count travels in edges.dead_barrels."""
    L, ny, nx = stack.masks.shape
    plane = ny * nx
    links, dead_barrels = sv._barrel_links(stack, problem)
    keep = keep_extra.copy()
    if stack.chain is not None:
        keep |= stack.chain
    if stack.buildup is not None:
        keep |= stack.buildup
    if stack.thick_scale is not None:
        keep |= stack.thick_scale != 1.0
    # pin every barrel attachment cell fine: a point-like barrel
    # injection into a coarse leaf makes the whole leaf equipotential
    # and deletes the local spreading resistance (via fields read up
    # to ~13% low otherwise); the guard ring then grades around it
    for _vi, la, ia_, ja_, lb, ib_, jb_, _r in links:
        keep[la, ia_, ja_] = True
        keep[lb, ib_, jb_] = True
    mb = _max_block(stack.h_nm)
    grids = [quadtree.build_leaves(stack.masks[li], keep_fine=keep[li],
                                   max_block=mb,
                                   guard=config.ADAPTIVE_GUARD)
             for li in range(L)]
    offs = np.zeros(L + 1, dtype=np.int64)
    for li in range(L):
        offs[li + 1] = offs[li] + grids[li].n
    N = int(offs[-1])
    n_cells = int(stack.masks.sum())
    print(f"adaptive grid: {N} leaves for {n_cells} copper cells "
          f"({n_cells / max(N, 1):.1f}x, max leaf "
          f"{max(int(g.size.max()) if g.n else 1 for g in grids)} cells)")

    # --- edges: in-plane faces, 1D chain links, barrels -------------------
    # aligned per-edge geometry: tangential center offset (fine units),
    # face axis (0/1, -1 = chain or barrel), layer (-1 = barrel/chain)
    aa, bb, ww, vv, dd, xx, ee = [], [], [], [], [], [], []
    sig_leaves, teq_leaves = [], []
    cxg = np.zeros(N)
    cyg = np.zeros(N)
    for li in range(L):
        g_ = grids[li]
        size = g_.size.astype(float)
        cxl = g_.x0 + size / 2.0
        cyl = g_.y0 + size / 2.0
        cxg[offs[li]:offs[li + 1]] = cxl
        cyg[offs[li]:offs[li + 1]] = cyl
        sig_leaf = np.full(g_.n, sigmas[li])
        s2d = sv._sigma_2d(stack, li, sigmas[li], sigma_buildup)
        fine = g_.size == 1
        if s2d is not None and fine.any():
            sig_leaf[fine] = s2d[g_.y0[fine], g_.x0[fine]]
        # J reference thickness: conduction-equivalent copper (= geometric
        # t at DC, skin-reduced at AC), same convention as the uniform grid
        teq_leaves.append(sig_leaf * problem.rho_ohm_m)
        sig_leaves.append(sig_leaf)
        chainleaf = np.zeros(g_.n, dtype=bool)
        if stack.chain is not None and fine.any():
            chainleaf[fine] = stack.chain[li][g_.y0[fine], g_.x0[fine]]
        ia, ib, wl, ax = quadtree.leaf_faces(g_)
        ok = ~(chainleaf[ia] | chainleaf[ib])
        ia, ib, wl, ax = ia[ok], ib[ok], wl[ok], ax[ok]
        gcond = wl / (g_.size[ia] / (2.0 * sig_leaf[ia])
                      + g_.size[ib] / (2.0 * sig_leaf[ib]))
        aa.append(offs[li] + ia)
        bb.append(offs[li] + ib)
        ww.append(gcond)
        vv.append(np.full(len(ia), -1, dtype=np.int32))
        dd.append(np.where(ax == 0, cyl[ib] - cyl[ia], cxl[ib] - cxl[ia]))
        xx.append(ax.astype(np.int8))
        ee.append(np.full(len(ia), li, dtype=np.int16))

    if stack.chain_edges is not None and len(stack.chain_edges[0]):
        ca, cb, cg, cl, _ = stack.chain_edges
        alive = stack.masks.ravel()[ca] & stack.masks.ravel()[cb]
        if alive.any():
            na = np.empty(len(ca), dtype=np.int64)
            nb = np.empty(len(ca), dtype=np.int64)
            for flat, out in ((ca, na), (cb, nb)):
                li_ = flat // plane
                rem = flat - li_ * plane
                for l in range(L):
                    m = li_ == l
                    if m.any():
                        ids = grids[l].id_grid[rem[m] // nx, rem[m] % nx]
                        out[m] = np.where(ids >= 0, offs[l] + ids, -1)
            alive &= (na >= 0) & (nb >= 0)
            fac = np.array([sigmas[l] * problem.rho_ohm_m
                            / (problem.layers[l].thickness_nm * 1e-9)
                            for l in range(L)])
            k = int(alive.sum())
            aa.append(na[alive])
            bb.append(nb[alive])
            ww.append((cg * fac[cl])[alive])
            vv.append(np.full(k, -1, dtype=np.int32))
            dd.append(np.zeros(k))
            xx.append(np.full(k, -1, dtype=np.int8))
            ee.append(np.full(k, -1, dtype=np.int16))

    for vi, la, ia_, ja_, lb, ib_, jb_, r_dc in links:
        na = offs[la] + grids[la].id_grid[ia_, ja_]
        nb = offs[lb] + grids[lb].id_grid[ib_, jb_]
        aa.append(np.array([na], dtype=np.int64))
        bb.append(np.array([nb], dtype=np.int64))
        ww.append(np.array([1.0 / (r_dc * via_factor)]))
        vv.append(np.array([vi], dtype=np.int32))
        dd.append(np.zeros(1))
        xx.append(np.full(1, -1, dtype=np.int8))
        ee.append(np.full(1, -1, dtype=np.int16))
    if dead_barrels:
        print(f"warning: {dead_barrels} via/pad barrel(s) found fill "
              f"copper on fewer than 2 layers and carry no current (pad "
              f"copper is not modeled; a finer grid may pick up thermal "
              f"spokes)")

    if not aa:
        raise ConnectivityError("No copper found on the selected layers.")
    edges = sv.Edges(a=np.concatenate(aa), b=np.concatenate(bb),
                     w=np.concatenate(ww), via_index=np.concatenate(vv),
                     dead_barrels=dead_barrels)
    e_delta = np.concatenate(dd)
    e_axis = np.concatenate(xx)
    e_layer = np.concatenate(ee)
    return (grids, offs, N, edges, e_delta, e_axis, e_layer, cxg, cyg,
            teq_leaves)


def _expand_fields(problem: Problem, stack: RasterStack, grids: list,
                   offs: np.ndarray, N: int, edges: sv.Edges,
                   e_axis: np.ndarray, e_layer: np.ndarray,
                   cxg: np.ndarray, cyg: np.ndarray, teq_leaves: list,
                   Vflat: np.ndarray, Ie: np.ndarray, s: float):
    """Leaf-space powers, via reports, mesh overlay and the fine-grid
    V/J/P expansion - shared by the classic and PDN adaptive solves
    (pure code motion out of run_solve_adaptive). PDN appends virtual
    supply nodes after N and tags its attachment edges PDN_EDGE: the
    in-plane selection (via_index == -1) and the via selection (>= 0)
    keep them out of every copper field and report here. Returns
    (Pe, P_layers, P_vias, via_reports, V3, J3, Parea)."""
    L, ny, nx = stack.masks.shape
    h_m = stack.h_nm * 1e-9

    # edge power = dV * I_corrected: sums exactly to I^2 R (KCL identity);
    # individual transition faces can go slightly negative
    Pe = (Vflat[edges.a] - Vflat[edges.b]) * Ie * s * s
    inplane = edges.via_index == -1
    Pnode = np.zeros(N)
    np.add.at(Pnode, edges.a[inplane], 0.5 * Pe[inplane])
    np.add.at(Pnode, edges.b[inplane], 0.5 * Pe[inplane])
    P_layers = [float(Pnode[offs[li]:offs[li + 1]].sum()) for li in range(L)]
    P_vias = float(Pe[edges.via_index >= 0].sum())

    via_reports = []
    if problem.vias:
        vidx = edges.via_index
        for vi in np.unique(vidx[vidx >= 0]):
            sel = vidx == vi
            via = problem.vias[vi]
            via_reports.append(sv.ViaReport(
                x_mm=via.x * 1e-6, y_mm=via.y * 1e-6, kind=via.kind,
                drill_mm=via.drill_nm * 1e-6,
                current_a=float(np.abs(Ie[sel]).max()) * s,
                power_w=float(Pe[sel].sum()),
            ))
        via_reports.sort(key=lambda v: v.current_a, reverse=True)

    # leaf boundaries for the raster map: draw the coarse mesh structure
    # (fine regions stay plain copper = fully resolved)
    stack.mesh = np.zeros_like(stack.masks)
    for li in range(L):
        if grids[li].n == 0:
            continue
        ids = grids[li].id_grid
        b = np.zeros_like(stack.masks[li])
        b[:, 1:] |= ids[:, 1:] != ids[:, :-1]
        b[1:, :] |= ids[1:, :] != ids[:-1, :]
        coarse = grids[li].size[np.maximum(ids, 0)] >= 2
        stack.mesh[li] = b & coarse & stack.masks[li]

    # piecewise-LINEAR potential expansion from the leaf gradients of the
    # final solution: constant-per-leaf expansion shows leaf-sized
    # staircase corners in the equipotential contours on coarse interiors
    faces = e_axis >= 0
    fa, fb = edges.a[faces], edges.b[faces]
    if faces.any():
        dgx, dgy = _leaf_gradients(N, fa, fb, cxg, cyg, Vflat)
    else:
        dgx = dgy = np.zeros(N)

    V3 = np.full((L, ny, nx), np.nan)
    J3 = np.full((L, ny, nx), np.nan)
    Parea = np.full((L, ny, nx), np.nan)
    for li in range(L):
        g_ = grids[li]
        ids = g_.id_grid
        m = stack.masks[li]
        ii, jj = np.nonzero(m)
        gid = offs[li] + ids[ii, jj]
        V3[li][ii, jj] = (Vflat[gid]
                          + dgx[gid] * (jj + 0.5 - cxg[gid])
                          + dgy[gid] * (ii + 0.5 - cyg[gid])) * s

        sel = (e_axis >= 0) & (e_layer == li)
        la = (edges.a[sel] - offs[li]).astype(np.int64)
        lb = (edges.b[sel] - offs[li]).astype(np.int64)
        If = Ie[sel]
        axl = e_axis[sel]
        Ixn = np.zeros(g_.n)
        Iyn = np.zeros(g_.n)
        for axis, acc in ((0, Ixn), (1, Iyn)):
            sub = axl == axis
            np.add.at(acc, la[sub], If[sub])
            np.add.at(acc, lb[sub], If[sub])
        span_m = g_.size.astype(float) * h_m
        with np.errstate(invalid="ignore", divide="ignore"):
            Jl = np.hypot(0.5 * Ixn, 0.5 * Iyn) / (span_m * teq_leaves[li])
        J3[li][m] = Jl[ids[m]] * s

        cellP = Pnode[offs[li]:offs[li + 1]] \
            / (g_.size.astype(float) ** 2 * h_m * h_m)
        Parea[li][m] = np.maximum(cellP, 0.0)[ids[m]]
    # chain cells accumulate no leaf-face currents (their links carry
    # axis -1): overlay the true 1D link density
    sv.overlay_chain_density(stack, problem.rho_ohm_m, V3, J3)
    return Pe, P_layers, P_vias, via_reports, V3, J3, Parea


def run_solve_adaptive(problem: Problem, stack: RasterStack,
                       e1: np.ndarray, e2: np.ndarray, i_test: float,
                       freq_hz: float, contact_model: str,
                       parts1: list | None,
                       parts2: list | None) -> sv.Result:
    timings = {}
    L, ny, nx = stack.masks.shape
    h_m = stack.h_nm * 1e-9

    sigmas, rs_ratios, via_factor, sigma_buildup = \
        sv._conductance_params(problem, stack, freq_hz)

    # --- leaves per layer -------------------------------------------------
    t0 = time.perf_counter()
    (grids, offs, N, edges, e_delta, e_axis, e_layer, cxg, cyg,
     teq_leaves) = _leaf_graph(problem, stack, sigmas, via_factor,
                               sigma_buildup, e1 | e2)
    dead_barrels = edges.dead_barrels

    # --- connectivity restriction on the leaf graph -----------------------
    graph = sparse.coo_matrix(
        (np.ones(len(edges.a)), (edges.a, edges.b)), shape=(N, N))
    _, labels = csgraph.connected_components(graph, directed=False)
    e1n = np.zeros(N, dtype=bool)
    e2n = np.zeros(N, dtype=bool)
    for li in range(L):
        e1n[_nodes_of_cells(grids, offs, li, e1[li])] = True
        e2n[_nodes_of_cells(grids, offs, li, e2[li])] = True
    common = np.intersect1d(np.unique(labels[e1n]), np.unique(labels[e2n]))
    if len(common) == 0:
        raise ConnectivityError(
            "The two terminals are not connected by the selected fill "
            "layers (not even through vias). Check the layer selection and "
            "that the fills are up to date."
        )
    if len(common) > 1 and contact_model != "equipotential":
        raise ConnectivityError(
            f"The selected fills form {len(common)} disconnected copper "
            f"groups that each touch both terminals. The uniform-injection "
            f"contact model cannot determine the current split between "
            f"disconnected sheets - switch to the equipotential contact "
            f"model (bonded lug), or include the layers/vias that join "
            f"them."
        )
    keepn = np.isin(labels, common)
    if not keepn.all():
        sel = keepn[edges.a] & keepn[edges.b]
        edges = sv.Edges(a=edges.a[sel], b=edges.b[sel], w=edges.w[sel],
                         via_index=edges.via_index[sel],
                         dead_barrels=dead_barrels)
        e_delta, e_axis, e_layer = e_delta[sel], e_axis[sel], e_layer[sel]
        for li in range(L):
            if grids[li].n == 0:
                continue
            ids = grids[li].id_grid
            kept_cells = (ids >= 0) & keepn[offs[li] + np.maximum(ids, 0)]
            stack.masks[li] &= kept_cells
            e1[li] &= kept_cells
            e2[li] &= kept_cells
        if stack.buildup is not None:
            stack.buildup &= stack.masks
        if stack.chain is not None:
            stack.chain &= stack.masks
        e1n &= keepn
        e2n &= keepn
    for label, m in (parts1 or []) + (parts2 or []):
        had = bool(m.any())
        m &= stack.masks
        if had and not m.any():
            print(f"warning: contact part '{label}' only touches copper "
                  f"that is not connected to both terminals - it carries "
                  f"no current")
    timings["edges_s"] = time.perf_counter() - t0

    # --- solve with deferred-correction interface fluxes -------------------
    t0 = time.perf_counter()
    state = np.zeros(N, dtype=np.uint8)
    state[keepn] = 1
    inj = None
    if contact_model == "equipotential":
        state[e1n] = 2
        state[e2n] = 3
    else:
        n1, n2 = int(e1n.sum()), int(e2n.sum())
        inj = np.zeros(N)
        inj[e1n] = 1.0 / n1
        inj[e2n] = -1.0 / n2
        state[int(np.flatnonzero(e2n)[0])] = 3

    A, rhs0, _ = sv._assemble(state, edges, inj)
    ps = sv.PreparedSolver(A)
    free = state == 1

    def expand(x):
        V = np.zeros(N)
        V[state == 2] = 1.0
        V[free] = x
        return V

    x, info = ps.solve(rhs0)
    Vflat = expand(x)
    rhs_last = rhs0
    corr = np.zeros(len(edges.a))
    faces = e_axis >= 0
    fa, fb = edges.a[faces], edges.b[faces]
    passes = max(0, int(config.ADAPTIVE_CORRECTION_PASSES))
    for p in range(passes):
        if not faces.any():
            break
        progress.stage(f"correction pass {p + 1}/{passes} ...")
        gx, gy = _leaf_gradients(N, fa, fb, cxg, cyg, Vflat)
        gt = np.where(e_axis[faces] == 0, 0.5 * (gy[fa] + gy[fb]),
                      0.5 * (gx[fa] + gx[fb]))
        corr = np.zeros(len(edges.a))
        corr[faces] = edges.w[faces] * e_delta[faces] * gt
        extra = np.zeros(N)
        np.add.at(extra, edges.a, -corr)
        np.add.at(extra, edges.b, corr)
        rhs_last = rhs0 + extra[free]
        x, info = ps.solve(rhs_last)
        Vflat = expand(x)

    # corrected currents at unit drive: satisfy KCL exactly
    Ie = edges.w * (Vflat[edges.a] - Vflat[edges.b]) + corr
    if contact_model == "equipotential":
        sa, sb = state[edges.a], state[edges.b]
        I1 = float(Ie[sa == 2].sum() - Ie[sb == 2].sum())
        I2 = float(Ie[sb == 3].sum() - Ie[sa == 3].sum())
        mismatch = abs(I1 - I2) / max(abs(I1), abs(I2), 1e-300)
        R = 1.0 / (0.5 * (I1 + I2))
        volts_per_amp = R
    else:
        v_plus = float(Vflat[e1n].mean())
        v_minus = float(Vflat[e2n].mean())
        R = v_plus - v_minus
        Vflat = Vflat - v_minus
        I1 = I2 = 1.0
        volts_per_amp = 1.0
        mismatch = info.residual
        if mismatch is None:
            mismatch = float(np.linalg.norm(A @ x - rhs_last)
                             / max(np.linalg.norm(rhs_last), 1e-300))
    timings["solve_s"] = time.perf_counter() - t0

    # --- fields on leaves, expanded to the fine grid ------------------------
    t0 = time.perf_counter()
    s = i_test * volts_per_amp

    Pe, P_layers, P_vias, via_reports, V3, J3, Parea = _expand_fields(
        problem, stack, grids, offs, N, edges, e_axis, e_layer, cxg, cyg,
        teq_leaves, Vflat, Ie, s)
    P_total = i_test ** 2 * R
    balance = abs((sum(P_layers) + P_vias) - P_total) / max(P_total, 1e-300)
    if not np.isfinite(balance) or balance > 1e-3:
        raise sv.SolverError(
            f"Inconsistent solve: R = {R:.6g} ohm with power-balance error "
            f"{balance:.2e} (sum of edge powers vs I^2*R). The result is "
            f"not trustworthy - try the equipotential contact model or a "
            f"different grid size."
        )

    def part_currents(parts, e_nodes, n_total_cells):
        out = []
        for label, mask3 in (parts or []):
            n_part = int(mask3.sum())
            if contact_model == "uniform":
                amps = i_test * n_part / max(n_total_cells, 1)
            else:
                pf = np.zeros(N, dtype=bool)
                for li in range(L):
                    pf[_nodes_of_cells(grids, offs, li, mask3[li])] = True
                pf &= e_nodes
                amps = abs(float(Ie[pf[edges.a]].sum()
                                 - Ie[pf[edges.b]].sum())) * s
            out.append((label, amps))
        return out

    part_currents1 = part_currents(parts1, e1n, int(e1.sum()))
    part_currents2 = part_currents(parts2, e2n, int(e2.sum()))
    timings["postprocess_s"] = time.perf_counter() - t0

    return sv.Result(
        R_ohm=R, i_test=i_test, V=V3, Jmag=J3, Parea=Parea,
        layer_names=list(stack.layer_names),
        P_total=P_total, P_layers=P_layers, P_vias=P_vias,
        power_balance_rel=balance, via_reports=via_reports,
        I1_a=I1, I2_a=I2, mismatch_rel=mismatch,
        n_free=info.n_unknowns, solve_info=info,
        part_currents1=part_currents1, part_currents2=part_currents2,
        contact_model=contact_model,
        freq_hz=freq_hz,
        skin_depth_um=(skin.skin_depth_m(freq_hz, problem.rho_ohm_m) * 1e6
                       if freq_hz > 0 else None),
        rs_ratios=rs_ratios,
        timings=timings,
    )


def run_solve_adaptive_pdn(problem: Problem, stack: RasterStack,
                           term_masks: list, term_parts: list,
                           freq_hz: float, v_nominal: float) -> sv.Result:
    """PDN solve on the leaf graph (dispatched from solver.run_solve_pdn,
    which already labeled and validated the terminals). Same electrical
    model as the uniform-grid path: Thevenin supplies as virtual
    Dirichlet nodes appended after the leaf id space, loads as uniform
    per-cell injection. Contact cells are pinned fine by _leaf_graph, so
    leaf nodes and contact cells are 1:1 and the per-node quantities
    match the uniform grid exactly there. The deferred-correction loop
    is unchanged: supply attachment edges carry e_axis = -1 / e_delta =
    0, so they are excluded from the gradient reconstruction and get
    zero correction (their currents stay exactly w * dV)."""
    timings = {}
    L, ny, nx = stack.masks.shape
    terminals = problem.terminals

    sigmas, rs_ratios, via_factor, sigma_buildup = \
        sv._conductance_params(problem, stack, freq_hz)

    # --- leaves per layer -------------------------------------------------
    t0 = time.perf_counter()
    keep_extra = np.zeros_like(stack.masks)
    for m in term_masks:
        keep_extra |= m
    (grids, offs, N, edges, e_delta, e_axis, e_layer, cxg, cyg,
     teq_leaves) = _leaf_graph(problem, stack, sigmas, via_factor,
                               sigma_buildup, keep_extra)
    dead_barrels = edges.dead_barrels

    # --- connectivity restriction on the leaf graph (PDN keep rule) -------
    graph = sparse.coo_matrix(
        (np.ones(len(edges.a)), (edges.a, edges.b)), shape=(N, N))
    _, labels = csgraph.connected_components(graph, directed=False)
    term_nodes_all = []
    for m in term_masks:
        tn = np.zeros(N, dtype=bool)
        for li in range(L):
            tn[_nodes_of_cells(grids, offs, li, m[li])] = True
        term_nodes_all.append(tn)
    per_term = [set(np.unique(labels[tn]).tolist()) if tn.any() else set()
                for tn in term_nodes_all]
    kept = sv._pdn_keep_components(terminals, per_term)
    keepn = np.isin(labels, sorted(kept))
    if not keepn.all():
        sel = keepn[edges.a] & keepn[edges.b]
        edges = sv.Edges(a=edges.a[sel], b=edges.b[sel], w=edges.w[sel],
                         via_index=edges.via_index[sel],
                         dead_barrels=dead_barrels)
        e_delta, e_axis, e_layer = e_delta[sel], e_axis[sel], e_layer[sel]
        for li in range(L):
            if grids[li].n == 0:
                continue
            ids = grids[li].id_grid
            kept_cells = (ids >= 0) & keepn[offs[li] + np.maximum(ids, 0)]
            stack.masks[li] &= kept_cells
            for m in term_masks:
                m[li] &= kept_cells
        if stack.buildup is not None:
            stack.buildup &= stack.masks
        if stack.chain is not None:
            stack.chain &= stack.masks
        for tn in term_nodes_all:
            tn &= keepn
        for t, m in zip(terminals, term_masks):
            if t.role == "supply" and not m.any():
                print(f"warning: supply '{t.label}' only touches copper "
                      f"not connected to any load - it delivers 0 A")
    for t, parts in zip(terminals, term_parts):
        for label, m in parts:
            had = bool(m.any())
            m &= stack.masks
            if had and not m.any():
                print(f"warning: contact part '{label}' of {t.role} "
                      f"'{t.label}' only touches disconnected copper - "
                      f"it carries no current")
    timings["edges_s"] = time.perf_counter() - t0

    # --- solve with deferred-correction interface fluxes -------------------
    t0 = time.perf_counter()
    state = np.zeros(N, dtype=np.uint8)
    state[keepn] = 1
    term_nodes = [np.flatnonzero(tn) for tn in term_nodes_all]
    state_base = state.copy()       # copper-only state for _pdn_pairs
    state, dirichlet_v, inj, edges_ext, attaches, merge = sv._pdn_attach(
        terminals, term_nodes, state, edges, v_nominal)
    n_pdn = len(edges_ext.a) - len(edges.a)
    e_delta = np.concatenate([e_delta, np.zeros(n_pdn)])
    e_axis = np.concatenate([e_axis, np.full(n_pdn, -1, dtype=np.int8)])
    e_layer = np.concatenate([e_layer, np.full(n_pdn, -1, dtype=np.int16)])
    # bonded terminals: solve on the merge-relabeled edges; contact
    # cells are pinned fine, so every face touching a member is a
    # fine-fine face with zero tangential offset - the deferred
    # correction never fires there and the lug's mixed-position
    # gradient can do no harm (it only ever multiplies delta = 0)
    edges_solve = sv._pdn_solve_edges(edges_ext, merge)

    A, rhs0, _ = sv._assemble(state, edges_solve, inj, dirichlet_v)
    ps = sv.PreparedSolver(A)
    free = state == 1

    def expand(x):
        V = np.where(state >= 2, dirichlet_v, 0.0)
        V[free] = x
        if merge is not None:
            V = V[merge]            # bonded members read their lug
        return V

    x, info = ps.solve(rhs0)
    Vflat = expand(x)
    corr = np.zeros(len(edges_ext.a))
    faces = e_axis >= 0
    fa, fb = edges_ext.a[faces], edges_ext.b[faces]
    passes = max(0, int(config.ADAPTIVE_CORRECTION_PASSES))
    for p in range(passes):
        if not faces.any():
            break
        progress.stage(f"correction pass {p + 1}/{passes} ...")
        gx, gy = _leaf_gradients(N, fa, fb, cxg, cyg, Vflat)
        gt = np.where(e_axis[faces] == 0, 0.5 * (gy[fa] + gy[fb]),
                      0.5 * (gx[fa] + gx[fb]))
        corr = np.zeros(len(edges_ext.a))
        corr[faces] = edges_ext.w[faces] * e_delta[faces] * gt
        extra = np.zeros(state.size)
        np.add.at(extra, edges_solve.a, -corr)
        np.add.at(extra, edges_solve.b, corr)
        x, info = ps.solve(rhs0 + extra[free])
        Vflat = expand(x)

    # corrected currents in absolute volts: satisfy KCL exactly
    Ie = edges_ext.w * (Vflat[edges_ext.a] - Vflat[edges_ext.b]) + corr
    timings["solve_s"] = time.perf_counter() - t0

    # --- fields on leaves, expanded to the fine grid ------------------------
    t0 = time.perf_counter()
    term_part_nodes = []
    for parts in term_parts:
        pn = []
        for pl, m3 in parts:
            nodes = np.zeros(N, dtype=bool)
            for li in range(L):
                nodes[_nodes_of_cells(grids, offs, li, m3[li])] = True
            pn.append((pl, np.flatnonzero(nodes)))
        term_part_nodes.append(pn)
    supplies, loads = sv._pdn_extract(terminals, term_nodes, attaches,
                                      Vflat, Ie, edges_ext, term_part_nodes)
    Pe, P_layers, P_vias, via_reports, V3, J3, Parea = _expand_fields(
        problem, stack, grids, offs, N, edges_ext, e_axis, e_layer,
        cxg, cyg, teq_leaves, Vflat, Ie, 1.0)
    balance, mismatch, i_sup, i_loads, p_loads = sv._pdn_balance(
        supplies, loads, P_layers, P_vias)
    timings["postprocess_s"] = time.perf_counter() - t0

    # --- source-sink pair matrix on the copper-only leaf graph -------------
    # same deferred-correction loop per pattern solve, so the pair
    # resistances match the uniform grid to the usual adaptive accuracy
    t0 = time.perf_counter()
    ebase = len(edges.a)
    axb = e_axis[:ebase]
    dlb = e_delta[:ebase]
    facb = axb >= 0

    def _pair_solver(state_g, dv, edges_pm, pmerge):
        A2, rhs0p, _ = sv._assemble(state_g, edges_pm, None, dv)
        ps2 = sv.PreparedSolver(A2)
        freeg = state_g == 1
        fa3, fb3 = edges.a[facb], edges.b[facb]

        def expand_g(x2):
            V = np.where(state_g >= 2, dv, 0.0)
            V[freeg] = x2
            if pmerge is not None:
                V = V[pmerge]       # members read their super-node
            return V

        def slv(inj_p):
            x2, _ = ps2.solve(rhs0p + inj_p[freeg])
            V = expand_g(x2)
            for _p in range(passes):
                if not facb.any():
                    break
                gx, gy = _leaf_gradients(N, fa3, fb3, cxg, cyg, V)
                gt = np.where(axb[facb] == 0,
                              0.5 * (gy[fa3] + gy[fb3]),
                              0.5 * (gx[fa3] + gx[fb3]))
                corrp = np.zeros(ebase)
                corrp[facb] = edges.w[facb] * dlb[facb] * gt
                extra = np.zeros(state_g.size)
                np.add.at(extra, edges_pm.a, -corrp)
                np.add.at(extra, edges_pm.b, corrp)
                x2, _ = ps2.solve(rhs0p + inj_p[freeg] + extra[freeg])
                V = expand_g(x2)
            return V
        return slv

    pairs = sv._pdn_pairs(terminals, term_nodes, attaches, merge,
                          state_base, edges, supplies, loads,
                          _pair_solver)
    timings["pairs_s"] = time.perf_counter() - t0

    return sv.Result(
        R_ohm=float("nan"), i_test=i_loads, V=V3, Jmag=J3, Parea=Parea,
        layer_names=list(stack.layer_names),
        P_total=float(sum(P_layers) + P_vias),
        P_layers=P_layers, P_vias=P_vias,
        power_balance_rel=balance, via_reports=via_reports,
        I1_a=i_sup, I2_a=i_loads, mismatch_rel=mismatch,
        n_free=info.n_unknowns, solve_info=info,
        contact_model="pdn",
        freq_hz=freq_hz,
        skin_depth_um=(skin.skin_depth_m(freq_hz, problem.rho_ohm_m) * 1e6
                       if freq_hz > 0 else None),
        rs_ratios=rs_ratios,
        timings=timings,
        mode="pdn", supplies=supplies, loads=loads,
        P_loads=p_loads,
        P_supply_internal=sum(s_.p_internal_w for s_ in supplies),
        v_nominal=v_nominal, pairs=pairs,
    )
