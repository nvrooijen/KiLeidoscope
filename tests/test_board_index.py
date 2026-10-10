"""Findings geometry: lookups, nearest items and closest points on a board snapshot."""

import json
import math
from pathlib import Path

import pytest

from kileido_bridge import model
from kileido_bridge.board_index import BoardIndex, copper_rank, gap, on_layer, point_gap, shape_of
from kileido_bridge.targets import SNAP_NM, resolve

MM = 1_000_000
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def square(x0, y0, x1, y1):
    return (((x0, y0), (x1, y0), (x1, y1), (x0, y1)),)


def board():
    """Two parts, a signal net with a track, an arc and a via, a ground plane with a hole."""
    footprints = (
        model.Footprint("f-u3", "U3", (10 * MM, 10 * MM), 0.0, "top", (), (9 * MM, 9 * MM, 2 * MM, 2 * MM)),
        model.Footprint("f-c12", "C12", (20 * MM, 10 * MM), 0.0, "top", ()),
        model.Footprint("f-r1a", "R1", (30 * MM, 30 * MM), 0.0, "top", ()),
        model.Footprint("f-r1b", "R1", (32 * MM, 30 * MM), 0.0, "top", ()),
    )
    pad_square = {"F.Cu": (square(19.5 * MM, 9.5 * MM, 20.5 * MM, 10.5 * MM),),
                  "F.Paste": (square(19.6 * MM, 9.6 * MM, 20.4 * MM, 10.4 * MM),)}
    pads = (
        model.Pad("p-u3-4", "f-u3", "4", "SIG", (10 * MM, 10 * MM), None,
                  {"F.Cu": (square(9.8 * MM, 9.8 * MM, 10.2 * MM, 10.2 * MM),)}),
        model.Pad("p-c12-1", "f-c12", "1", "SIG", (20 * MM, 10 * MM), None, pad_square),
        model.Pad("p-u3-ep1", "f-u3", "EP", "GND", (10 * MM, 9 * MM), None,
                  {"F.Cu": (square(9.9 * MM, 8.9 * MM, 10.1 * MM, 9.1 * MM),)}),
        model.Pad("p-u3-ep2", "f-u3", "EP", "GND", (10 * MM, 11 * MM), None,
                  {"F.Cu": (square(9.9 * MM, 10.9 * MM, 10.1 * MM, 11.1 * MM),)}),
    )
    tracks = (model.Track("aaaa0000-0000-4000-8000-00000000t001", "F.Cu", "SIG", (10 * MM, 10 * MM),
                          (20 * MM, 10 * MM), 200_000),
              model.Track("t-weird", "F.Cu", "/~{RST}", (0, 20 * MM), (10 * MM, 20 * MM), 200_000))
    arcs = (model.Arc("arc-1", "B.Cu", "SIG", (0, 0), (MM, MM), (2 * MM, 0), 100_000),)
    vias = (model.Via("00000000-0000-0000-0000-00005a222dbd", "SIG", (15 * MM, 15 * MM), 600_000, 300_000,
                      "F.Cu", "In2.Cu"),
            model.Via("11111111-0000-0000-0000-00005a222dbd", "GND", (16 * MM, 15 * MM), 600_000, 300_000,
                      "F.Cu", "B.Cu"))
    plane = ((((0, 0), (40 * MM, 0), (40 * MM, 40 * MM), (0, 40 * MM)),
              ((10 * MM, 20 * MM), (20 * MM, 20 * MM), (20 * MM, 30 * MM), (10 * MM, 30 * MM))),)
    zones = (model.ZoneFill("z-gnd", "GND", "In1.Cu", plane), model.ZoneFill("z-gnd", "GND", "In2.Cu", plane))
    outline = model.Outline((square(-5 * MM, -5 * MM, 45 * MM, 45 * MM),))
    stackup = model.Stackup(())
    return model.BoardSnapshot("t", {}, tracks, arcs, vias, pads, footprints, zones, outline, stackup, (), {})


@pytest.fixture
def index():
    return BoardIndex(board())


# geometry

def test_copper_rank_orders_inner_layers_numerically():
    assert copper_rank("F.Cu") < copper_rank("In2.Cu") < copper_rank("In10.Cu") < copper_rank("B.Cu")
    assert copper_rank("F.SilkS") is None


def test_parallel_tracks_gap_is_edge_to_edge():
    a = shape_of(model.Track("a", "F.Cu", "", (0, 0), (10 * MM, 0), 200_000))
    b = shape_of(model.Track("b", "F.Cu", "", (2 * MM, MM), (5 * MM, MM), 400_000))
    result = gap(a, b)
    assert result.distance == pytest.approx(MM - 100_000 - 200_000)
    assert result.a[1] == 100_000 and result.b[1] == MM - 200_000
    assert 2 * MM <= result.a[0] <= 5 * MM


def test_crossing_tracks_touch_at_the_crossing():
    a = shape_of(model.Track("a", "F.Cu", "", (0, 0), (10 * MM, 10 * MM), 100_000))
    b = shape_of(model.Track("b", "F.Cu", "", (0, 10 * MM), (10 * MM, 0), 100_000))
    result = gap(a, b)
    assert result.distance == 0 and result.a == result.b == (5 * MM, 5 * MM)


def test_via_inside_a_plane_hole_measures_to_the_hole_edge():
    hole_plane = shape_of(model.ZoneFill("z", "GND", "In1.Cu", board().zones[0].polygons))
    via = shape_of(model.Via("v", "SIG", (15 * MM, 25 * MM), 600_000, 300_000, "F.Cu", "B.Cu"))
    result = gap(via, hole_plane)
    assert result.distance == pytest.approx(5 * MM - 300_000)


def test_via_on_solid_plane_overlaps():
    plane = shape_of(model.ZoneFill("z", "GND", "In1.Cu", board().zones[0].polygons))
    via = shape_of(model.Via("v", "SIG", (5 * MM, 5 * MM), 600_000, 300_000, "F.Cu", "B.Cu"))
    assert gap(via, plane).distance == 0


def test_arc_distance_follows_the_curve():
    arc = shape_of(model.Arc("a", "F.Cu", "", (0, 0), (MM, MM), (2 * MM, 0), 0))  # radius 1 mm around (1, 0)
    result = point_gap(arc, (MM, 3 * MM))
    assert result.distance == pytest.approx(2 * MM, abs=10_000)


def test_copper_to_board_edge_measures_to_the_outline_not_its_area():
    outline = shape_of(model.Outline((square(0, 0, 10 * MM, 10 * MM),)))
    track = shape_of(model.Track("t", "F.Cu", "", (2 * MM, 9 * MM), (8 * MM, 9 * MM), 200_000))
    assert gap(track, outline).distance == pytest.approx(MM - 100_000)


# lookups

def test_reference_pad_and_repeated_pad_numbers(index):
    assert [f.id for f in index.footprints("U3")] == ["f-u3"]
    assert [p.id for p in index.pads("U3", "4")] == ["p-u3-4"]
    assert {p.id for p in index.pads("U3", "EP")} == {"p-u3-ep1", "p-u3-ep2"}


def test_net_on_layer_includes_vias_spanning_it(index):
    on_in1 = {item.id for item in index.net_items("SIG", "In1.Cu")}
    assert on_in1 == {"00000000-0000-0000-0000-00005a222dbd"}  # F.Cu..In2.Cu via
    assert not index.net_items("SIG", "B.Cu")[0].id.startswith("0000")  # only the arc
    via = index.records("00000000-0000-0000-0000-00005a222dbd")[0]
    assert on_layer(via, "In2.Cu") and not on_layer(via, "B.Cu")


def test_short_ids_use_the_last_eight_hex(index):
    assert index.ids_ending("00000001") == ()
    assert len(index.ids_ending("5a222dbd")) == 2  # legacy-style ids: ambiguous on 8 hex
    assert index.ids_ending("0000-00005a222dbd") == ("00000000-0000-0000-0000-00005a222dbd",
                                                     "11111111-0000-0000-0000-00005a222dbd")
    assert index.ids_ending("11111111-0000-0000-0000-00005A222DBD") == ("11111111-0000-0000-0000-00005a222dbd",)
    assert [r.layer for r in index.records("z-gnd")] == ["In1.Cu", "In2.Cu"]


def test_nearest_skips_far_items_and_respects_max_distance(index):
    near = index.nearest((15 * MM, 10.5 * MM), index.layer_items("F.Cu"))
    assert near.record.id.endswith("t001") and near.gap.distance == pytest.approx(400_000)
    assert index.nearest((15 * MM, 12 * MM), index.layer_items("F.Cu"), max_distance=MM) is None


def test_a_part_measures_as_its_pads_copper_not_its_box():
    """A footprint's box takes in its texts: distances from a part use its pads' copper,
    and only a part with no copper pads falls back to its box."""
    part = model.Footprint("f-u1", "U1", (10 * MM, 10 * MM), 0.0, "top", (), (5 * MM, 5 * MM, 10 * MM, 10 * MM))
    bare = model.Footprint("f-h1", "H1", (30 * MM, 10 * MM), 0.0, "top", (), (29 * MM, 9 * MM, 2 * MM, 2 * MM))
    pads = (model.Pad("p-u1-1", "f-u1", "1", "A", (10 * MM, 10 * MM), None,
                      {"F.Cu": (square(9.5 * MM, 9.5 * MM, 10.5 * MM, 10.5 * MM),)}),
            model.Pad("p-u1-2", "f-u1", "2", "B", (10 * MM, 12 * MM), None,
                      {"B.Cu": (square(9.5 * MM, 11.5 * MM, 10.5 * MM, 12.5 * MM),)}),
            model.Pad("p-h1", "f-h1", "", "", (30 * MM, 10 * MM), (MM, MM), {}))  # a plain hole: no copper
    track = model.Track("t1", "F.Cu", "A", (13 * MM, 0), (13 * MM, 20 * MM), 200_000)
    index = BoardIndex(model.BoardSnapshot("p", {}, (track,), (), (), pads, (part, bare), (), model.Outline(()),
                                           model.Stackup(()), (), {}))
    found, record, _ = index.closest((part,), (track,))
    assert record is part and found.distance == pytest.approx(2.4 * MM)  # its box would reach the track
    assert index.shape(part).bbox == pytest.approx((9.5 * MM, 9.5 * MM, 10.5 * MM, 12.5 * MM))
    assert index.shape(part, "B.Cu").bbox == pytest.approx((9.5 * MM, 11.5 * MM, 10.5 * MM, 12.5 * MM))
    assert index.shape(bare).bbox == shape_of(bare).bbox == pytest.approx((29 * MM, 9 * MM, 31 * MM, 11 * MM))
    assert index.nearest((13 * MM, 10 * MM), (part,)).gap.distance == pytest.approx(2.5 * MM)


def test_closest_between_part_pads(index):
    result, a, b = index.closest(index.pads("U3", "4"), index.pads("C12", "1"), "F.Cu")
    assert result.distance == pytest.approx(9.3 * MM)
    assert result.a == (10.2 * MM, result.a[1]) and result.b[0] == 19.5 * MM


# targets

def test_resolve_part_pad_and_missing(index):
    assert resolve(index, "U3").items[0].id == "f-u3"
    assert resolve(index, "C12.1").items[0].id == "p-c12-1"
    assert resolve(index, "C12.9").note == "C12 has no pad 9"
    assert not resolve(index, "U99").found
    shared = resolve(index, "R1")
    assert len(shared.items) == 2 and "share" in shared.note


def test_resolve_net_forms(index):
    assert len(resolve(index, "net:SIG").items) == 5  # track, arc, via, two pads
    assert {i.id for i in resolve(index, "net:SIG@B.Cu").items} == {"arc-1"}
    picked = resolve(index, "net:SIG@F.Cu~15,10.1")
    assert [i.id for i in picked.items] == ["aaaa0000-0000-4000-8000-00000000t001"] and picked.note == ""
    assert resolve(index, "net:/~{RST}").items[0].id == "t-weird"
    assert resolve(index, "net:/~{RST}@F.Cu~5,20").items[0].id == "t-weird"
    assert resolve(index, "net:NOPE").note == "net NOPE not found on board"


def test_resolve_far_point_says_how_far(index):
    far = resolve(index, "net:SIG@F.Cu~15,13")
    assert isinstance(far.items[0], model.Via) and far.note.endswith("is 1.70 mm from the point")


def test_resolve_uuid_layer_edge(index):
    assert resolve(index, "uuid:00000t001").items[0].layer == "F.Cu"
    assert "2 items end in" in resolve(index, "uuid:5a222dbd").note
    assert len(resolve(index, "uuid:z-gnd").items) == 2
    assert {i.id for i in resolve(index, "layer:In2.Cu").items} >= {"z-gnd"}
    assert resolve(index, "layer:F.SilkS").note == "F.SilkS is not a copper layer"
    assert isinstance(resolve(index, "edge").items[0], model.Outline)


def test_resolve_point_snaps_within_half_a_millimetre(index):
    snapped = resolve(index, "pt:15,10.4@F.Cu")
    assert snapped.items[0].id.endswith("t001") and snapped.note.startswith("snapped to track 0.30 mm")
    assert snapped.point == (15 * MM, 10.4 * MM)
    missed = resolve(index, "pt:15,12@F.Cu")
    assert missed.items == () and missed.found and "0.50 mm" in missed.note
    assert resolve(index, "pt:abc").note == "point is not x,y in mm"
    assert SNAP_NM == 500_000


def test_fixture_board_resolves_by_reference_and_short_id():
    snapshot = model.snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
    index = BoardIndex(snapshot)
    via = snapshot.vias[0]
    assert resolve(index, f"uuid:{via.id[-8:]}").items == (via,)
    assert resolve(index, "J1").items[0].reference == "J1"
    near = index.nearest(via.pos, index.layer_items("In1.Cu"))
    assert near is not None and math.isfinite(near.gap.distance)


def test_targets_tolerate_spaces_case_and_braces(index):
    track = "aaaa0000-0000-4000-8000-00000000t001"
    assert [i.id for i in resolve(index, "net:SIG@F.Cu ~15,10.1").items] == [track]
    assert resolve(index, "NET: SIG @ f.cu").layer == "F.Cu"
    assert resolve(index, "uuid:{00000000-0000-0000-0000-00005a222dbd}").found
    assert resolve(index, "pt:.5,-0.0@F.Cu").point == (500_000, 0)
    silk = resolve(index, "pt:15,10.4@F.SilkS")  # not snapped to copper, no note
    assert silk.found and silk.items == () and silk.layer == "F.SilkS" and silk.note == ""


def test_plain_holes_are_not_copper_but_keep_their_id():
    snapshot = board()
    hole = model.Pad("p-npth", "f-u3", "", "", (5 * MM, 5 * MM), (MM, MM), {})
    snapshot = model.BoardSnapshot(**{**snapshot.__dict__, "pads": (*snapshot.pads, hole)})
    index = BoardIndex(snapshot)
    assert hole not in index.layer_items("F.Cu")
    assert index.records("p-npth") == (hole,)


def test_large_zones_measure_fast():
    import time
    ring = [(round(20 * MM + 10 * MM * math.cos(t / 3000 * math.tau)),
             round(20 * MM + 10 * MM * math.sin(t / 3000 * math.tau))) for t in range(3000)]
    zone = shape_of(model.ZoneFill("z", "GND", "F.Cu", ((tuple(ring),),)))
    outline = shape_of(model.Outline((square(0, 0, 40 * MM, 40 * MM),)))
    other = shape_of(model.ZoneFill("o", "VCC", "F.Cu", ((tuple((x + 25 * MM, y) for x, y in ring),),)))
    begin = time.perf_counter()
    assert gap(zone, outline).distance == pytest.approx(10 * MM, abs=1_000)
    assert gap(zone, other).distance == pytest.approx(5 * MM, abs=1_000)
    assert time.perf_counter() - begin < 2.0


def test_a_point_target_in_capitals_counts_as_found(index):
    upper = resolve(index, "PT:15,10.4@F.Cu")
    assert upper.found and upper.point == resolve(index, "pt:15,10.4@F.Cu").point
