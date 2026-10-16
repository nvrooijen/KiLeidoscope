"""The board's area as a coverage image: 1 inside the Edge.Cuts outline, 0 outside.

Copper outside the board is see-through (holes.clip_to_board): a fab mills it away, as
for castellated pads and edge fingers drawn past the edge. The outline is filled
even-odd, as the board solid is (nodes.board), so cutouts count as outside.

Coverage is exact along each pixel row and sampled `SUBROWS` times across it, so the
filtered image crosses 0.5 on the outline itself and the shader's ramp puts a sharp
edge there (shading.sharp_alpha). Pure numpy, tested without Blender.
"""

import numpy as np

SUBROWS = 4  # samples across each pixel row
CHUNK_ROWS = 64  # pixel rows filled at once (bounds the crossing arrays)


def coverage(bounds, pixel, shape, a, b, subrows=SUBROWS):
    """(height, width) float32 coverage of the area inside edges a -> b (even-odd).

    bounds: (xmin, ymin, ...) of the image's lower left corner; row r, column c covers
    x in xmin + [c, c + 1) * pixel and y in ymin + [r, r + 1) * pixel. a, b: (n, 2)
    edge ends in the same units as bounds and pixel.
    """
    height, width = shape
    out = np.zeros((height, width), np.float32)
    a = np.asarray(a, np.float64).reshape(-1, 2)
    b = np.asarray(b, np.float64).reshape(-1, 2)
    if not len(a) or width <= 0 or height <= 0:
        return out
    ax, ay = (a[:, 0] - bounds[0]) / pixel, (a[:, 1] - bounds[1]) / pixel
    bx, by = (b[:, 0] - bounds[0]) / pixel, (b[:, 1] - bounds[1]) / pixel
    sloped = ay != by  # a level edge never crosses a sample row
    ax, ay, bx, by = ax[sloped], ay[sloped], bx[sloped], by[sloped]
    low, high = np.minimum(ay, by), np.maximum(ay, by)
    slope = (bx - ax) / (by - ay)
    for first in range(0, height, CHUNK_ROWS):
        rows = min(CHUNK_ROWS, height - first)
        out[first:first + rows] = _rows(first, rows, width, subrows, ax, ay, low, high, slope)
    return out


def _rows(first, rows, width, subrows, ax, ay, low, high, slope):
    """Coverage of pixel rows first .. first + rows."""
    y = first + (np.arange(rows * subrows) + 0.5) / subrows  # sample heights, in pixels
    crossed = (low <= y[:, None]) & (y[:, None] < high)  # half-open: a vertex counts once
    sample, edge = np.nonzero(crossed)
    acc = np.zeros((rows * subrows, width + 2), np.float64)
    if len(sample):
        x = ax[edge] + (y[sample] - ay[edge]) * slope[edge]
        order = np.lexsort((x, sample))
        sample, x = sample[order], x[order]
        starts = np.r_[0, np.flatnonzero(np.diff(sample)) + 1]
        counts = np.diff(np.r_[starts, len(sample)])
        rank = np.arange(len(sample)) - np.repeat(starts, counts)
        # An unclosed ring leaves a sample row one crossing over: its last one is dropped.
        keep = ~((np.repeat(counts, counts) % 2 == 1) & (rank == np.repeat(counts, counts) - 1))
        sample, x, rank = sample[keep], x[keep], rank[keep]
        opening = rank % 2 == 0
        x0, x1, at = np.clip(x[opening], 0, width), np.clip(x[~opening], 0, width), sample[opening]
        spans = x1 > x0
        x0, x1, at = x0[spans], x1[spans], at[spans]
        # Each span adds its exact overlap with every pixel once summed along the row.
        i0, i1 = np.floor(x0).astype(np.int64), np.floor(x1).astype(np.int64)
        f0, f1 = x0 - i0, x1 - i1
        np.add.at(acc, (at, i0), 1.0 - f0)
        np.add.at(acc, (at, i0 + 1), f0)
        np.add.at(acc, (at, i1), f1 - 1.0)
        np.add.at(acc, (at, i1 + 1), -f1)
    covered = np.cumsum(acc, axis=1)[:, :width]
    return np.clip(covered.reshape(rows, subrows, width).mean(axis=1), 0.0, 1.0)
