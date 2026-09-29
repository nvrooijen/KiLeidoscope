# Vendored from Fill Resistance 1.4.2, fill_resistance/solver.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Unchanged apart from this header.
"""Coupled multi-layer finite-difference solver.

Each included copper layer is a 2D 5-point sheet with per-layer face
conductance sigma_s = t/rho [S] (square cells: independent of h); via and
plated-through-pad barrels add vertical conductances between the layers
they span AND reach copper on. Per layer the barrel attaches to the cell
under it, or to the nearest copper cell within the pad footprint (+1
cell) - fills joined by thermal-relief spokes still connect. A barrel
passing a (wider) antipad still bridges the layers above/below it with
the full barrel length. At freq > 0 the per-layer sheet conductances and the
barrel walls get the 1D skin-effect correction (see skin.py; AC results
are a rigorous lower bound - lateral redistribution is not modeled).

Two contact models for the terminals (each terminal = merged parts):

- "uniform" (default): a conductor pressed onto the contact area injects
  the current orthogonally with UNIFORM surface density: every contact
  cell sources (sinks) I/N. The in-plane current density ramps across
  the contact instead of being zero. The pure-Neumann system is grounded
  at one V- cell (that cell's sink share is exactly the flux that exits
  through the ground reference, so the solution equals the singular
  system's). R = (<V over V+ cells> - <V over V- cells>) / I; because
  the injection and averaging weights coincide, sum(edge powers) = I^2 R
  holds exactly and remains the consistency check.

- "equipotential": ideal bonded lug; contact cells are Dirichlet
  (V+ = 1 V, V- = 0). R from the exact discrete electrode flux. Touching
  terminals are rejected (a direct face would short the Dirichlet
  regions); with "uniform" contacts touching is physically fine.

The two models bracket a real contact: R_equipotential <= R_real <=
R_uniform. Missing neighbors give no matrix term = insulated boundary.
Current density per layer comes from face currents (np.gradient across
the NaN staircase boundary would pollute the field). Power density per
layer distributes each in-plane edge's dissipation half to each endpoint
cell. All reported fields are rescaled to the test current I_test.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.sparse import linalg as sla

from . import config, progress, skin
from .errors import ConnectivityError, ElectrodeError, SolverError
from .geometry import Problem, slot_distance
from .raster import RasterStack, electrodes_touch


@dataclass
class SolveInfo:
    method: str                   # "spsolve" | "cg+jacobi"
    n_unknowns: int
    iterations: int | None = None
    residual: float | None = None


# via_index tag for PDN supply-attachment edges (virtual Thevenin node
# to contact cell): excluded from the in-plane fields (== -1) AND from
# the via power/reports (>= 0); their dissipation is P_supply_internal
PDN_EDGE = -2


@dataclass
class Edges:
    a: np.ndarray                 # int64 flat cell ids
    b: np.ndarray
    w: np.ndarray                 # conductance [S]
    via_index: np.ndarray         # int32; -1 = in-plane edge,
                                  # PDN_EDGE = supply attachment
    dead_barrels: int = 0         # barrels spanning >=2 layers that found
                                  # fill copper on fewer than 2 of them


@dataclass
class ViaReport:
    x_mm: float
    y_mm: float
    kind: str
    drill_mm: float
    current_a: float              # max barrel-segment current @ I_test
    power_w: float                # total barrel dissipation @ I_test


@dataclass
class SupplyReport:
    """One PDN supply after the solve. The delivered current is an
    OUTCOME (Thevenin split), not an input."""
    label: str
    v_oc: float                   # open-circuit volts used in the solve
    r_out_ohm: float
    i_a: float                    # delivered current [A]
    v_contact: float              # mean volts over the contact cells
    p_internal_w: float           # dissipated inside r_out
    part_currents: list = field(default_factory=list)  # [(label, amps)]
    v_eff: float = 0.0            # current-weighted contact volts: the
                                  # potential the delivered power sees
                                  # (= v_contact for ideal and bonded
                                  # contacts); makes the pair-loss
                                  # allocation sum EXACTLY to the
                                  # copper dissipation
    component: str = ""           # display: terminal's owner hint
    comment: str = ""             # display: terminal's free-text note


@dataclass
class LoadReport:
    """One PDN load after the solve. The draw is prescribed; the contact
    voltage is the outcome of interest."""
    label: str
    i_a: float                    # prescribed draw [A]
    v_mean: float                 # mean volts over the contact cells
    v_min: float                  # worst-case contact cell
    p_w: float                    # i_a * v_mean (exact: injection and
                                  # averaging weights coincide)
    part_currents: list = field(default_factory=list)  # [(label, amps)]
    component: str = ""           # display: terminal's owner hint
    comment: str = ""             # display: terminal's free-text note


@dataclass
class PairReport:
    """One (supply, load) pair: the effective COPPER resistance between
    the two contacts (source internals excluded; injection patterns as
    in the solve - uniform per cell, or the bonded lug) and the copper
    loss attributed to the pair by PROPORTIONAL SHARING
    (f_ij = I_i * I_j / I_component, P_ij = f_ij * (v_eff_i - v_mean_j)).
    The attribution is a convention, not unique physics - but it sums
    exactly to the total copper dissipation, and R is an operating-
    point-independent property of the board."""
    supply: str
    load: str
    r_ohm: float | None           # None: no common copper path
    i_share_a: float              # attributed current [A]
    p_w: float                    # attributed copper loss [W]


@dataclass
class Result:
    R_ohm: float
    i_test: float
    V: np.ndarray                 # (L, ny, nx) volts @ I_test, NaN off-copper
    Jmag: np.ndarray              # (L, ny, nx) A/m^2 @ I_test
    Parea: np.ndarray             # (L, ny, nx) W/m^2 @ I_test
    layer_names: list[str]
    P_total: float                # I_test^2 * R
    P_layers: list[float]         # in-plane dissipation per layer @ I_test
    P_vias: float                 # total barrel dissipation @ I_test
    power_balance_rel: float      # |sum(edge powers) - I^2 R| / I^2 R
    via_reports: list[ViaReport]  # sorted by current, descending
    I1_a: float                   # electrode currents (unit drive)
    I2_a: float
    mismatch_rel: float
    n_free: int
    solve_info: SolveInfo
    # per-part terminal currents @ I_test: [(label, amps), ...];
    # computed flux for "equipotential", prescribed area share for "uniform"
    part_currents1: list = field(default_factory=list)
    part_currents2: list = field(default_factory=list)
    contact_model: str = "uniform"
    freq_hz: float = 0.0
    skin_depth_um: float | None = None
    rs_ratios: list[float] = field(default_factory=list)  # R_AC/R_DC per layer
    timings: dict = field(default_factory=dict)
    # --- PDN mode (mode == "pdn"; classic solves leave these empty) ---
    # R_ohm is NaN there (no single two-terminal R); i_test carries the
    # summed load draw so %-of-total displays keep working; fields V/
    # Jmag/Parea are in ABSOLUTE volts / real operating current
    mode: str = "classic"                     # "classic" | "pdn"
    supplies: list = field(default_factory=list)   # [SupplyReport]
    loads: list = field(default_factory=list)      # [LoadReport]
    P_loads: float = 0.0                      # sum of load powers [W]
    P_supply_internal: float = 0.0            # sum of r_out dissipation
    v_nominal: float | None = None            # default supply v_oc used
    pairs: list = field(default_factory=list)  # [PairReport], every
                                               # supply x load


def _shifts2d():
    return [
        ((slice(None), slice(None, -1)), (slice(None), slice(1, None))),
        ((slice(None, -1), slice(None)), (slice(1, None), slice(None))),
    ]


def _sigma_2d(stack: RasterStack, li: int, sigma_layer: float,
              sigma_buildup: float) -> np.ndarray | None:
    """Per-cell sheet conductance for one layer, or None if uniform.
    Combines the via-mouth thickness map (cap-thin / partially drilled
    cells) with the solder-buildup addition."""
    have_b = (stack.buildup is not None and sigma_buildup > 0
              and stack.buildup[li].any())
    have_t = (stack.thick_scale is not None
              and bool((stack.thick_scale[li] != 1.0).any()))
    if not have_b and not have_t:
        return None
    s = np.full(stack.shape2d, sigma_layer)
    if have_t:
        s *= stack.thick_scale[li]
    if have_b:
        s[stack.buildup[li]] += sigma_buildup
    return s


def _barrel_links(stack: RasterStack, problem: Problem
                  ) -> tuple[list, int]:
    """Vertical barrel links on the fine grid, shared by the uniform and
    adaptive paths: [(via_index, layer_a, i_a, j_a, layer_b, i_b, j_b,
    r_dc), ...] plus the count of dead barrels (span >= 2 layers but
    reached copper on < 2). Connection cell per layer: the cell under
    the barrel, or the nearest copper cell whose center lies within the
    pad footprint (+1 cell of rasterization slop) - fills joined to the
    barrel by thermal-relief spokes still connect, wider antipads do not
    (the barrel then bridges the layers above/below)."""
    L, ny, nx = stack.masks.shape
    h = stack.h_nm
    links = []
    dead = 0
    for vi, via in enumerate(problem.vias):
        cell = stack.cell_of(via.x, via.y)
        if cell is None:
            continue
        i, j = cell
        span = [li for li, layer in enumerate(problem.layers)
                if via.spans(layer.z_nm)]
        r_nm = max(via.pad_nm, via.drill_nm + 300_000) / 2.0 + h
        win_j = int((r_nm + abs(via.slot_dx_nm)) // h) + 1
        win_i = int((r_nm + abs(via.slot_dy_nm)) // h) + 1
        i0, i1 = max(0, i - win_i), min(ny, i + win_i + 1)
        j0, j1 = max(0, j - win_j), min(nx, j + win_j + 1)
        xs = stack.x0_nm + (np.arange(j0, j1) + 0.5) * h - via.x
        ys = stack.y0_nm + (np.arange(i0, i1) + 0.5) * h - via.y
        d2 = slot_distance(xs[None, :], ys[:, None],
                           via.slot_dx_nm, via.slot_dy_nm) ** 2
        d2 = np.where(d2 <= r_nm * r_nm, d2, np.inf)
        present = []                            # (layer, i, j) per layer
        for li in span:
            if stack.masks[li, i, j]:
                present.append((li, i, j))
                continue
            dc = np.where(stack.masks[li, i0:i1, j0:j1], d2, np.inf)
            ci, cj = np.unravel_index(int(np.argmin(dc)), dc.shape)
            if np.isfinite(dc[ci, cj]):
                present.append((li, i0 + ci, j0 + cj))
        if len(span) >= 2 and len(present) < 2:
            dead += 1
        for (la, ia, ja), (lb, ib, jb) in zip(present[:-1], present[1:]):
            length = problem.layers[lb].z_nm - problem.layers[la].z_nm
            if length <= 0:
                continue
            # populated THT pads carry a soldered component lead: lead
            # cylinder (drill minus the fab clearance) + solder annulus
            # in parallel with the plating (DNP pads and vias stay
            # plating-only)
            r_dc = via.barrel_resistance(
                length, problem.rho_ohm_m, problem.plating_nm,
                solder_rho_ohm_m=(problem.solder_rho_ohm_m
                                  if via.solder_filled else None),
                lead_nm=max(via.drill_nm - problem.tht_lead_clearance_nm, 0),
                lead_rho_ohm_m=problem.tht_lead_rho_ohm_m)
            links.append((vi, la, ia, ja, lb, ib, jb, r_dc))
    return links, dead


def build_edges(stack: RasterStack, problem: Problem, sigmas: list[float],
                via_factor: float = 1.0,
                sigma_buildup: float = 0.0) -> Edges:
    """All copper-copper conductances: in-plane faces + via barrels.
    sigmas: effective (possibly AC) sheet conductance per layer;
    via_factor: R_AC/R_DC of the barrel wall; sigma_buildup: extra sheet
    conductance on solder-buildup cells. Faces between cells of unequal
    conductance use the harmonic mean (series half-cells), which reduces
    exactly to sigma for uniform regions."""
    L, ny, nx = stack.masks.shape
    plane = ny * nx
    aa, bb, ww, vv = [], [], [], []

    for li in range(L):
        m = stack.masks[li]
        if stack.chain is not None:
            # chain-only cells connect through their explicit 1D links,
            # never through sheet faces (their copper is narrower than h)
            m = m & ~stack.chain[li]
        sig = sigmas[li]
        scell = _sigma_2d(stack, li, sig, sigma_buildup)
        base = li * plane
        for src, dst in _shifts2d():
            pair = m[src] & m[dst]
            ii, jj = np.nonzero(pair)
            if src[0] == slice(None):            # horizontal: j, j+1
                a = base + ii * nx + jj
                b = a + 1
            else:                                 # vertical: i, i+1
                a = base + ii * nx + jj
                b = a + nx
            aa.append(a.astype(np.int64))
            bb.append(b.astype(np.int64))
            if scell is None:
                ww.append(np.full(len(a), sig))
            else:
                s_a = scell[src][pair]
                s_b = scell[dst][pair]
                ww.append(2.0 * s_a * s_b / (s_a + s_b))
            vv.append(np.full(len(a), -1, dtype=np.int32))

    links, dead_barrels = _barrel_links(stack, problem)
    for vi, la, ia, ja, lb, ib, jb, r_dc in links:
        aa.append(np.array([la * plane + ia * nx + ja], dtype=np.int64))
        bb.append(np.array([lb * plane + ib * nx + jb], dtype=np.int64))
        ww.append(np.array([1.0 / (r_dc * via_factor)]))
        vv.append(np.array([vi], dtype=np.int32))

    if stack.chain_edges is not None and len(stack.chain_edges[0]):
        ca, cb, cg, cl, _ = stack.chain_edges
        alive = stack.masks.ravel()[ca] & stack.masks.ravel()[cb]
        if alive.any():
            # skin correction: scale like the layer's sheet conductance
            fac = np.array([sigmas[l] * problem.rho_ohm_m
                            / (problem.layers[l].thickness_nm * 1e-9)
                            for l in range(L)])
            aa.append(ca[alive])
            bb.append(cb[alive])
            ww.append((cg * fac[cl])[alive])
            vv.append(np.full(int(alive.sum()), -1, dtype=np.int32))

    if not aa:
        raise ConnectivityError("No copper found on the selected layers.")
    return Edges(a=np.concatenate(aa), b=np.concatenate(bb),
                 w=np.concatenate(ww), via_index=np.concatenate(vv),
                 dead_barrels=dead_barrels)


def connected_restrict(stack: RasterStack, e1: np.ndarray, e2: np.ndarray,
                       edges: Edges) -> tuple[bool, int]:
    """Keep only components (through-plane AND through-via) touching both
    terminals. Mutates stack.masks / e1 / e2. Returns (changed,
    n_components): whether anything was dropped (caller must rebuild
    edges) and how many disjoint copper groups survive."""
    n = stack.masks.size
    graph = sparse.coo_matrix(
        (np.ones(len(edges.a)), (edges.a, edges.b)), shape=(n, n))
    _, labels = csgraph.connected_components(graph, directed=False)
    labels3 = labels.reshape(stack.masks.shape)
    common = np.intersect1d(np.unique(labels3[e1]), np.unique(labels3[e2]))
    if len(common) == 0:
        raise ConnectivityError(
            "The two terminals are not connected by the selected fill "
            "layers (not even through vias). Check the layer selection and "
            "that the fills are up to date."
        )
    keep = np.isin(labels3, common) & stack.masks
    changed = bool((stack.masks & ~keep).any())
    stack.masks &= keep
    e1 &= keep
    e2 &= keep
    return changed, len(common)


def _pdn_keep_components(terminals: list, per_term: list) -> set:
    """The PDN component keep rule + its diagnostics, on the label sets
    each terminal's contact touches (shared by the cell graph here and
    the adaptive leaf graph). Keep components holding >= 1 supply AND
    (>= 1 load OR >= 2 supplies); errors for unreachable / sheet-
    spanning loads, notes for sheet-spanning supplies."""
    n_sup: dict = {}
    has_load: set = set()
    for t, labs in zip(terminals, per_term):
        if t.role == "supply":
            for l in labs:
                n_sup[l] = n_sup.get(l, 0) + 1
        else:
            has_load |= labs
    kept = {l for l, c in n_sup.items() if l in has_load or c >= 2}
    if not kept:
        raise ConnectivityError(
            "No copper component connects a supply to a load (not even "
            "through vias). Check the layer selection, the terminal "
            "definitions and that the fills are up to date."
        )
    for t, labs in zip(terminals, per_term):
        if t.role != "load":
            continue
        kl = labs & kept
        if not kl:
            raise ConnectivityError(
                f"Load '{t.label}' sits on copper that is not connected "
                f"to any supply (not even through vias)."
            )
        if len(kl) > 1:
            if t.bonded:
                # the external bond IS the connection: the split
                # between the sheets is well-defined through the lug
                print(f"note: bonded load '{t.label}' spans {len(kl)} "
                      f"disconnected copper sheets; the split between "
                      f"them is set by its external bond")
                continue
            raise ConnectivityError(
                f"Load '{t.label}' spans {len(kl)} disconnected copper "
                f"sheets - the current split between them is undefined "
                f"with per-cell injection. Include the layers/vias that "
                f"join them, mark the load as bonded, or split it into "
                f"one terminal per sheet."
            )
    for t, labs in zip(terminals, per_term):
        if t.role == "supply" and len(labs & kept) > 1:
            print(f"note: supply '{t.label}' feeds {len(labs & kept)} "
                  f"disconnected copper sheets; the split between them "
                  f"is set by its output resistance (Thevenin)")
    return kept


def connected_restrict_multi(stack: RasterStack, term_masks: list,
                             terminals: list, edges: Edges
                             ) -> tuple[bool, int]:
    """PDN connectivity restriction: keep copper components holding
    >= 1 supply AND (>= 1 load OR >= 2 supplies) - the second clause
    keeps circulating-current paths between paralleled supplies with
    unequal v_oc. A load on copper reachable from no supply is an
    error; so is a load spanning several kept components (its uniform
    injection cannot decide the split between disconnected sheets). A
    load merely LOSING cells to dropped copper is fine: those cells
    could not carry current anyway, the draw renormalizes over the
    rest. Mutates stack.masks and the term_masks. Returns (changed,
    n_kept_components)."""
    n = stack.masks.size
    graph = sparse.coo_matrix(
        (np.ones(len(edges.a)), (edges.a, edges.b)), shape=(n, n))
    _, labels = csgraph.connected_components(graph, directed=False)
    labels3 = labels.reshape(stack.masks.shape)

    per_term = [set(np.unique(labels3[m]).tolist()) if m.any() else set()
                for m in term_masks]
    kept = _pdn_keep_components(terminals, per_term)
    keep = np.isin(labels3, sorted(kept)) & stack.masks
    changed = bool((stack.masks & ~keep).any())
    stack.masks &= keep
    for t, m in zip(terminals, term_masks):
        had = bool(m.any())
        m &= keep
        if t.role == "supply" and had and not m.any():
            print(f"warning: supply '{t.label}' only touches copper not "
                  f"connected to any load - it delivers 0 A")
    return changed, len(kept)


def _assemble(state: np.ndarray, edges: Edges, rhs_extra: np.ndarray | None,
              dirichlet_v: np.ndarray | None = None):
    """Weighted-Laplacian assembly with Dirichlet elimination.
    state: 0 off, 1 free, 2 Dirichlet@1V, 3 Dirichlet@0V.
    rhs_extra: per-flat-cell current injection [A] added for free cells.
    dirichlet_v (PDN mode): per-node Dirichlet volts - any state >= 2
    is then held at dirichlet_v[node] instead of the fixed 1 V / 0 V
    pair, and the direct-connection short check is skipped (edges
    between Dirichlet nodes simply conduct; their currents come out of
    the post-solve edge fluxes). None keeps the classic behavior
    bit-for-bit."""
    n = state.size
    sa, sb = state[edges.a], state[edges.b]
    if dirichlet_v is None:
        short = ((sa == 2) & (sb == 3)) | ((sa == 3) & (sb == 2))
        if short.any():
            n_via = int((edges.via_index[short] >= 0).sum())
            raise ElectrodeError(
                f"The terminals are directly connected by "
                f"{int(short.sum())} conductance(s) ({n_via} via "
                f"barrel(s)) without any free copper in between - move "
                f"the contacts apart."
            )

    free = state == 1
    n_free = int(free.sum())
    if n_free == 0:
        raise ElectrodeError(
            "No free copper cells remain between the terminals - the "
            "contacts cover the whole fill at this grid resolution."
        )
    idx = np.full(n, -1, dtype=np.int64)
    idx[free] = np.arange(n_free)

    diag = np.zeros(n_free)
    rhs = np.zeros(n_free)
    fa, fb = sa == 1, sb == 1
    np.add.at(diag, idx[edges.a[fa]], edges.w[fa])
    np.add.at(diag, idx[edges.b[fb]], edges.w[fb])
    if dirichlet_v is None:
        r1a = fa & (sb == 2)
        r1b = fb & (sa == 2)
        np.add.at(rhs, idx[edges.a[r1a]], edges.w[r1a])
        np.add.at(rhs, idx[edges.b[r1b]], edges.w[r1b])
    else:
        r1a = fa & (sb >= 2)
        r1b = fb & (sa >= 2)
        np.add.at(rhs, idx[edges.a[r1a]],
                  edges.w[r1a] * dirichlet_v[edges.b[r1a]])
        np.add.at(rhs, idx[edges.b[r1b]],
                  edges.w[r1b] * dirichlet_v[edges.a[r1b]])
    if rhs_extra is not None:
        rhs += rhs_extra[free]

    ff = fa & fb
    rows = np.concatenate([idx[edges.a[ff]], idx[edges.b[ff]],
                           np.arange(n_free)])
    cols = np.concatenate([idx[edges.b[ff]], idx[edges.a[ff]],
                           np.arange(n_free)])
    vals = np.concatenate([-edges.w[ff], -edges.w[ff], diag])
    A = sparse.coo_matrix((vals, (rows, cols)),
                          shape=(n_free, n_free)).tocsr()
    return A, rhs, idx


def solve_system(A: sparse.csr_matrix, b: np.ndarray) -> tuple[np.ndarray, SolveInfo]:
    n = A.shape[0]
    if n <= config.SPSOLVE_MAX_UNKNOWNS:
        x = sla.spsolve(A.tocsc(), b)
        return x, SolveInfo(method="spsolve", n_unknowns=n)
    try:
        return _solve_amg(A, b)
    except ImportError:
        print("note: pyamg not installed - falling back to Jacobi-CG "
              "(much slower on large grids)")
        return _solve_cg_jacobi(A, b)


class PreparedSolver:
    """Factor/set up once, solve several right-hand sides with the SAME
    matrix (deferred-correction passes): the direct path keeps the LU,
    the iterative path keeps the AMG hierarchy."""

    def __init__(self, A: sparse.csr_matrix):
        self.n = A.shape[0]
        self._A = A.tocsr()
        self._lu = None
        self._ml = None
        if self.n <= config.SPSOLVE_MAX_UNKNOWNS:
            self._lu = sla.splu(A.tocsc())
            self.method = "spsolve"
        else:
            try:
                import pyamg
                self._ml = pyamg.smoothed_aggregation_solver(self._A,
                                                             max_coarse=500)
                self.method = "amg+cg"
            except ImportError:
                print("note: pyamg not installed - falling back to "
                      "Jacobi-CG (much slower on large grids)")
                self.method = "cg+jacobi"

    def solve(self, b: np.ndarray) -> tuple[np.ndarray, SolveInfo]:
        if self._lu is not None:
            progress.tick()      # direct solve: one shot, no iterations
            return self._lu.solve(b), SolveInfo(method="spsolve",
                                                n_unknowns=self.n)
        if self._ml is not None:
            residuals: list[float] = []
            x = self._ml.solve(b, tol=config.AMG_TOL, maxiter=300,
                               accel="cg", residuals=residuals,
                               callback=lambda _: progress.tick())
            res = float(np.linalg.norm(b - self._A @ x)
                        / max(np.linalg.norm(b), 1e-300))
            if not np.isfinite(res) or res > 1e-6:
                raise SolverError(
                    f"AMG-CG did not converge (residual {res:.2e}). Try a "
                    f"different grid size, or force the direct solver by "
                    f"raising SPSOLVE_MAX_UNKNOWNS in config.py."
                )
            return x, SolveInfo(method="amg+cg", n_unknowns=self.n,
                                iterations=max(len(residuals) - 1, 0),
                                residual=res)
        return _solve_cg_jacobi(self._A, b)


def _solve_amg(A: sparse.csr_matrix, b: np.ndarray) -> tuple[np.ndarray, SolveInfo]:
    """CG preconditioned with smoothed-aggregation AMG: near-linear
    scaling on these 2D Laplacians and a fraction of spsolve's memory."""
    import pyamg

    n = A.shape[0]
    ml = pyamg.smoothed_aggregation_solver(A.tocsr(), max_coarse=500)
    residuals: list[float] = []
    x = ml.solve(b, tol=config.AMG_TOL, maxiter=300, accel="cg",
                 residuals=residuals, callback=lambda _: progress.tick())
    res = float(np.linalg.norm(b - A @ x) / max(np.linalg.norm(b), 1e-300))
    if not np.isfinite(res) or res > 1e-6:
        raise SolverError(
            f"AMG-CG did not converge (residual {res:.2e}). Try a "
            f"different grid size, or force the direct solver by raising "
            f"SPSOLVE_MAX_UNKNOWNS in config.py."
        )
    return x, SolveInfo(method="amg+cg", n_unknowns=n,
                        iterations=max(len(residuals) - 1, 0), residual=res)


def _solve_cg_jacobi(A: sparse.csr_matrix, b: np.ndarray) -> tuple[np.ndarray, SolveInfo]:
    # The matrix is SPD, so CG is guaranteed to converge. Jacobi is the
    # only preconditioner in scipy that keeps the preconditioned operator
    # SPD without a factorization that can break down at this scale.
    n = A.shape[0]
    d = A.diagonal()
    M = sla.LinearOperator((n, n), lambda v: v / d)
    iters = 0

    def count(_):
        nonlocal iters
        iters += 1
        progress.tick()

    try:
        x, code = sla.cg(A, b, M=M, rtol=config.CG_TOL,
                         maxiter=config.CG_MAXITER, callback=count)
    except TypeError:  # scipy < 1.12 uses tol=
        x, code = sla.cg(A, b, M=M, tol=config.CG_TOL,
                         maxiter=config.CG_MAXITER, callback=count)
    if code != 0:
        raise SolverError(
            f"CG did not converge in {config.CG_MAXITER} iterations "
            f"(code {code}). Try a coarser grid or raise CG_MAXITER."
        )
    res = float(np.linalg.norm(b - A @ x) / np.linalg.norm(b))
    return x, SolveInfo(method="cg+jacobi", n_unknowns=n, iterations=iters,
                        residual=res)


def _face_current_density(V2: np.ndarray, mask2: np.ndarray, sigma: float,
                          h_m: float, t_m: float,
                          sig2d: np.ndarray | None = None,
                          rho: float | None = None) -> np.ndarray:
    """|J| (A/m^2) for one layer from face currents; V2 in volts.
    J is referenced to the conductance-equivalent copper thickness
    t_eq = sigma_cell * rho: the geometric t for plain DC copper, the
    skin-reduced conducting cross-section at AC. With a per-cell
    conductance map (buildup), face currents use the harmonic mean."""
    ny, nx = mask2.shape
    face_x = mask2[:, :-1] & mask2[:, 1:]
    face_y = mask2[:-1, :] & mask2[1:, :]
    if sig2d is None:
        wx = wy = sigma
        teq = np.full((ny, nx), sigma * rho if rho is not None else t_m)
    else:
        wx = 2.0 * sig2d[:, :-1] * sig2d[:, 1:] / (sig2d[:, :-1] + sig2d[:, 1:])
        wy = 2.0 * sig2d[:-1, :] * sig2d[1:, :] / (sig2d[:-1, :] + sig2d[1:, :])
        teq = sig2d * rho
    with np.errstate(invalid="ignore"):
        Ix = np.where(face_x, (V2[:, :-1] - V2[:, 1:]) * wx, 0.0)
        Iy = np.where(face_y, (V2[:-1, :] - V2[1:, :]) * wy, 0.0)
    IxP = np.zeros((ny, nx + 1))
    IxP[:, 1:nx] = Ix
    IyP = np.zeros((ny + 1, nx))
    IyP[1:ny, :] = Iy
    Jx = 0.5 * (IxP[:, :-1] + IxP[:, 1:])
    Jy = 0.5 * (IyP[:-1, :] + IyP[1:, :])
    Jmag = np.hypot(Jx, Jy) / (h_m * teq)
    Jmag[~mask2] = np.nan
    return Jmag


def overlay_chain_density(stack: RasterStack, rho: float, V3: np.ndarray,
                          J3: np.ndarray) -> None:
    """Fill chain (sub-resolution trace) cells of J3 with the true 1D
    link current density |dV| / (rho * dl), referenced to the
    conduction-equivalent trace cross-section: the AC scaling of the
    link conductance and of the cross-section cancel, so the expression
    holds at any frequency. V3/J3 are the display-scaled (L, ny, nx)
    maps; chain cells carry the max density of their attached links."""
    if stack.chain is None or stack.chain_edges is None \
            or not len(stack.chain_edges[0]):
        return
    ca, cb, _, _, cdl = stack.chain_edges
    mflat = stack.masks.reshape(-1)
    alive = mflat[ca] & mflat[cb]
    if not alive.any():
        return
    V3f = np.nan_to_num(V3.reshape(-1))
    Jl = np.abs(V3f[ca] - V3f[cb]) / (rho * cdl)
    Jc = np.zeros(mflat.size)
    np.maximum.at(Jc, ca[alive], Jl[alive])
    np.maximum.at(Jc, cb[alive], Jl[alive])
    fill = stack.chain & stack.masks
    J3[fill] = Jc.reshape(J3.shape)[fill]


def _equipotential_core(state: np.ndarray, edges: Edges):
    """Dirichlet solve on any node space (fine cells or leaves): state
    codes 0 off / 1 free / 2 V+ / 3 V-. Returns (Vflat_unit, R, I1, I2,
    mismatch, volts_per_amp, info); fields at 1 V drive."""
    A, rhs, _ = _assemble(state, edges, None)
    x, info = solve_system(A, rhs)

    Vflat = np.zeros(state.size)
    Vflat[state == 2] = 1.0
    Vflat[state == 1] = x

    Ie = edges.w * (Vflat[edges.a] - Vflat[edges.b])
    sa, sb = state[edges.a], state[edges.b]
    I1 = float(Ie[sa == 2].sum() - Ie[sb == 2].sum())
    I2 = float(Ie[sb == 3].sum() - Ie[sa == 3].sum())
    mismatch = abs(I1 - I2) / max(abs(I1), abs(I2), 1e-300)
    R = 1.0 / (0.5 * (I1 + I2))
    return Vflat, R, I1, I2, mismatch, R, info


def _uniform_core(state: np.ndarray, inj: np.ndarray, e1f: np.ndarray,
                  e2f: np.ndarray, edges: Edges):
    """Uniform-injection solve on any node space. state must carry the
    single ground node (code 3); inj the per-node current shares."""
    A, rhs, _ = _assemble(state, edges, inj)
    x, info = solve_system(A, rhs)

    Vflat = np.zeros(state.size)
    Vflat[state == 1] = x

    v_plus = float(Vflat[e1f].mean())
    v_minus = float(Vflat[e2f].mean())
    R = (v_plus - v_minus) / 1.0
    Vflat = Vflat - v_minus                 # display reference: <V-> = 0

    # quality: KCL residual of the solved system
    res = info.residual
    if res is None:
        res = float(np.linalg.norm(A @ x - rhs)
                    / max(np.linalg.norm(rhs), 1e-300))
    return Vflat, R, 1.0, 1.0, res, 1.0, info


def _solve_equipotential(stack, e1, e2, edges):
    """Dirichlet terminals at 1 V / 0 V on the uniform grid. Fields at
    1 V drive; scale by i_test * R to get volts at I_test."""
    if (layer := electrodes_touch(stack, e1, e2)) is not None:
        raise ElectrodeError(
            f"The terminals touch on {layer}. With the equipotential "
            f"contact model at least one cell of copper must separate "
            f"them; the uniform-injection model allows touching contacts."
        )
    state = np.zeros(stack.masks.size, dtype=np.uint8)
    state[stack.masks.ravel()] = 1
    state[e1.ravel()] = 2
    state[e2.ravel()] = 3
    return _equipotential_core(state, edges)


def _solve_uniform(stack, e1, e2, edges):
    """Uniform orthogonal injection on the uniform grid: every contact
    cell sources (sinks) 1 A / N, grounded at one V- cell (its sink
    share is exactly the flux that exits through the reference, so the
    grounded solution equals the pure-Neumann one). Fields at 1 A."""
    n = stack.masks.size
    e1f, e2f = e1.ravel(), e2.ravel()
    n1, n2 = int(e1f.sum()), int(e2f.sum())

    inj = np.zeros(n)
    inj[e1f] = 1.0 / n1
    inj[e2f] = -1.0 / n2

    state = np.zeros(n, dtype=np.uint8)
    state[stack.masks.ravel()] = 1
    ground = int(np.flatnonzero(e2f)[0])
    state[ground] = 3
    return _uniform_core(state, inj, e1f, e2f, edges)


def _part_currents(parts, Ie, edges, e_flat, scale,
                   i_test, contact_model, n_terminal_cells):
    """Current through each contact part @ I_test. Equipotential: exact
    discrete flux out of the part's cells (same-terminal internal edges
    carry zero, opposite-terminal edges are forbidden). Uniform: the
    injection is prescribed, so a part carries exactly its cell share."""
    out = []
    for label, mask3 in parts:
        pf = mask3.ravel() & e_flat
        n = int(pf.sum())
        if contact_model == "uniform":
            amps = i_test * n / max(n_terminal_cells, 1)
        else:
            ina = pf[edges.a]
            inb = pf[edges.b]
            amps = abs(float(Ie[ina].sum() - Ie[inb].sum())) * scale
        out.append((label, amps))
    return out


def _postprocess_fields(problem: Problem, stack: RasterStack, edges: Edges,
                        Vflat: np.ndarray, s: float, sigmas: list[float],
                        sigma_buildup: float):
    """Edge powers, per-layer dissipation, via reports and the V/J/P
    display fields on the uniform grid - shared verbatim by the classic
    and PDN solves. PDN appends virtual supply nodes after the grid
    ids: everything here slices Vflat back to the grid (a no-op view
    for classic) and selects in-plane edges by via_index == -1 / via
    barrels by >= 0, so PDN_EDGE attachment edges stay out of the
    copper fields and the via reports. Returns (Pe, Ie, P_layers,
    P_vias, Parea, via_reports, V3, J3): Pe is s^2-scaled, Ie is at the
    drive of Vflat (unit drive classic, absolute volts PDN); via report
    currents are s-scaled here."""
    L, ny, nx = stack.masks.shape
    h_m = stack.h_nm * 1e-9
    n_grid = stack.masks.size

    # per-edge power @ I_test; distribute in-plane power to endpoint cells
    Pe = edges.w * ((Vflat[edges.a] - Vflat[edges.b]) * s) ** 2
    inplane = edges.via_index == -1
    Pflat = np.zeros(n_grid)
    np.add.at(Pflat, edges.a[inplane], 0.5 * Pe[inplane])
    np.add.at(Pflat, edges.b[inplane], 0.5 * Pe[inplane])
    Parea = Pflat.reshape(L, ny, nx) / (h_m * h_m)
    Parea[~stack.masks] = np.nan
    plane = ny * nx
    P_layers = [float(Pflat[li * plane:(li + 1) * plane].sum())
                for li in range(L)]
    P_vias = float(Pe[edges.via_index >= 0].sum())

    # via reports: max segment current + total power per via
    Ie = edges.w * (Vflat[edges.a] - Vflat[edges.b])
    via_reports = []
    if problem.vias:
        vidx = edges.via_index
        for vi in np.unique(vidx[vidx >= 0]):
            sel = vidx == vi
            via = problem.vias[vi]
            via_reports.append(ViaReport(
                x_mm=via.x * 1e-6, y_mm=via.y * 1e-6, kind=via.kind,
                drill_mm=via.drill_nm * 1e-6,
                current_a=float(np.abs(Ie[sel]).max()) * s,
                power_w=float(Pe[sel].sum()),
            ))
        via_reports.sort(key=lambda v: v.current_a, reverse=True)

    # embedded potential + per-layer current density @ I_test; chain
    # cells have no sheet faces in the model, so keep them out of the
    # face computation and overlay their true 1D link density instead
    V3 = np.full((L, ny, nx), np.nan)
    V3[stack.masks] = Vflat[:n_grid].reshape(L, ny, nx)[stack.masks] * s
    sheet = stack.masks if stack.chain is None \
        else stack.masks & ~stack.chain
    J3 = np.stack([
        _face_current_density(
            np.nan_to_num(V3[li]), sheet[li], sigmas[li],
            h_m, problem.layers[li].thickness_nm * 1e-9,
            sig2d=_sigma_2d(stack, li, sigmas[li], sigma_buildup),
            rho=problem.rho_ohm_m)
        for li in range(L)
    ])
    overlay_chain_density(stack, problem.rho_ohm_m, V3, J3)
    return Pe, Ie, P_layers, P_vias, Parea, via_reports, V3, J3


def _conductance_params(problem: Problem, stack: RasterStack,
                        freq_hz: float):
    """Effective (possibly AC) sheet conductances per layer, Rs ratios,
    barrel factor and buildup conductance - shared by the uniform-grid
    and adaptive solve paths."""
    L = stack.nlayers
    sigmas = [
        1.0 / skin.sheet_resistance_ac(
            problem.layers[li].thickness_nm * 1e-9, freq_hz,
            problem.rho_ohm_m, config.SKIN_SIDES)
        for li in range(L)
    ]
    rs_ratios = [
        skin.resistance_factor(problem.layers[li].thickness_nm * 1e-9,
                               freq_hz, problem.rho_ohm_m, config.SKIN_SIDES)
        for li in range(L)
    ]
    via_factor = skin.resistance_factor(problem.plating_nm * 1e-9, freq_hz,
                                        problem.rho_ohm_m, sides=2)
    sigma_buildup = 0.0
    if problem.buildups and stack.buildup is not None \
            and stack.buildup.any():
        sigma_buildup = 1.0 / skin.sheet_resistance_ac(
            problem.solder_thickness_nm * 1e-9, freq_hz,
            problem.solder_rho_ohm_m, config.SKIN_SIDES)
        if problem.extra_cu_nm > 0:
            sigma_buildup += 1.0 / skin.sheet_resistance_ac(
                problem.extra_cu_nm * 1e-9, freq_hz, problem.rho_ohm_m,
                config.SKIN_SIDES)
        eq_um = sigma_buildup * problem.rho_ohm_m * 1e6
        print(f"solder buildup: {problem.solder_thickness_nm / 1000:.0f} um "
              f"solder + {problem.extra_cu_nm / 1000:.0f} um Cu on "
              f"{int(stack.buildup.sum())} cells "
              f"(= {eq_um:.1f} um equivalent copper)")
    if freq_hz > 0:
        depth = skin.skin_depth_m(freq_hz, problem.rho_ohm_m)
        print(f"AC @ {freq_hz:g} Hz: skin depth {depth * 1e6:.0f} um, "
              f"per-layer Rs ratio "
              f"{', '.join(f'{r:.2f}' for r in rs_ratios)}, "
              f"via factor {via_factor:.2f}")
    return sigmas, rs_ratios, via_factor, sigma_buildup


def run_solve(problem: Problem, stack: RasterStack, e1: np.ndarray,
              e2: np.ndarray, i_test: float, freq_hz: float = 0.0,
              contact_model: str | None = None,
              parts1: list | None = None,
              parts2: list | None = None) -> Result:
    if contact_model is None:
        contact_model = config.CONTACT_MODEL
    if config.ADAPTIVE_CELLS:
        from . import adaptive
        return adaptive.run_solve_adaptive(problem, stack, e1, e2, i_test,
                                           freq_hz, contact_model,
                                           parts1, parts2)

    timings = {}
    L, ny, nx = stack.masks.shape
    h_m = stack.h_nm * 1e-9

    sigmas, rs_ratios, via_factor, sigma_buildup = \
        _conductance_params(problem, stack, freq_hz)

    t0 = time.perf_counter()
    edges = build_edges(stack, problem, sigmas, via_factor, sigma_buildup)
    changed, n_groups = connected_restrict(stack, e1, e2, edges)
    if changed:
        edges = build_edges(stack, problem, sigmas, via_factor, sigma_buildup)
    if edges.dead_barrels:
        print(f"warning: {edges.dead_barrels} via/pad barrel(s) found fill "
              f"copper on fewer than 2 layers and carry no current (pad "
              f"copper is not modeled; a finer grid may pick up thermal "
              f"spokes)")
    if n_groups > 1 and contact_model != "equipotential":
        raise ConnectivityError(
            f"The selected fills form {n_groups} disconnected copper groups "
            f"that each touch both terminals. The uniform-injection contact "
            f"model cannot determine the current split between disconnected "
            f"sheets - switch to the equipotential contact model (bonded "
            f"lug), or include the layers/vias that join them."
        )
    if stack.buildup is not None:
        stack.buildup &= stack.masks
    if stack.chain is not None:
        stack.chain &= stack.masks
    for label, m in (parts1 or []) + (parts2 or []):
        had = bool(m.any())
        m &= stack.masks                     # follow the component restriction
        if had and not m.any():
            print(f"warning: contact part '{label}' only touches copper "
                  f"that is not connected to both terminals - it carries "
                  f"no current")
    timings["edges_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    if contact_model == "equipotential":
        Vflat, R, I1, I2, mismatch, volts_per_amp, info = \
            _solve_equipotential(stack, e1, e2, edges)
    else:
        Vflat, R, I1, I2, mismatch, volts_per_amp, info = \
            _solve_uniform(stack, e1, e2, edges)
    timings["solve_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    s = i_test * volts_per_amp              # unit-drive volts -> volts @ I_test

    Pe, Ie, P_layers, P_vias, Parea, via_reports, V3, J3 = \
        _postprocess_fields(problem, stack, edges, Vflat, s, sigmas,
                            sigma_buildup)
    P_total = i_test ** 2 * R
    balance = abs((sum(P_layers) + P_vias) - P_total) / max(P_total, 1e-300)
    if not np.isfinite(balance) or balance > 1e-3:
        raise SolverError(
            f"Inconsistent solve: R = {R:.6g} ohm with power-balance error "
            f"{balance:.2e} (sum of edge powers vs I^2*R). The result is "
            f"not trustworthy - try the equipotential contact model or a "
            f"different grid size."
        )

    # per-injection-area currents
    part_currents1 = _part_currents(
        parts1 or [], Ie, edges, e1.ravel(), s, i_test,
        contact_model, int(e1.sum()))
    part_currents2 = _part_currents(
        parts2 or [], Ie, edges, e2.ravel(), s, i_test,
        contact_model, int(e2.sum()))
    timings["postprocess_s"] = time.perf_counter() - t0

    return Result(
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


# --- PDN mode ---------------------------------------------------------------
#
# N supplies + M loads instead of one driven terminal pair, solved in
# ABSOLUTE volts (no unit-drive rescale). Each supply is a Thevenin
# source: a virtual node held Dirichlet at v_oc, attached to its
# contact cells through 1/(r_out * n_cells) each (sum = 1/r_out) -
# because Dirichlet nodes are eliminated, virtual nodes never enter the
# matrix, they only shift the diagonal/RHS of their contact cells and
# the system stays SPD. r_out <= PDN_R_OUT_EPS degrades to a direct
# Dirichlet contact (the exact limit). Each load draws its prescribed
# current with uniform orthogonal injection (-I/n per cell), the same
# semantics as the classic "uniform" contact model. Supply currents are
# OUTCOMES (the Thevenin split); KCL makes them sum to the load draws.

def _label_terminals(terminals: list) -> None:
    """Assign S1/L1-style display tags to unlabeled terminals (in
    definition order, per role)."""
    ns = nl = 0
    for t in terminals:
        if t.role == "supply":
            ns += 1
            if not t.label:
                t.label = f"S{ns}"
        else:
            nl += 1
            if not t.label:
                t.label = f"L{nl}"


def _validate_terminals(terminals: list) -> None:
    for t in terminals:
        if t.role not in ("supply", "load"):
            raise ElectrodeError(
                f"Terminal '{t.label}': unknown role '{t.role}' "
                f"(expected 'supply' or 'load')."
            )
        if t.role == "load" and t.i_draw_a < 0:
            raise ElectrodeError(
                f"Load '{t.label}': i_draw_a must be >= 0 "
                f"(got {t.i_draw_a:g})."
            )
        if t.role == "supply" and t.r_out_ohm < 0:
            raise ElectrodeError(
                f"Supply '{t.label}': r_out_ohm must be >= 0 "
                f"(got {t.r_out_ohm:g})."
            )
    if not any(t.role == "supply" for t in terminals):
        raise ElectrodeError("PDN mode needs at least one supply terminal.")
    if not any(t.role == "load" for t in terminals):
        raise ElectrodeError("PDN mode needs at least one load terminal.")


@dataclass
class _Attach:
    """How one supply is wired into the extended graph."""
    v_oc: float
    r_out: float
    nodes: np.ndarray             # contact node ids (base space)
    ideal: bool                   # r_out below eps: direct Dirichlet
    e0: int = 0                   # its attachment edges in edges_ext
    e1: int = 0                   # (empty slice for ideal supplies)


def _pdn_attach(terminals: list, term_nodes: list, state: np.ndarray,
                edges: Edges, v_nominal: float):
    """Extend the copper graph with the PDN boundary conditions. state
    is uint8 over the base node space (1 = copper); term_nodes carries
    each terminal's contact node ids in that space (uniform grid: flat
    cell ids; adaptive: leaf ids - contact cells are pinned fine there,
    so nodes and cells are 1:1 and per-node injection equals per-cell).

    A BONDED terminal shorts all its contact cells into one super-node:
    `merge` maps every node id to its representative (identity outside
    bonded terminals; None when no terminal is bonded). The caller
    solves on merge-relabeled edges (member cells leave the system,
    state 0) and afterwards scatters the potentials back with
    Vflat = Vflat[merge], so all extraction runs on the ORIGINAL edge
    endpoints where internal member-member edges carry exactly zero.

    Returns (state_ext, dirichlet_v, inj, edges_ext, attaches, merge)
    with virtual supply nodes appended after state.size."""
    n_base = state.size
    n_virt = sum(1 for t, nd in zip(terminals, term_nodes)
                 if t.role == "supply" and len(nd)
                 and t.r_out_ohm > config.PDN_R_OUT_EPS)
    n_ext = n_base + n_virt
    state_ext = np.zeros(n_ext, dtype=np.uint8)
    state_ext[:n_base] = state
    dirichlet_v = np.zeros(n_ext)
    inj = np.zeros(n_ext)
    merge = None

    def bond(nodes):
        """Short the cells to nodes[0]; members leave the system."""
        nonlocal merge
        if merge is None:
            merge = np.arange(n_ext, dtype=np.int64)
        rep = int(nodes[0])
        merge[nodes] = rep
        state_ext[nodes] = 0
        return rep

    aa = [edges.a]
    bb = [edges.b]
    ww = [edges.w]
    vv = [edges.via_index]
    attaches: list = []
    nv = 0
    e_next = len(edges.a)
    for t, nodes in zip(terminals, term_nodes):
        if t.role != "supply":
            attaches.append(None)
            if len(nodes):
                if t.bonded:
                    rep = bond(nodes)
                    state_ext[rep] = 1
                    inj[rep] -= t.i_draw_a      # whole draw at the lug
                else:
                    inj[nodes] -= t.i_draw_a / len(nodes)
            continue
        v = v_nominal if t.v_oc is None else t.v_oc
        at = _Attach(v_oc=v, r_out=t.r_out_ohm, nodes=nodes,
                     ideal=t.r_out_ohm <= config.PDN_R_OUT_EPS)
        attaches.append(at)
        if len(nodes) == 0:
            continue                        # restricted away: reports 0 A
        if t.bonded:
            rep = bond(nodes)
            if at.ideal:
                state_ext[rep] = 2
                dirichlet_v[rep] = v
            else:
                state_ext[rep] = 1
                vid = n_base + nv
                nv += 1
                state_ext[vid] = 2
                dirichlet_v[vid] = v
                # the whole r_out in series with the equipotential lug
                aa.append(np.array([vid], dtype=np.int64))
                bb.append(np.array([rep], dtype=np.int64))
                ww.append(np.array([1.0 / t.r_out_ohm]))
                vv.append(np.array([PDN_EDGE], dtype=np.int32))
                at.e0, at.e1 = e_next, e_next + 1
                e_next += 1
        elif at.ideal:
            state_ext[nodes] = 2
            dirichlet_v[nodes] = v
        else:
            vid = n_base + nv
            nv += 1
            state_ext[vid] = 2
            dirichlet_v[vid] = v
            k = len(nodes)
            # oriented virtual -> cell so Ie = w * (v_oc - V_cell) is
            # the delivered current, positive out of the supply
            aa.append(np.full(k, vid, dtype=np.int64))
            bb.append(nodes.astype(np.int64))
            ww.append(np.full(k, 1.0 / (t.r_out_ohm * k)))
            vv.append(np.full(k, PDN_EDGE, dtype=np.int32))
            at.e0, at.e1 = e_next, e_next + k
            e_next += k
    edges_ext = Edges(a=np.concatenate(aa), b=np.concatenate(bb),
                      w=np.concatenate(ww), via_index=np.concatenate(vv),
                      dead_barrels=edges.dead_barrels)
    return state_ext, dirichlet_v, inj, edges_ext, attaches, merge


def _pdn_solve_edges(edges_ext: Edges, merge) -> Edges:
    """The edge set the linear system is assembled from: bonded
    terminals' member cells relabeled to their representative (edge
    ORDER and COUNT are preserved - only endpoints move; internal
    edges become self-loops, which cancel exactly in the COO
    assembly). Identity when nothing is bonded."""
    if merge is None:
        return edges_ext
    return Edges(a=merge[edges_ext.a], b=merge[edges_ext.b],
                 w=edges_ext.w, via_index=edges_ext.via_index,
                 dead_barrels=edges_ext.dead_barrels)


def _pdn_extract(terminals: list, term_nodes: list, attaches: list,
                 Vflat: np.ndarray, Ie: np.ndarray, edges_ext: Edges,
                 term_part_nodes: list):
    """Per-supply delivered currents and per-load voltages/powers from
    the solved potentials, shared by the uniform-grid and adaptive PDN
    paths (Ie must satisfy KCL - the corrected currents on the adaptive
    path; bonded terminals' Vflat already scattered back, so their
    internal edges carry exactly zero). Returns (supplies, loads)."""
    n_ext = Vflat.size
    copper = edges_ext.via_index != PDN_EDGE

    def part_flux_out(pn):
        """Oriented copper-edge flux out of a part's cells: the part's
        boundary current (same-terminal internal edges carry zero for
        Dirichlet and bonded contacts; attachment edges excluded so a
        bonded supply's lug edge is not double-counted)."""
        pm = np.zeros(n_ext, dtype=bool)
        pm[pn] = True
        return float(Ie[pm[edges_ext.a] & copper].sum()
                     - Ie[pm[edges_ext.b] & copper].sum())

    supplies: list = []
    loads: list = []
    for t, nodes, at, pnodes in zip(terminals, term_nodes, attaches,
                                    term_part_nodes):
        if t.role == "supply":
            if len(at.nodes) == 0:
                supplies.append(SupplyReport(
                    label=t.label, v_oc=at.v_oc, r_out_ohm=at.r_out,
                    i_a=0.0, v_contact=at.v_oc, p_internal_w=0.0,
                    part_currents=[(pl, 0.0) for pl, _ in pnodes],
                    v_eff=at.v_oc, component=t.component,
                    comment=t.comment))
                continue
            if at.ideal:
                # exact discrete flux out of the Dirichlet contact
                # (edges inside the contact cancel a-side vs b-side)
                member = np.zeros(n_ext, dtype=bool)
                member[at.nodes] = True
                i_a = float(Ie[member[edges_ext.a] & copper].sum()
                            - Ie[member[edges_ext.b] & copper].sum())
                v_contact = at.v_oc
                v_eff = at.v_oc
                p_int = 0.0
                pcs = [(pl, part_flux_out(pn)) for pl, pn in pnodes]
            else:
                sl = slice(at.e0, at.e1)
                i_a = float(Ie[sl].sum())
                v_contact = float(Vflat[at.nodes].mean())
                dv = Vflat[edges_ext.a[sl]] - Vflat[edges_ext.b[sl]]
                p_int = float((edges_ext.w[sl] * dv * dv).sum())
                if t.bonded:
                    # one lug edge carries the total; the per-part
                    # split is the copper boundary flux (an outcome)
                    v_eff = v_contact       # equipotential lug
                    pcs = [(pl, part_flux_out(pn)) for pl, pn in pnodes]
                else:
                    cells = edges_ext.b[sl]
                    # the potential the delivered power actually sees:
                    # per-cell currents weight their cell potentials
                    v_eff = (float((Ie[sl] * Vflat[cells]).sum() / i_a)
                             if abs(i_a) > 1e-300 else v_contact)
                    pcs = []
                    for pl, pn in pnodes:
                        pm = np.zeros(n_ext, dtype=bool)
                        pm[pn] = True
                        pcs.append((pl, float(Ie[sl][pm[cells]].sum())))
            supplies.append(SupplyReport(
                label=t.label, v_oc=at.v_oc, r_out_ohm=at.r_out,
                i_a=i_a, v_contact=v_contact, p_internal_w=p_int,
                part_currents=pcs, v_eff=v_eff,
                component=t.component, comment=t.comment))
        else:
            v = Vflat[nodes]
            n_k = len(nodes)
            if t.bonded:
                # split by the network through the external bond
                pcs = [(pl, -part_flux_out(pn)) for pl, pn in pnodes]
            else:
                pcs = [(pl, t.i_draw_a * len(pn) / max(n_k, 1))
                       for pl, pn in pnodes]
            loads.append(LoadReport(
                label=t.label, i_a=t.i_draw_a,
                v_mean=float(v.mean()), v_min=float(v.min()),
                p_w=t.i_draw_a * float(v.mean()), part_currents=pcs,
                component=t.component, comment=t.comment))
    return supplies, loads


def _pdn_balance(supplies: list, loads: list, P_layers: list,
                 P_vias: float) -> tuple[float, float, float, float, float]:
    """Generalized power balance (Tellegen): source power = copper
    dissipation + R_out dissipation + load power, exactly, independent
    of the voltage reference (supply and load currents sum equal).
    Returns (balance_rel, mismatch_rel, i_sup, i_loads, p_loads_rout)
    or raises SolverError; a zero-power probe run (no draws, equal
    v_oc) has nothing to balance and reports 0."""
    i_loads = sum(l.i_a for l in loads)
    i_sup = sum(s_.i_a for s_ in supplies)
    p_rout = sum(s_.p_internal_w for s_ in supplies)
    p_loads = sum(l.p_w for l in loads)
    p_src = sum(s_.v_oc * s_.i_a for s_ in supplies)
    # gross scale, not |net|: with circulating currents between supplies
    # the net source power nearly cancels while watts really flow - the
    # residual must be judged against what flows, or the check trips on
    # pure floating-point cancellation
    p_gross = sum(abs(s_.v_oc * s_.i_a) for s_ in supplies)
    p_sink = sum(P_layers) + P_vias + p_rout + p_loads
    vocs = [s_.v_oc for s_ in supplies]
    if i_loads == 0.0 and max(vocs) == min(vocs):
        balance = 0.0
    else:
        balance = abs(p_src - p_sink) / max(p_gross, 1e-300)
        if not np.isfinite(balance) or balance > 1e-3:
            raise SolverError(
                f"Inconsistent PDN solve: power-balance error "
                f"{balance:.2e} (sources {p_src:.6g} W vs copper + "
                f"R_out + loads {p_sink:.6g} W). The result is not "
                f"trustworthy - try a different grid size."
            )
    i_scale = max(abs(i_loads),
                  max((abs(s_.i_a) for s_ in supplies), default=0.0),
                  1e-300)
    mismatch = abs(i_sup - i_loads) / i_scale
    return balance, mismatch, i_sup, i_loads, p_loads


def _pdn_pairs(terminals: list, term_nodes: list, attaches: list,
               merge, state_base: np.ndarray, edges: Edges,
               supplies: list, loads: list, make_solver) -> list:
    """Effective copper resistance between every supply and every load,
    plus the proportional-sharing loss allocation (see PairReport).

    The pair network is the COPPER alone - attachment resistances and
    Thevenin sources stripped. Contact patterns mirror the solve's
    models: loads and resistive supplies inject uniformly per cell,
    bonded terminals and ideal supplies are equipotential super-nodes.
    R_ij = (p_i - p_j)^T L_g^{-1} (p_i - p_j) costs ONE extra solve per
    terminal (same factorization; one node per connected component is
    grounded - the balanced quadratic form is ground-independent).
    Pairs without a common copper component get r_ohm None and no
    allocation; the allocation splits each component's copper loss by
    f_ij = I_i * I_j / I_loads_of_that_component, which sums exactly
    to the total copper dissipation.

    make_solver(state_g, dirichlet_v, edges_pm, pmerge) -> solve(inj)
    -> V lets the adaptive path plug in its deferred-correction loop
    (pmerge is the pair system's own node-merge map, or None)."""
    n_base = state_base.size
    idx0 = np.arange(n_base, dtype=np.int64)
    # pair-system merge: the solve's bonded lugs plus every ideal
    # supply contact (a Dirichlet region is equipotential too)
    pmerge = idx0.copy() if merge is None else merge[:n_base].copy()
    for t, at, nodes in zip(terminals, attaches, term_nodes):
        if (t.role == "supply" and at is not None and at.ideal
                and not t.bonded and len(nodes)):
            pmerge[nodes] = pmerge[int(nodes[0])]
    if bool((pmerge == idx0).all()):
        pmerge = None

    state_g = state_base.copy()
    if pmerge is not None:
        state_g[pmerge != idx0] = 0
        edges_pm = Edges(a=pmerge[edges.a], b=pmerge[edges.b],
                         w=edges.w, via_index=edges.via_index,
                         dead_barrels=edges.dead_barrels)
    else:
        edges_pm = edges

    pats = []
    for t, at, nodes in zip(terminals, attaches, term_nodes):
        if len(nodes) == 0:
            pats.append(None)
            continue
        p = np.zeros(n_base)
        if (t.bonded or (t.role == "supply" and at is not None
                         and at.ideal)):
            rep = (int(pmerge[nodes[0]]) if pmerge is not None
                   else int(nodes[0]))
            p[rep] = 1.0
        else:
            p[nodes] = 1.0 / len(nodes)
        pats.append(p)

    graph = sparse.coo_matrix(
        (np.ones(len(edges_pm.a)), (edges_pm.a, edges_pm.b)),
        shape=(n_base, n_base))
    _, labels = csgraph.connected_components(graph, directed=False)
    tcomp = []
    for p in pats:
        if p is None:
            tcomp.append(None)
            continue
        cs = set(labels[np.flatnonzero(p)].tolist())
        # a terminal spread over several components has no single
        # pair resistance - its pairs report "no path"
        tcomp.append(cs.pop() if len(cs) == 1 else None)

    dv = np.zeros(n_base)
    grounds: set = set()
    for c, p in zip(tcomp, pats):
        if c is not None and c not in grounds:
            state_g[int(np.flatnonzero(p)[0])] = 2
            grounds.add(c)
    if not grounds:
        return []

    progress.stage("source-sink pair resistances ...")
    solve = make_solver(state_g, dv, edges_pm, pmerge)
    us = [solve(p) if p is not None and c is not None else None
          for p, c in zip(pats, tcomp)]

    # per-component load current: proportional sharing never crosses a
    # copper gap (nothing flows between disconnected sheets)
    comp_load_i: dict = {}
    lidx = -1
    for j, tj in enumerate(terminals):
        if tj.role != "load":
            continue
        lidx += 1
        if tcomp[j] is not None:
            comp_load_i[tcomp[j]] = (comp_load_i.get(tcomp[j], 0.0)
                                     + loads[lidx].i_a)

    rows: list = []
    sidx = -1
    for i, ti in enumerate(terminals):
        if ti.role != "supply":
            continue
        sidx += 1
        s = supplies[sidx]
        lidx = -1
        for j, tj in enumerate(terminals):
            if tj.role != "load":
                continue
            lidx += 1
            ld = loads[lidx]
            same = tcomp[i] is not None and tcomp[i] == tcomp[j]
            r = None
            f = 0.0
            if same:
                r = float(pats[i] @ us[i] + pats[j] @ us[j]
                          - 2.0 * (pats[i] @ us[j]))
                i_tot = comp_load_i.get(tcomp[i], 0.0)
                if i_tot > 0.0:
                    f = s.i_a * ld.i_a / i_tot
            rows.append(PairReport(
                supply=s.label, load=ld.label, r_ohm=r,
                i_share_a=f, p_w=f * (s.v_eff - ld.v_mean)))
    return rows


def run_solve_pdn(problem: Problem, stack: RasterStack, term_masks: list,
                  term_parts: list, freq_hz: float = 0.0,
                  v_nominal: float | None = None) -> Result:
    """PDN solve on the uniform grid (adaptive dispatch on top, like
    run_solve). term_masks/term_parts come from raster.terminal_masks /
    terminal_partition, aligned with problem.terminals. Contact models
    are fixed: Thevenin supplies, uniform-injection loads."""
    if v_nominal is None:
        v_nominal = config.PDN_V_NOMINAL
    terminals = problem.terminals
    _label_terminals(terminals)
    _validate_terminals(terminals)
    if config.ADAPTIVE_CELLS:
        from . import adaptive
        return adaptive.run_solve_adaptive_pdn(problem, stack, term_masks,
                                               term_parts, freq_hz,
                                               v_nominal)

    timings = {}
    sigmas, rs_ratios, via_factor, sigma_buildup = \
        _conductance_params(problem, stack, freq_hz)

    t0 = time.perf_counter()
    edges = build_edges(stack, problem, sigmas, via_factor, sigma_buildup)
    changed, _n_kept = connected_restrict_multi(stack, term_masks,
                                                terminals, edges)
    if changed:
        edges = build_edges(stack, problem, sigmas, via_factor,
                            sigma_buildup)
    if edges.dead_barrels:
        print(f"warning: {edges.dead_barrels} via/pad barrel(s) found fill "
              f"copper on fewer than 2 layers and carry no current (pad "
              f"copper is not modeled; a finer grid may pick up thermal "
              f"spokes)")
    if stack.buildup is not None:
        stack.buildup &= stack.masks
    if stack.chain is not None:
        stack.chain &= stack.masks
    for t, parts in zip(terminals, term_parts):
        for label, m in parts:
            had = bool(m.any())
            m &= stack.masks
            if had and not m.any():
                print(f"warning: contact part '{label}' of {t.role} "
                      f"'{t.label}' only touches disconnected copper - "
                      f"it carries no current")
    timings["edges_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    state = np.zeros(stack.masks.size, dtype=np.uint8)
    state[stack.masks.ravel()] = 1
    term_nodes = [np.flatnonzero(m.ravel()) for m in term_masks]
    state_base = state.copy()       # copper-only state for _pdn_pairs
    state, dirichlet_v, inj, edges_ext, attaches, merge = _pdn_attach(
        terminals, term_nodes, state, edges, v_nominal)
    A, rhs, _ = _assemble(state, _pdn_solve_edges(edges_ext, merge),
                          inj, dirichlet_v)
    x, info = solve_system(A, rhs)
    Vflat = np.where(state >= 2, dirichlet_v, 0.0)
    Vflat[state == 1] = x
    if merge is not None:
        Vflat = Vflat[merge]        # bonded members read their lug
    timings["solve_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    Pe, Ie, P_layers, P_vias, Parea, via_reports, V3, J3 = \
        _postprocess_fields(problem, stack, edges_ext, Vflat, 1.0, sigmas,
                            sigma_buildup)
    term_part_nodes = [
        [(pl, np.flatnonzero(m.ravel())) for pl, m in parts]
        for parts in term_parts]
    supplies, loads = _pdn_extract(terminals, term_nodes, attaches, Vflat,
                                   Ie, edges_ext, term_part_nodes)
    balance, mismatch, i_sup, i_loads, p_loads = _pdn_balance(
        supplies, loads, P_layers, P_vias)
    timings["postprocess_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()

    def _pair_solver(state_g, dv, edges_pm, pmerge):
        A2, rhs0p, _ = _assemble(state_g, edges_pm, None, dv)
        ps2 = PreparedSolver(A2)
        freeg = state_g == 1

        def slv(inj_p):
            x2, _ = ps2.solve(rhs0p + inj_p[freeg])
            V = np.where(state_g >= 2, dv, 0.0)
            V[freeg] = x2
            return V
        return slv

    pairs = _pdn_pairs(terminals, term_nodes, attaches, merge,
                       state_base, edges, supplies, loads, _pair_solver)
    timings["pairs_s"] = time.perf_counter() - t0

    return Result(
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
