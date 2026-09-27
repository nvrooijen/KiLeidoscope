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


def test_compensation_far_from_the_mismatch_is_an_excursion_despite_zero_skew():
    """N's 0.5 mm detour at x = 47 is made up by P's at x = 2: matched end to end, but
    out of phase over the 45 mm between."""
    snapshot = boards.compensated_far()
    result = only_pair(snapshot)
    assert abs(result.skew_ps) < 1e-9
    plateau = 0.5 * unit_delay(snapshot)  # ps: the detour's extra length
    middle = np.abs(result.centre_nm[:, 0] - 25 * MM) < 5 * MM
    assert np.allclose(result.dt_ps[middle], -plateau, atol=0.01)  # walking from J1: N longer so far
    assert len(result.excursions) == 1
    excursion = result.excursions[0]
    assert 46.5 * MM <= excursion.pos[0] <= 47.3 * MM  # where it starts, at N's detour
    assert excursion.length_nm > 40 * MM
    assert excursion.peak_ps < -plateau
    assert excursion.item_id in {track.id for track in snapshot.tracks if track.net == "D_P"}


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


def test_flipping_walks_from_the_other_end():
    snapshot = boards.compensated_far()
    forward = only_pair(snapshot)
    flipped = only_pair(snapshot, flipped=frozenset({forward.channel.key}))
    assert flipped.flipped and flipped.p.start.label == "U1.1"
    middle = [np.abs(result.centre_nm[:, 0] - 25 * MM) < 5 * MM for result in (forward, flipped)]
    assert np.allclose(flipped.dt_ps[middle[1]], forward.skew_ps - forward.dt_ps[middle[0]][0], atol=1e-6)
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
    assert model_.track("D_P", "F.Cu", boards.WIDTH) == delays.UnitDelay(6.0, "tuning profile 'Fast'")
    assert model_.track("D_P", "B.Cu", boards.WIDTH).source == delays.STACKUP  # no entry for B.Cu
    assert model_.barrel("D_P", "F.Cu", "B.Cu", ("F.Cu", "B.Cu"), 1e6) == (12.5, "tuning profile 'Fast'")
    assert model_.barrel("D_P", "F.Cu", "In1.Cu", ("F.Cu", "B.Cu"), 0.2e6)[0] == pytest.approx(1.6)
    result = phase.analyze_board(snapshot, model_)[0][0]
    assert result.sources == (delays.STACKUP, "tuning profile 'Fast'")


def test_without_dielectric_constants_fr4_is_named_as_the_source():
    snapshot = boards.matched()
    bare = model.Stackup(tuple(replace(layer, epsilon_r=None) for layer in snapshot.stackup.layers))
    result = only_pair(replace(snapshot, stackup=bare))
    assert result.sources == (delays.DEFAULT,)
    inner = delays.StackupDelays(bare.layers, protocol.layer_heights_nm(snapshot)[0])
    assert inner.track("D_P", "In1.Cu", 0).ps_per_mm == pytest.approx(delays.PS_PER_MM_VACUUM * math.sqrt(4.2))


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


# --- Live tracking and frames -------------------------------------------------------------

def two_pairs(detour=False):
    board = boards.ends(boards.Board())
    board.part("U2", (1, "E_P", (0, 5)), (2, "E_N", (0, 6))).part("J2", (1, "E_P", (50, 5)), (2, "E_N", (50, 6)))
    board.path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (50, 1))
    board.path("E_P", (0, 5), *(boards.bump(10, 5, -0.25) if detour else ()), (50, 5)).path("E_N", (0, 6), (50, 6))
    return board.snapshot()


def decoded(frames):
    return protocol.FrameDecoder().feed(b"".join(frames))


def test_the_tracker_recomputes_only_pairs_whose_copper_changed():
    tracker = phase.PhaseTracker()
    first = decoded(tracker.update(two_pairs(), revision=1))
    assert [header["type"] for header, _ in first] == ["phase_list", "phase_pair", "phase_pair"]
    listing = first[0][0]
    assert listing["keys"] == ["D_P", "E_P"] and listing["pending"] == 0
    assert listing["settings"] == {"tolerance_ps": 1.0, "min_length_mm": 5.0, "follow_series": True, "flipped": []}
    header, arrays = first[1]
    assert header["name"] == "D_P/N" and header["start"]["label"] == "J1.1/2" and header["end"]["label"] == "U1.1/2"
    assert header["start"]["ids"] == ["J1.1", "J1.2"] and header["start"]["layer"] == "F.Cu"
    assert arrays["centre"].shape[1] == 3 and arrays["dt"].shape == (arrays["centre"].shape[0],)
    assert tracker.update(two_pairs(), revision=2) == []  # the same copper (equal records): nothing to send
    changed = decoded(tracker.update(two_pairs(detour=True), revision=3))
    assert [(header["type"], header.get("key")) for header, _ in changed] == [("phase_pair", "E_P")]
    assert changed[0][0]["skew_ps"] > 1.0
    assert [header["type"] for header, _ in decoded(tracker.snapshot_frames())] == ["phase_list", "phase_pair",
                                                                                     "phase_pair"]


def test_settings_from_blender_recompute_and_flip():
    tracker = phase.PhaseTracker()
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
