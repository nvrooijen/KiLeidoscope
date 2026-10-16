"""Planes and return paths (`kileido_bridge/planes.py`): stackup neighbours, coverage with
antipads filled, plane edges, islands and their ties, return vias, stubs and the lines
stitching follows. On the split-plane board (tests/split_board.py) and small hand-built
snapshots."""

import math
from dataclasses import replace

import numpy as np
import pytest

import split_board
from kileido_bridge import model, planes
from kileido_bridge.board_index import BoardIndex

MM = 1_000_000


def rect(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def square(x, y, half):
    return rect(x - half, y - half, x + half, y + half)


def snapshot(tracks=(), arcs=(), vias=(), pads=(), zones=(), stackup=()):
    return model.BoardSnapshot("t", {}, tuple(tracks), tuple(arcs), tuple(vias), tuple(pads), (), tuple(zones),
                               model.Outline(()), model.Stackup(tuple(stackup)), (), {})


def via(id, net, x, y, top="F.Cu", bottom="B.Cu", diameter=600_000):
    return model.Via(id, net, (x, y), diameter, 300_000, top, bottom)


def track(id, net, layer, x0, y0, x1, y1, width=150_000):
    return model.Track(id, layer, net, (x0, y0), (x1, y1), width)


def zone(id, net, layer, *polygons):
    return model.ZoneFill(id, net, layer, tuple(polygons))


@pytest.fixture(scope="module")
def split():
    return BoardIndex(split_board.board())


# stackup

def test_copper_order_from_the_stackup_counts_dielectric_only():
    names, gaps, exact = planes.copper_order(split_board.board())
    assert names == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu") and gaps == (200_000, 1_000_000, 200_000) and exact
    neighbours = planes.copper_neighbours(split_board.board())
    assert neighbours["F.Cu"] == (None, planes.Neighbour("In1.Cu", 200_000))
    assert neighbours["In2.Cu"] == (planes.Neighbour("In1.Cu", 1_000_000), planes.Neighbour("B.Cu", 200_000))
    assert planes.next_to(split_board.board(), "In1.Cu") == ("F.Cu", "In2.Cu")
    assert planes.next_to(split_board.board(), "In7.Cu") == ()


def test_copper_order_without_a_stackup_spreads_the_layers_evenly():
    board = snapshot(tracks=[track("a", "A", layer, 0, 0, MM, 0) for layer in ("B.Cu", "In10.Cu", "In2.Cu", "F.Cu")])
    names, gaps, exact = planes.copper_order(board)
    assert names == ("F.Cu", "In2.Cu", "In10.Cu", "B.Cu") and not exact  # numeric order, not In10 < In2
    assert gaps == (533_333,) * 3


# antipads and coverage

def test_split_board_coverage(split):
    usb = [t for t in split.snapshot.tracks if t.net == "USB_D+"]
    stretches = planes.coverage(usb, planes.plane_under(split, "GND", "In1.Cu", usb))
    assert [(s.start[0], s.end[0], s.covered) for s in stretches] == [(10 * MM, 49_750_000, True),
                                                                      (49_750_000, 90 * MM, False)]
    sata = [t for t in split.snapshot.tracks if t.net == "SATA_P"]
    stretches = planes.coverage(sata, planes.plane_under(split, "GND", "In1.Cu", sata))
    assert [(s.start[0], s.end[0], s.covered) for s in stretches] == [
        (10 * MM, 25 * MM, True), (25 * MM, 26 * MM, False), (26 * MM, 40 * MM, True)]  # the 1 mm void
    assert stretches[1].length == MM and stretches[1].middle == (25_500_000, 35 * MM)
    assert stretches[1].layer == "F.Cu" and stretches[1].width == split_board.WIDTH and stretches[1].id == "sata-0"


def test_own_antipads_count_as_plane_and_the_partners_too(split):
    lvds = [t for t in split.snapshot.tracks if t.net == "LVDS_P"]
    raw = planes.coverage(lvds, planes.plane(split, "GND", "In1.Cu"))
    assert [s.covered for s in raw] == [True, False, False, True]  # the antipad under each end at the via
    filled = planes.coverage(lvds, planes.plane_under(split, "GND", "In1.Cu", lvds))
    assert [s.covered for s in filled] == [True, True]
    assert planes.signal_nets(split, ["LVDS_P"]) == {"LVDS_P", "LVDS_N"}
    assert planes.signal_nets(split, ["CLK", ""]) == {"CLK"}
    # the void of the SATA pair holds no via of theirs: it stays
    sata = [t for t in split.snapshot.tracks if t.net == "SATA_P"]
    assert len(planes.plane_under(split, "GND", "In1.Cu", sata)[0]) == 4  # outline, void, two LVDS antipads


def test_an_antipad_is_a_hole_closing_near_the_via():
    big = square(10 * MM, 0, 2_500_000)  # a 5 mm void around the via: a void, not its antipad
    small = square(20 * MM, 0, 400_000)
    other = square(30 * MM, 0, 400_000)  # around another net's via
    plane = zone("z", "GND", "In1.Cu", (rect(0, -5 * MM, 40 * MM, 5 * MM), big, small, other))
    vias = [via("v1", "SIG", 10 * MM, 0), via("v2", "SIG", 20 * MM, 0), via("v3", "OTHER", 30 * MM, 0)]
    index = BoardIndex(snapshot(vias=vias, zones=[plane]))
    holes = planes.own_holes(index, {"SIG"}, "In1.Cu")
    assert holes.tolist() == [[10 * MM, 0], [20 * MM, 0]]
    (rings,) = planes.fill_antipads(planes.plane(index, "GND", "In1.Cu"), holes)
    assert len(rings) == 3 and not any(np.array_equal(ring, np.array(small, dtype=float)) for ring in rings)
    assert planes.own_holes(index, {"SIG"}, "F.Cu").shape == (2, 2)
    blind = BoardIndex(snapshot(vias=[via("b", "SIG", 20 * MM, 0, "F.Cu", "In1.Cu")], zones=[plane]))
    assert planes.own_holes(blind, {"SIG"}, "In2.Cu").shape == (0, 2)  # the via stops above In2.Cu


def test_a_plated_hole_of_the_net_has_an_antipad_too():
    pad = model.Pad("p", "", "1", "SIG", (20 * MM, 0), (1 * MM, 1 * MM),
                    {"F.Cu": ((square(20 * MM, 0, 800_000),),), "B.Cu": ((square(20 * MM, 0, 800_000),),)})
    hole = model.Pad("h", "", "", "", (30 * MM, 0), (1 * MM, 1 * MM), {})  # not plated: no antipad of SIG
    plane = zone("z", "GND", "In1.Cu", (rect(0, -5 * MM, 40 * MM, 5 * MM), square(20 * MM, 0, MM)))
    index = BoardIndex(snapshot(pads=[pad, hole], zones=[plane]))
    assert planes.own_holes(index, {"SIG"}, "In1.Cu").tolist() == [[20 * MM, 0]]
    line = [track("t", "SIG", "F.Cu", 15 * MM, 0, 25 * MM, 0)]
    assert all(s.covered for s in planes.coverage(line, planes.plane_under(index, "GND", "In1.Cu", line)))


def test_coverage_of_an_arc_and_of_two_overlapping_fills():
    one = zone("z1", "GND", "In1.Cu", (rect(0, 0, 4 * MM, 12 * MM),))
    two = zone("z2", "GND", "In1.Cu", (rect(MM, 0, 25 * MM, 12 * MM),))  # overlaps z1: no cancelling out
    arc = model.Arc("a", "F.Cu", "SIG", (2 * MM, 5 * MM), (12 * MM, 15 * MM), (22 * MM, 5 * MM), 100_000)
    index = BoardIndex(snapshot(arcs=[arc], zones=[one, two]))
    stretches = planes.coverage([arc], planes.plane(index, "GND", "In1.Cu"))
    covered = sum(s.length for s in stretches if s.covered)
    bare = sum(s.length for s in stretches if not s.covered)
    states = [s.covered for s in stretches]  # one or more per chord of the sampled arc
    assert [state for i, state in enumerate(states) if not i or state != states[i - 1]] == [True, False, True]
    assert all(a.end == b.start for a, b in zip(stretches, stretches[1:]))  # out past y = 12 mm and back
    assert covered + bare == pytest.approx(math.pi * 10 * MM, rel=1e-3)
    assert stretches[0].start == (2 * MM, 5 * MM) and stretches[-1].end == (22 * MM, 5 * MM)


def test_a_track_through_a_corner_leaves_no_sliver():
    plane = zone("z", "GND", "In1.Cu", (rect(0, 0, 10 * MM, 10 * MM),))
    line = [track("t", "SIG", "F.Cu", 9 * MM, 1 - MM, 11 * MM, MM + 1)]  # clips the corner (10, 0) by 1 nm
    index = BoardIndex(snapshot(tracks=line, zones=[plane]))
    stretches = planes.coverage(line, planes.plane(index, "GND", "In1.Cu"))
    assert len(stretches) == 1 and not stretches[0].covered


def test_runs_join_gaps_that_meet():
    stretches = [planes.Stretch("a", "F.Cu", 1, (0, 0), (MM, 0), False),
                 planes.Stretch("b", "F.Cu", 1, (MM, 0), (MM, 3 * MM), False),
                 planes.Stretch("c", "F.Cu", 1, (5 * MM, 0), (7 * MM, 0), False),
                 planes.Stretch("d", "B.Cu", 1, (MM, 3 * MM), (MM, 9 * MM), False)]  # meets b, but on B.Cu
    found = planes.joined(stretches)
    assert [length for length, _ in found] == [6 * MM, 4 * MM, 2 * MM]
    assert [s.id for s in found[1][1]] == ["a", "b"] and planes.joined([]) == []


# edges

def test_edges_near_a_gap_are_clipped_to_the_box(split):
    copper = planes.plane(split, "GND", "In1.Cu")
    edges = planes.edges_near(copper, [(49_750_000, 10 * MM, 90 * MM, 10 * MM)], MM)
    assert edges == [[(49_750_000, 9 * MM), (49_750_000, 11 * MM)]]  # the split's edge, not the whole side
    void = planes.edges_near(copper, [(25 * MM, 35 * MM, 26 * MM, 35 * MM)], MM)
    assert void == [[(25 * MM, 34 * MM), (26 * MM, 34 * MM), (26 * MM, 36 * MM), (25 * MM, 36 * MM),
                     (25 * MM, 34 * MM)]]  # a hole wholly in the box: closed
    twice = planes.edges_near(copper, [(25 * MM, 35 * MM, 26 * MM, 35 * MM), (25 * MM, 35.2 * MM, 26 * MM, 35.2 * MM)],
                              MM)
    assert twice == void  # overlapping boxes are merged: each edge once
    assert planes.edges_near(copper, [(60 * MM, 20 * MM, 61 * MM, 20 * MM)], MM) == []


def test_an_edge_leaving_and_entering_the_box_is_two_lines():
    ring = ((0, 0), (10 * MM, 0), (10 * MM, 10 * MM), (0, 10 * MM))
    copper = ((np.array(ring, dtype=float),),)
    lines = planes.edges_near(copper, [(-MM, 4 * MM, 11 * MM, 6 * MM)], 0)
    assert sorted(lines) == [[(0, 6 * MM), (0, 4 * MM)], [(10 * MM, 4 * MM), (10 * MM, 6 * MM)]]
    corner = planes.edges_near(copper, [(9 * MM, -MM, 11 * MM, MM)], 0)
    assert corner == [[(9 * MM, 0), (10 * MM, 0), (10 * MM, MM)]]  # one line round the corner


# vias

def test_via_layers_are_where_it_meets_its_net(split):
    lvds = next(v for v in split.snapshot.vias if v.net == "LVDS_P")
    assert planes.via_layers(split, lvds) == ("F.Cu", "B.Cu")
    stub = via("v", "SIG", 0, 0)
    tracks = [track("t", "SIG", "In1.Cu", 0, 0, MM, 0), track("u", "SIG", "F.Cu", 0, 0, 0, MM)]
    index = BoardIndex(snapshot(tracks=tracks, vias=[stub, via("w", "SIG", 5 * MM, 0)]))
    assert planes.via_layers(index, stub) == ("F.Cu", "In1.Cu")
    assert planes.via_layers(index, index.snapshot.vias[1]) == ()
    assert planes.via_layers(BoardIndex(snapshot(vias=[via("n", "", 0, 0)])), via("n", "", 0, 0)) == ()


def test_reference_at_finds_the_fill_of_another_net_next_to_the_layer(split):
    lvds = next(v for v in split.snapshot.vias if v.net == "LVDS_P")
    nets = planes.signal_nets(split, ["LVDS_P"])
    assert planes.reference_at(split, "F.Cu", lvds.pos, nets) == ("GND", "In1.Cu")  # inside its antipad
    assert planes.reference_at(split, "B.Cu", lvds.pos, nets) == ("GND", "In2.Cu")
    assert planes.reference_at(split, "F.Cu", (75 * MM, 10 * MM), {"USB_D+"}) == ("+3V3", "In1.Cu")
    assert planes.reference_at(split, "F.Cu", (25_500_000, 35 * MM), {"SATA_P"}) is None  # the void
    assert planes.reference_at(split, "F.Cu", (75 * MM, 10 * MM), {"+3V3"}) is None  # own fill is no reference
    assert planes.reference_at(split, "In7.Cu", (75 * MM, 10 * MM), set()) is None


def test_reference_at_takes_the_nearer_side_and_the_upper_one_on_a_tie():
    layer = model.StackupLayer
    stack = [layer("F.Cu", "copper", 35_000, None, None, None), layer("d1", "dielectric", 300_000, None, None, None),
             layer("In1.Cu", "copper", 35_000, None, None, None), layer("d2", "dielectric", 300_000, None, None, None),
             layer("In2.Cu", "copper", 35_000, None, None, None), layer("d3", "dielectric", 100_000, None, None, None),
             layer("B.Cu", "copper", 35_000, None, None, None)]
    fills = [zone(f"z-{name}", net, name, (rect(0, 0, 10 * MM, 10 * MM),))
             for name, net in (("F.Cu", "GND"), ("In2.Cu", "VCC"), ("B.Cu", "GND"))]
    index = BoardIndex(snapshot(zones=fills, stackup=stack))
    assert planes.reference_at(index, "In1.Cu", (5 * MM, 5 * MM), set()) == ("GND", "F.Cu")  # 0.3 mm each way
    assert planes.reference_at(index, "In2.Cu", (5 * MM, 5 * MM), set()) == ("GND", "B.Cu")  # 0.1 below


def test_return_vias_join_both_planes_nearest_first():
    vias = [via("sig", "SIG", 0, 0), via("far", "GND", 3 * MM, 0), via("near", "GND", 0, MM),
            via("blind", "GND", 0, 500_000, "F.Cu", "In1.Cu"), via("vcc", "VCC", 100_000, 0)]
    hole = model.Pad("p", "", "1", "GND", (2 * MM, 0), (MM, MM), {"F.Cu": ((square(2 * MM, 0, 800_000),),)})
    smd = model.Pad("s", "", "2", "GND", (100_000, 100_000), None, {"F.Cu": ((square(100_000, 100_000, 50_000),),)})
    index = BoardIndex(snapshot(vias=vias, pads=[hole, smd]))
    found = planes.return_vias(index, {"GND"}, {"In1.Cu", "In2.Cu"}, (0, 0), skip=index.snapshot.vias[0])
    assert [(distance, record.id) for distance, record in found] == [(MM, "near"), (2 * MM, "p"), (3 * MM, "far")]
    assert [r.id for _, r in planes.return_vias(index, {"GND"}, {"In1.Cu"}, (0, 0))][:2] == ["blind", "near"]


# islands

def test_islands_on_the_split_board_join_through_a_gnd_via(split):
    found = planes.islands(split, "GND")
    assert [(i.layer, i.zone, i.main, i.tied) for i in found] == [("In1.Cu", "zone-gnd-in1", False, False),
                                                                    ("In2.Cu", "zone-gnd-in2", True, True)]
    assert found[0].group != found[1].group  # no GND via joins the two planes
    in1 = 48.75 * 38 - 1 * 2 - 2 * 1.1 * 1.1  # mm²: outline less the void and two antipads
    assert found[0].area_nm2 / 1e12 == pytest.approx(in1)
    tied = planes.islands(BoardIndex(split_board.with_return_via(split_board.board())), "GND")
    assert [(i.main, i.tied) for i in tied] == [(True, True), (True, True)] and tied[0].group == tied[1].group


def test_islands_are_tied_by_vias_plated_holes_and_overlapping_fills():
    plane_in1 = zone("z1", "GND", "In1.Cu", (rect(0, 0, 10 * MM, 10 * MM),), (rect(20 * MM, 0, 25 * MM, 5 * MM),),
                     (rect(30 * MM, 0, 32 * MM, 2 * MM),), (rect(40 * MM, 0, 42 * MM, 2 * MM),),
                     (rect(50 * MM, 0, 52 * MM, 2 * MM),))
    plane_in2 = zone("z2", "GND", "In2.Cu", (rect(0, 0, 60 * MM, 20 * MM),))
    patch = zone("z3", "GND", "In1.Cu", (rect(51 * MM, 1 * MM, 55 * MM, 3 * MM),))  # overlaps the last piece
    vias = [via("a", "GND", 5 * MM, 5 * MM), via("b", "GND", 22 * MM, 2 * MM),  # tie pieces 1 and 2 to In2.Cu
            via("c", "GND", 35 * MM, 1 * MM),  # beside piece 3: no copper of it within its land
            via("d", "GND", 54 * MM, 2 * MM, "In1.Cu", "In2.Cu"),  # ties the patch, so the last piece too
            via("e", "SIG", 31 * MM, 1 * MM)]  # another net's via ties nothing
    edge = model.Pad("p", "", "1", "GND", (43 * MM, MM), (MM, MM),  # a plated hole touching piece 4's edge
                     {"In1.Cu": ((square(43 * MM, MM, MM),),), "In2.Cu": ((square(43 * MM, MM, MM),),)})
    index = BoardIndex(snapshot(vias=vias, pads=[edge], zones=[plane_in1, plane_in2, patch]))
    found = planes.islands(index, "GND")
    loose = [(i.layer, round(i.area_nm2 / 1e12, 3)) for i in found if not i.tied]
    assert loose == [("In1.Cu", 4.0)]  # piece 3 alone
    mains = [(i.layer, i.zone, round(i.area_nm2 / 1e12)) for i in found if i.main]
    assert mains == [("In1.Cu", "z1", 100), ("In2.Cu", "z2", 1200)]
    assert len({i.group for i in found}) == 2
    assert planes.islands(index, "NOPE") == []


def test_a_via_on_a_thermal_spoke_ties_and_one_in_a_clearance_does_not():
    # the fill stops 0.2 mm from the via's 0.3 mm land; a spoke reaches the land on one side
    k = 100_000
    relief = ((45 * k, 45 * k), (55 * k, 45 * k), (55 * k, 49 * k), (53 * k, 49 * k), (53 * k, 51 * k),
              (55 * k, 51 * k), (55 * k, 55 * k), (45 * k, 55 * k))
    with_spoke = zone("z1", "GND", "In1.Cu", (rect(0, 0, 10 * MM, 10 * MM), relief))
    clear = zone("z2", "GND", "In1.Cu", (rect(20 * MM, 0, 30 * MM, 10 * MM), square(25 * MM, 5 * MM, 500_000)))
    under = zone("z3", "GND", "In2.Cu", (rect(0, 0, 30 * MM, 10 * MM),))
    vias = [via("a", "GND", 5 * MM, 5 * MM), via("b", "GND", 25 * MM, 5 * MM)]
    found = planes.islands(BoardIndex(snapshot(vias=vias, zones=[with_spoke, clear, under])), "GND")
    assert [(i.zone, round(i.area_nm2 / 1e12, 2), i.tied) for i in found] == [
        ("z1", 99.04, True), ("z2", 99.0, False), ("z3", 300.0, True)]


def test_a_track_ties_pieces_its_copper_touches():
    pieces = zone("z", "GND", "F.Cu", (rect(0, 0, 10 * MM, 10 * MM),), (rect(20 * MM, 0, 25 * MM, 5 * MM),))
    bridge = track("t", "GND", "F.Cu", 5 * MM, 2 * MM, 22 * MM, 2 * MM)
    short = track("s", "GND", "F.Cu", 12 * MM, 2 * MM, 18 * MM, 2 * MM)  # touches neither piece
    other = track("o", "SIG", "F.Cu", 5 * MM, 3 * MM, 22 * MM, 3 * MM)  # another net ties nothing
    tied = planes.islands(BoardIndex(snapshot(tracks=[bridge], zones=[pieces])), "GND")
    assert [i.tied for i in tied] == [True, True] and tied[0].group == tied[1].group
    apart = planes.islands(BoardIndex(snapshot(tracks=[short, other], zones=[pieces])), "GND")
    assert [(i.main, i.tied) for i in apart] == [(True, True), (False, False)]


def test_a_track_between_two_vias_ties_pieces_on_another_layer():
    # a 2-layer board: two B.Cu pieces, a via in each, joined by F.Cu tracks through a pad
    pieces = zone("z", "GND", "B.Cu", (rect(0, 0, 10 * MM, 10 * MM),), (rect(20 * MM, 0, 25 * MM, 5 * MM),))
    vias = [via("a", "GND", 5 * MM, 5 * MM), via("b", "GND", 22 * MM, 2 * MM)]
    pad = model.Pad("p", "", "1", "GND", (15 * MM, 5 * MM), None, {"F.Cu": ((square(15 * MM, 5 * MM, MM),),)})
    tracks = [track("t1", "GND", "F.Cu", 5 * MM, 5 * MM, 14 * MM, 5 * MM),  # ends in the pad
              track("t2", "GND", "F.Cu", 16 * MM, 5 * MM, 22 * MM, 2 * MM)]
    found = planes.islands(BoardIndex(snapshot(tracks=tracks, vias=vias, pads=[pad], zones=[pieces])), "GND")
    assert [i.tied for i in found] == [True, True]
    cut = planes.islands(BoardIndex(snapshot(tracks=tracks[:1], vias=vias, pads=[pad], zones=[pieces])), "GND")
    assert [i.tied for i in cut] == [True, False]


# stubs

def test_barrel_length_counts_dielectric_and_copper_past_the_used_layer():
    board = split_board.board()
    assert planes.barrel_length(board, "F.Cu", "B.Cu") == (1_505_000, True)
    assert planes.barrel_length(board, "In2.Cu", "B.Cu") == (235_000, True)  # 0.2 mm and B.Cu's copper
    assert planes.barrel_length(board, "In1.Cu", "F.Cu") == (235_000, True)  # upwards
    assert planes.barrel_length(board, "In1.Cu", "In1.Cu") == (0, True)
    layers = list(board.stackup.layers)
    layers[3] = replace(layers[3], thickness_nm=None)  # Dielectric 2
    holed = replace(board, stackup=model.Stackup(tuple(layers)))
    assert planes.barrel_length(holed, "In2.Cu", "B.Cu") == (235_000, True)  # does not cross it
    assert planes.barrel_length(holed, "F.Cu", "B.Cu") is None
    layer = model.StackupLayer
    two = (layer("F.Cu", "copper", 35_000, None, None, None), layer("core", "dielectric", 1_500_000, None, None, None),
           layer("B.Cu", "copper", 35_000, None, None, None))
    unnamed = snapshot(tracks=[track("t", "A", "In1.Cu", 0, 0, MM, 0)], stackup=two)  # In1.Cu not in the stackup
    assert planes.barrel_length(unnamed, "In1.Cu", "B.Cu") == (785_000, False)  # half of 1.57 mm


def test_stubs_past_the_last_used_layer(split):
    lvds = next(v for v in split.snapshot.vias if v.net == "LVDS_P")
    through = planes.stub(split, lvds)
    assert through.used == ("F.Cu", "B.Cu") and through.stub == () and through.length_nm == 0
    board = split_board.with_vias(split_board.board(), "GND", [(5 * MM, 2 * MM)])
    board = split_board.with_vias(board, "CLK", [(60 * MM, 5 * MM)], "clk")
    index = BoardIndex(board)
    ground = planes.stub(index, index.records("stitch-0")[0])  # meets both GND planes, nothing outside
    assert (ground.used, ground.stub, ground.length_nm) == (("In1.Cu", "In2.Cu"), ("In2.Cu", "B.Cu"), 235_000)
    assert (ground.other, ground.other_nm, ground.exact) == (("In1.Cu", "F.Cu"), 235_000, True)
    clock = planes.stub(index, index.records("clk-0")[0])  # on the F.Cu track only
    assert (clock.used, clock.stub, clock.length_nm, clock.other) == (("F.Cu", "F.Cu"), ("F.Cu", "B.Cu"),
                                                                      1_505_000, ())
    loose = BoardIndex(split_board.with_vias(split_board.board(), "", [(60 * MM, 25 * MM)]))
    found = planes.stub(loose, loose.snapshot.vias[-1])  # no net: its whole barrel
    assert found.used == () and found.stub == ("F.Cu", "B.Cu") and found.length_nm == 1_505_000
    bare = BoardIndex(replace(board, stackup=model.Stackup(())))
    assert planes.stub(bare, bare.records("clk-0")[0]) is None
    assert planes.stub(bare, bare.records("via-lvds-0")[0]).stub == ()  # no stub: no length needed


def test_a_plated_hole_and_a_stacked_via_meet_their_net():
    land = {"F.Cu": ((square(0, 0, MM),),), "B.Cu": ((square(0, 0, MM),),)}
    pad = model.Pad("p", "", "1", "SIG", (0, 0), (MM, MM), land)
    smd = model.Pad("s", "", "2", "SIG", (5 * MM, 0), None, {"F.Cu": ((square(5 * MM, 0, MM),),)})
    tracks = [track("t", "SIG", "F.Cu", 0, 0, 3 * MM, 0), track("u", "SIG", "In1.Cu", 0, 0, 0, 3 * MM)]
    index = BoardIndex(snapshot(tracks=tracks, pads=[pad, smd]))
    assert planes.plated(pad) and not planes.plated(smd)
    assert planes.barrel(index, pad) == ("F.Cu", "In1.Cu", "B.Cu")
    assert planes.via_layers(index, pad) == ("F.Cu",)  # no land on In1.Cu: the track there misses it
    assert planes.stub(index, pad) is None  # no stackup: no length
    micro = via("m", "SIG", 10 * MM, 0, "F.Cu", "In1.Cu")
    buried = via("b", "SIG", 10 * MM, 0, "In1.Cu", "In2.Cu")
    stack = [track("f", "SIG", "F.Cu", 10 * MM, 0, 12 * MM, 0), track("g", "SIG", "In2.Cu", 10 * MM, 0, 12 * MM, 0)]
    index = BoardIndex(snapshot(tracks=stack, vias=[micro, buried]))
    assert planes.via_layers(index, micro) == ("F.Cu", "In1.Cu")  # In1.Cu: the buried via under it
    assert planes.via_layers(index, buried) == ("In1.Cu", "In2.Cu")


# lines followed

def test_a_line_round_a_ring():
    ring = rect(0, 0, 10 * MM, 10 * MM)
    line = planes.Line.of(ring)
    assert line.length == 40 * MM and line.at(15 * MM) == (10 * MM, 5 * MM) and line.at(45 * MM) == (5 * MM, 0)
    distance, along = line.project(np.array([[5 * MM, -MM], [11 * MM, 5 * MM]], dtype=float))
    assert distance.tolist() == [MM, MM] and along.tolist() == [5 * MM, 15 * MM]
    assert line.stretch(35 * MM, 10 * MM) == [(0, 5 * MM), (0, 0), (5 * MM, 0)]  # round past the start
    whole = line.stretch(5 * MM, 40 * MM)
    assert whole[0] == whole[-1] == (5 * MM, 0) and len(whole) == 6


def test_gaps_between_vias_along_a_line():
    assert planes.gaps([5.0, 15.0], 40.0) == [(5.0, 10.0), (15.0, 30.0)]
    assert planes.gaps([], 40.0) == [(0.0, 40.0)]


def test_stitches_along_a_pour_and_along_the_edge():
    points = [(5 * MM, 2 * MM), (30 * MM, 2 * MM), (10 * MM, 2 * MM), (20 * MM, 10 * MM), (40 * MM, 500_000)]
    index = BoardIndex(split_board.with_vias(split_board.board(), "GND", points, "st"))
    pour = planes.largest(planes.plane(index, "GND", "In1.Cu"))
    line = planes.Line.of(pour[0])
    found = planes.stitches(index, "GND", pour, line, 2 * MM, "In1.Cu")
    assert [(along, apart, v.id) for along, apart, v in found] == [(4 * MM, MM, "st-0"), (9 * MM, MM, "st-2"),
                                                                    (29 * MM, MM, "st-1")]  # (40, 0.5) misses it
    (board,) = planes.outline(index.snapshot)
    edge = planes.stitches(index, "GND", board, planes.Line.of(board[0]), 2 * MM)
    assert [v.id for _, _, v in edge] == ["st-0", "st-2", "st-1", "st-4"]  # inside the board, 2 mm at most
    assert planes.stitches(index, "+3V3", pour, line, 2 * MM, "In1.Cu") == []


def test_on_centre_line_and_the_reference_across_a_split(split):
    usb = next(t for t in split.snapshot.tracks if t.net == "USB_D+")
    assert planes.on_centre_line(usb, (50 * MM, 11 * MM)) == ((50 * MM, 10 * MM), (1.0, 0.0))
    arc = model.Arc("a", "F.Cu", "SIG", (0, 0), (MM, MM), (2 * MM, 0), 100_000)
    spot, direction = planes.on_centre_line(arc, (MM, 2 * MM))
    assert math.dist(spot, (MM, MM)) < 1_000 and abs(direction[1]) < 0.15  # a chord at the top
    nets = planes.signal_nets(split, ["USB_D+"])
    assert planes.reference_across(split, "F.Cu", (49 * MM, 10 * MM), (51 * MM, 10 * MM), nets) == (
        ("GND", "In1.Cu"), ("+3V3", "In1.Cu"), (49_750_000, 10 * MM))
    assert planes.reference_across(split, "F.Cu", (20 * MM, 10 * MM), (22 * MM, 10 * MM), nets) is None
    void = planes.reference_across(split, "F.Cu", (24 * MM, 35 * MM), (25_500_000, 35 * MM), {"SATA_P"})
    assert void == (("GND", "In1.Cu"), None, (25 * MM, 35 * MM))  # into the void: no plane
