# Vendored from Fill Resistance 1.4.2, fill_resistance/quadtree.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Unchanged apart from this header.
"""Adaptive quadtree grid engine (phase 1 of the adaptive-cell work).

Decomposes a layer's fine copper mask into a 2:1-BALANCED set of square
leaves (power-of-two sizes in fine-cell units): cells at copper
boundaries and in keep_fine regions stay at the fine size, interiors
coarsen with their Chebyshev distance to the nearest feature (guard
factor), and an explicit enforcement pass splits any leaf more than
twice the size of an edge-adjacent neighbor.

The uniform limit (max_block=1) reproduces the fine grid EXACTLY: one
leaf per copper cell and face conductance sigma - the production
solver's grid is the special case, which keeps the exact-value test
suite authoritative for this engine.

Face conductance between edge-adjacent leaves a, b sharing w fine-cell
faces is the series-half-cell expression

    g = w / (size_a / (2 sigma_a) + size_b / (2 sigma_b))

which reduces to the harmonic mean 2 sigma_a sigma_b / (sigma_a +
sigma_b) for equal sizes (the production buildup/mouth face rule) and
to sigma in the uniform limit.

Phase 2 (not here) wires this into the pipeline: electrodes, barrels,
1D chains, buildup and field output still run on the uniform grid.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage


@dataclass
class LeafGrid:
    """Square leaves over one layer, in fine-cell units."""
    y0: np.ndarray                # int32, aligned: y0 % size == 0
    x0: np.ndarray
    size: np.ndarray              # int32, power of two
    id_grid: np.ndarray           # (ny, nx) int32; -1 = not copper

    @property
    def n(self) -> int:
        return len(self.size)


def build_leaves(mask: np.ndarray, keep_fine: np.ndarray | None = None,
                 max_block: int = 32, guard: int = 4) -> LeafGrid:
    """Balanced leaf decomposition of a boolean copper mask. keep_fine
    marks cells that must stay at the fine size (electrodes, and later
    via mouths / buildup edges). guard scales how much clearance a block
    of size s needs (Chebyshev distance >= guard * s)."""
    ny, nx = mask.shape
    if keep_fine is None:
        keep_fine = np.zeros_like(mask)
    coarsenable = mask & ~keep_fine

    # Chebyshev distance to the nearest non-coarsenable cell (array
    # border padded as background so edges never look like interior)
    pad = np.pad(coarsenable, 1)
    D = ndimage.distance_transform_cdt(pad, metric="chessboard")[1:-1, 1:-1]

    S = np.zeros((ny, nx), dtype=np.int32)
    S[mask] = 1
    s = 2
    while s <= max_block:
        S[D >= guard * s] = s
        s *= 2

    grid = _emit(S, mask, max_block)
    for _ in range(32):
        if _split_unbalanced(grid, S):
            grid = _emit(S, mask, max_block)
        else:
            return grid
    raise RuntimeError("quadtree balance did not converge")


def _emit(S: np.ndarray, mask: np.ndarray, max_block: int) -> LeafGrid:
    """Greedy top-down emission: an aligned s-block becomes a leaf where
    every cell allows size s and nothing larger claimed it."""
    ny, nx = mask.shape
    py, px = (-ny) % max_block, (-nx) % max_block
    Sp = np.pad(S, ((0, py), (0, px)))
    NY, NX = Sp.shape
    id_grid = np.full((NY, NX), -1, dtype=np.int32)
    covered = np.zeros((NY, NX), dtype=bool)
    y0s, x0s, sizes = [], [], []
    nid = 0
    s = max_block
    while s >= 2:
        min_S = Sp.reshape(NY // s, s, NX // s, s).min(axis=(1, 3))
        free = ~covered.reshape(NY // s, s, NX // s, s).any(axis=(1, 3))
        cand = (min_S >= s) & free
        k = int(cand.sum())
        if k:
            lvl = np.full(cand.shape, -1, dtype=np.int32)
            lvl[cand] = nid + np.arange(k, dtype=np.int32)
            up = np.repeat(np.repeat(lvl, s, axis=0), s, axis=1)
            sel = up >= 0
            id_grid[sel] = up[sel]
            covered |= sel
            ii, jj = np.nonzero(cand)
            y0s.append(ii.astype(np.int32) * s)
            x0s.append(jj.astype(np.int32) * s)
            sizes.append(np.full(k, s, dtype=np.int32))
            nid += k
        s //= 2
    fi, fj = np.nonzero(np.pad(mask, ((0, py), (0, px))) & ~covered)
    id_grid[fi, fj] = nid + np.arange(len(fi), dtype=np.int32)
    y0s.append(fi.astype(np.int32))
    x0s.append(fj.astype(np.int32))
    sizes.append(np.ones(len(fi), dtype=np.int32))
    return LeafGrid(
        y0=np.concatenate(y0s) if y0s else np.zeros(0, np.int32),
        x0=np.concatenate(x0s) if x0s else np.zeros(0, np.int32),
        size=np.concatenate(sizes) if sizes else np.zeros(0, np.int32),
        id_grid=id_grid[:ny, :nx],
    )


def _split_unbalanced(grid: LeafGrid, S: np.ndarray) -> bool:
    """Cap S over any leaf more than 2x an edge-adjacent neighbor, so the
    next emission splits it. Returns True if anything was capped."""
    ia, ib, _, _ = leaf_faces(grid)
    sa, sb = grid.size[ia], grid.size[ib]
    big = np.unique(np.concatenate([ia[sa > 2 * sb], ib[sb > 2 * sa]]))
    for lid in big:
        y, x, s = int(grid.y0[lid]), int(grid.x0[lid]), int(grid.size[lid])
        np.minimum(S[y:y + s, x:x + s], s // 2, out=S[y:y + s, x:x + s])
    return len(big) > 0


def leaf_faces(grid: LeafGrid) -> tuple[np.ndarray, np.ndarray, np.ndarray,
                                        np.ndarray]:
    """(ia, ib, w, axis) for every edge-adjacent leaf pair, each pair
    once; w = shared face length in fine-cell units. axis 0 = x-faces
    (a left of b), axis 1 = y-faces (a above b)."""
    n = grid.n
    if n == 0:
        z = np.zeros(0, dtype=np.int64)
        return z, z, z, z
    out = []
    for axis, (sl_a, sl_b) in enumerate(((np.s_[:, :-1], np.s_[:, 1:]),
                                         (np.s_[:-1, :], np.s_[1:, :]))):
        a = grid.id_grid[sl_a].ravel().astype(np.int64)
        b = grid.id_grid[sl_b].ravel().astype(np.int64)
        ok = (a >= 0) & (b >= 0) & (a != b)
        key, counts = np.unique(a[ok] * n + b[ok], return_counts=True)
        out.append((key // n, key % n, counts,
                    np.full(len(key), axis, dtype=np.int8)))
    ia = np.concatenate([o[0] for o in out])
    ib = np.concatenate([o[1] for o in out])
    w = np.concatenate([o[2] for o in out])
    ax = np.concatenate([o[3] for o in out])
    return ia, ib, w, ax


def leaf_edges(grid: LeafGrid, sigma) -> tuple[np.ndarray, np.ndarray,
                                               np.ndarray]:
    """Face conductances [S]: sigma is a scalar or per-leaf array of
    sheet conductance. Uniform limit -> exactly sigma per face."""
    ia, ib, w, _ = leaf_faces(grid)
    sig = np.broadcast_to(np.asarray(sigma, dtype=float), (grid.n,))
    g = w / (grid.size[ia] / (2.0 * sig[ia])
             + grid.size[ib] / (2.0 * sig[ib]))
    return ia, ib, g


def balanced(grid: LeafGrid) -> bool:
    """2:1 balance invariant: edge-adjacent leaves differ <= 2x in size."""
    ia, ib, _, _ = leaf_faces(grid)
    sa, sb = grid.size[ia], grid.size[ib]
    return bool((np.maximum(sa, sb) <= 2 * np.minimum(sa, sb)).all())
