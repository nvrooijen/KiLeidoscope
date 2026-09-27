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
    gap_path = found["usb-0"].subpath(*found["usb-0"].below.gaps[0])
    assert planes.nets_near("In1.Cu", gap_path, MARGIN, MARGIN) == ("+3V3", "GND")


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
    _, found = look_up(board, ["lvds-f-0", "lvds-f-1", "lvds-b-0"])
    assert found["lvds-f-0"].below.gaps == () and found["lvds-b-0"].above.gaps == ()
    assert found["lvds-f-1"].below.gaps == ()  # the partner's antipad beside it too
    # The same holes under vias of another net are voids.
    foreign = replace(board, vias=tuple(replace(via, net="OTHER") for via in board.vias))
    _, found = look_up(foreign, ["lvds-f-0"])
    (gap,) = found["lvds-f-0"].below.gaps
    assert gap[1] == 10 * MM and gap[0] < 10 * MM - (split_board.ANTIPAD + MARGIN) + PIXEL


def with_antipad(board, hole):
    """The board with In1.Cu's GND antipad of the first LVDS via replaced by `hole`."""
    gnd = board.zones[0]
    outer, void, _, second = gnd.polygons[0]
    return replace(board, zones=(replace(gnd, polygons=((outer, void, hole, second),)), *board.zones[1:]))


def test_antipad_size_comes_from_the_geometry():
    x, y = split_board.VIA_X, split_board.LVDS_Y
    board = with_antipad(split_board.board(), split_board.square(x, y, 1_200_000))  # 0.9 mm clearance
    _, found = look_up(board, ["lvds-f-0"])
    assert found["lvds-f-0"].below.gaps == ()


def test_a_slot_running_out_of_an_antipad_is_a_void():
    x, y = split_board.VIA_X, split_board.LVDS_Y
    slot = split_board.rect(x - 550_000, y - 4 * MM, x + 550_000, y + 550_000)  # the antipad, 4 mm long
    planes, found = look_up(with_antipad(split_board.board(), slot), ["lvds-f-0"])
    (gap,) = found["lvds-f-0"].below.gaps
    assert gap[1] == 10 * MM and gap[0] < 10 * MM - 550_000
    assert planes.stats["antipads"] >= 1


def test_all_copper_on_a_layer_can_be_a_reference():
    board = split_board.board()
    graphic = model.CopperGraphic("gnd-graphic", "GND", "In1.Cu",
                                  ((split_board.rect(5 * MM, 28 * MM, 45 * MM, 32 * MM),),))
    wide = model.Track("gnd-wide", "In1.Cu", "GND", (5 * MM, 35_125_000), (45 * MM, 35_125_000), 4 * MM)
    narrow = model.Track("sig", "In1.Cu", "SIG", (5 * MM, 10_125_000), (95 * MM, 10_125_000), 200_000)
    pad = model.Pad("gnd-pad", "", "1", "GND", (20 * MM, 5 * MM), None,
                    {"In1.Cu": ((split_board.rect(10 * MM, 3 * MM, 30 * MM, 7 * MM),),)})
    board = replace(board, zones=board.zones[2:], graphics=(graphic,), tracks=(*board.tracks, wide, narrow),
                    pads=(pad,))
    _, found = look_up(board, ["eth-0", "sata-0", "usb-0", "clk"])
    assert found["eth-0"].below.planes == ((0, 30 * MM, "GND"),)  # a copper graphic
    assert found["sata-0"].below.covered_fraction == 1.0  # a 4 mm wide GND track
    assert found["usb-0"].below.covered_fraction == 0.0  # a narrow track is no plane
    # A pad 20 mm long under CLK, less the margin at both ends.
    assert found["clk"].below.covered_fraction == pytest.approx((20 * MM - 2 * MARGIN) / (80 * MM), abs=0.002)


def test_both_sides_are_reported_and_the_fuller_one_is_primary():
    track = model.Track("inner", "In1.Cu", "SIG", (10 * MM, 20 * MM), (40 * MM, 20 * MM), WIDTH)
    _, found = look_up(replace(split_board.board(), tracks=(track,)), ["inner"])
    segment = found["inner"]
    assert segment.above.layer == "F.Cu" and segment.above.covered_fraction == 0.0
    assert segment.below.layer == "In2.Cu" and segment.below.margin_nm == 3_000_000
    assert segment.primary is segment.below


def test_only_changed_copper_and_items_are_looked_up_again():
    board = split_board.board()
    planes = ReferencePlanes()
    planes.update(board)
    planes.segments()
    stats = dict(planes.stats)
    assert (stats["rasterized"], stats["regions"], stats["computed"]) == (2, 0, len(board.tracks))
    assert planes.update(replace(board)) == frozenset()
    planes.segments()
    assert planes.stats == stats  # nothing new
    # A moved F.Cu track: F.Cu is nobody's reference here, so only that track is looked up.
    moved = replace(board.tracks[0], end=(80 * MM, 10 * MM))
    board = replace(board, tracks=(moved, *board.tracks[1:]))
    assert planes.update(board) == frozenset({"F.Cu"})
    assert planes.segments()[moved.id].length_nm == 70 * MM
    assert planes.stats["computed"] == stats["computed"] + 1 and planes.stats["rasterized"] == 2
    # GND copper over the SATA void: In1.Cu is patched there, and only the tracks near it
    # (the SATA and ETH pairs) are looked up again.
    patch = model.CopperGraphic("patch", "GND", "In1.Cu",
                                ((split_board.rect(24 * MM, 33 * MM, 27 * MM, 37 * MM),),))
    board = replace(board, graphics=(patch,))
    assert planes.update(board) == frozenset({"In1.Cu"})
    found = planes.segments()
    assert found["sata-0"].below.gaps == ()
    assert planes.stats["regions"] == 1 and planes.stats["rasterized"] == 2
    assert planes.stats["computed"] == stats["computed"] + 1 + 4
