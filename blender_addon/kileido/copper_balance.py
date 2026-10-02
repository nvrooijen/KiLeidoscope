"""Copper balance: copper % per copper layer, a tiled density map and the check
between mirrored layers (numpy only, no bpy).

Fabs press and plate a board as a stack: copper spread unevenly between mirrored
layers (L1 and Ln, L2 and Ln-1, ...) warps the board, and dense or sparse areas
plate unevenly. The analysis reads the copper frames the add-on already receives
(tracks, pads, copper graphics, zones and vias; `CopperFrames`) and draws each
layer with the overlay renderer's scanline pass (gerber.py, C library or numpy):
the nonzero winding rule over every shape gives their union, so overlapping
copper counts once. Drill holes are cut out; only copper inside the board outline
counts, and percentages are of the outline's area (cutouts excluded).

Pixels are whole fractions of a tile, so tile sums are exact image sums.
"""

import math
from dataclasses import dataclass

import numpy as np

from . import gerber

TILE_MM = 5.0
THRESHOLD_PCT = 15.0  # percentage points between mirrored layers
MAX_PIXELS = 2048  # long side of a layer image, roughly: pixels are whole fractions of a tile
FINEST_NM = 25_000  # no finer pixels than this, even on a small board
MIN_BOARD_FRACTION = 0.25  # tiles with less board than this (edge slivers) are never "worst"
WORST_TILES = 3  # per mirrored pair
CHORD_NM = 250  # chord error of sampled circles: polygons inside a circle lose area, 0.3 % of a 0.2 mm drill
KINDS = ("tracks", "pads", "graphics", "zones")  # per-layer copper frames; vias and the outline come alone


@dataclass(frozen=True)
class Grid:
    """Tiles from the outline's top-left corner (KiCad nm, y down); `per_tile` pixels along a tile."""
    x0: float
    y0: float
    tile_nm: float
    columns: int
    rows: int
    per_tile: int

    @property
    def pixel_nm(self):
        return self.tile_nm / self.per_tile

    @property
    def shape(self):
        return self.rows * self.per_tile, self.columns * self.per_tile

    def tile_center_nm(self, row, column):
        return self.x0 + (column + 0.5) * self.tile_nm, self.y0 + (row + 0.5) * self.tile_nm


@dataclass(frozen=True)
class Layer:
    name: str
    percent: float
    copper_mm2: float
    density: np.ndarray  # (rows, columns): copper % of each tile's board area, nan without board


@dataclass(frozen=True)
class Tile:
    row: int
    column: int
    center_nm: tuple[float, float]  # KiCad board coordinates
    top_pct: float
    bottom_pct: float

    @property
    def difference(self):
        return abs(self.top_pct - self.bottom_pct)


@dataclass(frozen=True)
class Pair:
    top: str
    bottom: str
    difference: float  # percentage points
    flagged: bool
    worst: tuple[Tile, ...]


@dataclass(frozen=True)
class Result:
    grid: Grid
    board_mm2: float
    order: tuple[str, ...]  # copper layers top to bottom
    layers: dict  # name -> Layer
    board_fraction: np.ndarray  # (rows, columns): how much of each tile is board
    mask: np.ndarray  # board coverage at up to 8 pixels per tile, for the heatmap's edges


# --- The frames ---------------------------------------------------------------------------------

class CopperFrames:
    """The copper frames seen so far, as received (decoded arrays are read-only views,
    safe to hand to a worker thread). A snapshot starts over; a live edit replaces its
    (layer, kind) group, as the viewer does."""

    def __init__(self):
        self.groups = {}  # (layer, kind) -> (header, arrays)
        self.heights = {}  # copper layer -> z (m), from the board frame
        self.version = 0

    def observe(self, header, arrays) -> bool:
        """Keep a frame if it carries copper or the outline; True when it did."""
        kind = header.get("type")
        if kind == "snapshot_begin":
            self.groups.clear()
        elif kind == "board":
            self.heights = {entry["name"]: float(entry["z_m"]) for entry in header.get("layers", ())
                            if entry.get("copper", True)}
        elif kind == "layer_data" and header.get("kind") in (*KINDS, "vias", "outline"):
            self.groups[(header.get("layer", ""), header["kind"])] = (header, arrays)
        else:
            return False
        self.version += 1
        return True

    def inputs(self):
        """What `analyze` reads, detached from later frames."""
        return dict(self.groups), dict(self.heights)


def layer_order(names, heights=None):
    """Copper layers top to bottom: by stackup height, else F.Cu, In1.Cu, ..., B.Cu."""
    heights = heights or {}

    def key(name):
        if name in heights:
            return 0, -heights[name]
        if name == "F.Cu":
            return 1, -1e9
        if name == "B.Cu":
            return 1, 1e9
        digits = "".join(c for c in name if c.isdigit())
        return 1, int(digits) if digits else 0
    return tuple(sorted(names, key=key))


# --- Shapes ------------------------------------------------------------------------------------

def _rings(arrays):
    """(contours, is_hole) of a ring frame (pads, graphics, zones, outline)."""
    points = np.asarray(arrays["points"], np.float64)
    starts = arrays["ring_start"]
    contours = [points[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]
    return contours, [bool(h) for h in arrays["ring_hole"]]


def _nested(contours):
    """Which rings lie inside an odd number of others: the board outline's cutouts. Each
    closed Edge.Cuts shape arrives as its own polygon and the viewer fills them even-odd."""
    odd = []
    for index, contour in enumerate(contours):
        px, py = contour[0]
        inside = 0
        for other, ring in enumerate(contours):
            if other == index or len(ring) < 3:
                continue
            (x1, y1), (x2, y2) = ring.T, np.roll(ring, -1, axis=0).T
            straddles = (y1 > py) != (y2 > py)
            with np.errstate(divide="ignore", invalid="ignore"):
                crossing = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            inside += int(np.count_nonzero(straddles & (px < crossing)) % 2)
        odd.append(inside % 2 == 1)
    return odd


def _capsules(seg, tolerance):
    """Tracks (x1, y1, x2, y2, width) as round-ended stroke outlines, one width at a time."""
    seg = np.asarray(seg, np.float64).reshape(-1, 5)
    contours = []
    for width in np.unique(seg[:, 4]):
        rows = seg[seg[:, 4] == width, :4]
        contours.extend(gerber._capsules(rows, width / 2, tolerance))
    return contours


def _disks(centres, diameters, tolerance):
    contours = []
    for diameter in np.unique(diameters):
        circle = gerber._circle(0.0, 0.0, diameter / 2, tolerance)
        contours.extend(circle + centre for centre in centres[diameters == diameter])
    return contours


def _drill(row, angle, oval, tolerance):
    """A pad's hole: a circle, or a slot of its width and height turned by `angle`.
    The angle is Blender's (y up), so it turns the other way in KiCad's frame."""
    x, y, width, height = (float(v) for v in row)
    if not oval:
        return gerber._circle(x, y, min(width, height) / 2, tolerance)
    shape = gerber._aperture_shape("O", [width, height], tolerance)
    c, s = math.cos(-angle), math.sin(-angle)
    return shape @ np.array([[c, s], [-s, c]]) + (x, y)


def _edges(contours, holes, grid):
    """Pixel-space edges (n, 4); solid contours one way round and holes the other, so
    the nonzero rule fills the union of the solids less each one's own holes."""
    keep = [(c, h) for c, h in zip(contours, holes) if len(c) >= 3]
    if not keep:
        return np.empty((0, 4))
    sizes = np.array([len(c) for c, _ in keep])
    points = np.concatenate([c for c, _ in keep]).astype(np.float64)
    points = (points - (grid.x0, grid.y0)) / grid.pixel_nm  # y down: row 0 on top
    ends = np.cumsum(sizes)
    following = np.arange(1, len(points) + 1)
    following[ends - 1] = ends - sizes
    x, y = points[:, 0], points[:, 1]
    twice_area = np.add.reduceat(x * y[following] - x[following] * y, ends - sizes)
    wanted = np.where([h for _, h in keep], -1.0, 1.0)
    flip = np.repeat(twice_area * wanted < 0, sizes)
    edges = np.column_stack((points, points[following]))
    edges[flip] = edges[flip][:, [2, 3, 0, 1]]
    return edges


def _coverage(contours, holes, grid):
    height, width = grid.shape
    edges = _edges(contours, holes, grid)
    if not len(edges):
        return np.zeros((height, width), np.float32)
    return gerber._coverage(edges, width, height)


# --- Analysis ----------------------------------------------------------------------------------

def make_grid(bounds, tile_nm, max_pixels=MAX_PIXELS):
    """Tiles over `bounds` (xmin, ymin, xmax, ymax nm), the last row and column running past."""
    xmin, ymin, xmax, ymax = bounds
    columns = max(1, math.ceil((xmax - xmin) / tile_nm - 1e-9))
    rows = max(1, math.ceil((ymax - ymin) / tile_nm - 1e-9))
    target = max(FINEST_NM, max(xmax - xmin, ymax - ymin) / max_pixels)
    return Grid(float(xmin), float(ymin), float(tile_nm), columns, rows, max(1, math.ceil(tile_nm / target)))


def _tile_sums(image, grid):
    k = grid.per_tile
    return image.reshape(grid.rows, k, grid.columns, k).sum(axis=(1, 3), dtype=np.float64)


def analyze(groups, heights=None, tile_mm=TILE_MM):
    """Copper per layer over the board outline. `groups` as CopperFrames.inputs gives
    them: (layer, kind) -> (header, arrays). Raises ValueError without an outline."""
    outline = groups.get(("", "outline"))
    contours = _rings(outline[1])[0] if outline else []
    holes = _nested(contours)
    solid = [c for c, h in zip(contours, holes) if not h and len(c) >= 3]
    if not solid:
        raise ValueError("no board outline")
    stacked = np.concatenate(solid)
    # The numpy fallback takes ~12x as long as the C pass: half the resolution each way.
    max_pixels = MAX_PIXELS if gerber._native_coverage is not None else MAX_PIXELS // 2
    grid = make_grid((*stacked.min(axis=0), *stacked.max(axis=0)), tile_mm * 1e6, max_pixels)
    pixel_mm2 = (grid.pixel_nm * 1e-6) ** 2

    board = _coverage(contours, holes, grid)
    board_tiles = _tile_sums(board, grid)
    board_mm2 = float(board_tiles.sum()) * pixel_mm2
    if board_mm2 <= 0:
        raise ValueError("board outline has no area")

    names = {layer for layer, kind in groups if kind in KINDS} | set(heights or ())
    vias = groups.get(("", "vias"))
    if vias is not None:
        names |= set(vias[0].get("layers", ()))
    order = layer_order(names, heights)
    position = {name: index for index, name in enumerate(order)}

    # Pad holes go through every layer (each pad frame repeats them; keep one of each).
    drills = {}
    for (layer, kind), (_, arrays) in groups.items():
        if kind == "pads" and "drill" in arrays:
            for row, angle, oval in zip(arrays["drill"], arrays["drill_angle"], arrays["drill_oval"]):
                drills[(*(int(v) for v in row), float(angle), int(oval))] = (row, float(angle), bool(oval))
    pad_holes = [_drill(row, angle, oval, CHORD_NM) for row, angle, oval in drills.values()]

    layers = {}
    for name in order:
        copper, flags = [], []
        for kind in KINDS:
            entry = groups.get((name, kind))
            if entry is None:
                continue
            if kind == "tracks":
                shapes = _capsules(entry[1]["seg"], CHORD_NM)
                copper += shapes
                flags += [False] * len(shapes)
            else:
                rings, ring_holes = _rings(entry[1])
                copper += rings
                flags += ring_holes
        cut = list(pad_holes)
        if vias is not None and len(vias[1]["via"]):
            via, span = vias[1]["via"], vias[1]["span"]
            via_layers = vias[0]["layers"]
            ends = np.array([[position.get(via_layers[a], -1), position.get(via_layers[b], -1)] for a, b in span])
            here = (ends.min(axis=1) <= position[name]) & (position[name] <= ends.max(axis=1)) & (ends.min(axis=1) >= 0)
            centres = via[here, :2].astype(np.float64)
            lands = _disks(centres, via[here, 2].astype(np.float64), CHORD_NM)
            copper += lands
            flags += [False] * len(lands)
            cut += _disks(centres, via[here, 3].astype(np.float64), CHORD_NM)
        image = _coverage(copper, flags, grid)
        if cut:
            image *= 1.0 - _coverage(cut, [False] * len(cut), grid)
        image *= board
        tiles = _tile_sums(image, grid)
        with np.errstate(divide="ignore", invalid="ignore"):
            density = np.where(board_tiles > 0, 100.0 * tiles / board_tiles, np.nan)
        copper_mm2 = float(tiles.sum()) * pixel_mm2
        layers[name] = Layer(name, 100.0 * copper_mm2 / board_mm2, copper_mm2, density)

    k = grid.per_tile
    step = next(s for s in range(max(1, k // 8), k + 1) if k % s == 0)  # about 8 mask pixels per tile
    height, width = grid.shape
    mask = board.reshape(height // step, step, width // step, step).mean(axis=(1, 3), dtype=np.float32)
    return Result(grid, board_mm2, order, layers, board_tiles / grid.per_tile ** 2, mask)


def pairs(result, threshold_pct=THRESHOLD_PCT, worst=WORST_TILES):
    """Mirrored layer pairs (L1 and Ln, L2 and Ln-1, ...), flagged when their copper
    differs by more than `threshold_pct` percentage points, each with its most
    imbalanced tiles (edge slivers left out)."""
    order, found = result.order, []
    usable = result.board_fraction >= MIN_BOARD_FRACTION
    for index in range(len(order) // 2):
        top, bottom = result.layers[order[index]], result.layers[order[-1 - index]]
        difference = abs(top.percent - bottom.percent)
        gap = np.where(usable, np.abs(top.density - bottom.density), -1.0)
        tiles = []
        for flat in np.argsort(gap, axis=None, kind="stable")[::-1][:worst]:
            row, column = divmod(int(flat), result.grid.columns)
            if gap[row, column] <= 0:
                break
            tiles.append(Tile(row, column, result.grid.tile_center_nm(row, column),
                              float(top.density[row, column]), float(bottom.density[row, column])))
        found.append(Pair(top.name, bottom.name, difference, difference > threshold_pct, tuple(tiles)))
    return found


# --- Heatmap ------------------------------------------------------------------------------------

# One hue, light (little copper) to dark (full copper), sRGB: a sequential blue ramp.
RAMP = np.array([(0xcd, 0xe2, 0xfb), (0xb7, 0xd3, 0xf6), (0x9e, 0xc5, 0xf4), (0x86, 0xb6, 0xef),
                 (0x6d, 0xa7, 0xec), (0x55, 0x98, 0xe7), (0x39, 0x87, 0xe5), (0x2a, 0x78, 0xd6),
                 (0x25, 0x6a, 0xbf), (0x1c, 0x5c, 0xab), (0x18, 0x4f, 0x95), (0x10, 0x42, 0x81),
                 (0x0d, 0x36, 0x6b)], np.float64) / 255


def ramp(percent):
    """sRGB colours (..., 3) for copper percentages 0..100."""
    position = np.clip(np.nan_to_num(np.asarray(percent, np.float64)) / 100, 0, 1) * (len(RAMP) - 1)
    low = np.minimum(np.floor(position).astype(int), len(RAMP) - 2)
    fraction = (position - low)[..., None]
    return RAMP[low] * (1 - fraction) + RAMP[low + 1] * fraction


def heatmap(result, layer, opacity=0.8):
    """RGBA image (row 0 on top) of a layer's tile densities, clear outside the board."""
    rows, columns = result.mask.shape
    scale = rows // result.grid.rows
    colours = ramp(result.layers[layer].density)
    image = np.empty((rows, columns, 4), np.float32)
    image[..., :3] = np.repeat(np.repeat(colours, scale, axis=0), scale, axis=1)
    image[..., 3] = result.mask * opacity
    return image
