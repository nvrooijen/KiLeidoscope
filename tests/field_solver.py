"""A 2D quasi-static finite-difference field solver for a trace on a dielectric over a
ground plane: odd mode of a coupled pair (electric wall at the pair's midline) or a
lone trace, with or without a coplanar ground `sg` from its outer edge. Zero-thickness
conductors on the interface. eps_eff = C(eps_r) / C(air), capacitance from field energy."""

import math

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve


def capacitance(w, h, gap, sg, eps_r, cells_per_h=24, far=14.0, odd=True):
    """Half-domain capacitance per unit length / eps0. Lengths in units of h."""
    d = h / cells_per_h
    x0 = gap / 2 if odd else 0.0  # the strip's inner edge (lone trace: the domain starts at its middle)
    inner, outer = x0, x0 + w if odd else w / 2
    width = outer + (sg if sg is not None else 0.0) + far * h
    nx, ny = int(round(width / d)) + 1, int(round((h + far * h) / d)) + 1
    jh = int(round(h / d))  # the interface row
    xs = np.arange(nx) * d
    fixed = np.full((ny, nx), np.nan)
    fixed[0, :] = 0.0  # ground plane
    fixed[-1, :] = 0.0  # far top
    fixed[:, -1] = 0.0  # far side
    if odd:
        fixed[:, 0] = 0.0  # electric wall: the pair's midline
    on_strip = (xs >= inner - 1e-9) & (xs <= outer + 1e-9)
    fixed[jh, on_strip] = 1.0
    if sg is not None:
        fixed[jh, xs >= outer + sg - 1e-9] = 0.0
    eps_row = np.where(np.arange(ny - 1) < jh, eps_r, 1.0)  # between row j and j+1
    eps_h = np.where(np.arange(ny) < jh, eps_r, np.where(np.arange(ny) == jh, (eps_r + 1) / 2, 1.0))  # along rows
    unknown = np.isnan(fixed)
    index = -np.ones((ny, nx), int)
    index[unknown] = np.arange(unknown.sum())
    rows, cols, vals, rhs = [], [], [], np.zeros(unknown.sum())
    jj, ii = np.nonzero(unknown)
    for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nj, ni = jj + dj, ii + di
        inside = (ni >= 0) & (ni < nx)
        if di == 0:
            eps = eps_row[np.minimum(jj, nj)]
        else:
            eps = eps_h[jj]
        if not odd and di == -1:
            mirrored = ni < 0  # the lone trace's middle: a magnetic wall, the field mirrors
            ni = np.where(mirrored, 1, ni)
            inside = np.ones_like(inside)
        k = index[jj, ii]
        rows.append(k[inside]); cols.append(k[inside]); vals.append(eps[inside])
        neighbour = index[nj[inside], ni[inside]]
        free = neighbour >= 0
        rows.append(k[inside][free]); cols.append(neighbour[free]); vals.append(-eps[inside][free])
        known = fixed[nj[inside], ni[inside]]
        np.add.at(rhs, k[inside][~free], eps[inside][~free] * known[~free])
    matrix = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                        shape=(len(rhs), len(rhs))).tocsr()
    phi = fixed.copy()
    phi[unknown] = spsolve(matrix, rhs)
    energy = (eps_row[:, None] * np.diff(phi, axis=0) ** 2).sum() + (eps_h[:, None] * np.diff(phi, axis=1) ** 2).sum()
    if not odd:
        energy -= 0.5 * (eps_h * (phi[:, 1] - phi[:, 0]) ** 2).sum()  # the column at the middle counts half
    return energy  # = C / eps0 for V = 1 (energy = C V^2 / 2, each edge's term carries the 1/2 already doubled)


def eps_eff(w, h, gap, sg, eps_r, **options):
    return capacitance(w, h, gap, sg, eps_r, **options) / capacitance(w, h, gap, sg, 1.0, **options)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"C:\Users\nickv\Documents\KiLeidoscope\.claude\worktrees\busy-aryabhata-59c1c1")
    from kileido_bridge import delays
    for w, gap in ((0.97, 0.84), (1.1, 1.24), (1.0, 0.5), (1.0, 3.0), (0.5, 0.5), (2.0, 1.0)):
        kj = delays.microstrip_epsilon(4.5, w, 1.0, gap)
        for cells in (16, 32):
            fd = eps_eff(w, 1.0, gap, None, 4.5, cells_per_h=cells)
            print(f"w/h {w} g/h {gap} cells {cells}: FD odd {fd:.4f}  K&J odd {kj:.4f}  diff {(fd / kj - 1) * 100:+.1f} %")
    for w in (0.5, 1.0, 2.0):
        fd = eps_eff(w, 1.0, 0.0, None, 4.5, odd=False, cells_per_h=32)
        hj = delays.microstrip_epsilon(4.5, w, 1.0)
        print(f"lone w/h {w}: FD {fd:.4f}  Hammerstad-Jensen {hj:.4f}  diff {(fd / hj - 1) * 100:+.1f} %")
