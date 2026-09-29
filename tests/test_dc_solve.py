"""DC analysis: synthetic boards through the snapshot adapter and the vendored solver,
against analytic results."""

import math

import numpy as np
import pytest

import dc_board
from dc_board import MM
from kileido_bridge import dc, model
from kileido_bridge.dcsolve import raster
from kileido_bridge.dcworker import solve

RHO = dc.RHO_CU_OHM_M
SIGMA = dc_board.COPPER_NM * 1e-9 / RHO  # sheet conductance, S per square


def setup(supply="J1.1", load="J2.1", volts=3.3, amps=1.0, **settings):
    return dc.DcSetup(dc_board.NET, [dc.DcTerminal("S1", "supply", [supply], volts),
                                     dc.DcTerminal("L1", "load", [load], amps)], **settings)


def test_uniform_strip_matches_rho_l_over_tw():
    """R' = rho / (t W) per metre. The supply pad is ideal (one potential), the load
    draws evenly over its 2 mm: its mean sits a third of the pad beyond the gap."""
    board = dc_board.strip()
    result = solve(dc.build_problem(board, setup(cell_um=100)), cell_um=100)
    per_m = RHO / (dc_board.COPPER_NM * 1e-9 * 10e-3)
    gap, pad = 46e-3, 2e-3
    (pair,) = result["pairs"]
    assert pair["r_ohm"] == pytest.approx(per_m * (gap + pad / 3), rel=5e-3)
    (load,) = result["loads"]
    assert load["drop_mean_v"] == pytest.approx(per_m * (gap + pad / 3), rel=5e-3)
    assert load["drop_max_v"] == pytest.approx(per_m * (gap + pad / 2), rel=5e-3)
    assert result["supplies"][0]["i_a"] == pytest.approx(1.0, rel=1e-6)
    assert result["p_copper_w"] == pytest.approx(result["supplies"][0]["i_a"] * 3.3 - load["p_w"], rel=1e-4)
    # |J| = I / (W t) in the strip's middle, flowing from the supply (x = 0) to the load
    grid, fields = result["grid"], result["fields"]
    row = int((5 * MM - grid["y0_nm"]) // grid["pitch_nm"])
    column = int((25 * MM - grid["x0_nm"]) // grid["pitch_nm"])
    assert fields["j"][0, row, column] == pytest.approx(1 / (10e-3 * 35e-6) * 1e-6, rel=5e-3)
    assert fields["jx"][0, row, column] == pytest.approx(fields["j"][0, row, column], rel=1e-3)
    assert abs(fields["jy"][0, row, column]) < 1e-3 * fields["j"][0, row, column]
    assert fields["v"][0, row, 3] > fields["v"][0, row, -4]
    assert np.isnan(fields["j"][0, 0, 0])  # the margin around the copper


def test_two_layers_joined_by_one_via_is_a_series_chain():
    """1 mm cells over 1 mm wide strips: a 1D chain, exact. F.Cu from J1 to the via
    (5 faces), the barrel (the centres of F.Cu and B.Cu apart), B.Cu to J2 (4 faces)."""
    board = dc_board.via_chain()
    built = dc.build_problem(board, setup(cell_um=1000))
    assert [layer.layer_name for layer in built.problem.layers] == ["F.Cu", "B.Cu"]
    assert built.barrel_ids == ["via-1"]
    result = solve(built, cell_um=1000)
    length = (dc_board.CORE_NM + dc_board.COPPER_NM) * 1e-9
    r_via = RHO * length / (math.pi * 0.3e-3 * dc.PLATING_UM * 1e-6)
    (pair,) = result["pairs"]
    assert pair["r_ohm"] == pytest.approx(9 / SIGMA + r_via, rel=1e-6)
    barrels = result["barrels"]
    assert barrels["ids"] == ["via-1"] and barrels["spans"] == [("F.Cu", "B.Cu")]
    assert barrels["current_a"][0] == pytest.approx(1.0, rel=1e-6)
    assert barrels["power_w"][0] == pytest.approx(r_via, rel=1e-6)
    # Thicker plating: a lower barrel resistance, the difference exactly the barrel's.
    thick = solve(dc.build_problem(board, setup(cell_um=1000, plating_um=36.0)), cell_um=1000)
    assert pair["r_ohm"] - thick["pairs"][0]["r_ohm"] == pytest.approx(r_via / 2, rel=1e-6)


def test_load_voltages_follow_the_supply_voltage():
    board = dc_board.strip()
    low = solve(dc.build_problem(board, setup(volts=1.8, cell_um=250)), cell_um=250)
    high = solve(dc.build_problem(board, setup(volts=5.0, cell_um=250)), cell_um=250)
    assert high["loads"][0]["v_mean"] - low["loads"][0]["v_mean"] == pytest.approx(3.2, rel=1e-9)
    assert high["loads"][0]["drop_mean_v"] == pytest.approx(low["loads"][0]["drop_mean_v"], rel=1e-9)
    assert high["v_ref"] == 5.0


def test_problem_takes_the_nets_copper_only():
    board = dc_board.strip()
    other = model.ZoneFill("zone-gnd", "GND", "B.Cu", ((dc_board.rect(0, 0, 50 * MM, 10 * MM),),))
    track = model.Track("t1", "F.Cu", dc_board.NET, (0, 5 * MM), (50 * MM, 5 * MM), 200_000)
    arc = model.Arc("a1", "F.Cu", dc_board.NET, (0, 0), (MM, MM), (2 * MM, 0), 200_000)
    foreign = model.Track("t2", "F.Cu", "GND", (0, 0), (MM, 0), 200_000)
    board = dc_board.snapshot((*board.zones, other), board.pads, board.footprints, tracks=(track, foreign),
                              arcs=(arc,))
    problem = dc.build_problem(board, setup()).problem
    assert [layer.layer_name for layer in problem.layers] == ["F.Cu"]  # GND's B.Cu pour is not ours
    assert len(problem.tracks) == 2 and {len(t.points) for t in problem.tracks} == {2, 3}
    assert len(problem.layers[0].polygons) == 3  # the pour and both pads
    assert [t.role for t in problem.terminals] == ["supply", "load"]
    assert problem.terminals[0].v_oc == 3.3 and problem.terminals[1].i_draw_a == 1.0
    assert problem.terminals[0].electrodes[0].contact == "F.Cu"


def test_through_hole_pads_are_barrels_and_soldered_contacts():
    board = dc_board.via_chain()
    hole = model.Pad("pad-tht", "fp-j3", "1", dc_board.NET, (2 * MM, MM // 2), (800_000, 800_000),
                     {layer: ((dc_board.rect(1_300_000, -200_000, 2_700_000, 1_200_000),),)
                      for layer in ("F.Cu", "B.Cu")}, "round", 0.0)
    board = dc_board.snapshot(board.zones, (*board.pads, hole), (*board.footprints,
                                                                 dc_board.footprint("fp-j3", "J3", 2 * MM, 0)),
                              board.vias)
    built = dc.build_problem(board, setup(load="J3.1"))
    barrel = built.problem.vias[built.barrel_ids.index("pad-tht")]
    assert barrel.kind == "pad" and barrel.drill_nm == 800_000 and barrel.solder_filled
    assert barrel.protrusion_side == "B.Cu"  # the component is on top
    (electrode,) = built.problem.terminals[1].electrodes
    assert electrode.contact == "all" and electrode.drill_nm == 800_000 and electrode.solder
    assert electrode.pad_nm == pytest.approx(2 * math.hypot(700_000, 700_000), abs=2)
    assert electrode.pad_min_nm == 1_400_000
    result = solve(built, cell_um=100)
    assert result["loads"][0]["drop_mean_v"] > 0


def test_parts_off_the_net_are_reported():
    board = dc_board.strip()
    with pytest.raises(dc.SetupError, match="J9.1 is not on"):
        dc.build_problem(board, setup(load="J9.1"))
    with pytest.raises(dc.SetupError, match="no copper"):
        dc.build_problem(board, dc.DcSetup("GND", setup().terminals))


def test_whole_component_and_via_parts_resolve():
    board = dc_board.via_chain()
    assert dc.part_for(board, "pad-j2") == ("J2.1", dc_board.NET)
    assert dc.part_for(board, "pad-j2", whole_component=True) == ("J2", dc_board.NET)
    part, net = dc.part_for(board, "via-1")
    assert part == {"via_mm": [5.5, 0.5]} and net == dc_board.NET
    assert [via.id for via in dc.resolve_part(board, {"via_mm": [5.9, 0.5]}, dc_board.NET)] == ["via-1"]
    assert dc.resolve_part(board, {"via_mm": [7.0, 0.5]}, dc_board.NET) == []  # over 1 mm away
    assert [pad.id for pad in dc.resolve_part(board, "J2", dc_board.NET)] == ["pad-j2"]
    assert dc.net_of(board, "zone-back") == dc_board.NET and dc.net_of(board, "nothing") == ""
    assert dc.part_for(board, "zone-back") is None


def test_raster_port_is_pixel_centre_even_odd():
    """The vendored raster fills with reference.rasterize: exactly the cells whose centre
    lies inside the ring (the original tested its fill against matplotlib's)."""
    rng = np.random.default_rng(7)
    stack = raster.RasterStack(masks=np.zeros((1, 60, 80), bool), x0_nm=-1234.5, y0_nm=987.0, h_nm=25_000.0,
                               layer_names=["F.Cu"])
    for _ in range(20):
        count = int(rng.integers(3, 12))
        angles = np.sort(rng.uniform(0, 2 * np.pi, count))
        radius = rng.uniform(100_000, 900_000, count)
        centre = rng.uniform(300_000, 1_500_000, 2)
        ring = np.round(centre + np.column_stack([radius * np.cos(angles), radius * np.sin(angles)])).astype(np.int64)
        target = np.zeros((60, 80), bool)
        raster._paint_ring(stack, ring, True, target)
        ys, xs = np.mgrid[0:60, 0:80]
        cx, cy = stack.x0_nm + (xs + 0.5) * stack.h_nm, stack.y0_nm + (ys + 0.5) * stack.h_nm
        inside = np.zeros_like(target)
        a, b = ring, np.roll(ring, -1, axis=0)
        for (x1, y1), (x2, y2) in zip(a, b):
            crosses = (y1 > cy) != (y2 > cy)
            with np.errstate(divide="ignore", invalid="ignore"):
                at = x1 + (cy - y1) * (x2 - x1) / (y2 - y1)
            inside ^= crosses & (cx < at)
        assert np.array_equal(target, inside)


def test_guessed_supply_voltages():
    assert dc.guess_voltage("+3V3") == 3.3
    assert dc.guess_voltage("/power/1V8") == 1.8
    assert dc.guess_voltage("+5V") == 5.0
    assert dc.guess_voltage("VCC_12V") == 12.0
    assert dc.guess_voltage("VBAT") == dc.DEFAULT_SUPPLY_V


def test_stack_depths_from_the_stackup():
    stack = dc.copper_stack(dc_board.via_chain())
    assert stack.names == ("F.Cu", "B.Cu")
    assert stack.z_nm["F.Cu"] == 17_500
    assert stack.z_nm["B.Cu"] - stack.z_nm["F.Cu"] == dc_board.CORE_NM + dc_board.COPPER_NM
    assert not stack.warnings
