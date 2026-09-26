"""KiCad's Gerber plots drawn into antialiased coverage images (numpy only, no bpy).

Mask, silkscreen and drawing overlays come from Gerbers: KiCad's SVG/PDF/DXF
plotters carry ~3.5 s of fixed start-up cost that its Gerber plotter does not, and
drawing them here needs no extra tools. The export worker asks kicad-cli for
Gerbers with `--disable-aperture-macros` (rounded and rotated pads then arrive as
plain regions).

Only what KiCad 10 writes is supported: %FS/%MO, round/rectangle/obround/polygon
apertures, G01 lines and G02/G03 arcs (G75), D01/D02/D03, G36/G37 regions and
%LPD/%LPC polarity. Anything else raises `ValueError`, never a silent wrong image.

Drawing is a scanline pass over polygon edges with the nonzero winding rule:
16 sample rows per pixel, exact coverage along each row. Every stroke, flash and region contour is turned into a
counter-clockwise polygon first, so overlapping shapes add up instead of
cancelling. This is image generation only; no geometry is merged or meshed.
"""

import math
import re
import struct
import zlib

import numpy as np

DPI = 600  # 42.3 um per pixel ...
RESOLUTION = 2048  # ... with the long side capped at this many pixels
SAMPLES = 16  # sample rows per pixel: horizontal edges within 1/32 px; along a row coverage is exact

_BLOCK = re.compile(r"%([^%]*)%|([^%*]*)\*")
_COORD = re.compile(r"([XYIJ])([+-]?\d+)")
_APERTURE = re.compile(r"ADD(\d+)([A-Za-z_.$][\w.$]*)(?:,(.*))?$", re.S)


class Plot:
    """One Gerber file as polygons in nanometres (Gerber frame: y up)."""

    def __init__(self):
        self.groups = []  # [(dark, [contour (n, 2) float arrays])], in drawing order
        self.path_points = []  # centre-line points of D01/D02 (for Edge.Cuts bounds)

    def bounds(self, centre_line=False):
        """(xmin, ymin, xmax, ymax) in nm, of the drawn shapes or of the centre lines."""
        if centre_line:
            points = self.path_points
        else:
            points = [contour for _, contours in self.groups for contour in contours]
        if not points:
            return None
        stacked = np.concatenate([np.asarray(p, dtype=np.float64).reshape(-1, 2) for p in points])
        return (*stacked.min(axis=0), *stacked.max(axis=0))


def _circle(cx, cy, radius, tolerance, start=0.0, sweep=math.tau):
    """Points on a circle or arc, chord error <= tolerance, counter-clockwise for sweep > 0."""
    if radius <= 0:
        return np.array([[cx, cy]])
    step = 2 * math.acos(max(-1.0, 1 - min(tolerance / radius, 2)))
    count = max(8 if abs(sweep) >= math.tau - 1e-9 else 1, math.ceil(abs(sweep) / step))
    angles = start + sweep * np.arange(count + (0 if abs(sweep) >= math.tau - 1e-9 else 1)) / count
    return np.column_stack((cx + radius * np.cos(angles), cy + radius * np.sin(angles)))


def _arc(start, end, offset, clockwise, tolerance):
    """G75 arc from start to end around start + offset (I, J); start == end is a full circle."""
    cx, cy = start[0] + offset[0], start[1] + offset[1]
    radius = math.hypot(start[0] - cx, start[1] - cy)
    first = math.atan2(start[1] - cy, start[0] - cx)
    last = math.atan2(end[1] - cy, end[0] - cx)
    if clockwise:
        sweep = -((first - last) % math.tau) or -math.tau
    else:
        sweep = ((last - first) % math.tau) or math.tau
    points = _circle(cx, cy, radius, tolerance, first, sweep)
    points[0], points[-1] = start, end
    return points


def _aperture_shape(kind, params, tolerance):
    """Aperture outline centred on the origin (counter-clockwise), or None for no area."""
    if kind == "C":
        return _circle(0.0, 0.0, params[0] / 2, tolerance)
    if kind == "R":
        w, h = params[0] / 2, params[1] / 2
        return np.array([[-w, -h], [w, -h], [w, h], [-w, h]], dtype=np.float64)
    if kind == "O":
        w, h = params[0], params[1]
        radius = min(w, h) / 2
        half = (max(w, h) - min(w, h)) / 2
        if w >= h:  # horizontal stadium: right cap then left cap
            right = _circle(half, 0.0, radius, tolerance, -math.pi / 2, math.pi)
            left = _circle(-half, 0.0, radius, tolerance, math.pi / 2, math.pi)
        else:
            right = _circle(0.0, half, radius, tolerance, 0.0, math.pi)
            left = _circle(0.0, -half, radius, tolerance, math.pi, math.pi)
        return np.concatenate((right, left))
    if kind == "P":
        diameter, vertices = params[0], int(params[1])
        rotation = math.radians(params[2]) if len(params) > 2 else 0.0
        angles = rotation + math.tau * np.arange(vertices) / vertices
        return np.column_stack((diameter / 2 * np.cos(angles), diameter / 2 * np.sin(angles)))
    raise ValueError(f"unsupported Gerber aperture {kind!r} (aperture macros must be disabled)")


def _capsules(segments, radius, tolerance):
    """Round-ended strokes as counter-clockwise polygons, all at once: (n, k, 2)."""
    x0, y0, x1, y1 = segments.T
    angle = np.arctan2(y1 - y0, x1 - x0)
    step = 2 * math.acos(max(-1.0, 1 - min(tolerance / radius, 2))) if radius > 0 else math.pi
    half = max(2, math.ceil(math.pi / step))
    cap = np.linspace(-math.pi / 2, math.pi / 2, half + 1)  # end cap, relative to the direction
    around = angle[:, None] + cap[None, :]
    end = np.stack((x1[:, None] + radius * np.cos(around), y1[:, None] + radius * np.sin(around)), -1)
    around = around + math.pi
    begin = np.stack((x0[:, None] + radius * np.cos(around), y0[:, None] + radius * np.sin(around)), -1)
    return np.concatenate((end, begin), axis=1)


def _swept_rectangle(start, end, half_w, half_h):
    """A rectangle aperture drawn along a line: the convex hull of its two end positions."""
    corners = np.array([[-half_w, -half_h], [half_w, -half_h], [half_w, half_h], [-half_w, half_h]])
    points = np.concatenate((corners + start, corners + end))
    points = points[np.lexsort((points[:, 1], points[:, 0]))]

    def half_hull(sequence):
        hull = []
        for point in sequence:
            while len(hull) >= 2 and np.cross(hull[-1] - hull[-2], point - hull[-2]) <= 0:
                hull.pop()
            hull.append(point)
        return hull

    lower, upper = half_hull(points), half_hull(points[::-1])
    return np.array(lower[:-1] + upper[:-1])


def parse(text, tolerance_nm=1_000.0):
    """Read one Gerber file into polygons. `tolerance_nm`: chord error of sampled curves."""
    plot = Plot()
    decimals = None  # coordinate decimals from %FS
    unit = 1e6  # nm per mm, or per inch after %MOIN
    apertures = {}
    current = None
    mode = "G01"
    region = None  # list of contours while inside G36/G37
    contour = []
    position = (0.0, 0.0)
    dark = True
    contours = []  # of the current polarity group
    lines = {}  # aperture -> list of (x0, y0, x1, y1) straight round strokes

    def flush_group():
        nonlocal contours, lines
        for number, rows in lines.items():
            kind, params = apertures[number]
            radius = params[0] / 2 if kind == "C" else 0.0
            contours.extend(_capsules(np.asarray(rows, dtype=np.float64), radius, tolerance_nm))
        if contours:
            plot.groups.append((dark, contours))
        contours, lines = [], {}

    def close_contour():
        nonlocal contour
        if len(contour) > 1 and contour[-1] == contour[0]:
            contour.pop()  # the closing point repeats the first
        if len(contour) >= 3:
            region.append(np.array(contour, dtype=np.float64))
        contour = []

    def stroke(points):
        """Draw along a polyline with the current aperture."""
        if current is None:
            raise ValueError("Gerber draw before any aperture was selected")
        kind, params = apertures[current]
        if kind == "C":
            lines.setdefault(current, []).extend(
                (a[0], a[1], b[0], b[1]) for a, b in zip(points[:-1], points[1:]))
        elif kind == "R":
            for a, b in zip(points[:-1], points[1:]):
                contours.append(_swept_rectangle(np.asarray(a), np.asarray(b), params[0] / 2, params[1] / 2))
        else:
            raise ValueError(f"Gerber stroke with a {kind!r} aperture is not supported")

    for match in _BLOCK.finditer(text):
        extended, word = match.group(1), match.group(2)
        if extended is not None:
            for command in (part.strip() for part in extended.split("*") if part.strip()):
                if command.startswith("FS"):
                    found = re.search(r"X\d(\d)", command)
                    if found is None or "I" in command[:4]:
                        raise ValueError(f"unsupported Gerber format statement: {command}")
                    decimals = int(found[1])
                elif command.startswith("MO"):
                    unit = 25.4e6 if command == "MOIN" else 1e6
                elif command.startswith("AD"):
                    found = _APERTURE.match(command)
                    if found is None:
                        raise ValueError(f"unreadable Gerber aperture: {command}")
                    values = [float(v) for v in (found[3] or "").split("X") if v]
                    kind = found[2]
                    count = {"C": 1, "R": 2, "O": 2, "P": 2}.get(kind)
                    if count is None:
                        raise ValueError(f"unsupported Gerber aperture {kind!r} (aperture macros must be disabled)")
                    sizes = [v * unit for v in values[:count]] if kind != "P" else [values[0] * unit, *values[1:]]
                    apertures[int(found[1])] = (kind, sizes)
                elif command in ("LPD", "LPC"):
                    flush_group()
                    dark = command == "LPD"
                elif command.startswith(("AM", "SR", "AB")):
                    raise ValueError(f"unsupported Gerber command %{command[:2]} (aperture macros must be disabled)")
                # TF/TA/TO/TD attributes, IP, OF, IN, LN: no effect on the image
            continue
        word = word.strip()
        if not word or word.startswith("G04"):
            continue
        for code in re.findall(r"G(\d+)", word.split("X")[0].split("Y")[0]):
            code = int(code)
            if code in (1, 2, 3):
                mode = f"G0{code}"
            elif code == 36:
                region, contour = [], []
            elif code == 37:
                close_contour()
                contours.extend(region)
                region = None
            elif code in (74,):
                raise ValueError("single-quadrant Gerber arcs (G74) are not supported")
        if word.startswith("M02"):
            break
        d_code = re.search(r"D(\d+)$", word)
        coordinates = dict((axis, int(value)) for axis, value in _COORD.findall(word))
        if d_code is None:
            continue
        number = int(d_code[1])
        if number >= 10 and not coordinates:
            if number not in apertures:
                raise ValueError(f"Gerber aperture D{number} used before it was defined")
            current = number
            continue
        if decimals is None:
            raise ValueError("Gerber coordinates before a %FS format statement")
        scale = unit / 10 ** decimals
        target = (coordinates["X"] * scale if "X" in coordinates else position[0],
                  coordinates["Y"] * scale if "Y" in coordinates else position[1])
        if number == 1:
            if mode == "G01":
                points = [position, target]
            else:
                offset = (coordinates.get("I", 0) * scale, coordinates.get("J", 0) * scale)
                points = [tuple(p) for p in _arc(position, target, offset, mode == "G02", tolerance_nm)]
            if region is not None:
                if not contour:
                    contour.append(position)
                contour.extend(points[1:])
            else:
                stroke(points)
                plot.path_points.append(np.array(points))
        elif number == 2:
            if region is not None:
                close_contour()
            plot.path_points.append(np.array([target]))
        elif number == 3:
            if current is None:
                raise ValueError("Gerber flash before any aperture was selected")
            kind, params = apertures[current]
            contours.append(_aperture_shape(kind, params, tolerance_nm) + target)
        else:
            raise ValueError(f"unsupported Gerber operation D0{number}")
        position = target
    flush_group()
    return plot


# --- Drawing ------------------------------------------------------------------------------

def _edges(contours):
    """All contour edges, each contour turned counter-clockwise: (n, 4) x0 y0 x1 y1."""
    if not contours:
        return np.empty((0, 4))
    sizes = np.array([len(c) for c in contours])
    points = np.concatenate(contours)
    following = np.arange(1, len(points) + 1)
    ends = np.cumsum(sizes)
    following[ends - 1] = ends - sizes  # each contour closes on its first point
    x, y = points[:, 0], points[:, 1]
    twice_area = np.add.reduceat(x * y[following] - x[following] * y, ends - sizes)
    flip = np.repeat(twice_area < 0, sizes)
    edges = np.column_stack((points, points[following]))
    edges[flip] = edges[flip][:, [2, 3, 0, 1]]
    return edges


def _coverage(edges, width, height):
    """Nonzero-winding coverage (0..1) of `edges` given in pixel units (row 0 on top).

    Each of SAMPLES rows per pixel crosses the edges; between crossings where the
    running winding is nonzero the row is inside. Those spans are added with exact
    horizontal coverage through a difference array, so the cost follows the number
    of crossings (< 1 M per layer on the reference board), not the image size.
    """
    if not len(edges):
        return np.zeros((height, width), np.float32)
    x0, x1 = edges[:, 0], edges[:, 2]
    y0, y1 = edges[:, 1] * SAMPLES, edges[:, 3] * SAMPLES
    low, high = np.minimum(y0, y1), np.maximum(y0, y1)
    first = np.clip(np.ceil(low - 0.5), 0, height * SAMPLES).astype(np.int64)  # rows j + 0.5 in [low, high)
    stop = np.clip(np.ceil(high - 0.5), 0, height * SAMPLES).astype(np.int64)
    counts = np.maximum(stop - first, 0)
    edge = np.repeat(np.arange(len(edges)), counts)
    row = np.repeat(first, counts) + (np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts))
    x = x0[edge] + (row + 0.5 - y0[edge]) * ((x1 - x0) / np.where(y1 != y0, y1 - y0, 1))[edge]
    direction = np.where(y1 > y0, 1, -1)[edge]
    order = np.argsort(row * (width + 4.0) + np.clip(x, -1, width + 1), kind="stable")
    row, x, direction = row[order], np.clip(x[order], 0, width), direction[order]
    # Closed contours cross every row a net zero times, so one running sum restarts at 0 per row.
    inside = np.flatnonzero(np.cumsum(direction)[:-1] != 0)
    left, right, pixel_row = x[inside], x[inside + 1], row[inside] // SAMPLES
    stride = width + 2
    positions = np.concatenate((left, right))
    weights = np.concatenate((np.full(len(left), 1.0 / SAMPLES), np.full(len(right), -1.0 / SAMPLES)))
    rows = np.concatenate((pixel_row, pixel_row))
    column = np.floor(positions).astype(np.int64)
    fraction = positions - column
    delta = np.bincount(np.concatenate((rows * stride + column, rows * stride + column + 1)),
                        weights=np.concatenate((weights * (1 - fraction), weights * fraction)),
                        minlength=height * stride).reshape(height, stride)
    return np.clip(np.cumsum(delta, axis=1)[:, :width], 0, 1).astype(np.float32)


def contours(plot, spacing=None):
    """Every drawn outline (flashes, strokes, regions), counter-clockwise, for walls
    along silkscreen ink: (points (n, 2) nm, sizes per contour).

    `spacing` (nm): extra points so no edge is longer, keeping every corner. The
    walls are tested point by point against the image, so a long edge that is
    partly inside another shape needs points along it.
    """
    rings = [ring for dark, group in plot.groups if dark for ring in group if len(ring) >= 3]
    if not rings:
        return np.empty((0, 2)), np.empty(0, np.int64)
    sizes = np.array([len(ring) for ring in rings], np.int64)
    points = np.concatenate(rings)
    ends = np.cumsum(sizes)
    following = np.arange(1, len(points) + 1)
    following[ends - 1] = ends - sizes
    x, y = points[:, 0], points[:, 1]
    twice_area = np.add.reduceat(x * y[following] - x[following] * y, ends - sizes)
    for index in np.flatnonzero(twice_area < 0):  # a few clockwise regions
        start, end = ends[index] - sizes[index], ends[index]
        points[start:end] = points[start:end][::-1]
    if spacing:
        # Recompute `following` after the reversal (same contour sizes, so still valid).
        step = points[following] - points
        pieces = np.maximum(1, np.ceil(np.hypot(step[:, 0], step[:, 1]) / spacing)).astype(np.int64)
        owner = np.repeat(np.arange(len(points)), pieces)
        fraction = (np.arange(pieces.sum()) - np.repeat(np.cumsum(pieces) - pieces, pieces)) / pieces[owner]
        points = points[owner] + step[owner] * fraction[:, None]
        sizes = np.add.reduceat(pieces, ends - sizes)
    return points, sizes


def grid(bounds, resolution=RESOLUTION):
    """(pixel size nm, width, height, rectangle) for `bounds` (nm, y up); the rectangle
    is the exact area the image covers (whole pixels from the top-left, within half a
    pixel of `bounds`)."""
    xmin, ymin, xmax, ymax = bounds
    pixel = max(25.4e6 / DPI, max(xmax - xmin, ymax - ymin, 1.0) / resolution)
    width = max(1, round((xmax - xmin) / pixel))  # 37 mm at 600 dpi: 874 px
    height = max(1, round((ymax - ymin) / pixel))
    return pixel, width, height, (xmin, ymax - height * pixel, xmin + width * pixel, ymax)


def rasterize(plot, bounds, resolution=RESOLUTION):
    """Coverage image (row 0 on top) of `plot` over `bounds` (nm, Gerber frame)."""
    pixel, width, height, rect = grid(bounds, resolution)
    alpha = np.zeros((height, width), np.float32)
    for dark, contours in plot.groups:
        edges = _edges(contours)
        if len(edges):
            edges = np.column_stack(((edges[:, 0] - rect[0]) / pixel, (rect[3] - edges[:, 1]) / pixel,
                                     (edges[:, 2] - rect[0]) / pixel, (rect[3] - edges[:, 3]) / pixel))
        coverage = _coverage(edges, width, height)
        if dark:
            np.maximum(alpha, coverage, out=alpha)
        else:
            alpha *= 1.0 - coverage
    return alpha


def blur(alpha, sigma):
    """Gaussian blur of a coverage image (`sigma` in pixels), separable, edges extended."""
    radius = max(1, math.ceil(3 * sigma))
    offsets = np.arange(-radius, radius + 1)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    kernel /= kernel.sum()
    for axis in (0, 1):
        moved = np.moveaxis(alpha, axis, 0)
        padded = np.pad(moved, ((radius, radius), (0, 0)), mode="edge")
        result = np.zeros_like(moved, dtype=np.float32)
        for index, weight in enumerate(kernel):
            result += weight * padded[index:index + len(moved)]
        alpha = np.moveaxis(result, 0, axis)
    return np.ascontiguousarray(alpha, dtype=np.float32)


def write_png(path, alpha):
    """Black RGBA with `alpha` (0..1) as its alpha channel."""
    height, width = alpha.shape
    rgba = np.zeros((height, width, 4), np.uint8)
    rgba[..., 3] = np.round(np.clip(alpha, 0, 1) * 255)
    raw = np.concatenate((np.zeros((height, 1), np.uint8), rgba.reshape(height, -1)), axis=1)  # filter 0

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    with open(path, "wb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")
        stream.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)))
        stream.write(chunk(b"IDAT", zlib.compress(raw.tobytes(), 1)))
        stream.write(chunk(b"IEND", b""))
