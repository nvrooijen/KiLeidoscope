"""IMS mode's stack and checks (`blender_addon/kileido/ims.py`), without Blender."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "ims.py"
_SPEC = importlib.util.spec_from_file_location("kileido_ims", _PATH)
ims = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ims)

MM, UM = 1e-3, 1e-6

# A 2-layer IMS board as KiCad holds it: 1.6 mm ("aluminum, 1.6 mm" at the fab), 35 um copper.
HEIGHTS = {"F.Cu": 1600 * UM, "B.Cu": 0.0}
THICKNESS = {"F.Cu": 35 * UM, "B.Cu": 35 * UM, "F.Mask": 10 * UM}


def test_only_two_layer_boards_are_eligible():
    assert ims.eligible(HEIGHTS) == (True, "")
    ok, why = ims.eligible({"F.Cu": 1.6 * MM, "In1.Cu": 1.2 * MM, "In2.Cu": 0.4 * MM, "B.Cu": 0.0})
    assert not ok and "4 copper layers" in why


def test_board_keeps_kicad_thickness_and_the_base_takes_the_rest():
    result = ims.stack(HEIGHTS, THICKNESS, 100 * UM)
    assert result.thickness_m == pytest.approx(1600 * UM) and result.heights == HEIGHTS
    assert result.dielectric == pytest.approx((1465 * UM, 1565 * UM))  # the epoxy, under F.Cu
    assert result.base == pytest.approx((0.0, 1465 * UM))  # B.Cu's place included
    assert result.layer_thickness["B.Cu"] == pytest.approx(1465 * UM)
    assert result.layer_thickness["F.Cu"] == 35 * UM and result.layer_thickness["F.Mask"] == 10 * UM
    assert THICKNESS["B.Cu"] == 35 * UM  # the stackup's own values stay untouched


def test_flat_copper_and_a_board_thinner_than_the_epoxy():
    assert ims.stack(HEIGHTS, {}, 100 * UM).base == pytest.approx((0.0, 1500 * UM))  # no thickness invented
    thin = ims.stack({"F.Cu": 80 * UM, "B.Cu": 0.0}, {}, 100 * UM)
    assert thin.base == (0.0, 0.0) and thin.dielectric == pytest.approx((0.0, 80 * UM))
    assert ims.stack(HEIGHTS, THICKNESS, -1).dielectric[0] == pytest.approx(1565 * UM)  # no epoxy: all base


def test_fill_area_subtracts_holes():
    """A 10 x 6 square with a 2 x 2 hole, rings stored as apply._ring_edges does."""
    xy = [(0, 0), (10, 0), (10, 6), (0, 6), (4, 2), (6, 2), (6, 4), (4, 4)]
    following = [1, 2, 3, 0, 5, 6, 7, 4]
    hole = [0, 0, 0, 0, 1, 1, 1, 1]
    assert ims.fill_area(xy, following, hole) == pytest.approx(56.0)
    assert ims.fill_area(xy[:4][::-1], following[:4], hole[:4]) == pytest.approx(60.0)  # either winding
    assert ims.fill_area([], [], []) == 0.0


@pytest.mark.parametrize("routed, copper, board, expected", [
    (0, 0.0, 100.0, "empty"),
    (0, 95.0, 100.0, "pour"),
    (0, 40.0, 100.0, "routed"),  # a partial pour is copper of its own
    (3, 95.0, 100.0, "routed"),  # tracks beside a pour too
])
def test_bottom_copper(routed, copper, board, expected):
    assert ims.bottom_copper(routed, copper, board) == expected


def test_warnings_name_what_shorts():
    assert ims.warnings(0, 0, "empty") == []
    assert ims.warnings(0, 0, "pour") == []
    lines = ims.warnings(1, 4, "routed")
    assert lines[0] == "1 via would short to the metal base"
    assert lines[1] == "4 plated holes would short to the metal base"
    assert "B.Cu" in lines[2] and len(lines) == 3
