"""The cut plane's cross section (`blender_addon/kileido/section.py`), without Blender.

Expected stretches are worked out by hand from the shapes' dimensions (mm and um).
"""

import importlib.util
import itertools
from pathlib import Path

import numpy as np
import pytest

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "section.py"
_SPEC = importlib.util.spec_from_file_location("kileido_section", _PATH)
section = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(section)

MM, UM = 1e-3, 1e-6
ALONG_X = section.Line((0.0, 0.0), (0.0, -1.0))  # the default plane (normal -Y): s is x


def square(x0, y0, x1, y1, item=0):
    corners = np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    return corners, np.roll(corners, -1, axis=0), np.full(4, item)


def test_interval_merge_and_subtract():
    assert section.merge([(3, 4), (0, 1), (0.5, 2)]).tolist() == [[0, 2], [3, 4]]
    assert section.subtract(np.array([[0.0, 10.0]]), np.array([[2.0, 3.0], [5.0, 12.0]])).tolist() == [[0, 2], [3, 5]]
    assert section.intersect(np.array([[0.0, 10.0]]), np.array([[-1.0, 1.0], [9.0, 11.0]])).tolist() == [[0, 1], [9, 10]]


def test_rings_are_even_odd_per_item_and_united():
    outer = square(-10, -5, 10, 5)
    hole = square(-2, -1, 2, 1)
    other = square(8, -1, 12, 1, item=1)  # a second item overlapping the first: united, not cancelled
    a, b, item = (np.concatenate(parts) for parts in zip(outer, hole, other))
    assert section.rings(ALONG_X, a, b, item).tolist() == [[-10, -2], [2, 12]]


def test_capsules_across_and_along_the_cut():
    across = section.capsules(ALONG_X, [(0, -3)], [(0, 3)], 0.25)  # a track crossing the cut: its width
    assert np.allclose(across, [[-0.25, 0.25]])
    along = section.capsules(ALONG_X, [(-2, 0)], [(2, 0)], 0.25)  # lying in the cut: length plus round ends
    assert np.allclose(along, [[-2.25, 2.25]])
    assert not len(section.capsules(ALONG_X, [(-2, 1)], [(2, 1)], 0.25))  # beside the cut


def test_oblong_drill_follows_its_long_axis():
    assert np.allclose(section.drills(ALONG_X, [(0, 0, 3, 1, 0, 1)]), [[-1.5, 1.5]])
    assert np.allclose(section.drills(ALONG_X, [(0, 0, 3, 1, np.pi / 2, 1)]), [[-0.5, 0.5]])


STACK = [
    {"name": "F.Cu", "type": "copper", "thickness_nm": 35_000},
    {"name": "Dielectric 1", "type": "dielectric", "thickness_nm": 200_000},
    {"name": "In1.Cu", "type": "copper", "thickness_nm": 35_000},
    {"name": "Dielectric 2", "type": "dielectric", "thickness_nm": 400_000},
    {"name": "In2.Cu", "type": "copper", "thickness_nm": 35_000},
    {"name": "Dielectric 3", "type": "dielectric", "thickness_nm": 800_000},
    {"name": "B.Cu", "type": "copper", "thickness_nm": 35_000},
]
HEIGHTS = {"B.Cu": 0.0, "In2.Cu": 870 * UM, "In1.Cu": 1305 * UM, "F.Cu": 1540 * UM}
THICKNESS = {name: 35 * UM for name in HEIGHTS}
SAVED = [{"type": "prepreg", "material": "FR4"}, {"type": "core", "material": "FR4"},
         {"type": "prepreg", "material": "FR4"}]


def test_stack_layout_places_copper_and_colours_each_dielectric():
    copper, bands = section.stack_layout(HEIGHTS, THICKNESS, STACK, SAVED)
    assert {name: tuple(round(z / UM) for z in zr) for name, zr in copper.items()} == {
        "B.Cu": (0, 35), "In2.Cu": (835, 870), "In1.Cu": (1270, 1305), "F.Cu": (1505, 1540)}
    assert [(round(z0 / UM), round(z1 / UM), color) for z0, z1, color in bands] == [
        (35, 835, section.PREPREG), (835, 870, section.PREPREG),  # In2's level: the prepreg below
        (870, 1270, section.CORE), (1270, 1305, section.CORE), (1305, 1505, section.PREPREG)]
    _, unknown = section.stack_layout(HEIGHTS, THICKNESS, STACK, SAVED[:2])  # saved does not match the stack
    assert {color for _, _, color in unknown} == {section.CORE}


def _board(plug, capped=False, cap_plating=0.0):
    """40 x 30 mm board; In1 poured over all of it; a 0.5 mm F.Cu track from x = -5 to 5;
    a blind via F.Cu-In1 at x = 10 mm (0.6 mm land, 0.3 mm drill)."""
    copper, bands = section.stack_layout(HEIGHTS, THICKNESS, STACK, SAVED)
    layers = {"In1.Cu": section.rings(ALONG_X, *square(-20 * MM, -15 * MM, 20 * MM, 15 * MM)),
              "F.Cu": section.capsules(ALONG_X, [(-5 * MM, 0)], [(5 * MM, 0)], 0.25 * MM)}
    vias = {"xy": np.array([(10 * MM, 0.0)]), "diameter": np.array([0.6 * MM]), "drill": np.array([0.3 * MM]),
            "top": ["F.Cu"], "bottom": ["In1.Cu"]}
    return section.cross_section(ALONG_X, square(-20 * MM, -15 * MM, 20 * MM, 15 * MM), layers, copper, bands,
                                 vias=vias, plating=25 * UM, plug=plug, capped=capped, cap_plating=cap_plating)


def color_at(rects, s, z):
    found = [color for s0, s1, z0, z1, color in rects if s0 <= s < s1 and z0 <= z < z1]
    assert len(found) <= 1, found  # never two rectangles on one spot
    return found[0] if found else None


@pytest.mark.parametrize("s_mm, z_um, expected", [
    (-15, 500, section.PREPREG), (-15, 1000, section.CORE), (-15, 1400, section.PREPREG),
    (-15, 1290, section.COPPER),  # the In1 pour, at its stackup thickness
    (-15, 850, section.PREPREG),  # In2 has no copper here: laminate fills its level
    (-15, 1520, None),  # F.Cu's level outside its copper: nothing
    (0, 1520, section.COPPER),  # the track
    (10, 1400, section.RESIN),  # the plugged bore
    (10 + 0.15 - 0.0125, 1400, section.COPPER),  # the barrel wall
    (10 + 0.2, 1400, section.PREPREG),  # laminate around the barrel
    (10 + 0.25, 1520, section.COPPER),  # the F.Cu land
    (10, 1290, section.RESIN),  # In1's level inside the bore: plug, not the pour
    (10, 1000, section.CORE),  # below the blind via
    (25, 1000, None),  # beyond the board edge
])
def test_cross_section_at_known_points(s_mm, z_um, expected):
    assert color_at(_board("RESIN"), s_mm * MM, z_um * UM) == expected


def test_cap_plating_stands_on_the_land_above_the_outer_copper():
    rects = _board("RESIN", capped=True, cap_plating=20 * UM)
    assert color_at(rects, 10 * MM, 1550 * UM) == section.COPPER  # over the drill, 10 um above F.Cu
    assert color_at(rects, (10 + 0.25) * MM, 1550 * UM) == section.COPPER  # over the land (0.3 mm radius)
    assert color_at(rects, (10 + 0.35) * MM, 1550 * UM) is None  # beyond the land
    assert color_at(rects, 10 * MM, 1565 * UM) is None  # above the 20 um cap
    assert color_at(_board("RESIN", capped=False, cap_plating=20 * UM), 10 * MM, 1550 * UM) is None  # not capped


def test_capped_via_is_plated_over_at_the_outer_copper_only():
    rects = _board("RESIN", capped=True)
    assert color_at(rects, 10 * MM, 1520 * UM) == section.COPPER  # the cap, across the drill in F.Cu
    assert color_at(rects, 10 * MM, 1400 * UM) == section.RESIN  # the plug under it
    assert color_at(rects, 10 * MM, 1290 * UM) == section.RESIN  # the In1 end is inner copper: no cap
    assert color_at(_board(None, capped=True), 10 * MM, 1520 * UM) is None  # nothing to cap without a plug


def test_open_via_leaves_its_bore_empty_and_nothing_overlaps():
    rects = _board(None)
    assert color_at(rects, 10 * MM, 1400 * UM) is None
    assert color_at(rects, (10 + 0.15 - 0.0125) * MM, 1400 * UM) == section.COPPER
    for first, second in itertools.combinations(rects, 2):
        apart = (first[1] <= second[0] or second[1] <= first[0] or first[3] <= second[2] or second[3] <= first[2])
        assert apart, (first, second)
