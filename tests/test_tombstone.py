"""Tombstone check: the board-side measures of small two-pad parts (kileido_bridge.tombstone)."""

import math

import numpy as np
import pytest

from kileido_bridge import model, tombstone

MM = 1_000_000
PAD_U, PAD_V = 500_000, 600_000  # an 0402's pads: 0.5 mm along the part, 0.6 mm across
PITCH = 1_000_000


def rect(cx, cy, width, height):
    w, h = width // 2, height // 2
    return ((cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h))


def pad(pad_id, net, x, layer="F.Cu", paste=1.0):
    polygons = {layer: ((rect(x, 0, PAD_U, PAD_V),),)}
    if paste:
        side = "F.Paste" if layer == "F.Cu" else "B.Paste"
        polygons[side] = ((rect(x, 0, int(PAD_U * paste), int(PAD_V * paste)),),)
    return model.Pad(pad_id, "R1", "1" if x < 0 else "2", net, (x, 0), None, polygons)


def board(pads=None, tracks=(), zones=(), graphics=(), vias=(), footprints=None):
    pads = pads if pads is not None else (pad("a", "SIG", -PITCH // 2), pad("b", "GND", PITCH // 2))
    footprints = footprints or (model.Footprint("R1", "R1", (0, 0), 0.0, "top", ()),)
    stack = model.Stackup((model.StackupLayer("F.Cu", "copper", 35_000, None, None, None),
                           model.StackupLayer("dielectric 1", "dielectric", 1_530_000, "FR4", 4.5, None),
                           model.StackupLayer("B.Cu", "copper", 35_000, None, None, None)))  # a 1.6 mm board
    return model.BoardSnapshot("test", {}, tuple(tracks), (), tuple(vias), tuple(pads), tuple(footprints),
                               tuple(zones), model.Outline(()), stack, (), {}, tuple(graphics))


def measures(snapshot):
    return tombstone.measure(snapshot, tombstone.candidates(snapshot))


# The GND pad's right edge is at x = 0.75 mm; copper leaves it towards +x.
EDGE = PITCH // 2 + PAD_U // 2
TRACE = model.Track("t", "F.Cu", "GND", (PITCH // 2, 0), (5 * MM, 0), 200_000)
CLEARANCE = 250_000
OTHER_PAD_HOLE = rect(-PITCH // 2, 0, PAD_U + 2 * CLEARANCE, PAD_V + 2 * CLEARANCE)
SQUARE = rect(0, 0, 10 * MM, 10 * MM)
SOLID = model.ZoneFill("z", "GND", "F.Cu", ((SQUARE, OTHER_PAD_HOLE),))
RELIEF_HOLE = rect(PITCH // 2, 0, PAD_U + 2 * CLEARANCE, PAD_V + 2 * CLEARANCE)
RELIEVED = model.ZoneFill("z", "GND", "F.Cu", ((SQUARE, OTHER_PAD_HOLE, RELIEF_HOLE),))
SPOKES = tuple(model.CopperGraphic(f"s{i}", "GND", "F.Cu", ((polygon,),)) for i, polygon in enumerate((
    rect(PITCH // 2, 0, PAD_U + 2 * CLEARANCE + 100_000, 250_000),  # left and right spokes
    rect(PITCH // 2, 0, 250_000, PAD_V + 2 * CLEARANCE + 100_000),  # up and down
)))


def test_a_two_pad_chip_part_is_a_candidate_and_others_are_not():
    assert [c.footprint.id for c in tombstone.candidates(board())] == ["R1"]
    through = pad("b", "GND", PITCH // 2)
    through = model.Pad(through.id, "R1", "2", "GND", through.pos, (300_000, 300_000), through.polygons)
    assert tombstone.candidates(board(pads=(pad("a", "SIG", -PITCH // 2), through))) == []
    far = (pad("a", "SIG", -3 * MM), pad("b", "GND", 3 * MM))
    assert tombstone.candidates(board(pads=far)) == []
    dnp = model.Footprint("R1", "R1", (0, 0), 0.0, "top", (), dnp=True)
    assert tombstone.candidates(board(footprints=(dnp,))) == []


def test_areas_and_the_frame_along_the_part():
    found = measures(board(pads=(pad("a", "SIG", -PITCH // 2, paste=0.9), pad("b", "GND", PITCH // 2, paste=0))))
    a, b = found["a"], found["b"]
    assert a.copper_nm2 == pytest.approx(PAD_U * PAD_V)
    assert a.paste_nm2 == pytest.approx(0.81 * PAD_U * PAD_V, rel=1e-3)
    assert b.paste_nm2 == 0
    assert (a.length_nm, a.width_nm) == pytest.approx((PAD_U, PAD_V))


def test_an_unconnected_pad_has_no_copper_path():
    found = measures(board())
    assert found["b"].resistance_k_w == math.inf
    assert max(found["b"].coverage) == 0


def test_a_trace_conducts_like_a_strip_of_its_width():
    found = measures(board(tracks=[TRACE]))["b"]
    # 0.5 mm of 0.2 mm wide, 35 um copper: L / (k t w) = 183 K/W
    expected = tombstone.REACH_NM * 1e-9 / (tombstone.COPPER_W_MK * 35e-6 * 200e-6)
    assert found.resistance_k_w == pytest.approx(expected, rel=0.25)
    perimeter = 2 * (PAD_U + PAD_V) + 2 * math.pi * 300_000
    assert found.coverage[12] == pytest.approx(200_000 / perimeter, rel=0.3)


def test_a_plane_conducts_far_better_than_its_thermal_relief_and_both_better_than_a_trace():
    solid = measures(board(zones=[SOLID]))["b"]
    relief = measures(board(zones=[RELIEVED], graphics=SPOKES))["b"]
    trace = measures(board(tracks=[TRACE]))["b"]
    assert min(solid.coverage) > 0.6
    spokes = 4 * 250_000 / (2 * (PAD_U + PAD_V) + 2 * math.pi * 2.5 * tombstone.CELL_NM)
    assert relief.coverage[2] == pytest.approx(spokes, rel=0.15)  # four 0.25 mm spokes,
    assert relief.coverage[-1] > 0.6  # then the plane
    assert solid.resistance_k_w < 0.6 * relief.resistance_k_w  # wide spokes: 45 % of the edge
    assert relief.resistance_k_w < trace.resistance_k_w


def test_another_nets_copper_does_not_count():
    other = model.ZoneFill("z", "VCC", "F.Cu", ((SQUARE, OTHER_PAD_HOLE, RELIEF_HOLE),))
    assert max(measures(board(zones=[other]))["b"].coverage) == 0


def test_vias_and_an_open_via_in_pad():
    def via(x, protection=model.FROM_RULES):
        return model.Via(f"v{x}", "GND", (x, 0), 400_000, 200_000, "F.Cu", "B.Cu", protection)
    filled = tuple(1 if name == "fill" else 0 for name in model.PROTECTION)
    found = measures(board(vias=[via(PITCH // 2), via(EDGE + 400_000), via(EDGE + 2 * MM)]))["b"]
    assert found.vias == 2 and found.open_via_in_pad
    assert not measures(board(vias=[via(PITCH // 2, filled)]))["b"].open_via_in_pad


def test_the_strip_raster_fills_rings_with_holes_and_overlaps():
    edges, owners, labels = tombstone._copper_edges(board(zones=[SOLID], tracks=[TRACE]), "F.Cu")
    x0 = y0 = -6 * MM
    columns = rows = int(12 * MM / tombstone.CELL_NM)
    cells = tombstone._strip(edges, owners, x0, y0, columns, 0, rows)
    gnd = labels["GND"]
    area = (cells == gnd).sum() * tombstone.CELL_NM ** 2
    hole = (PAD_U + 2 * CLEARANCE) * (PAD_V + 2 * CLEARANCE)
    assert area == pytest.approx(100 * MM * MM - hole, rel=0.01)  # the trace inside the plane adds nothing
    centre = int(6 * MM / tombstone.CELL_NM)
    assert cells[centre, int(5.5 * MM / tombstone.CELL_NM)] == labels["SIG"]  # the other pad, in its hole


def test_thousands_of_parts_stay_quick():
    pads, footprints, tracks = [], [], []
    for i in range(2500):
        x, y = (i % 50) * 3 * MM, (i // 50) * 3 * MM
        footprints.append(model.Footprint(f"R{i}", f"R{i}", (x, y), 0.0, "top", ()))
        for j, dx in enumerate((-PITCH // 2, PITCH // 2)):
            net = "GND" if j else f"N{i}"
            pads.append(model.Pad(f"p{i}{j}", f"R{i}", str(j + 1), net, (x + dx, y), None,
                                  {"F.Cu": ((rect(x + dx, y, PAD_U, PAD_V),),)}))
        tracks.append(model.Track(f"t{i}", "F.Cu", f"N{i}", (x - PITCH // 2, y), (x - 2 * MM, y), 150_000))
    plane = model.ZoneFill("z", "GND", "F.Cu", ((rect(75 * MM, 75 * MM, 160 * MM, 160 * MM),),))
    snapshot = board(pads=pads, tracks=tracks, zones=[plane], footprints=footprints)
    import time
    started = time.perf_counter()
    found = measures(snapshot)
    elapsed = time.perf_counter() - started
    assert len(found) == 5000
    assert elapsed < 3.0, elapsed  # measured and reported below; the limit only catches a blow-up
    print(f"5000 pads: {elapsed * 1000:.0f} ms")


# --- Risk ---

SIG_TRACE = model.Track("u", "F.Cu", "SIG", (-PITCH // 2, 0), (-5 * MM, 0), 200_000)


def levels(snapshot):
    return {f.reference: f.level for f in tombstone.check(snapshot)[1]}


def test_a_symmetric_part_is_not_reported():
    gnd_trace = model.Track("t", "F.Cu", "GND", (PITCH // 2, 0), (5 * MM, 0), 200_000)
    assert levels(board(tracks=[SIG_TRACE, gnd_trace])) == {}


def test_one_pad_in_a_plane_and_the_other_on_a_trace_is_high():
    checked, found = tombstone.check(board(tracks=[SIG_TRACE], zones=[SOLID]))
    assert checked == 1 and [f.level for f in found] == ["HIGH"]
    assert found[0].package == "0402" and "F2" in found[0].factors[0]
    assert "pad 1 melts first" in found[0].message and found[0].items == ("R1",)
    assert found[0].lifts == 1 and found[0].keys == ("F2",)  # pad 2's end, in the plane, is still unmelted
    assert found[0].layer == "F.Cu" and found[0].pads == ("a", "b") and found[0].pad_names == ("pad 1", "pad 2")


def test_an_open_via_in_one_pad_is_high_and_a_filled_one_is_not():
    def via(protection=model.FROM_RULES):
        return model.Via("v", "SIG", (-PITCH // 2, 0), 400_000, 200_000, "F.Cu", "B.Cu", protection)
    gnd_trace = model.Track("t", "F.Cu", "GND", (PITCH // 2, 0), (5 * MM, 0), 200_000)
    found = tombstone.check(board(tracks=[SIG_TRACE, gnd_trace], vias=[via()]))[1]
    assert found and found[0].level == "HIGH" and any(f.startswith("F1 HIGH") for f in found[0].factors)
    capped = tuple(1 if name == "cap" else 0 for name in model.PROTECTION)
    found = tombstone.check(board(tracks=[SIG_TRACE, gnd_trace], vias=[via(capped)]))[1]
    assert not any("F1" in f for finding in found for f in finding.factors)


def test_paste_on_one_pad_only_is_high():
    gnd_trace = model.Track("t", "F.Cu", "GND", (PITCH // 2, 0), (5 * MM, 0), 200_000)
    pads = (pad("a", "SIG", -PITCH // 2), pad("b", "GND", PITCH // 2, paste=0))
    found = tombstone.check(board(pads=pads, tracks=[SIG_TRACE, gnd_trace]))[1]
    assert found[0].level == "HIGH" and found[0].message == "R1: paste on pad 1 only"
    assert found[0].lifts == 1  # the dry end


def test_heat_imbalance_steps_with_the_copper_each_pad_is_tied_into():
    # a thermal relief against a trace: MEDIUM (a plane against a trace: HIGH, above)
    assert levels(board(tracks=[SIG_TRACE], zones=[RELIEVED], graphics=SPOKES)) == {"R1": "MEDIUM"}
    # a pad in a plane against the other in that plane's thermal relief: not worth a word
    both = (pad("a", "GND", -PITCH // 2), pad("b", "GND", PITCH // 2))
    plane = model.ZoneFill("z", "GND", "F.Cu", ((SQUARE, RELIEF_HOLE),))
    assert levels(board(pads=both, zones=[plane], graphics=SPOKES)) == {}


def test_packages_are_recognised_from_the_pads_and_bigger_ones_are_not_checked():
    def part(pitch, along, across):
        pads = tuple(model.Pad(n, "R1", n, net, (x, 0), None, {"F.Cu": ((rect(x, 0, along, across),),)})
                     for n, net, x in (("1", "SIG", -pitch // 2), ("2", "GND", pitch // 2)))
        fp = model.Footprint("R1", "R1", (0, 0), 0.0, "top", ())
        hole = rect(-pitch // 2, 0, along + 2 * CLEARANCE, across + 2 * CLEARANCE)  # the signal pad's clearance
        plane = model.ZoneFill("z", "GND", "F.Cu", ((SQUARE, hole),))
        trace = model.Track("u", "F.Cu", "SIG", (-pitch // 2, 0), (-5 * MM, 0), 200_000)
        return board(pads=pads, footprints=(fp,), tracks=[trace], zones=[plane])

    def package(snapshot):
        found = tombstone.candidates(snapshot)
        return tombstone.package_of(found[0], tombstone.measure(snapshot, found))

    assert package(part(500_000, 400_000, 300_000)).name == "01005"
    assert package(part(640_000, 460_000, 400_000)).name == "0201"
    assert package(part(1_020_000, 540_000, 640_000)).name == "0402"
    # The same plane-against-trace imbalance: HIGH on an 0402
    assert levels(part(1_020_000, 540_000, 640_000)) == {"R1": "HIGH"}
    # An 0402's pitch with pads like no size: measured, then ruled out, so not counted
    assert package(part(1_000_000, 200_000, 200_000)) is None
    assert tombstone.check(part(1_000_000, 200_000, 200_000)) == (0, [])
    # 0603 and bigger are not candidates at all
    for pitch, along, across in ((1_650_000, 800_000, 950_000), (2_925_000, 1_125_000, 1_750_000)):
        assert tombstone.candidates(part(pitch, along, across)) == []
        assert tombstone.check(part(pitch, along, across)) == (0, [])


def test_the_report_is_json_ready():
    import json
    report = tombstone.report(board(tracks=[SIG_TRACE], zones=[SOLID]))
    assert json.loads(json.dumps(report))["findings"][0]["reference"] == "R1"
