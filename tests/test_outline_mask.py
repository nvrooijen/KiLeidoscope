"""The board-area coverage image (`blender_addon/kileido/outline_mask.py`), without Blender."""

import importlib.util
import time
from pathlib import Path

import numpy as np
import pytest

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "outline_mask.py"
_SPEC = importlib.util.spec_from_file_location("kileido_outline_mask", _PATH)
outline_mask = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(outline_mask)


def ring(*corners):
    """Edges a -> b of one closed ring."""
    points = np.array(corners, np.float64)
    return points, np.roll(points, -1, axis=0)


def rings(*parts):
    return np.vstack([p[0] for p in parts]), np.vstack([p[1] for p in parts])


def test_inside_is_one_outside_is_zero():
    a, b = ring((1, 1), (9, 1), (9, 5), (1, 5))
    cover = outline_mask.coverage((0, 0), 1.0, (6, 10), a, b)
    assert cover.dtype == np.float32
    assert np.all(cover[1:5, 1:9] == 1.0)
    assert cover[0].sum() == 0 and cover[5].sum() == 0
    assert cover[:, 0].sum() == 0 and cover[:, 9].sum() == 0


def test_winding_direction_does_not_matter():
    a, b = ring((1, 1), (9, 1), (9, 5), (1, 5))
    forward = outline_mask.coverage((0, 0), 1.0, (6, 10), a, b)
    backward = outline_mask.coverage((0, 0), 1.0, (6, 10), b, a)
    assert np.array_equal(forward, backward)


def test_an_edge_through_a_pixel_covers_its_share():
    # x from 1.25 to 8.5: a quarter pixel short on the left, half a pixel on the right
    a, b = ring((1.25, 1), (8.5, 1), (8.5, 5), (1.25, 5))
    cover = outline_mask.coverage((0, 0), 1.0, (6, 10), a, b)
    assert cover[2, 1] == pytest.approx(0.75)
    assert cover[2, 8] == pytest.approx(0.5)
    assert cover[2, 2:8].tolist() == [1.0] * 6


def test_a_span_inside_one_pixel():
    a, b = ring((3.25, 1), (3.5, 1), (3.5, 5), (3.25, 5))
    cover = outline_mask.coverage((0, 0), 1.0, (6, 10), a, b)
    assert cover[2, 3] == pytest.approx(0.25)
    assert cover[2].sum() == pytest.approx(0.25)


def test_a_cutout_is_outside():
    board = ring((0, 0), (10, 0), (10, 10), (0, 10))
    cutout = ring((4, 4), (6, 4), (6, 6), (4, 6))
    cover = outline_mask.coverage((0, 0), 1.0, (10, 10), *rings(board, cutout))
    assert np.all(cover[4:6, 4:6] == 0.0)
    assert cover.sum() == pytest.approx(100 - 4)


def test_two_boards_side_by_side():
    left, right = ring((0, 0), (3, 0), (3, 3), (0, 3)), ring((5, 0), (8, 0), (8, 3), (5, 3))
    cover = outline_mask.coverage((0, 0), 1.0, (3, 8), *rings(left, right))
    assert cover[1].tolist() == [1, 1, 1, 0, 0, 1, 1, 1]


def test_a_diagonal_edge_covers_its_area():
    a, b = ring((0, 0), (8, 0), (0, 8))  # right triangle, area 32
    cover = outline_mask.coverage((0, 0), 1.0, (8, 8), a, b)
    assert cover.sum() == pytest.approx(32.0, abs=0.01)
    # a pixel the hypotenuse halves (through its corners) is half covered, within sampling
    assert cover[3, 4] == pytest.approx(0.5, abs=1 / outline_mask.SUBROWS)


def test_bounds_and_pixel_size_in_metres():
    # a 20 x 10 mm board on a 0.5 mm grid whose corner sits at (-5, -2) mm
    mm = 1e-3
    a, b = ring((-5 * mm, -2 * mm), (15 * mm, -2 * mm), (15 * mm, 8 * mm), (-5 * mm, 8 * mm))
    cover = outline_mask.coverage((-5 * mm, -2 * mm), 0.5 * mm, (20, 40), a, b)
    assert np.all(cover == 1.0)


def test_copper_past_the_outline_box_stays_outside():
    # the image only covers the outline's box; the shader reads past it as 0 (CLIP), outside
    a, b = ring((0, 0), (4, 0), (4, 2), (0, 2))
    cover = outline_mask.coverage((0, 0), 1.0, (2, 4), a, b)
    assert np.all(cover == 1.0)


def test_an_unclosed_ring_does_not_flood_the_row():
    a = np.array([(1.0, 0.5), (9.0, 0.5), (9.0, 3.5)])  # three edges of a box, the left one missing
    b = np.array([(9.0, 0.5), (9.0, 3.5), (1.0, 3.5)])
    cover = outline_mask.coverage((0, 0), 1.0, (4, 10), a, b)
    assert cover.max() <= 1.0 and cover[1:3].sum() == 0  # one crossing per row: dropped, not filled


def test_no_outline_is_empty():
    empty = np.empty((0, 2))
    assert outline_mask.coverage((0, 0), 1.0, (3, 3), empty, empty).sum() == 0


def test_full_resolution_fills_quickly():
    # a 2048 x 2048 mask, a round board of 2000 edges (rounded outlines are sampled arcs)
    angles = np.linspace(0, 2 * np.pi, 2000, endpoint=False)
    points = np.column_stack((np.cos(angles), np.sin(angles))) * 0.05 + 0.05
    a, b = points, np.roll(points, -1, axis=0)
    started = time.perf_counter()
    cover = outline_mask.coverage((0, 0), 0.1 / 2048, (2048, 2048), a, b)
    elapsed = time.perf_counter() - started
    assert cover.sum() * (0.1 / 2048) ** 2 == pytest.approx(np.pi * 0.05 ** 2, rel=1e-3)
    assert elapsed < 5.0
