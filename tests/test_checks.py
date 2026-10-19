"""The check catalogue: ids, groups, tools, the quick selection."""

import re

from kileido_bridge import checks, findings
from kileido_bridge.checks import CATALOGUE, GROUPS, by_id


def test_ids_are_unique_and_pass_the_findings_check_rule():
    ids = [check.id for check in CATALOGUE]
    assert len(ids) == len(set(ids)) == 66
    for check in CATALOGUE:
        assert findings._CHECK.fullmatch(check.id), check.id
        group, dot, name = check.id.partition(".")
        assert dot and group == check.group and re.fullmatch(r"[a-z][a-z_]*", name), check.id


def test_groups_in_display_order_cover_the_catalogue():
    assert set(GROUPS) == {check.group for check in CATALOGUE}
    assert len(GROUPS) == len(set(GROUPS)) and set(checks.GROUP_TITLES) == set(GROUPS)
    order = [GROUPS.index(check.group) for check in CATALOGUE]
    assert order == sorted(order)  # the catalogue walks the groups in display order


def test_rows_are_filled_and_draw_only_with_known_tools():
    for check in CATALOGUE:
        assert check.title and check.instruction and check.evidence, check.id
        assert len(check.title) <= 40 and "\n" not in check.instruction, check.id
        assert set(check.draws) <= findings.TOOLS and len(set(check.draws)) == len(check.draws), check.id
        assert not set(check.later) & findings.TOOLS, check.id  # tools not built yet only
    no_drawing = [check.id for check in CATALOGUE if not check.draws]
    assert no_drawing == ["decap.pdn_impedance", "emc.emission_estimate", "fab.copper_balance"]
    for check_id in no_drawing[:2]:  # about parts: named in the message, highlighted from it
        assert "name the parts it concerns in the message" in by_id(check_id)[0].instruction, check_id
    assert "Board-wide: no drawing needed." in by_id("fab.copper_balance")[0].instruction  # the whole board
    for check_id in ("plane.stackup", "plane.plane_spacing"):  # the stackup measures their distance
        check = by_id(check_id)[0]
        assert "layers" in check.draws and "checks it against the stackup" in check.instruction, check_id
        assert findings.measurable(check.draws, check.values), check_id


def test_value_keys_carry_a_unit_the_findings_file_accepts():
    for check in CATALOGUE:
        assert len(set(check.values)) == len(check.values), check.id
        for key in check.values:
            assert findings._VALUE_KEY.fullmatch(key) and findings._unit(key), (check.id, key)
    assert by_id("plane.gap_crossing")[0].values == ("gap_mm", "coverage_pct", "length_mm")
    assert "via_density_per_cm2" in by_id("plane.stitching_sparse")[0].values


def test_the_quick_checks_are_three():
    assert [check.id for check in CATALOGUE if check.v1] == ["plane.gap_crossing", "plane.layer_change_no_return",
                                                             "decap.distance"]
    # the plane tools first, then how they draw with the marks alone
    assert by_id(["plane.gap_crossing"])[0].draws == ("plane_gap", "return_path", "highlight", "area", "label",
                                                      "ghost")
    assert by_id(["plane.layer_change_no_return"])[0].draws == ("layer_change", "highlight", "distance", "ghost")
    assert set(by_id(["decap.distance"])[0].draws) == {"distance", "highlight", "ghost"}


def test_checks_offer_the_ghost_kinds_that_fit_them():
    """A route over the plane, a cap or resistor moved nearer its pin, a row of stitching vias."""
    for check_id, kind in (("plane.gap_crossing", "ghost track"), ("decap.distance", "ghost part"),
                           ("power.boot_cap_far", "ghost part"), ("hs.termination_far", "ghost part"),
                           ("plane.stitching_sparse", "ghost via_row"), ("plane.edge_unstitched", "ghost via_row")):
        (check,) = by_id([check_id])
        assert "ghost" in check.draws and kind in check.instruction, check_id


def test_checks_draw_with_their_measuring_tool_first():
    measured = {"dc.trace_width": "width", "hs.edge_trace": "edge_gap", "fab.clearance": "clearance",
                "hs.bus_match": "bars", "dp.skew": "bars", "emc.esd_far": "routed_length",
                "analog.sensitive_near_noise": "clearance", "power.fb_near_noise": "clearance",
                "hs.crosstalk": "clearance", "fab.part_spacing": "clearance", "emc.esd_ground": "width"}
    planed = {"plane.gap_crossing": "plane_gap", "plane.layer_change_no_return": "layer_change",
              "plane.edge_run": "return_path", "plane.fragmented": "plane_islands", "plane.coverage": "plane_islands",
              "plane.weak_tie": "plane_islands", "dp.ref_change": "plane_gap", "analog.crossing": "return_path",
              "plane.stitching_sparse": "stitching", "plane.edge_unstitched": "stitching", "hs.stub": "stub",
              "hs.impedance_step": "discontinuity"}
    for check_id, tool in {**measured, **planed}.items():
        (check,) = by_id([check_id])
        assert check.draws[0] == tool and tool in check.instruction, check_id
    assert "edge_gap" in by_id(["fab.clearance"])[0].draws
    assert "routed_length" in by_id(["hs.clock_outer"])[0].draws
    plane_tools = {"layer_change", "plane_gap", "return_path", "plane_islands", "stitching", "stub", "discontinuity"}
    assert plane_tools <= findings.TOOLS
    for tools in ({"clearance", "width", "routed_length", "edge_gap", "bars"}, plane_tools):
        assert tools <= {tool for check in CATALOGUE for tool in check.draws}  # every tool is used
        assert not any(set(check.later) & tools for check in CATALOGUE)
    later = {check.id: check.later for check in CATALOGUE if check.later}
    assert not any(check_id in later for check_id in planed if check_id not in ("plane.weak_tie", "dp.ref_change"))
    assert later["plane.weak_tie"] == ("neck",) and later["dp.ref_change"] == ("pair_vias",)
    assert "width" in by_id(["hs.impedance_step"])[0].draws  # still drawn segment by segment
    assert by_id(["hs.stub"])[0].values == ("stub_mm", "length_mm")


def test_by_id_keeps_catalogue_order_and_drops_unknown_ids():
    found = by_id(["decap.distance", "nope.x", "plane.gap_crossing", "decap.distance"])
    assert [check.id for check in found] == ["plane.gap_crossing", "decap.distance"]
    assert by_id("hs.stub")[0].id == "hs.stub" and by_id([]) == ()


def test_rules_are_the_bridges_and_sit_on_the_pair_checks():
    """Every rule a check names exists in findings.RULES (and has prompt text); the pair checks carry them."""
    for check in CATALOGUE:
        for rule in check.rules:
            assert rule in findings.RULES and rule in findings.RULE_TEXT, (check.id, rule)
    by_rule = {}
    for check in CATALOGUE:
        for rule in check.rules:
            by_rule.setdefault(rule, []).append(check.id)
    assert by_rule == {
        "decap_pair": ["decap.distance", "decap.long_return", "decap.via_first", "decap.shared_via", "power.boot_cap_far"],
        "same_net": ["hs.termination_far", "emc.esd_far", "analog.crystal"],
        "signal_via": ["plane.layer_change_no_return"],
        "diff_pair": ["dp.skew", "dp.uncoupled", "dp.gap_change", "dp.via_asymmetry", "dp.ref_change", "dp.outer_layer"]}
