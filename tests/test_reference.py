"""The reference-plane engine on the synthetic split board (tests/split_board.py)."""

from dataclasses import replace

import numpy as np

import split_board
from kileido_bridge import model
from kileido_bridge.reference import Grid, Neighbour, ReferencePlanes, copper_neighbours, copper_order, rasterize
from split_board import ANTIPAD, LVDS_PITCH, LVDS_Y, MM, VIA_X

PIXEL = 50_000


def planes_of(snapshot):
    planes = ReferencePlanes()
    planes.update(snapshot)
    return planes


def at(planes, layer, x, y):
    """The net under a board point, or None."""
    grid = planes.grid
    label = planes.labels(layer)[(y - grid.y0) // grid.pixel, (x - grid.x0) // grid.pixel]
    return planes.net_of(layer, int(label))


def with_antipad(board, hole):
    """The board with In1.Cu's GND antipad of the first LVDS via replaced by `hole`."""
    gnd = board.zones[0]
    outer, void, _, second = gnd.polygons[0]
    return replace(board, zones=(replace(gnd, polygons=((outer, void, hole, second),)), *board.zones[1:]))


def first_via(planes):
    return planes.holes["LVDS_P"][0]


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


def test_every_piece_of_copper_is_in_the_raster():
    board = split_board.board()
    graphic = model.CopperGraphic("gnd-graphic", "GND", "In2.Cu",
                                  ((split_board.rect(5 * MM, 28 * MM, 45 * MM, 32 * MM),),))
    pad = model.Pad("pad", "", "1", "PWR", (70 * MM, 5 * MM), None,
                    {"In2.Cu": ((split_board.rect(69 * MM, 4 * MM, 71 * MM, 6 * MM),),)})
    track = model.Track("sig", "In2.Cu", "SIG", (5 * MM, 38 * MM), (95 * MM, 38 * MM), 200_000)
    planes = planes_of(replace(board, zones=(), graphics=(graphic,), pads=(pad,), tracks=(*board.tracks, track)))
    assert at(planes, "In2.Cu", 20 * MM, 30 * MM) == "GND"  # a copper graphic
    assert at(planes, "In2.Cu", 70 * MM, 5 * MM) == "PWR"  # a pad
    assert at(planes, "In2.Cu", 50 * MM, 38 * MM) == "SIG"  # a track
    assert at(planes, "In2.Cu", VIA_X, LVDS_Y) == "LVDS_P"  # a via land
    assert at(planes, "In2.Cu", 50 * MM, 20 * MM) is None
    assert at(planes, "F.Cu", 50 * MM, 10 * MM) == "USB_D+"


def test_split_plane_labels_both_nets():
    planes = planes_of(split_board.board())
    assert at(planes, "In1.Cu", 30 * MM, 10 * MM) == "GND"
    assert at(planes, "In1.Cu", 70 * MM, 10 * MM) == "+3V3"
    assert at(planes, "In1.Cu", 50 * MM, 10 * MM) is None  # the 0.5 mm split
    assert at(planes, "In1.Cu", 25_500_000, 35 * MM) is None  # the void


def test_only_changed_copper_is_rasterized_again():
    board = split_board.board()
    planes = planes_of(board)
    planes.labels("In1.Cu")
    assert planes.stats == {"rasterized": 1, "regions": 0, "antipads": 0}
    stamp = planes.version("In1.Cu")
    assert planes.update(replace(board)) == frozenset()
    patch = model.CopperGraphic("patch", "GND", "In1.Cu",
                                ((split_board.rect(24 * MM, 33 * MM, 27 * MM, 37 * MM),),))
    assert planes.update(replace(board, graphics=(patch,))) == frozenset({"In1.Cu"})
    assert at(planes, "In1.Cu", 25_500_000, 35 * MM) == "GND"  # the void is filled now
    assert planes.stats == {"rasterized": 1, "regions": 1, "antipads": 0}
    assert planes.changed_since("In1.Cu", stamp, (25 * MM, 30 * MM, 26 * MM, 34 * MM))  # overlaps it
    assert not planes.changed_since("In1.Cu", stamp, (60 * MM, 10 * MM, 61 * MM, 11 * MM))  # far from it
    assert not planes.changed_since("In1.Cu", planes.version("In1.Cu"), (0, 0, 100 * MM, 40 * MM))


def test_plane_breaks_show_whole_holes_near_a_path():
    planes = planes_of(split_board.board())
    path = ((24 * MM, 35 * MM), (27 * MM, 35 * MM))
    assert planes.plane_breaks("In1.Cu", path, 600_000, ("GND",)) == ((25 * MM, 34 * MM, 26 * MM, 36 * MM),)
    ((left, top, right, bottom),) = planes.plane_breaks("In1.Cu", ((48 * MM, 10 * MM), (52 * MM, 10 * MM)),
                                                        600_000, ("GND", "+3V3"))
    assert (left, right) == (49_750_000, 50_250_000)  # the split channel,
    assert 7 * MM < top < 10 * MM < bottom < 13 * MM  # clipped near the path
    assert planes.plane_breaks("In1.Cu", ((10 * MM, 30 * MM), (20 * MM, 30 * MM)), 600_000, ("GND",)) == ()
    assert planes.plane_breaks("In1.Cu", path, 600_000, ()) == ()  # nothing to compare with


def test_antipad_is_read_from_the_plane():
    planes = planes_of(split_board.board())
    window, mask, fill = planes.antipad("In1.Cu", first_via(planes), ("LVDS_P", "LVDS_N"))
    assert planes.net_of("In1.Cu", fill) == "GND"
    side = 2 * ANTIPAD // PIXEL
    assert mask.sum() == 2 * side * side  # its own square and the partner's next to it
    assert planes.antipad("In1.Cu", first_via(planes), ("LVDS_P", "LVDS_N"))[1] is mask  # cached
    assert planes.stats["antipads"] == 1


def test_a_larger_antipad_is_still_an_antipad():
    # 0.7 mm clearance, short of the partner via's antipad above it
    hole = split_board.rect(VIA_X - MM, LVDS_Y - MM, VIA_X + MM, LVDS_Y + 600_000)
    planes = planes_of(with_antipad(split_board.board(), hole))
    _, mask, _ = planes.antipad("In1.Cu", first_via(planes), ("LVDS_P",))
    assert mask.sum() == (2 * MM // PIXEL) * (1_600_000 // PIXEL)


def test_a_slot_running_out_of_an_antipad_is_no_antipad():
    slot = split_board.rect(VIA_X - ANTIPAD, LVDS_Y - 4 * MM, VIA_X + ANTIPAD, LVDS_Y + ANTIPAD)
    planes = planes_of(with_antipad(split_board.board(), slot))
    assert planes.antipad("In1.Cu", first_via(planes), ("LVDS_P",)) is None


def test_an_antipad_run_together_with_another_nets_hole_keeps_to_its_own_side():
    """A via of another net 1 mm below the LVDS via, both in one hole: only the part
    nearer the LVDS via is its antipad."""
    hole = split_board.rect(VIA_X - ANTIPAD, LVDS_Y - MM - ANTIPAD, VIA_X + ANTIPAD, LVDS_Y + ANTIPAD)
    board = with_antipad(split_board.board(), hole)
    other = model.Via("via-other", "OTHER", (VIA_X, LVDS_Y - MM), 600_000, 300_000, "F.Cu", "B.Cu")
    planes = planes_of(replace(board, vias=(*board.vias, other)))
    window, mask, _ = planes.antipad("In1.Cu", first_via(planes), ("LVDS_P",))
    rows = np.nonzero(mask.any(axis=1))[0] + window[0].start
    nearest = planes.grid.y0 + rows.min() * PIXEL  # the antipad's edge towards the other via
    assert LVDS_Y - MM // 2 - 2 * PIXEL <= nearest <= LVDS_Y - MM // 2 + 2 * PIXEL  # split halfway
    planes = planes_of(replace(board, vias=(*board.vias, replace(other, net="LVDS_P"))))
    _, mask, _ = planes.antipad("In1.Cu", first_via(planes), ("LVDS_P",))
    assert mask.sum() == (2 * ANTIPAD // PIXEL) * ((MM + 2 * ANTIPAD) // PIXEL)  # both its own: the whole hole


def test_antipads_are_found_again_only_after_copper_near_them_changes():
    board = split_board.board()
    planes = planes_of(board)
    planes.antipad("In1.Cu", first_via(planes), ("LVDS_P", "LVDS_N"))
    far = model.CopperGraphic("far", "GND", "In1.Cu", ((split_board.rect(80 * MM, 30 * MM, 81 * MM, 31 * MM),),))
    planes.update(replace(board, graphics=(far,)))
    planes.antipad("In1.Cu", first_via(planes), ("LVDS_P", "LVDS_N"))
    assert planes.stats["antipads"] == 1
    near = model.CopperGraphic("near", "GND", "In1.Cu",
                               ((split_board.rect(VIA_X - 300_000, LVDS_Y + LVDS_PITCH - 300_000,
                                                  VIA_X + 300_000, LVDS_Y + LVDS_PITCH + 300_000),),))
    planes.update(replace(board, graphics=(far, near)))
    planes.antipad("In1.Cu", first_via(planes), ("LVDS_P", "LVDS_N"))
    assert planes.stats["antipads"] == 2
