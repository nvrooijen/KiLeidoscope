"""The overlay Gerber renderer (`blender_addon/kileido/gerber.py`), without Blender.

Expected areas are worked out by hand from the shapes' dimensions.
"""

import importlib.util
import math
import zlib
from pathlib import Path

import numpy as np
import pytest

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "gerber.py"
_SPEC = importlib.util.spec_from_file_location("kileido_gerber", _PATH)
gerber = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gerber)

HEADER = "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\nG01*\n"
BOUNDS = (0.0, 0.0, 10e6, 10e6)  # 10 x 10 mm, nm


def mm(value):
    return int(round(value * 1e6))


def area_mm2(text, bounds=BOUNDS, resolution=1000):
    plot = gerber.parse(HEADER + text + "M02*\n")
    pixel = gerber.grid(bounds, resolution)[0]
    return gerber.rasterize(plot, bounds, resolution).sum() * (pixel / 1e6) ** 2


def test_round_flash_and_rectangle_flash():
    circle = f"%ADD10C,2.000000*%\nD10*\nX{mm(5)}Y{mm(5)}D03*\n"
    assert area_mm2(circle) == pytest.approx(math.pi, rel=2e-3)
    rectangle = f"%ADD11R,3.000000X1.000000*%\nD11*\nX{mm(5)}Y{mm(5)}D03*\n"
    assert area_mm2(rectangle) == pytest.approx(3.0, rel=2e-3)


def test_obround_flash_both_orientations():
    expected = 2 * 1 + math.pi * 0.5 ** 2  # 3 x 1 mm stadium: 2 x 1 body plus two half discs
    for size in ("3.000000X1.000000", "1.000000X3.000000"):
        text = f"%ADD12O,{size}*%\nD12*\nX{mm(5)}Y{mm(5)}D03*\n"
        assert area_mm2(text) == pytest.approx(expected, rel=3e-3)


def test_round_stroke_is_a_capsule_and_overlaps_add_up():
    stroke = f"%ADD10C,0.500000*%\nD10*\nX{mm(2)}Y{mm(5)}D02*\nX{mm(8)}Y{mm(5)}D01*\n"
    capsule = 6 * 0.5 + math.pi * 0.25 ** 2
    assert area_mm2(stroke) == pytest.approx(capsule, rel=3e-3)
    # The same stroke twice (opposite directions) is still one capsule, not a hole.
    twice = stroke + f"X{mm(2)}Y{mm(5)}D01*\n"
    assert area_mm2(twice) == pytest.approx(capsule, rel=3e-3)


def test_region_with_arc_edge():
    # A half disc of radius 3 mm: straight edge along y = 2, arc (G02, clockwise) over the top.
    text = ("G36*\n"
            f"X{mm(2)}Y{mm(2)}D02*\nX{mm(8)}Y{mm(2)}D01*\n"
            f"G75*\nG03*\nX{mm(2)}Y{mm(2)}I{mm(-3)}J0D01*\nG01*\nG37*\n")
    assert area_mm2(text) == pytest.approx(math.pi * 9 / 2, rel=3e-3)


def test_clockwise_region_is_filled_too():
    square = [(2, 2), (2, 6), (6, 6), (6, 2)]  # clockwise
    text = "G36*\n" + f"X{mm(2)}Y{mm(2)}D02*\n" + "".join(
        f"X{mm(x)}Y{mm(y)}D01*\n" for x, y in square[1:] + square[:1]) + "G37*\n"
    assert area_mm2(text) == pytest.approx(16.0, rel=2e-3)


def test_clear_polarity_cuts_what_was_drawn_before():
    text = (f"%ADD10C,4.000000*%\n%ADD11C,2.000000*%\nD10*\nX{mm(5)}Y{mm(5)}D03*\n"
            f"%LPC*%\nD11*\nX{mm(5)}Y{mm(5)}D03*\n%LPD*%\n")
    assert area_mm2(text) == pytest.approx(math.pi * (4 - 1), rel=3e-3)


def test_edge_positions_are_exact_to_a_fraction_of_a_pixel():
    """A 1 mm square at a non-integer pixel position: the 50 % coverage edge lands on
    the true edge (the viewer's shading puts the visible edge at 50 %)."""
    bounds = (0.0, 0.0, 10e6, 10e6)
    pixel, width, height, rect = gerber.grid(bounds, 100)  # 0.1 mm pixels
    text = "%ADD11R,1.000000X1.000000*%\nD11*\n" + f"X{mm(3.33)}Y{mm(6.66)}D03*\n"
    alpha = gerber.rasterize(gerber.parse(HEADER + text + "M02*\n"), bounds, 100)
    row = alpha[int((rect[3] - 6.66e6) / pixel)]
    left_edge_px = np.flatnonzero(row > 0)[0] + (1 - row[row > 0][0])  # partial first pixel
    assert left_edge_px * pixel / 1e6 == pytest.approx(2.83, abs=0.002)


def test_grid_keeps_600_dpi_and_caps_the_long_side():
    small = gerber.grid((0, 0, 37e6, 37e6))
    assert small[1:3] == (874, 874)  # 37 mm at 600 dpi
    large = gerber.grid((0, 0, 200e6, 100e6))
    assert large[1] == 2048 and large[2] == 1024


def test_unsupported_input_raises_instead_of_drawing_wrong():
    with pytest.raises(ValueError, match="macro"):
        gerber.parse(HEADER + "%AMROUNDRECT*1,1,$1,0,0*%\n")
    with pytest.raises(ValueError, match="aperture"):
        gerber.parse(HEADER + "%ADD10ROUNDRECT,1.0*%\n")
    with pytest.raises(ValueError, match="before"):
        gerber.parse(HEADER + f"X{mm(1)}Y{mm(1)}D03*\n")


def test_centre_line_bounds_include_arc_bulge():
    # A full circle (start == end) of radius 2 mm around (5, 5), drawn as a thin outline.
    text = (f"%ADD10C,0.100000*%\nD10*\nX{mm(7)}Y{mm(5)}D02*\n"
            f"G75*\nG03*\nX{mm(7)}Y{mm(5)}I{mm(-2)}J0D01*\n")
    bounds = gerber.parse(HEADER + text + "M02*\n").bounds(centre_line=True)
    assert np.allclose(np.array(bounds) / 1e6, (3, 3, 7, 7), atol=0.002)


def test_png_is_valid_and_stores_alpha(tmp_path):
    alpha = np.linspace(0, 1, 12, dtype=np.float32).reshape(3, 4)
    path = tmp_path / "plot.png"
    gerber.write_png(path, alpha)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert (width, height) == (4, 3)
    start = data.index(b"IDAT") + 4
    length = int.from_bytes(data[start - 8:start - 4], "big")
    raw = np.frombuffer(zlib.decompress(data[start:start + length]), np.uint8).reshape(3, 1 + 4 * 4)
    assert np.array_equal(raw[:, 4::4], np.round(alpha * 255).astype(np.uint8))


def test_contours_are_counter_clockwise_and_densified_with_corners_kept():
    square = [(2, 2), (2, 6), (6, 6), (6, 2)]  # a clockwise region, 4 mm sides
    text = "G36*\n" + f"X{mm(2)}Y{mm(2)}D02*\n" + "".join(
        f"X{mm(x)}Y{mm(y)}D01*\n" for x, y in square[1:] + square[:1]) + "G37*\n"
    plot = gerber.parse(HEADER + text + "M02*\n")
    points, sizes = gerber.contours(plot)
    assert list(sizes) == [4]
    x, y = points[:, 0], points[:, 1]
    assert (x * np.roll(y, -1) - np.roll(x, -1) * y).sum() > 0  # counter-clockwise now
    dense, dense_sizes = gerber.contours(plot, spacing=mm(0.5))
    assert list(dense_sizes) == [32]  # 16 mm perimeter in 0.5 mm steps
    corners = {(mm(a), mm(b)) for a, b in square}
    assert corners <= {tuple(np.round(p).astype(int)) for p in dense}
    steps = np.hypot(*(np.roll(dense, -1, axis=0) - dense).T)
    assert steps.max() <= mm(0.5) + 1


def test_blur_softens_edges_and_keeps_the_amount_inside():
    alpha = np.zeros((40, 60), np.float32)
    alpha[10:30, 20:40] = 1.0  # a pad well inside the image
    soft = gerber.blur(alpha, 2.0)
    assert soft.shape == alpha.shape and soft.dtype == np.float32
    assert math.isclose(soft.sum(), alpha.sum(), rel_tol=1e-5)  # nothing reaches the image border
    assert math.isclose(soft[20, 30], 1.0, abs_tol=1e-3)  # the middle stays at full height
    assert 0.4 < soft[20, 20] < 0.6 and 0.0 < soft[20, 18] < soft[20, 20]  # a ramp across the edge
    assert np.allclose(gerber.blur(np.ones((5, 5), np.float32), 3.0), 1.0)  # edges extend, no dark rim
