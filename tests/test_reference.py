"""Reference-plane lookup on the synthetic split board (tests/split_board.py)."""

from dataclasses import replace

import numpy as np
import pytest

import split_board
from kileido_bridge import model
from kileido_bridge.reference import (Grid, Neighbour, ReferencePlanes, copper_neighbours, copper_order, erode,
                                      rasterize)
from split_board import MM

PIXEL = 50_000
MARGIN = 600_000  # 3 x 0.2 mm dielectric
WIDTH = split_board.WIDTH


def look_up(snapshot, ids=None):
    planes = ReferencePlanes()
    planes.update(snapshot)
    return planes, planes.segments(ids)


def test_neighbours_and_dielectric_distances():
    neighbours = copper_neighbours(split_board.board())
    assert neighbours["F.Cu"] == (None, Neighbour("In1.Cu", 200_000))
    assert neighbours["In1.Cu"] == (Neighbour("F.Cu", 200_000), Neighbour("In2.Cu", 1_000_000))
    assert neighbours["B.Cu"] == (Neighbour("In2.Cu", 200_000), None)


def test_missing_stackup_falls_back_to_the_viewer_heights():
    snapshot = replace(split_board.board(), stackup=model.Stackup(()))
    names, gaps, exact = copper_order(snapshot)
    assert not exact
    assert names == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert sum(gaps) == 1_600_000 and len(set(gaps)) <= 2  # evenly spaced over 1.6 mm (rounded)


def test_rasterize_paints_pixel_centres_even_odd():
    grid = Grid(0, 0, PIXEL, 400, 400)
    square = ((0, 0), (10 * MM, 0), (10 * MM, 10 * MM), (0, 10 * MM))
    hole = ((4 * MM, 4 * MM), (6 * MM, 4 * MM), (6 * MM, 6 * MM), (4 * MM, 6 * MM))
    labels = rasterize(grid, [(1, (square, hole)), (2, (((12 * MM, 0), (14 * MM, 0), (14 * MM, 2 * MM)),))])
    assert (labels == 1).sum() == 200 * 200 - 40 * 40
    assert labels[100, 100] == 0 and labels[10, 10] == 1
    assert 0 < (labels == 2).sum() < 40 * 40 // 2 + 40  # a triangle: half of its 2 x 2 mm box


def test_erode_keeps_a_label_only_inside_uniform_squares():
    labels = np.zeros((20, 20), np.int16)
    labels[:, :10], labels[:, 11:] = 1, 2  # two planes with a one-pixel split
    eroded = erode(labels, 3)
    assert (eroded[5, 3:7] == 1).all() and (eroded[5, 7:14] == 0).all() and (eroded[5, 14:17] == 2).all()
    # The raster's edge counts as no copper.
    assert not eroded[:3].any() and not eroded[17:].any() and not eroded[:, :3].any() and not eroded[:, 17:].any()
    assert (erode(labels, 0) == labels).all()


def test_split_under_a_diff_pair():
    planes, found = look_up(split_board.board(), ["usb-0", "usb-1"])
    for segment in found.values():
        assert segment.above is None and segment.primary is segment.below
        cover = segment.below
        assert (cover.layer, cover.distance_nm, cover.margin_nm) == ("In1.Cu", 200_000, MARGIN)
        (gap,) = cover.gaps
        # The 0.5 mm split at x = 50 mm, widened by the margin, 40 mm from the start (x = 10 mm).
        assert abs(gap[0] - (39_750_000 - MARGIN)) <= PIXEL and abs(gap[1] - (40_250_000 + MARGIN)) <= PIXEL
        assert [net for _, _, net in cover.planes] == ["GND", "+3V3"]
        assert cover.covered_fraction == pytest.approx(1 - (gap[1] - gap[0]) / segment.length_nm)
    assert planes.nets_near("In1.Cu", found["usb-0"].subpath(*found["usb-0"].below.gaps[0]), MARGIN) == \
        ("+3V3", "GND")


def test_solid_plane_covers_the_whole_track():
    _, found = look_up(split_board.board(), ["eth-0"])
    cover = found["eth-0"].below
    assert cover.covered_fraction == 1.0 and cover.gaps == ()
    assert cover.planes == ((0, 30 * MM, "GND"),)


def test_arc_is_looked_up_along_its_path():
    arc = model.Arc("arc", "F.Cu", "SATA_P", (20 * MM, 35 * MM), (25_500_000, 35_500_000), (31 * MM, 35 * MM), WIDTH)
    _, found = look_up(replace(split_board.board(), arcs=(arc,)), ["arc"])
    segment = found["arc"]
    assert len(segment.path) > 3 and segment.length_nm > 11 * MM
    (gap,) = segment.below.gaps
    assert 24 * MM < segment.point_at((gap[0] + gap[1]) / 2)[0] < 27 * MM  # over the void at x = 25..26 mm


def test_own_via_antipads_are_excused():
    board = split_board.board()
    _, found = look_up(board, ["lvds-f-0", "lvds-b-0"])
    assert found["lvds-f-0"].below.gaps == () and found["lvds-b-0"].above.gaps == ()
    # The same holes under a via of another net are voids.
    foreign = replace(board, vias=tuple(replace(via, net="OTHER") for via in board.vias))
    _, found = look_up(foreign, ["lvds-f-0"])
    (gap,) = found["lvds-f-0"].below.gaps
    assert gap[1] == 10 * MM and gap[0] < 10 * MM - (split_board.ANTIPAD + MARGIN) + PIXEL


def test_both_sides_are_reported_and_the_fuller_one_is_primary():
    track = model.Track("inner", "In1.Cu", "SIG", (10 * MM, 20 * MM), (40 * MM, 20 * MM), WIDTH)
    _, found = look_up(replace(split_board.board(), tracks=(track,)), ["inner"])
    segment = found["inner"]
    assert segment.above.layer == "F.Cu" and segment.above.covered_fraction == 0.0
    assert segment.below.layer == "In2.Cu" and segment.below.margin_nm == 3_000_000
    assert segment.primary is segment.below


def test_only_changed_items_and_layers_are_looked_up_again():
    board = split_board.board()
    planes = ReferencePlanes()
    planes.update(board)
    planes.segments()
    assert planes.stats == {"rasterized": 2, "eroded": 2, "computed": len(board.tracks)}
    assert planes.update(replace(board)) == frozenset()
    planes.segments()
    assert planes.stats["computed"] == len(board.tracks)  # nothing new
    moved = replace(board.tracks[0], end=(80 * MM, 10 * MM))
    board = replace(board, tracks=(moved, *board.tracks[1:]))
    planes.update(board)
    assert planes.segments()[moved.id].length_nm == 70 * MM
    assert planes.stats == {"rasterized": 2, "eroded": 2, "computed": len(board.tracks) + 1}
    # A refilled In2.Cu: only the B.Cu tracks it references are looked up again.
    gnd = next(zone for zone in board.zones if zone.layer == "In2.Cu")
    board = replace(board, zones=(*[zone for zone in board.zones if zone is not gnd],
                                  replace(gnd, polygons=((gnd.polygons[0][0],),))))  # antipads gone
    assert planes.update(board) == frozenset({"In2.Cu"})
    planes.segments()
    assert planes.stats == {"rasterized": 3, "eroded": 3, "computed": len(board.tracks) + 3}
