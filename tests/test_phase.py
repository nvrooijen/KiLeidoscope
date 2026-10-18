"""Dynamic phase of differential pairs on synthetic boards (tests/phase_boards.py)."""

import math
import tempfile

import numpy as np
import pytest

import phase_boards as boards
from kileido_bridge import channels, delays, model, phase, protocol

MM = boards.MM


def only_pair(snapshot, **settings):
    results, warnings = phase.analyze_board(snapshot, boards.delay_model(), phase.Settings(**settings))
    assert len(results) == 1 and not warnings
    return results[0]


def unit_delay(layer="F.Cu"):
    """The test profile's unit delay on `layer` (ps/mm): one number per layer, as KiCad's."""
    return boards.PROFILE.layers[layer]


def test_matched_pair_stays_in_phase():
    result = only_pair(boards.matched())
    assert result.channel.name == "D_P/N"
    assert phase.end_label(result.p.start, result.n.start) == "J1.1/2"  # the end whose label sorts first
    assert phase.end_label(result.p.end, result.n.end) == "U1.1/2"
    assert result.p.length_nm == pytest.approx(50 * MM)
    assert result.delay_p_ps == pytest.approx(50 * unit_delay())
    assert abs(result.skew_ps) < 1e-9 and result.max_abs_ps < 1e-9
    assert result.excursions == ()
    assert result.sources == ("tuning profile 'Test'",)
    assert result.width_nm == pytest.approx(MM + boards.WIDTH)  # across both tracks
    # The pads at both ends go to Blender with their outlines, tinted and pulsed like the trace there.
    pads = {pad["id"]: pad for pad in result.pads}
    assert set(pads) == {"U1.1", "J1.1", "U1.2", "J1.2"}
    assert pads["J1.1"]["side"] == "p" and pads["J1.1"]["layer"] == "F.Cu" and len(pads["J1.1"]["rings"][0]) == 8
    assert pads["J1.1"]["t"] == 0.0  # the start end
    assert pads["U1.1"]["t"] == pytest.approx(result.delay_p_ps)


def test_compensation_far_from_the_mismatch_is_an_excursion_despite_zero_skew():
    """N's 0.5 mm detour at x = 47 is made up by P's at x = 2: matched end to end, but
    out of phase over the 45 mm between."""
    snapshot = boards.compensated_far()
    result = only_pair(snapshot)
    assert abs(result.skew_ps) < 1e-9
    plateau = 0.5 * unit_delay()  # ps: the detour's extra length
    middle = np.abs(result.centre_nm[:, 0] - 25 * MM) < 5 * MM
    assert np.allclose(result.dt_ps[middle], -plateau, atol=0.02)  # walking from J1: N longer so far
    assert len(result.excursions) == 1
    excursion = result.excursions[0]
    assert 46.5 * MM <= excursion.pos[0] <= 47.3 * MM  # where it starts, at N's detour
    assert excursion.length_nm > 40 * MM
    assert excursion.peak_ps < -plateau
    assert excursion.item_id in {track.id for track in snapshot.tracks if track.net == "D_P"}


def test_each_side_carries_its_delay_and_how_far_it_is_ahead():
    """What Blender colours and pulses: both traces follow their own copper, corners
    included, with the delay from the start and the lead over the partner at each point."""
    snapshot = boards.compensated_far()
    result = only_pair(snapshot)
    p, n = result.traces
    for trace, total, route in ((p, result.delay_p_ps, result.p), (n, result.delay_n_ps, result.n)):
        assert trace.delay_ps[0] == 0 and trace.delay_ps[-1] == pytest.approx(total)
        assert np.all(np.diff(trace.delay_ps) >= 0)
        corners = {tuple(point) for piece in route.pieces for point in piece.points}
        assert corners <= {(round(x), round(y)) for x, y in trace.xyz_nm[:, :2]}  # its own corners
        assert set(trace.width_nm) == {boards.WIDTH}
    plateau = 0.5 * unit_delay()
    middle = [np.abs(trace.xyz_nm[:, 0] - 25 * MM) < 5 * MM for trace in (p, n)]
    assert np.allclose(p.lead_ps[middle[0]], plateau, atol=0.02)  # N took the detour: P ahead...
    assert np.allclose(n.lead_ps[middle[1]], -plateau, atol=0.02)  # ...and N lagging by as much
    assert abs(p.lead_ps[0]) < 1e-9 and abs(p.lead_ps[-1]) < 1e-3  # in phase at both ends


def test_compensation_close_to_the_mismatch_is_not_an_excursion():
    snapshot = boards.compensated_locally()
    result = only_pair(snapshot)
    assert abs(result.skew_ps) < 1e-9
    assert result.max_abs_ps > 1.0  # out of phase between the detours...
    assert result.excursions == ()  # ...but for only ~2 mm, under the 5 mm default
    assert len(only_pair(snapshot, min_length_nm=1 * MM).excursions) == 1  # the user's distance decides


def test_a_layer_change_adds_each_via_barrel_between_its_layers():
    snapshot = boards.layer_change()
    result = only_pair(snapshot)
    heights = protocol.layer_heights_nm(snapshot)[0]
    barrel = heights["F.Cu"] - heights["B.Cu"]
    assert [(c.side, c.from_layer, c.to_layer) for c in result.crossings] == [("P", "B.Cu", "F.Cu"),
                                                                          ("N", "B.Cu", "F.Cu")]
    crossing = result.crossings[0]
    assert crossing.length_nm == barrel and crossing.pos == (20 * MM, 0) and crossing.span == ("F.Cu", "B.Cu")
    assert crossing.at_nm == pytest.approx(30 * MM)  # from J1
    assert crossing.diameter_nm == 600_000 and crossing.drill_nm == 300_000
    assert abs(crossing.lead_ps) < 1e-6  # in phase there
    assert crossing.reached_ps == pytest.approx(30 * unit_delay("B.Cu"))
    assert crossing.delay_ps == pytest.approx(boards.PROFILE.via_ps_per_mm * barrel / 1e6)  # the profile's via delay
    expected = 20 * unit_delay() + 30 * unit_delay("B.Cu") + crossing.delay_ps
    assert result.delay_p_ps == pytest.approx(expected)
    assert result.p.length_nm == pytest.approx(50 * MM + barrel)
    assert abs(result.skew_ps) < 1e-9 and result.excursions == ()
    assert np.ptp(result.centre_nm[:, 2]) == pytest.approx(barrel)  # the centreline climbs through the vias


def test_series_ac_caps_join_both_pairs_into_one_channel():
    snapshot = boards.ac_coupled()
    result = only_pair(snapshot)
    assert result.channel.key == "TX_C_P|TX_P"
    assert result.channel.name == "TX_C_P/N → TX_P/N"
    assert [link.reference for link in result.channel.p_links + result.channel.n_links] == ["C1", "C2"]
    assert (result.p.start.label, result.p.end.label) == ("J1.1", "U1.1")  # through C1, not ending on it
    assert result.p.length_nm == pytest.approx(50 * MM)  # 29 mm + 1 mm across C1 + 20 mm
    assert abs(result.skew_ps) < 1e-9
    separate = phase.analyze_board(snapshot, boards.delay_model(), phase.Settings(follow_series=False))[0]
    assert sorted(r.channel.key for r in separate) == ["TX_C_P", "TX_P"]
    assert {(r.p.start.label, r.p.end.label) for r in separate} == {("C1.1", "U1.1"), ("C1.2", "J1.1")}


def test_an_esd_stub_is_a_branch_not_a_terminal():
    result = only_pair(boards.esd_stub())
    assert (result.p.start.label, result.p.end.label) == ("J1.1", "U1.1")
    assert (result.n.start.label, result.n.end.label) == ("J1.2", "U1.2")
    [p_branch], [n_branch] = result.p.branches, result.n.branches
    assert p_branch.terminal.label == "D1.1" and n_branch.terminal.label == "D1.2"
    assert p_branch.at_nm == pytest.approx(25 * MM) and p_branch.length_nm == pytest.approx(3 * MM)
    assert n_branch.at_nm == pytest.approx(24 * MM)  # the N stub tees off at x = 26, walking from x = 50
    assert result.p.length_nm == pytest.approx(50 * MM)  # the stub is not on the route
    assert "P side has 1 branch" in result.warnings


def test_both_sides_end_on_the_same_parts():
    result = only_pair(boards.multi_drop())  # N alone would run J1 -> U3
    assert (result.p.start.label, result.p.end.label) == ("J1.1", "U1.1")
    assert (result.n.start.label, result.n.end.label) == ("J1.2", "U1.2")
    assert [branch.terminal.label for branch in result.n.branches] == ["U3.2"]
    assert abs(result.skew_ps) < 1e-9 and result.max_abs_ps < 1e-9


def test_pads_that_connect_twice_still_end_or_carry_the_route():
    result = only_pair(boards.pass_through())  # J1.1 and D1's pads each connect twice
    assert {result.p.start.label, result.p.end.label} == {"J1.1", "U1.1"}
    # the copper runs on to J1.1's centre, so it counts from there, straight to the pad's edge (KiCad's
    # clipLineToPad); the second short track lying over it is not on the route
    assert result.p.length_nm == pytest.approx(50 * MM)
    assert not result.p.branches  # the signal runs through D1, it does not branch to it
    assert {pad["id"] for pad in result.pads if pad["side"] == "p"} == {"J1.1", "D1.1", "D1.2", "U1.1"}
    assert [branch.terminal.label for branch in result.n.branches] == ["D1.3"]


def test_the_pair_lists_every_item_on_its_nets():
    ids = set(only_pair(boards.esd_stub()).net_ids)
    assert {"D1.1", "D1.2"} <= ids  # the stubs' pads too: Blender highlights them white


def test_flipping_walks_from_the_other_end():
    snapshot = boards.compensated_far()
    forward = only_pair(snapshot)
    flipped = only_pair(snapshot, flipped=frozenset({forward.channel.key}))
    assert flipped.flipped and flipped.p.start.label == "U1.1"
    middle = [np.abs(result.centre_nm[:, 0] - 25 * MM) < 5 * MM for result in (forward, flipped)]
    assert np.allclose(flipped.dt_ps[middle[1]], forward.skew_ps - forward.dt_ps[middle[0]][0], atol=1e-3)
    [excursion] = flipped.excursions
    assert 2.0 * MM <= excursion.pos[0] <= 2.3 * MM and excursion.peak_ps > 0  # P's detour comes first now


def test_an_unfinished_route_is_reported():
    board = boards.ends(boards.Board()).path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (30, 1))
    results, warnings = phase.analyze_board(board.snapshot(), boards.delay_model())
    assert results == [] or "N side is not fully routed" in results[0].warnings
    assert results or warnings == ["D_P/N: no routed path between two pads yet"]


def test_arcs_count_their_true_length():
    arc = model.Arc("a", "F.Cu", "D_P", (0, 0), (MM, MM), (2 * MM, 0), boards.WIDTH)
    points, length = channels._arc_polyline(arc)
    assert length == pytest.approx(math.pi * MM)
    assert points[0] == (0, 0) and points[-1] == (2 * MM, 0)


# --- Turns: the length rule, as KiCad counts them -------------------------------------------

def coupled_turn(back: bool = False):
    """A tightly coupled pair (0.1 mm gap; P at y = 0, N at 0.25) running +x, then round a
    90° turn towards +y with P outside (2 mm centreline radius) and on to y = 6; with
    `back`, a second turn the other way (an S) and on to x = 24."""
    board = boards.Board().part("U1", (1, "D_P", (0, 0)), (2, "D_N", (0, 0.25)), pad=200_000)
    board.path("D_P", (0, 0), (10, 0)).turn("D_P", (10, 2), 2.0, -90, 0).path("D_P", (12, 2), (12, 6))
    board.path("D_N", (0, 0.25), (10, 0.25)).turn("D_N", (10, 2), 1.75, -90, 0).path("D_N", (11.75, 2), (11.75, 6))
    if not back:
        return board.part("J1", (1, "D_P", (12, 6)), (2, "D_N", (11.75, 6)), pad=200_000).snapshot()
    board.turn("D_P", (14, 6), 2.0, 180, 90).path("D_P", (14, 8), (24, 8))
    board.turn("D_N", (14, 6), 2.25, 180, 90).path("D_N", (14, 8.25), (24, 8.25))
    return board.part("J1", (1, "D_P", (24, 8)), (2, "D_N", (24, 8.25)), pad=200_000).snapshot()


def from_u1(snapshot):
    """The pair walked from U1 (J1 sorts first, so that is the flipped direction)."""
    results, warnings = phase.analyze_board(snapshot, boards.delay_model(), phase.Settings(flipped=frozenset({"D_P"})))
    assert len(results) == 1 and not warnings
    return results[0]


def test_a_coupled_turn_makes_its_length_difference_and_a_turn_back_takes_it_away():
    """The outer trace is longer by the pitch times the angle: that is the skew, as KiCad
    counts it (the straight before the turn nothing, halfway round half). An S, the same
    turn back, takes it all back."""
    plain = 0.25 * math.pi / 2 * unit_delay()  # the pitch times the angle, times the profile's ps/mm
    result = from_u1(coupled_turn())
    assert result.p.start.label == "U1.1"
    assert result.dt_ps[-1] == pytest.approx(plain, rel=0.02)
    assert result.skew_ps == pytest.approx(result.dt_ps[-1], abs=0.02)
    assert np.allclose(result.delays_ps[:, 0] - result.delays_ps[:, 1], result.dt_ps)
    assert np.abs(result.dt_ps[result.along_nm < 9 * MM]).max() < 1e-9  # the straight before it: nothing
    halfway = int(np.argmin(np.linalg.norm(result.centre_nm[:, :2] - np.array((11.326, 0.674)) * MM, axis=1)))
    assert result.dt_ps[halfway] == pytest.approx(plain / 2, rel=0.15)
    s_curve = from_u1(coupled_turn(back=True))
    middle = int(np.argmin(np.abs(s_curve.centre_nm[:, 1] - 4 * MM)))  # on the leg between the turns
    assert s_curve.dt_ps[middle] == pytest.approx(plain, rel=0.02)
    assert s_curve.dt_ps[-1] == pytest.approx(0.0, abs=0.05)


def test_the_frame_carries_dt_along_the_pair():
    result = from_u1(coupled_turn())
    header, arrays = protocol.FrameDecoder().feed(phase.pair_frame(result, 1))[0]
    assert {"along", "dt"} <= set(arrays) and np.allclose(arrays["dt"], result.dt_ps, atol=1e-4)
    assert "notes" not in header and "lines" not in header


# --- Delay sources ------------------------------------------------------------------------

PROJECT = {
    "net_settings": {"classes": [{"name": "Default", "tuning_profile": ""},
                                 {"name": "HS", "tuning_profile": "Fast"}],
                     "netclass_patterns": [{"netclass": "HS", "pattern": "D_*"}]},
    "tuning_profiles": {"tuning_profiles_impedance_geometric": [{
        "profile_name": "Fast", "type": 1, "target_impedance": 90.0, "enable_time_domain_tuning": True,
        "layer_entries": [{"signal_layer": "F.Cu", "top_reference_layer": "", "bottom_reference_layer": "In1.Cu",
                           "width": 150000, "diff_pair_gap": 150000, "delay": 6_000_000}],
        "via_prop_delay": 8_000_000,
        "via_overrides": [{"signal_layer_from": "F.Cu", "signal_layer_to": "B.Cu", "via_layer_from": "F.Cu",
                           "via_layer_to": "B.Cu", "delay": 12_500_000}]}]},
}


def test_the_netclass_tuning_profile_sets_the_delay():
    profiles = delays.tuning_profiles(PROJECT)
    assert profiles["Fast"].layers == {"F.Cu": 6.0}  # attoseconds per mm -> ps per mm
    nets = delays.net_profiles_from_project(PROJECT, ["D_P", "GND"])
    assert nets == {"D_P": "Fast", "GND": ""}  # D_* matches the HS pattern
    assert delays.net_profiles_from_project(PROJECT, ["D_P"], {"D_P": "Default"}) == {"D_P": ""}  # KiCad wins
    model_ = delays.ProfileDelays(profiles, {"D_P": "Fast", "D_N": "Fast"})
    profiled = model_.track("D_P", "F.Cu", 150_000)
    assert profiled == delays.UnitDelay(6.0, "tuning profile 'Fast'")  # KiCad's number
    assert model_.track("D_P", "F.Cu", boards.WIDTH) == profiled  # for every track of the net, as KiCad charges it
    assert model_.track("D_P", "B.Cu", boards.WIDTH) is None  # no entry for B.Cu: no number
    assert model_.track("GND", "F.Cu", boards.WIDTH) is None and model_.profile("GND") == ""  # no profile
    assert model_.barrel("D_P", "F.Cu", "B.Cu", ("F.Cu", "B.Cu"), 1e6) == (12.5, "tuning profile 'Fast'")
    assert model_.barrel("D_P", "F.Cu", "In1.Cu", ("F.Cu", "B.Cu"), 0.2e6)[0] == pytest.approx(1.6)
    assert model_.barrel("GND", "F.Cu", "B.Cu", ("F.Cu", "B.Cu"), 1e6) is None


def test_a_profiles_number_counts_every_mm_as_kicad_does():
    """A pair with a profile reads its number times the length, a bump away from the
    partner included; an inner layer has its own."""
    profile = delays.TuningProfile("Fast", {"F.Cu": 6.0, "In1.Cu": 7.0})
    model_ = delays.ProfileDelays({"Fast": profile}, {"D_P": "Fast", "D_N": "Fast"})
    assert model_.track("D_P", "In1.Cu", boards.WIDTH).ps_per_mm == 7.0

    def pair(*p_points):
        board = boards.Board().part("U1", (1, "D_P", (0, 0)), (2, "D_N", (0, 0.5)))
        board.part("J1", (1, "D_P", (50, 0)), (2, "D_N", (50, 0.5)))
        return board.path("D_P", *p_points).path("D_N", (0, 0.5), (50, 0.5)).snapshot()
    straight = phase.analyze_board(pair((0, 0), (50, 0)), model_)[0][0]
    assert straight.delay_p_ps == pytest.approx(6.0 * straight.p.length_nm / 1e6)
    bumped = phase.analyze_board(pair((0, 0), *boards.bump(2, 0, -2.0), (50, 0)), model_)[0][0]
    assert bumped.delay_p_ps - straight.delay_p_ps == pytest.approx(6.0 * 4.0)  # 4 mm more copper
    assert bumped.sources == ("tuning profile 'Fast'",)


def test_short_pieces_are_walked_not_hopped():
    """Pieces shorter than half a track width (a fine zig-zag, a piece of no length) whose
    ends meet exactly are copper KiCad counts in full; joining their ends as touching
    would collapse them into one straight hop."""
    zigzag = [(10, 0)]
    for k in range(20):  # 0.06 mm pieces, under half the 0.15 mm width: 4.8 mm of copper over 2.4 mm
        x = 10 + 0.12 * k
        zigzag += [(x, 0.06), (x + 0.06, 0.06), (x + 0.06, 0), (x + 0.12, 0)]
    board = boards.ends(boards.Board())
    board.path("D_P", (0, 0), *zigzag, (20, 0), (20, 0), (50, 0))  # (20, 0) twice: a track of no length
    board.path("D_N", (0, 1), (50, 1))
    result = only_pair(board.snapshot())
    copper = 10 + 4.8 + (50 - 12.4)
    assert result.p.length_nm == pytest.approx(copper * MM)
    assert not any(piece.kind == "hop" for piece in result.p.pieces)
    assert result.delay_p_ps == pytest.approx(copper * unit_delay())


def test_ends_that_do_not_meet_still_join_within_half_a_width():
    """A track end that stops short of the next track's end, or lands beside a track's
    middle, still connects there, as KiCad's connectivity has it: the gap is a hop, the
    middle a T."""
    board = boards.ends(boards.Board())
    board.path("D_P", (0, 0), (20, 0)).path("D_P", (20.05, 0), (50, 0))  # 0.05 mm short of the next end
    board.path("D_N", (0, 1), (30, 1)).path("D_N", (30, 1), (30, 3))  # a stub off N's route...
    board.path("D_N", (30, 1.04), (50, 1))  # ...and N's route on, starting 0.04 mm beside the corner
    result = only_pair(board.snapshot())
    assert result.p.complete and result.n.complete
    assert result.p.length_nm == pytest.approx(50 * MM)
    assert [piece.kind for piece in result.p.pieces] == ["track", "hop", "track"]
    assert result.delay_p_ps == pytest.approx(49.95 * unit_delay())  # KiCad counts the tracks, not the gap
    assert result.n.length_nm == pytest.approx((30 + math.dist((30, 1.04), (50, 1))) * MM, abs=0.05 * MM)


# --- Vias and pads, as KiCad 10 counts them (pcbnew/length_delay_calculation) ----------------

def test_a_via_counts_kicads_stackup_distance_between_the_outermost_layers_ending_in_it():
    """KiCad's via height (BOARD_STACKUP::GetLayerDistance) counts outer copper whole and
    inner copper half, between the outermost layers whose copper ends in the via."""
    board = boards.ends(boards.Board())
    board.pads[-2:] = []  # J1 on the bottom instead
    board.part("J1", (1, "D_P", (50, 0)), (2, "D_N", (50, 1)), layer="B.Cu")
    for net, y in (("D_P", 0), ("D_N", 1)):
        board.path(net, (0, y), (10, y)).via(net, (10, y)).path(net, (10, y), (20, y), layer="In1.Cu")
        board.via(net, (20, y)).path(net, (20, y), (30, y), layer="In2.Cu")
        board.via(net, (30, y)).path(net, (30, y), (50, y), layer="B.Cu")
    result = only_pair(board.snapshot())
    # F.Cu 35 + 200 dielectric + half of In1's 35; In1 half + 1000 + In2 half; In2 half + 200 + B.Cu 35 (um)
    assert [c.length_nm for c in result.crossings if c.side == "P"] == [252_500, 1_035_000, 252_500]
    assert result.delay_p_ps == pytest.approx(10 * 6.0 + 10 * 7.0 + 10 * 7.0 + 20 * 6.0 + 7.0 * 1.54)

    board.path("D_P", (10, 0), (10, -2), layer="B.Cu")  # copper on B.Cu ending in the first via too
    first = next(c for c in only_pair(board.snapshot()).crossings if c.side == "P" and c.pos == (10 * MM, 0))
    assert first.length_nm == 1_540_000  # F.Cu to B.Cu, the whole board


def test_a_track_ending_inside_a_via_pad_counts_from_the_via_centre():
    """KiCad cuts a track end inside a via's pad at the pad's edge and joins it to the via
    centre by a straight line (clipLineToVia), wherever in the pad the track ends."""
    board = boards.Board().part("U1", (1, "D_P", (0, 0)), (2, "D_N", (0, 1)))
    board.part("J1", (1, "D_P", (50, -5)), (2, "D_N", (50, -6)), layer="B.Cu")
    board.path("D_P", (0, 0), (19.8, 0)).via("D_P", (20, 0))  # stops 0.2 mm short of the centre
    board.path("D_P", (20, -0.2), (20, -5), (50, -5), layer="B.Cu")  # starts 0.2 mm below it, at right angles
    board.path("D_N", (0, 1), (19, 1)).via("D_N", (19, 1)).path("D_N", (19, 1), (19, -6), (50, -6), layer="B.Cu")
    result = only_pair(board.snapshot())
    # the via pad's radius is 0.3 mm: F.Cu 19.7 + 0.3, B.Cu 0.3 + 4.7 + 30
    assert result.delay_p_ps == pytest.approx(20.0 * 6.0 + 35.0 * 6.0 + 7.0 * 1.54)


def test_a_track_ending_on_a_pad_centre_counts_straight_from_it_and_one_ending_elsewhere_as_it_runs():
    """KiCad cuts a track that ends exactly on a pad's centre at the pad's edge and joins it
    to the centre by a straight line (clipLineToPad); one ending elsewhere in the pad counts
    from its end."""
    board = boards.ends(boards.Board())
    board.path("D_P", (0, 0), (0, 0.15), (0.1, 0.15), (0.1, 0), (50, 0))  # a wiggle inside U1's 0.4 mm pad
    board.path("D_N", (0.1, 1), (50, 1))  # starts 0.1 mm off the pad's centre
    result = only_pair(board.snapshot())
    assert result.delay_p_ps == pytest.approx(50.0 * 6.0)  # straight from the centre, not 0.3 mm of wiggle
    assert result.delay_n_ps == pytest.approx(49.9 * 6.0)


def test_copper_dead_ending_without_a_pad_is_a_branch():
    """A stub that ends in no pad is copper KiCad counts and the route leaves out: listed and
    warned about like a branch to a pad."""
    board = boards.ends(boards.Board())
    board.path("D_P", (0, 0), (50, 0)).path("D_P", (25, 0), (25, 0.5))
    board.path("D_N", (0, 1), (50, 1))
    result = only_pair(board.snapshot())
    assert [(b.terminal.label, b.length_nm) for b in result.p.branches] == [("track end", pytest.approx(0.5 * MM))]
    assert "P side has 1 branch" in result.warnings
    assert result.delay_p_ps == pytest.approx(50 * unit_delay())


def test_a_pair_without_a_tuning_profile_is_listed_not_measured():
    """KiCad has no delay without a tuning profile, and neither has dynamic phase: the pair
    is listed with why, and nothing is drawn for it."""
    (result,), warnings = phase.analyze_board(boards.matched())  # no profiles at all
    assert isinstance(result, phase.Unprofiled) and not warnings
    assert result.reason == "No tuning profile in KiCad for D_N, D_P"
    assert {"U1.1", "J1.2"} <= set(result.net_ids)
    header, arrays = protocol.FrameDecoder().feed(phase.pair_frame(result, 1))[0]
    assert header["unprofiled"] == result.reason and header["warnings"] == [result.reason]
    assert not arrays and "start" not in header and "skew_ps" not in header
    p_only = delays.ProfileDelays({"Test": boards.PROFILE}, {"D_P": "Test"})
    assert phase.analyze_board(boards.matched(), p_only)[0][0].reason == "No tuning profile in KiCad for D_N"
    # A profile without a delay for a layer the pair runs on: KiCad's tuner has none there either.
    outer_only = delays.TuningProfile("Outer", {"F.Cu": 6.0})
    (layered,), _ = phase.analyze_board(boards.layer_change(), boards.delay_model(outer_only))
    assert layered.reason == "Tuning profile 'Outer' has no delay for B.Cu"
    # Shorted and without a profile: still the short, without pulses.
    (short,), _ = phase.analyze_board(boards.shorted())
    assert isinstance(short, phase.Shorted) and short.measured is None
    listing = decoded(tracker_without_project().update(boards.matched(), revision=1))
    assert listing[0][0]["keys"] == ["D_P"] and listing[1][0]["unprofiled"]


def test_a_profile_with_time_domain_tuning_off_is_not_measured():
    """KiCad charges 0 ps to everything on such a profile; dynamic phase lists the pair
    with the reason instead of showing a number KiCad does not."""
    entry = PROJECT["tuning_profiles"]["tuning_profiles_impedance_geometric"][0]
    project = {**PROJECT, "tuning_profiles": {"tuning_profiles_impedance_geometric": [
        {**entry, "enable_time_domain_tuning": False}]}}
    profiles = delays.tuning_profiles(project)
    assert not profiles["Fast"].time_domain and delays.tuning_profiles(PROJECT)["Fast"].time_domain
    model_ = delays.ProfileDelays(profiles, {"D_P": "Fast", "D_N": "Fast"})
    assert model_.track("D_P", "F.Cu", boards.WIDTH) is None
    assert model_.barrel("D_P", "F.Cu", "B.Cu", ("F.Cu", "B.Cu"), 1e6) is None
    assert not model_.time_domain("D_P") and model_.time_domain("GND")  # no profile at all: not this reason
    off = delays.TuningProfile("Test", boards.PROFILE.layers, 7.0, time_domain=False)
    (result,), warnings = phase.analyze_board(boards.matched(), boards.delay_model(off))
    assert isinstance(result, phase.Unprofiled) and not warnings
    assert result.reason == "Time-domain tuning is off in KiCad for tuning profile 'Test'"


def test_vias_add_no_delay_when_the_board_counts_no_stackup_height():
    """KiCad's StackupHeight() is 0 with the design rules' option off, so its vias add no
    delay, overrides included; the barrels here add none either and say why."""
    assert delays.via_height(PROJECT) and delays.via_height({})  # absent: KiCad's default, on
    off = {**PROJECT, "board": {"design_settings": {"rules": {"use_height_for_length_calcs": False}}}}
    assert not delays.via_height(off)
    model_ = delays.ProfileDelays(delays.tuning_profiles(PROJECT), {"D_P": "Fast"}, via_height=False)
    assert model_.barrel("D_P", "F.Cu", "B.Cu", ("F.Cu", "B.Cu"), 1e6) == (0.0, delays.NO_VIA_HEIGHT)  # the override too
    assert model_.barrel("GND", "F.Cu", "B.Cu", ("F.Cu", "B.Cu"), 1e6) is None  # still no profile
    (result,), _ = phase.analyze_board(boards.layer_change(), boards.delay_model(via_height=False))
    assert len(result.crossings) == 2 and all(crossing.delay_ps == 0.0 for crossing in result.crossings)
    assert result.delay_p_ps == pytest.approx(50 * unit_delay())  # 20 mm F.Cu + 30 mm B.Cu, both 6 ps/mm
    assert result.sources == tuple(sorted((delays.NO_VIA_HEIGHT, "tuning profile 'Test'")))


def test_pad_to_die_delays_count_at_both_ends_as_kicad_adds_them():
    """U1's dies put P 2 ps behind N before any copper and J1's make it up: matched end to
    end, out of phase along the whole pair. Walked from J1 (the default start) P is ahead."""
    result = only_pair(boards.die_delays())
    assert result.p.start.label == "J1.1"  # the start: the end whose label sorts first
    assert np.allclose(result.dt_ps, -2.0, atol=1e-6)  # J1.1 has no die delay, J1.2 2 ps: P ahead everywhere
    assert result.delay_p_ps == pytest.approx(3.0 + 50 * unit_delay())  # J1.1 0 + copper + U1.1 3
    assert result.delay_n_ps == pytest.approx(3.0 + 50 * unit_delay())  # J1.2 2 + copper + U1.2 1
    assert result.skew_ps == pytest.approx(0.0)
    [excursion] = result.excursions  # from the first point over the whole pair
    assert excursion.at_nm == 0 and excursion.length_nm == pytest.approx(50 * MM, rel=0.02)
    assert excursion.peak_ps == pytest.approx(-2.0)
    assert {"pad-to-die delay of J1.2", "pad-to-die delay of U1.1", "pad-to-die delay of U1.2"} <= set(result.sources)
    assert "pad-to-die delay of J1.1" not in result.sources
    assert result.traces[0].delay_ps[0] == pytest.approx(0.0) and result.traces[1].delay_ps[0] == pytest.approx(2.0)
    flipped = only_pair(boards.die_delays(), flipped=frozenset({result.channel.key}))
    assert np.allclose(flipped.dt_ps, 2.0, atol=1e-6)  # (3): skew − Δt
    header, _ = protocol.FrameDecoder().feed(phase.pair_frame(result, 1))[0]
    assert header["delay_p_ps"] == pytest.approx(303.0) and header["skew_ps"] == pytest.approx(0.0)


def test_a_changed_die_delay_recomputes_the_pair():
    tracker = unhurried()
    tracker.update(boards.matched(), revision=1)
    frames = decoded(tracker.update(boards.die_delays(), revision=2))  # the same copper, new die delays
    assert [header["type"] for header, _ in frames] == ["phase_pair"]
    assert frames[0][0]["max_ps"] == pytest.approx(2.0)


def tracker_without_project():
    return phase.PhaseTracker(clock=lambda: 0.0)


def test_a_shorted_pair_is_not_measured_and_says_where():
    (result,), _ = phase.analyze_board(boards.shorted(), boards.delay_model())
    assert isinstance(result, phase.Shorted)
    bridge = next(track.id for track in boards.shorted().tracks if track.start == (20 * MM, 0))
    n_track = next(track.id for track in boards.shorted().tracks if track.net == "D_N")
    assert [(contact.pos, contact.layer, contact.p_id, contact.n_id) for contact in result.contacts] == [
        ((20 * MM, MM), "F.Cu", bridge, n_track)]
    (header, arrays), = protocol.FrameDecoder().feed(phase.pair_frame(result, 1))
    assert header["shorts"] == [{"x": 20 * MM, "y": MM, "layer": "F.Cu", "ids": [bridge, n_track]}]
    assert header["warnings"] == ["P and N are shorted (1 place)"]
    # No phase, but the pulses run from the start terminal (J1, at x = 50) up to the short.
    assert header["start"]["label"] == "J1.1/2" and "end" not in header and "skew_ps" not in header
    for side, y in (("p", 0), ("n", MM)):
        xyz = arrays[f"{side}_xyz"]
        assert tuple(xyz[0, :2]) == (50 * MM, y) and tuple(xyz[-1, :2]) == (20 * MM, y)
        assert not arrays[f"{side}_lead"].any() and arrays[f"{side}_t"][-1] == pytest.approx(header[f"delay_{side}_ps"])
    assert header["delay_p_ps"] == pytest.approx(header["delay_n_ps"]) and header["delay_p_ps"] > 0
    listing = decoded(unhurried().update(boards.shorted(), revision=1))[0][0]
    assert listing["keys"] == ["D_P"]  # still listed: the panel says SHORT


def test_a_loop_beside_the_route_is_reported():
    (result,), _ = phase.analyze_board(boards.looped(), boards.delay_model())
    assert result.p.length_nm == pytest.approx(50 * MM)  # the shortest path is measured
    (loop,) = result.p.loops
    detour = {track.id for track in boards.looped().tracks if track.net == "D_P" and track.start != (0, 0)}
    assert set(loop.item_ids) == detour and loop.length_nm == pytest.approx(36 * MM)
    assert loop.pos == (25 * MM, -3 * MM) and loop.layer == "F.Cu" and not result.n.loops
    assert "P side has a loop: 36.0 mm of copper beside the route" in result.warnings
    header = protocol.FrameDecoder().feed(phase.pair_frame(result, 1))[0][0]
    assert [(entry["side"], entry["length_mm"]) for entry in header["loops"]] == [("P", 36.0)]


@pytest.mark.parametrize("board", [boards.matched, boards.compensated_far, boards.compensated_locally,
                                   boards.layer_change, boards.ac_coupled, boards.esd_stub, boards.multi_drop,
                                   boards.pass_through, boards.grounded, boards.two_channels])
def test_ordinary_pairs_have_no_loops_or_shorts(board):
    results, _ = phase.analyze_board(board(), boards.delay_model())
    assert results and all(isinstance(result, phase.PairPhase) for result in results)
    assert not any(route.loops for result in results for route in (result.p, result.n))


# --- Live tracking and frames -------------------------------------------------------------

def two_pairs(detour=False):
    board = boards.ends(boards.Board())
    board.part("U2", (1, "E_P", (0, 5)), (2, "E_N", (0, 6))).part("J2", (1, "E_P", (50, 5)), (2, "E_N", (50, 6)))
    board.path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (50, 1))
    board.path("E_P", (0, 5), *(boards.bump(10, 5, -0.25) if detour else ()), (50, 5)).path("E_N", (0, 6), (50, 6))
    return board.snapshot()


def decoded(frames):
    return protocol.FrameDecoder().feed(b"".join(frames))


PROJECT_DIR = tempfile.mkdtemp(prefix="kls-phase-")  # the test profile as a project file, for the trackers


def tracker(clock=lambda: 0.0):
    """A tracker reading the test profile from its project file."""
    made = phase.PhaseTracker(clock=clock)
    made.set_project(boards.project_file(PROJECT_DIR))
    return made


def unhurried():
    """A tracker whose clock stands still: every pair in one poll, however slow the machine."""
    return tracker()


def test_the_tracker_recomputes_only_pairs_whose_copper_changed():
    tracker = unhurried()
    first = decoded(tracker.update(two_pairs(), revision=1))
    assert [header["type"] for header, _ in first] == ["phase_list", "phase_pair", "phase_pair"]
    listing = first[0][0]
    assert listing["keys"] == ["D_P", "E_P"] and listing["pending"] == 0
    assert listing["settings"] == {"tolerance_ps": 1.0, "min_length_mm": 5.0, "follow_series": True, "flipped": []}
    header, arrays = first[1]
    assert header["name"] == "D_P/N" and header["start"]["label"] == "J1.1/2" and header["end"]["label"] == "U1.1/2"
    assert header["start"]["ids"] == ["J1.1", "J1.2"] and header["start"]["layer"] == "F.Cu"
    for side in "pn":
        count = len(arrays[f"{side}_xyz"])
        assert arrays[f"{side}_xyz"].shape == (count, 3) and count > 2
        assert all(arrays[f"{side}_{name}"].shape == (count,) for name in ("t", "lead", "width"))
    assert tracker.update(two_pairs(), revision=2) == []  # the same copper (equal records): nothing to send
    changed = decoded(tracker.update(two_pairs(detour=True), revision=3))
    assert [(header["type"], header.get("key")) for header, _ in changed] == [("phase_pair", "E_P")]
    assert changed[0][0]["skew_ps"] > 1.0
    assert [header["type"] for header, _ in decoded(tracker.snapshot_frames())] == ["phase_list", "phase_pair",
                                                                                     "phase_pair"]


def test_settings_from_blender_recompute_and_flip():
    tracker = unhurried()
    tracker.update(two_pairs(detour=True), revision=1)
    tracker.configure({"type": "phase_settings", "tolerance_ps": 0.5, "min_length_mm": 2,
                       "follow_series": True, "flipped": ["E_P"]})
    frames = decoded(tracker.update(two_pairs(detour=True), revision=2))
    assert frames[0][0]["type"] == "phase_list" and frames[0][0]["settings"]["flipped"] == ["E_P"]
    flipped = next(header for header, _ in frames if header.get("key") == "E_P")
    assert flipped["flipped"] and flipped["start"]["label"] == "U2.1/2"
    assert phase.Settings.from_request({"tolerance_ps": "bad", "flipped": "E_P"}) == phase.Settings()


def test_the_tracker_spreads_work_over_polls():
    ticks = iter(range(1000))
    spread = tracker(clock=lambda: next(ticks) * phase.BUDGET_S * 0.6)  # one pair per poll
    first = decoded(spread.update(two_pairs(), revision=1))
    assert first[0][0]["pending"] == 1 and len(first) == 2
    second = decoded(spread.update(two_pairs(), revision=2))
    assert [header["type"] for header, _ in second] == ["phase_list", "phase_pair"]
    assert second[0][0]["pending"] == 0 and second[0][0]["keys"] == ["D_P", "E_P"]


def test_a_pair_that_cannot_be_measured_leaves_the_others(monkeypatch):
    measure = phase.analyze

    def fails_on_e(channel, *args):
        if channel.key == "E_P":
            raise ValueError("odd geometry")
        return measure(channel, *args)

    monkeypatch.setattr(phase, "analyze", fails_on_e)
    frames = decoded(unhurried().update(two_pairs(), revision=1))
    assert [(header["type"], header.get("key")) for header, _ in frames] == [("phase_list", None),
                                                                             ("phase_pair", "D_P")]
    assert frames[0][0]["keys"] == ["D_P"]
    assert frames[0][0]["warnings"] == ["E_P/N: not measured (ValueError: odd geometry)"]


def test_after_a_failure_the_list_and_every_pair_come_back(monkeypatch):
    tracker = unhurried()
    tracker.update(two_pairs(), revision=1)
    edited = two_pairs(detour=True)

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(phase.channels, "find_channels", broken)
    with pytest.raises(RuntimeError):
        tracker.update(edited, revision=2)
    stopped = decoded(tracker.failed(RuntimeError("boom"), revision=2))
    assert stopped[0][0]["keys"] == [] and stopped[0][0]["warnings"] == ["Dynamic phase stopped: RuntimeError: boom"]
    with pytest.raises(RuntimeError):
        tracker.update(edited, revision=3)  # tried again on the next poll
    assert tracker.failed(RuntimeError("boom"), revision=3) == []  # said once, not every poll
    monkeypatch.undo()
    back = decoded(tracker.update(edited, revision=4))
    assert [(header["type"], header.get("key")) for header, _ in back] == [
        ("phase_list", None), ("phase_pair", "D_P"), ("phase_pair", "E_P")]
    assert back[0][0]["keys"] == ["D_P", "E_P"] and back[0][0]["warnings"] == []


def test_netclasses_are_asked_again_after_kicad_was_busy():
    tracker, asked = unhurried(), []

    def busy(nets):
        asked.append(set(nets))
        return None

    def ready(nets):
        asked.append(set(nets))
        return {net: ("HS", "") for net in nets}

    tracker.update(two_pairs(), revision=1, read_netclasses=busy)
    tracker.update(two_pairs(), revision=2, read_netclasses=ready)
    assert len(asked) == 2 and tracker.netclasses["D_P"] == ("HS", "")
    tracker.update(two_pairs(), revision=3, read_netclasses=ready)
    assert len(asked) == 2  # answered: not asked again


def test_a_merged_netclass_falls_back_to_the_project_patterns():
    project = {"net_settings": {"classes": [{"name": "Default", "tuning_profile": ""},
                                            {"name": "USB_90", "tuning_profile": "USB_90"}],
                                "netclass_patterns": [{"pattern": "/USB/USB*", "netclass": "USB_90"}]}}
    kicad = {"/USB/USB_SSRX1_P": "Effective for net: /USB/USB_SSRX1_P"}  # several netclasses apply
    assert delays.net_profiles_from_project(project, ["/USB/USB_SSRX1_P"], kicad) == {"/USB/USB_SSRX1_P": "USB_90"}


def test_kicad_names_each_pair_nets_netclass_and_its_tuning_profile():
    from types import SimpleNamespace

    from kipy.project_types import NetClass
    from kipy.proto.common.types import project_settings_pb2

    from kileido_bridge.kicad_reader import net_classes

    fast = project_settings_pb2.NetClass(name="HS")
    fast.board.tuning_profile = "Fast"
    plain = project_settings_pb2.NetClass(name="Default")
    asked = []

    class Board:
        def get_nets(self):
            return [SimpleNamespace(name=name) for name in ("D_P", "D_N", "GND")]

        def get_netclass_for_nets(self, nets):
            asked.extend(net.name for net in nets)
            return {"D_P": NetClass(fast), "D_N": NetClass(plain)}

    assert net_classes(Board(), {"D_P", "D_N"}) == {"D_P": ("HS", "Fast"), "D_N": ("Default", "")}
    assert sorted(asked) == ["D_N", "D_P"]  # only the pair nets are asked about
