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


def _board(plug, capped=False, cap_plating=0.0, halves=(True, True), tented=False, ink=False):
    """40 x 30 mm board; In1 poured over all of it; a 0.5 mm F.Cu track from x = -5 to 5;
    a blind via F.Cu-In1 at x = 10 mm (0.6 mm land, 0.3 mm drill). plug: None (open),
    "RESIN" or "COPPER", in the barrel's (upper, lower) `halves`."""
    copper, bands = section.stack_layout(HEIGHTS, THICKNESS, STACK, SAVED)
    layers = {"In1.Cu": section.rings(ALONG_X, *square(-20 * MM, -15 * MM, 20 * MM, 15 * MM)),
              "F.Cu": section.capsules(ALONG_X, [(-5 * MM, 0)], [(5 * MM, 0)], 0.25 * MM)}
    vias = {"xy": np.array([(10 * MM, 0.0)]), "diameter": np.array([0.6 * MM]), "drill": np.array([0.3 * MM]),
            "top": ["F.Cu"], "bottom": ["In1.Cu"],
            "core_top": [bool(plug) and halves[0]], "core_bottom": [bool(plug) and halves[1]],
            "fill_copper": [plug == "COPPER"], "capped": [capped], "tent_top": [tented], "tent_bottom": [tented],
            "plug_ink": [ink]}
    return section.cross_section(ALONG_X, square(-20 * MM, -15 * MM, 20 * MM, 15 * MM), layers, copper, bands,
                                 vias=vias, plating=25 * UM, cap_plating=cap_plating,
                                 tents={"F.Cu": (20 * UM, MASK), "B.Cu": (20 * UM, MASK)}, plug_color=MASK)


MASK = (0.1, 0.3, 0.6)


def test_a_tent_is_a_mask_film_over_the_land_and_drill():
    rects = _board(None, tented=True)  # F.Cu's top at 1.535 mm
    assert color_at(rects, 10 * MM, 1545 * UM) == MASK  # over the empty drill
    assert color_at(rects, (10 + 0.25) * MM, 1545 * UM) == MASK  # over the land
    assert color_at(rects, 10 * MM, 1560 * UM) is None  # 20 um thick
    assert color_at(rects, 10 * MM, 1400 * UM) is None  # the barrel under it stays empty
    assert color_at(_board(None), 10 * MM, 1545 * UM) is None  # untented
    assert MASK not in section.WOVEN


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
    (10, 1290, section.COPPER),  # the blind via's floor: its In1 land, whole under the drill
    (10, 1310, section.RESIN),  # the plug, from the floor up
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
    assert color_at(rects, 10 * MM, 1290 * UM) == section.COPPER  # the In1 end: the via's floor, not a cap
    assert color_at(rects, 10 * MM, 1310 * UM) == section.RESIN
    assert color_at(_board(None, capped=True), 10 * MM, 1520 * UM) is None  # nothing to cap without a plug


def test_a_plug_from_one_side_fills_half_the_barrel():
    upper = _board("RESIN", halves=(True, False))  # the blind via's bore, In1 up to F.Cu, halves at ~1.4 mm
    assert color_at(upper, 10 * MM, 1480 * UM) == section.RESIN
    assert color_at(upper, 10 * MM, 1320 * UM) is None  # the lower half stays empty
    lower = _board("RESIN", halves=(False, True))
    assert color_at(lower, 10 * MM, 1480 * UM) is None
    assert color_at(lower, 10 * MM, 1320 * UM) == section.RESIN


def test_a_copper_fill_is_copper_across_the_bore():
    rects = _board("COPPER")
    assert color_at(rects, 10 * MM, 1400 * UM) == section.COPPER
    assert color_at(rects, (10 + 0.15 - 0.0125) * MM, 1400 * UM) == section.COPPER  # wall and fill, one metal


def test_open_via_leaves_its_bore_empty_and_nothing_overlaps():
    rects = _board(None)
    assert color_at(rects, 10 * MM, 1400 * UM) is None
    assert color_at(rects, (10 + 0.15 - 0.0125) * MM, 1400 * UM) == section.COPPER
    for first, second in itertools.combinations(rects, 2):
        apart = (first[1] <= second[0] or second[1] <= first[0] or first[3] <= second[2] or second[3] <= first[2])
        assert apart, (first, second)


def test_plated_edges_where_both_outer_layers_reach_the_edge():
    outline = square(-20 * MM, -15 * MM, 20 * MM, 15 * MM)
    front = {"rings": [square(-20 * MM, -15 * MM, 0, 15 * MM)]}  # the left half
    back = {"rings": [square(-20 * MM, -15 * MM, 10 * MM, 15 * MM)]}  # three quarters
    stretches = section.plated_edges(outline, front, back)
    lengths = sorted(round(float(np.hypot(*(end - start))) / MM, 3) for start, end, _ in stretches)
    assert lengths == [20.0, 20.0, 30.0]  # bottom and top up to x = 0, and the whole left edge
    left = next(stretch for stretch in stretches if abs(stretch[0][0] + 20 * MM) < 1e-9 and abs(stretch[1][0] + 20 * MM) < 1e-9)
    assert np.allclose(left[2], (-1, 0))  # facing out of the board
    assert not section.plated_edges(outline, front, {"rings": []})  # the back has no copper at the edge


def test_cross_section_shows_the_plated_edge_as_a_strip_outside_the_board():
    outline = square(-20 * MM, -15 * MM, 20 * MM, 15 * MM)
    stretches = section.plated_edges(outline, {"rings": [square(-20 * MM, -15 * MM, 0, 15 * MM)]},
                                     {"rings": [square(-20 * MM, -15 * MM, 10 * MM, 15 * MM)]})
    copper, bands = section.stack_layout(HEIGHTS, THICKNESS, STACK, SAVED)
    rects = section.cross_section(ALONG_X, outline, {}, copper, bands, plated_edges=stretches, edge_plating=25 * UM)
    assert color_at(rects, -20 * MM - 10 * UM, 800 * UM) == section.COPPER  # the plating, outside the left edge
    assert color_at(rects, -20 * MM - 10 * UM, 1530 * UM) == section.COPPER  # the board's whole height
    assert color_at(rects, -20 * MM - 30 * UM, 800 * UM) is None  # 25 um thick
    assert color_at(rects, 20 * MM + 10 * UM, 800 * UM) is None  # the right edge is not plated


def _through(rings, land_lift=0.0, capped=False):
    """The same board with a through via at x = 10 mm instead (0.6 mm land, 0.3 mm drill);
    In1 is poured over the whole board, In2 is empty."""
    copper, bands = section.stack_layout(HEIGHTS, THICKNESS, STACK, SAVED)
    layers = {"In1.Cu": section.rings(ALONG_X, *square(-20 * MM, -15 * MM, 20 * MM, 15 * MM))}
    vias = {"xy": np.array([(10 * MM, 0.0)]), "diameter": np.array([0.6 * MM]), "drill": np.array([0.3 * MM]),
            "top": ["F.Cu"], "bottom": ["B.Cu"], "rings": [rings], "core_top": [capped], "core_bottom": [capped],
            "capped": [capped]}
    return section.cross_section(ALONG_X, square(-20 * MM, -15 * MM, 20 * MM, 15 * MM), layers, copper, bands,
                                 vias=vias, plating=25 * UM, cap_plating=20 * UM, land_lift=land_lift)


@pytest.mark.parametrize("rings, on_in2", [(section.RINGS_ALL, True), (section.RINGS_CONNECTED, False),
                                           (section.RINGS_ENDS_AND_CONNECTED, False), (section.RINGS_ENDS, False)])
def test_annular_rings_on_inner_layers_follow_kicad(rings, on_in2):
    rects = _through(rings)
    ring = (10 + 0.25) * MM  # on the land, beside the drill
    assert (color_at(rects, ring, 850 * UM) == section.COPPER) == on_in2  # In2: nothing connects here
    assert color_at(rects, ring, 1290 * UM) == section.COPPER  # In1's pour reaches it (and is copper anyway)
    assert color_at(rects, ring, 1520 * UM) == section.COPPER and color_at(rects, ring, 20 * UM) == section.COPPER
    assert color_at(rects, 10 * MM, 850 * UM) is None  # a ring, not across the bore


def test_lands_stand_as_far_out_as_in_3d():
    rects = _through(section.RINGS_ALL, land_lift=3 * UM)
    assert color_at(rects, (10 + 0.25) * MM, 1542 * UM) == section.COPPER  # F.Cu's top is 1540 um
    assert color_at(rects, (10 + 0.25) * MM, -2 * UM) == section.COPPER
    assert color_at(rects, 10 * MM, 1542 * UM) is None  # still a ring
    capped = _through(section.RINGS_ALL, land_lift=3 * UM, capped=True)
    assert color_at(capped, 10 * MM, 1542 * UM) == section.COPPER  # across the drill under the cap
    assert color_at(capped, 10 * MM, 1560 * UM) == section.COPPER  # the cap plating, from 1543 um
    assert color_at(capped, 10 * MM, 1564 * UM) is None


def test_a_plug_is_solder_mask_ink():
    rects = _board("RESIN", ink=True)
    assert color_at(rects, 10 * MM, 1400 * UM) == MASK
    assert color_at(rects, (10 + 0.15 - 0.0125) * MM, 1400 * UM) == section.COPPER  # inside the wall
