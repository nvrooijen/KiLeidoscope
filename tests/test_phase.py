"""Dynamic phase of differential pairs on synthetic boards (tests/phase_boards.py)."""

import math
from dataclasses import replace

import numpy as np
import pytest

import phase_boards as boards
from kileido_bridge import channels, delays, model, phase, protocol
from kileido_bridge.board_text import stackup_layers

MM = boards.MM


def only_pair(snapshot, **settings):
    results, warnings = phase.analyze_board(snapshot, settings=phase.Settings(**settings))
    assert len(results) == 1 and not warnings
    return results[0]


def unit_delay(snapshot, layer="F.Cu"):
    """The pair's unit delay: one number per layer, as KiCad's."""
    heights = protocol.layer_heights_nm(snapshot)[0]
    return delays.StackupDelays(snapshot.stackup.layers, heights).track("D_P", layer, boards.WIDTH).ps_per_mm


def test_matched_pair_stays_in_phase():
    result = only_pair(boards.matched())
    assert result.channel.name == "D_P/N"
    assert phase.end_label(result.p.start, result.n.start) == "J1.1/2"  # the end whose label sorts first
    assert phase.end_label(result.p.end, result.n.end) == "U1.1/2"
    assert result.p.length_nm == pytest.approx(50 * MM)
    assert result.delay_p_ps == pytest.approx(50 * unit_delay(boards.matched()))
    assert abs(result.skew_ps) < 1e-9 and result.max_abs_ps < 1e-9
    assert result.excursions == ()
    assert result.sources == (delays.STACKUP,)
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
    plateau = 0.5 * unit_delay(snapshot)  # ps: the detour's extra length
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
    plateau = 0.5 * unit_delay(snapshot)
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
    assert crossing.reached_ps == pytest.approx(30 * unit_delay(snapshot, "B.Cu"))
    model_ = delays.StackupDelays(snapshot.stackup.layers, heights)
    expected = (20 * unit_delay(snapshot) + 30 * unit_delay(snapshot, "B.Cu")
                + model_.barrel("D_P", "B.Cu", "F.Cu", ("F.Cu", "B.Cu"), barrel)[0])
    assert crossing.delay_ps == pytest.approx(math.sqrt(4.0) * delays.PS_PER_MM_VACUUM * barrel / 1e6)
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
    separate = phase.analyze_board(snapshot, settings=phase.Settings(follow_series=False))[0]
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
    assert result.p.length_nm == pytest.approx(49.9 * MM)  # to where the copper first reaches J1.1's pad
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
    results, warnings = phase.analyze_board(board.snapshot())
    assert results == [] or "N side is not fully routed" in results[0].warnings
    assert results or warnings == ["D_P/N: no routed path between two pads yet"]


def test_arcs_count_their_true_length():
    arc = model.Arc("a", "F.Cu", "D_P", (0, 0), (MM, MM), (2 * MM, 0), boards.WIDTH)
    points, length = channels._arc_polyline(arc)
    assert length == pytest.approx(math.pi * MM)
    assert points[0] == (0, 0) and points[-1] == (2 * MM, 0)


# --- Notes: where one delay per layer is known to be off ----------------------------------

def coupled_turn(back: bool = False, layer: str = "F.Cu"):
    """A tightly coupled pair (0.1 mm gap on 0.2 mm of laminate; P at y = 0, N at 0.25)
    running +x, then round a 90° turn towards +y with P outside (2 mm centreline radius)
    and on to y = 6; with `back`, a second turn the other way (an S) and on to x = 24."""
    board = boards.Board().part("U1", (1, "D_P", (0, 0)), (2, "D_N", (0, 0.25)), pad=200_000, layer=layer)
    board.path("D_P", (0, 0), (10, 0), layer=layer).turn("D_P", (10, 2), 2.0, -90, 0, layer=layer)
    board.path("D_P", (12, 2), (12, 6), layer=layer)
    board.path("D_N", (0, 0.25), (10, 0.25), layer=layer).turn("D_N", (10, 2), 1.75, -90, 0, layer=layer)
    board.path("D_N", (11.75, 2), (11.75, 6), layer=layer)
    if not back:
        return board.part("J1", (1, "D_P", (12, 6)), (2, "D_N", (11.75, 6)), pad=200_000, layer=layer).snapshot()
    board.turn("D_P", (14, 6), 2.0, 180, 90, layer=layer).path("D_P", (14, 8), (24, 8), layer=layer)
    board.turn("D_N", (14, 6), 2.25, 180, 90, layer=layer).path("D_N", (14, 8.25), (24, 8.25), layer=layer)
    return board.part("J1", (1, "D_P", (24, 8)), (2, "D_N", (24, 8.25)), pad=200_000, layer=layer).snapshot()


def rounded_bump(radius_mm: float):
    """The 1 mm-pitch pair with N leaving it at x = 10 through four arcs of `radius_mm`
    and two legs 1.2 mm apart (centre to centre), 2 mm up, back on it at x = 12.4."""
    board = boards.ends(boards.Board()).path("D_P", (0, 0), (50, 0))
    r, top = radius_mm, 3.0
    board.path("D_N", (0, 1), (10, 1)).turn("D_N", (10, 1 + r), r, -90, 0).path("D_N", (10 + r, 1 + r), (10 + r, top))
    board.turn("D_N", (10.6 + r, top), 0.6, 180, 90).turn("D_N", (10.6 + r, top), 0.6, 90, 0)
    board.path("D_N", (11.2 + r, top), (11.2 + r, 1 + r)).turn("D_N", (11.2 + 2 * r, 1 + r), r, 180, 270)
    return board.path("D_N", (11.2 + 2 * r, 1), (50, 1)).snapshot()


def from_u1(snapshot, model_=None):
    """The pair walked from U1 (J1 sorts first, so that is the flipped direction)."""
    results, warnings = phase.analyze_board(snapshot, model_, phase.Settings(flipped=frozenset({"D_P"})))
    assert len(results) == 1 and not warnings
    return results[0]


def test_a_straight_pair_has_no_notes():
    assert only_pair(boards.matched()).notes == ()


def test_a_coupled_turn_reads_its_length_difference_and_a_note_adds_the_full_wave_share():
    """KiCad's number counts a coupled turn by its length difference; a full-wave check found
    such a turn makes about 8.5 % more. Δt reads the length rule (the straight before it
    nothing, halfway round half) and a note at the turn says what it made and what to add.
    An S, the same turn back, takes the skew back and has two notes that cancel."""
    plain = 0.25 * math.pi / 2 * unit_delay(coupled_turn())  # the length rule
    snapshot = coupled_turn()
    result = from_u1(snapshot)
    assert result.p.start.label == "U1.1"
    assert result.dt_ps[-1] == pytest.approx(plain, rel=0.02)
    assert result.skew_ps == pytest.approx(result.dt_ps[-1], abs=0.02)
    assert np.allclose(result.delays_ps[:, 0] - result.delays_ps[:, 1], result.dt_ps)
    assert np.abs(result.dt_ps[result.along_nm < 9 * MM]).max() < 1e-9  # the straight before it: nothing
    halfway = int(np.argmin(np.linalg.norm(result.centre_nm[:, :2] - np.array((11.326, 0.674)) * MM, axis=1)))
    assert result.dt_ps[halfway] == pytest.approx(plain / 2, rel=0.15)
    (note,) = result.notes
    assert note.kind == "coupled turn" and 9 * MM < note.at_nm < 11 * MM and note.length_nm < 4 * MM
    assert note.made_ps == pytest.approx(plain, rel=0.02) and note.add_ps == pytest.approx(phase.TURN_SHARE * note.made_ps)
    assert note.text == f"Coupled turn: {note.made_ps:+.1f} ps of Δt, about 8.5 % more in a full-wave check: " \
                        f"add {note.add_ps:+.1f} ps"
    arcs = {arc.id for arc in snapshot.arcs}
    assert arcs <= set(note.item_ids)  # both sides' arcs: select-in-KiCad lights the turn
    s_curve = from_u1(coupled_turn(back=True))
    middle = int(np.argmin(np.abs(s_curve.centre_nm[:, 1] - 4 * MM)))  # on the leg between the turns
    assert s_curve.dt_ps[middle] == pytest.approx(plain, rel=0.02)
    assert s_curve.dt_ps[-1] == pytest.approx(0.0, abs=0.05)
    there, back = s_curve.notes
    assert there.add_ps == pytest.approx(-back.add_ps, rel=0.02) and there.add_ps > 0


def test_a_coupled_turn_on_stripline_gets_no_note():
    """Inside the board the two modes run alike; turns were checked on microstrip only."""
    assert from_u1(coupled_turn(layer="In1.Cu")).notes == ()


def test_a_bump_after_the_turn_has_its_own_note_and_leaves_the_turns_alone():
    """N bumps away from P 1 mm after a tight turn (sharp corners: a tight meander). P runs
    straight past it: P's stretch beside the bump does not reach back over the turn's short
    chords, so the turn keeps its own note and the bump gets one of its own."""
    board = boards.Board().part("U1", (1, "D_P", (0, 0)), (2, "D_N", (0, 0.25)), pad=200_000)
    board.path("D_P", (0, 0), (10, 0)).turn("D_P", (10, 0.6), 0.6, -90, 0).path("D_P", (10.6, 0.6), (10.6, 8))
    board.path("D_N", (0, 0.25), (10, 0.25)).turn("D_N", (10, 0.6), 0.35, -90, 0)
    board.path("D_N", (10.35, 0.6), (10.35, 1.6), (9.6, 1.6), (9.6, 2.2), (10.35, 2.2), (10.35, 8))
    snapshot = board.part("J1", (1, "D_P", (10.6, 8)), (2, "D_N", (10.35, 8)), pad=200_000).snapshot()
    result = from_u1(snapshot)
    plain = 0.25 * math.pi / 2 * unit_delay(snapshot)
    turn, bump = result.notes
    # Within 4 %: on an arc this tight (0.475 mm, about 3 widths) the steps at its two ends
    # are only partly counted.
    assert turn.kind == "coupled turn" and turn.made_ps == pytest.approx(plain, rel=0.04)
    assert bump.kind == "tight meander" and bump.at_nm > turn.at_nm + turn.length_nm
    assert bump.made_ps < -5 and bump.add_ps == pytest.approx(phase.TIGHT_SHARE * bump.made_ps)


def test_each_tight_meander_of_a_far_compensated_pair_has_a_note():
    """The study's largest error: a tight meander (sharp corners, legs closer than three
    widths) makes about 17 % less skew than its extra length. Walked from J1, N's bump at
    x = 47 comes first and makes −Δt; P's bump at x = 2 takes it back. A note on each: the
    excursion between them is about 17 % smaller than Δt shows, and at the end they cancel."""
    snapshot = boards.compensated_far()
    result = only_pair(snapshot)
    plateau = (result.along_nm > 5 * MM) & (result.along_nm < 45 * MM)
    made = float(np.median(result.dt_ps[plateau]))
    assert made < -2.0
    first, second = result.notes
    assert first.kind == second.kind == "tight meander" and first.at_nm < 5 * MM and second.at_nm > 45 * MM
    assert first.made_ps == pytest.approx(made, rel=0.02) and second.made_ps == pytest.approx(-made, rel=0.02)
    assert first.add_ps == pytest.approx(phase.TIGHT_SHARE * made) and first.add_ps > 0
    n_bump = {track.id for track in snapshot.tracks if track.net == "D_N" and track.start[0] > 46 * MM}
    assert n_bump & set(first.item_ids)


@pytest.mark.parametrize("radius_mm, tight", [(0.6, False), (0.3, True)])
def test_a_looser_meander_gets_no_note_and_a_tight_arc_one(radius_mm, tight):
    """Arcs of 4 widths and legs 7 widths apart: not tight, and without a tuning profile its
    lone copper gets no note either (the stackup estimate is the lone trace's). Arcs of 2
    widths with the same legs: tight."""
    result = from_u1(rounded_bump(radius_mm))
    made = float(np.median(result.dt_ps[result.along_nm > 20 * MM]))
    assert made < -10.0  # N is longer: Δt < 0
    if tight:
        (note,) = result.notes
        assert note.kind == "tight meander" and note.made_ps == pytest.approx(made, rel=0.02)
    else:
        assert result.notes == ()


def test_with_a_tuning_profile_a_bump_away_from_the_partner_gets_a_note():
    """KiCad's number is the coupled pair's; a lone trace runs about 3 % slower (openEMS on a
    straight lone trace). With a profile, the looser bump's Δt gets that note."""
    snapshot = rounded_bump(0.6)
    heights = protocol.layer_heights_nm(snapshot)[0]
    model_ = delays.StackupDelays(snapshot.stackup.layers, heights, {"Fast": delays.TuningProfile("Fast", {"F.Cu": 6.0})},
                                  {"D_P": "Fast", "D_N": "Fast"})
    result = from_u1(snapshot, model_)
    (note,) = result.notes
    assert note.kind == "apart" and note.made_ps < -10 and note.add_ps == pytest.approx(phase.APART_SHARE * note.made_ps)
    assert note.made_ps == pytest.approx(result.dt_ps[-1], rel=0.02)  # the bump made all of the skew


def test_the_frame_carries_the_notes_and_only_dt_along_the_pair():
    result = from_u1(coupled_turn())
    header, arrays = protocol.FrameDecoder().feed(phase.pair_frame(result, 1))[0]
    (note,) = header["notes"]
    assert note["kind"] == "coupled turn" and note["add_ps"] == pytest.approx(result.notes[0].add_ps)
    assert note["at_mm"] == pytest.approx(result.notes[0].at_nm / 1e6) and note["ids"]
    assert {"along", "dt"} <= set(arrays) and not {"dt_read", "band_lo", "band_hi"} & set(arrays)
    assert "lines" not in header and "band_note" not in header


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


def test_the_netclass_tuning_profile_sets_the_delay_first():
    profiles = delays.tuning_profiles(PROJECT)
    assert profiles["Fast"].layers == {"F.Cu": 6.0}  # attoseconds per mm -> ps per mm
    nets = delays.net_profiles_from_project(PROJECT, ["D_P", "GND"])
    assert nets == {"D_P": "Fast", "GND": ""}  # D_* matches the HS pattern
    assert delays.net_profiles_from_project(PROJECT, ["D_P"], {"D_P": "Default"}) == {"D_P": ""}  # KiCad wins
    snapshot = boards.layer_change()
    heights = protocol.layer_heights_nm(snapshot)[0]
    model_ = delays.StackupDelays(snapshot.stackup.layers, heights, profiles, {"D_P": "Fast", "D_N": "Fast"})
    profiled = model_.track("D_P", "F.Cu", 150_000)
    assert profiled == delays.UnitDelay(6.0, "tuning profile 'Fast'")  # KiCad's number
    assert model_.track("D_P", "F.Cu", boards.WIDTH) == profiled  # for every track of the net, as KiCad charges it
    assert model_.track("D_P", "B.Cu", boards.WIDTH).source == delays.STACKUP  # no entry for B.Cu
    assert model_.barrel("D_P", "F.Cu", "B.Cu", ("F.Cu", "B.Cu"), 1e6) == (12.5, "tuning profile 'Fast'")
    assert model_.barrel("D_P", "F.Cu", "In1.Cu", ("F.Cu", "B.Cu"), 0.2e6)[0] == pytest.approx(1.6)
    result = phase.analyze_board(snapshot, model_)[0][0]
    assert result.sources == (delays.STACKUP, "tuning profile 'Fast'")


def test_a_profiles_number_counts_every_mm_as_kicad_does():
    """A pair with a profile reads its number times the length, a bump away from the
    partner included (the note on it says what that misses); an inner layer has its own."""
    heights = protocol.layer_heights_nm(boards.matched())[0]
    profile = delays.TuningProfile("Fast", {"F.Cu": 6.0, "In1.Cu": 7.0})
    model_ = delays.StackupDelays(boards.STACKUP.layers, heights, {"Fast": profile}, {"D_P": "Fast", "D_N": "Fast"})
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


def test_without_dielectric_constants_fr4_is_named_as_the_source():
    snapshot = boards.matched()
    bare = model.Stackup(tuple(replace(layer, epsilon_r=None) for layer in snapshot.stackup.layers))
    result = only_pair(replace(snapshot, stackup=bare))
    assert result.sources == (delays.DEFAULT,)
    inner = delays.StackupDelays(bare.layers, protocol.layer_heights_nm(snapshot)[0])
    assert inner.track("D_P", "In1.Cu", 0).ps_per_mm == pytest.approx(delays.PS_PER_MM_VACUUM * math.sqrt(4.2))


def test_the_lone_microstrip_estimate_is_near_the_full_wave():
    """Without a profile an outer layer is a lone microstrip (Hammerstad and Jensen). A 0.2 mm
    trace over 0.12 mm of εr 4.1 alone, in a 3D FDTD run (openEMS, 2026-10-16, 8 cells per
    dielectric height, not in the repository): 5.876 ps/mm."""
    epsilon = delays.microstrip_epsilon(4.1, 200_000, 120_000)
    assert delays.PS_PER_MM_VACUUM * math.sqrt(epsilon) == pytest.approx(5.876, rel=0.005)


def test_microstrip_is_faster_than_stripline():
    heights = protocol.layer_heights_nm(boards.matched())[0]
    model_ = delays.StackupDelays(boards.STACKUP.layers, heights)
    outer, inner = (model_.track("D_P", layer, boards.WIDTH).ps_per_mm for layer in ("F.Cu", "In1.Cu"))
    assert delays.PS_PER_MM_VACUUM < outer < inner == pytest.approx(delays.PS_PER_MM_VACUUM * 2.0)


def test_stackup_dielectric_constants_come_from_the_board_text():
    text = """(kicad_pcb (setup (stackup
        (layer "F.SilkS" (type "Top Silk Screen"))
        (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) (epsilon_r 3.3))
        (layer "F.Cu" (type "copper") (thickness 0.035))
        (layer "dielectric 1" (type "prepreg") (thickness 0.1) (material "FR4") (epsilon_r 4.4)
            (loss_tangent 0.02)
            addsublayer (thickness 0.1) (material "FR4") (epsilon_r 3.6) (loss_tangent 0.01))
        (layer "B.Cu" (type "copper") (thickness 0.035))
        (copper_finish "ENIG"))))"""
    layers = stackup_layers(text)
    assert [layer.name for layer in layers] == ["F.SilkS", "F.Mask", "F.Cu", "dielectric 1", "B.Cu"]
    dielectric = layers[3]
    assert dielectric.type == "dielectric" and dielectric.thickness_nm == 200_000
    assert dielectric.epsilon_r == pytest.approx(4.0) and dielectric.loss_tangent == pytest.approx(0.015)
    assert layers[1].epsilon_r is None  # only dielectrics carry the signal's epsilon_r
    assert stackup_layers("(kicad_pcb)") == ()


def test_a_shorted_pair_is_not_measured_and_says_where():
    (result,), _ = phase.analyze_board(boards.shorted())
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
    (result,), _ = phase.analyze_board(boards.looped())
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
    results, _ = phase.analyze_board(board())
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


def unhurried():
    """A tracker whose clock stands still: every pair in one poll, however slow the machine."""
    return phase.PhaseTracker(clock=lambda: 0.0)


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
    tracker = phase.PhaseTracker(clock=lambda: next(ticks) * phase.BUDGET_S * 0.6)  # one pair per poll
    first = decoded(tracker.update(two_pairs(), revision=1))
    assert first[0][0]["pending"] == 1 and len(first) == 2
    second = decoded(tracker.update(two_pairs(), revision=2))
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
