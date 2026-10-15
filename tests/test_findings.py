"""Findings files: validation limits, target resolution, measured distances, keys, handshake,
the changed notice."""

import json
import math
from dataclasses import replace

import pytest

import split_board
from kileido_bridge import findings, ghost_drc, model
from kileido_bridge.board_index import BoardIndex
from kileido_bridge.findings import Context, FindingsError, finding_key, named, parse, resolve_file
from kileido_bridge.targets import resolve
from test_board_index import MM, board, square

TRACK = "aaaa0000-0000-4000-8000-00000000t001"
VIA = "00000000-0000-0000-0000-00005a222dbd"
STAMP = "a1b2c3d4"


@pytest.fixture
def index():
    return BoardIndex(board())


def raw(*items, **top):
    data = {"format": "kls-findings 1", "run": "4", "stamp": STAMP, "tool": "test", "findings": list(items)}
    data.update(top)
    return json.dumps(data).encode("utf-8")


def finding(*draws, **fields):
    return {"check": "test.check", "severity": "error", "confidence": "high", "title": "T",
            "message": "m", "draw": list(draws), **fields}


def context(source="file", runs=None, live=STAMP, states=None):
    return Context(source, {"4": STAMP} if runs is None else runs, live, states or {})


def payload(index, *items, ctx=None, **top):
    return resolve_file(parse(raw(*items, **top)), index, ctx or context())


def only_draw(result):
    (item,) = result["findings"]
    (draw,) = item["draws"]
    return draw


def parsed_draws(draws, skipped):
    """`draws` parsed in one finding, `skipped` of them refused: (the draws kept, its problems)."""
    item = parse(raw(finding(*draws))).findings[0]
    assert sum("skipped" in p for p in item.problems) == skipped and len(item.draws) == len(draws) - skipped
    return item.draws, item.problems


# whole-file errors

@pytest.mark.parametrize("data, status", [
    (b"{not json", "error"),
    (b'{"format": "kls-findings 1", "findings": [{"values": {"x_mm": NaN}}]}', "error"),
    (b'{"format": "kls-findings 1", "x": Infinity}', "error"),
    (b"[" * 100_000 + b"]" * 100_000, ("error", "unknown_format")),  # 3.14's decoder parses it: then not an object
    (b"\xff\xfe{}", "error"),
    (b'{"format": "kls-findings 2"}', "unknown_format"),
    (b'{"findings": []}', "unknown_format"),
    (b"[1, 2]", "unknown_format"),
], ids=["not-json", "nan", "infinity", "deep", "not-utf8", "format-2", "no-format", "list"])
def test_refused_files(data, status):
    with pytest.raises(FindingsError) as error:
        parse(data)
    assert error.value.status in ((status,) if isinstance(status, str) else status)


def test_file_size_limit_and_bom():
    with pytest.raises(FindingsError, match="2 MB"):
        parse(raw(message="x" * (2 << 20)))
    found = parse(b"\xef\xbb\xbf" + raw(finding()))
    assert found.run == "4" and found.stamp == STAMP and len(found.findings) == 1


def test_huge_number_is_not_a_value():
    found = parse(b'{"format": "kls-findings 1", "findings": [{"title": "t", "values": {"x_mm": 1e400}}]}')
    assert found.findings[0].values == {} and "x_mm" in found.findings[0].values_text


# limits and plain text

def test_findings_and_draws_past_the_limits_are_dropped():
    found = parse(raw(*[finding(check=f"test.c{i % 30}") for i in range(400)]))
    assert len(found.findings) == 300 and any("100 findings not shown" in p for p in found.problems)
    assert sum(count for _, count in found.left_out) == 100
    draws = [{"tool": "highlight", "targets": ["U3"]}] * 21
    found = parse(raw(finding(*draws)))
    assert len(found.findings[0].draws) == 20 and any("past 20" in p for p in found.findings[0].problems)


def test_one_check_keeps_its_worst_twenty_and_counts_the_rest():
    many = [finding(check="drc.unconnected_items", severity="warning", title=f"U{i}") for i in range(2000)]
    few = [finding(check="drc.clearance", title=f"C{i}") for i in range(3)]
    found = parse(raw(*many, finding(check="drc.unconnected_items", title="the error"), *few))
    checks = [item.check for item in found.findings]
    assert checks.count("drc.unconnected_items") == 20 and checks.count("drc.clearance") == 3  # never pushed out
    assert "the error" in [item.title for item in found.findings]  # errors first, wherever they are in the file
    assert dict(found.left_out) == {"drc.unconnected_items": 1981}
    payload = resolve_file(found, None, context())
    assert payload["left_out"] == {"drc.unconnected_items": 1981}


def test_the_total_is_shared_out_one_check_at_a_time():
    items = [finding(check=f"test.c{c}", severity="info" if c else "error", title=f"{c}.{i}")
             for c in range(40) for i in range(20)]  # 800 findings of 40 checks
    found = parse(raw(*items))
    counts = {}
    for item in found.findings:
        counts[item.check] = counts.get(item.check, 0) + 1
    assert len(found.findings) == 300 and len(counts) == 40  # every check is shown
    assert max(counts.values()) - min(counts.values()) <= 1 and counts["test.c0"] == 8


def test_a_file_within_the_limits_loses_nothing_and_keeps_its_order():
    items = [finding(check="test.b", severity="info", title="1"), finding(check="test.a", title="2")]
    found = parse(raw(*items))
    assert [item.title for item in found.findings] == ["1", "2"] and found.left_out == ()


def test_text_is_cut_and_controls_become_spaces():
    found = parse(raw(finding(title="a" * 301, message="one\ntwo\x1b[31m‮red", fix=None)))
    item = found.findings[0]
    assert len(item.title) == 300 and item.title.endswith("…")
    assert any("title cut" in p for p in item.problems)
    assert item.message == "one two [31m red" and item.fix == ""


def test_values_numbers_with_units_only():
    values = {"distance_mm": 0.2, "area_mm2": 3, "skew_ps": 1.5, "ok_mm": True, "note_mm": "about 2",
              "count": 4, "Bad-Key": 1, "lst_a": [1, 2], "via_density_per_cm2": 2.5}
    item = parse(raw(finding(values=values))).findings[0]
    assert item.values == {"distance_mm": 0.2, "area_mm2": 3.0, "skew_ps": 1.5, "via_density_per_cm2": 2.5}
    assert item.values_text == {"ok_mm": "true", "note_mm": "about 2", "count": "4", "lst_a": "[1, 2]"}
    assert any("Bad-Key" in p for p in item.problems)
    many = {f"v{i}_mm": i for i in range(25)}
    item = parse(raw(finding(values=many))).findings[0]
    assert len(item.values) == 20 and any("past 20" in p for p in item.problems)


def test_bad_enums_fall_back():
    item = parse(raw(finding(severity="fatal", confidence="sure"))).findings[0]
    assert item.severity == "warning" and item.confidence == ""
    assert len(item.problems) == 2
    item = parse(raw({"check": "x"})).findings[0]
    assert item.title == "x" and "no title" in item.problems


def test_bad_check_ids_become_other(index):
    result = payload(index, finding(check="my.own_check"), finding(check="plane.gap_crossing"),
                     finding(check="has space"), finding(check="drc.clearance"))
    assert [f["check"] for f in result["findings"]] == ["my.own_check", "plane.gap_crossing", "other",
                                                        "drc.clearance"]


def test_bad_draws_are_skipped_and_the_finding_stays():
    draws = [{"tool": "explode", "targets": ["U3"]},
             {"tool": "highlight"},
             {"tool": "highlight", "targets": ["U3", 5]},
             {"tool": "highlight", "targets": ["x" * 201]},
             {"tool": "highlight", "targets": ["U3"] * 51},
             {"tool": "distance", "from": "U3", "to": "C12", "mode": "diagonal"},
             {"tool": "distance", "from": "U3", "to": "C12", "limit_is": "around"},
             {"tool": "distance", "from": "U3", "to": "C12", "limit": "0.2"},
             {"tool": "ghost", "kind": "resistor", "at": "U3"},
             {"tool": "area", "layer": "F.SilkS", "targets": ["U3"]},
             {"tool": "area", "layer": "F.Cu"},
             {"tool": "area", "layer": "F.Cu", "polygon": [[0, 0], [1, 1]]},
             {"tool": "area", "layer": "F.Cu", "polygon": [[0, 0], [1, 1], [1, 0]], "targets": ["U3"]},
             {"tool": "layers", "layers": ["F.Cu", "Edge.Cuts"]},
             {"tool": "label", "at": "U3"},
             "highlight",
             {"tool": "highlight", "targets": ["U3"], "step": 0, "emphasis": "loud", "label": "ok"}]
    item = parse(raw(finding(*draws))).findings[0]
    assert len(item.draws) == 1 and item.draws[0].emphasis == "normal"
    assert sum("skipped" in p for p in item.problems) == len(draws) - 1
    assert "draw 17 (highlight): step is not a whole number 1..100" in item.problems
    assert any("unknown tool explode" in p for p in item.problems)


def test_valid_draw_fields_are_normalised():
    draws = [{"tool": "distance", "from": "U3", "to": "C12", "mode": "center", "limit": 0.2, "step": 3},
             {"tool": "area", "layer": "in1.cu", "targets": ["uuid:z-gnd"], "emphasis": "strong"},
             {"tool": "area", "layer": "in1.cu", "polygon": [[0, 0], [1, 0], [1, 1]]},
             {"tool": "layers", "layers": ["f.cu", "In2.Cu"]},
             {"tool": "ghost", "kind": "via", "at": "pt:1,2@F.Cu", "size": 0.6, "emphasis": "strong"}]
    item = parse(raw(finding(*draws))).findings[0]
    distance, zone, area, layers, ghost = item.draws
    assert distance.fields["mode"] == "centre" and distance.fields["limit_is"] == "max" and not item.problems
    assert zone.fields == {"layer": "In1.Cu", "targets": ("uuid:z-gnd",), "polygon": ()} and zone.emphasis == "strong"
    assert area.fields["layer"] == "In1.Cu" and len(area.fields["polygon"]) == 3
    assert layers.fields["layers"] == ("F.Cu", "In2.Cu")
    assert ghost.fields["size"] == 0.6 and ghost.emphasis == "strong"


def test_header_fields():
    found = parse(raw(stamp="NOTHEX!!", run="r" * 40, skipped=[{"check": "decap.distance", "reason": "no caps"}, 3]))
    assert found.stamp == "" and len(found.run) == 32
    assert found.skipped == (("decap.distance", "no caps"),)
    assert any("stamp" in p for p in found.problems) and any("skipped entry" in p for p in found.problems)
    assert parse(raw(stamp="A1B2C3D4")).stamp == "a1b2c3d4"


# resolving

def test_every_target_form_resolves(index):
    targets = ["U3", "C12.1", "net:SIG", "net:SIG@B.Cu", "net:SIG@F.Cu~15,10.1", "layer:In1.Cu",
               f"uuid:{TRACK[-9:]}", "uuid:z-gnd", "pt:15,10.4@F.Cu", "edge"]
    draw = only_draw(payload(index, finding({"tool": "highlight", "targets": targets})))
    assert draw["ok"] and all(t["found"] for t in draw["targets"])
    assert draw["targets"][-1]["kind"] == "board edge"
    assert {"f-u3", "p-c12-1", TRACK, "arc-1", VIA, "z-gnd"} <= set(draw["ids"])
    assert len(draw["ids"]) == len(set(draw["ids"]))
    assert [t["kind"] for t in draw["targets"]][:2] == ["part", "pad"]


def test_missing_targets_are_noted_and_the_rest_is_drawn(index):
    result = payload(index, finding({"tool": "highlight", "targets": ["U3", "U99"]},
                                    {"tool": "label", "at": "U99", "text": "here"}))
    highlight, label = result["findings"][0]["draws"]
    assert highlight["ok"] and highlight["targets"][1] == {"text": "U99", "found": False,
                                                          "note": "U99 not found on board", "kind": "", "net": ""}
    assert not label["ok"] and label["problem"] == "at: U99 not found on board"
    assert result["findings"][0]["faded"] is False


def test_edge_distance_equals_board_index_closest(index):
    draw = only_draw(payload(index, finding({"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}"})))
    gap, _, _ = index.closest(index.records(VIA), index.records(TRACK))
    assert draw["measured_nm"] == round(gap.distance) == 4_600_000
    assert draw["points"] == [[*gap.a, "F.Cu"], [*gap.b, "F.Cu"]]
    assert draw["ids"] == [VIA, TRACK] and draw["limit_nm"] is None and draw["over"] is False


def test_centre_distance_and_point_ends(index):
    draw = only_draw(payload(index, finding({"tool": "distance", "from": "U3", "to": "C12", "mode": "centre"})))
    assert draw["measured_nm"] == 10 * MM and draw["points"][0] == [10 * MM, 10 * MM, "F.Cu"]
    draw = only_draw(payload(index, finding({"tool": "distance", "from": "pt:15,12@F.Cu", "to": f"uuid:{TRACK}"})))
    assert draw["measured_nm"] == 1_900_000 and draw["points"][0] == [15 * MM, 12 * MM, "F.Cu"]
    draw = only_draw(payload(index, finding({"tool": "distance", "from": f"uuid:{TRACK}", "to": "pt:15,12@F.Cu"})))
    assert draw["measured_nm"] == 1_900_000 and draw["points"][1] == [15 * MM, 12 * MM, "F.Cu"]
    draw = only_draw(payload(index, finding({"tool": "distance", "from": "pt:1,1@F.SilkS", "to": "pt:4,5@F.SilkS"})))
    assert draw["measured_nm"] == 5 * MM and draw["points"][0][2] == "F.SilkS"


def test_distance_to_the_board_edge(index):
    draw = only_draw(payload(index, finding({"tool": "distance", "from": "net:/~{RST}", "to": "edge"})))
    assert draw["measured_nm"] == 5 * MM - 100_000 and draw["ids"] == ["t-weird"]
    assert draw["targets"][1]["kind"] == "board edge"
    assert draw["points"][1][2] == "F.Cu"  # the edge end on the track's own layer: no line through the board


@pytest.mark.parametrize("limit, limit_is, over", [(5, "min", True), (5, "max", False), (4, "max", True),
                                                   (4, "min", False), (4.6, "min", False)])
def test_limit_is_and_over(index, limit, limit_is, over):
    draw = only_draw(payload(index, finding({"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}",
                                             "limit": limit, "limit_is": limit_is})))
    assert draw["limit_nm"] == round(limit * MM) and draw["limit_is"] == limit_is and draw["over"] is over


@pytest.mark.parametrize("claimed, disagrees", [(4.6, False), (4.2, False), (4.0, True), (5.2, True)])
def test_claimed_distance_against_measured(index, claimed, disagrees):
    result = payload(index, finding({"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}"},
                                    values={"distance_mm": claimed}))
    disagree = result["findings"][0]["disagree"]
    assert bool(disagree) is disagrees
    if disagrees:
        assert disagree == [{"key": "distance_mm", "claimed": claimed, "measured": 4.6}]


@pytest.mark.parametrize("limits, passes", [((5,), False), ((4,), True), ((4, 5), False), ((), False)])
def test_a_finding_its_measurements_do_not_back_up(index, limits, passes):
    """Every limit met (an edge gap of 1.91 mm against a 0.50 mm minimum): `passes`; Blender
    shows it within the limit, not as the problem. No limit given: nothing to judge."""
    draws = [{"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}", "limit": limit, "limit_is": "min"}
             for limit in limits] or [{"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}"}]
    assert payload(index, finding(*draws))["findings"][0]["passes"] is passes  # 4.6 mm measured


def test_label_arrow_ghost_area_layers(index):
    result = payload(index, finding(
        {"tool": "label", "at": "U3.4", "text": "pin 4", "label": "ignored here"},
        {"tool": "arrow", "from": "U3", "to": "C12.1"},
        {"tool": "ghost", "kind": "cap", "at": "pt:12,12@B.Cu", "size": 1.0},
        {"tool": "area", "layer": "In1.Cu", "polygon": [[0, 0], [1, 0], [1, 1]]},
        {"tool": "area", "layer": "In1.Cu", "targets": ["uuid:z-gnd"]},
        {"tool": "layers", "layers": ["In1.Cu"]}))
    label, arrow, ghost, polygon, zone, layers = result["findings"][0]["draws"]
    assert label["text"] == "pin 4" and label["points"] == [[10 * MM, 10 * MM, "F.Cu"]] and label["ids"] == ["p-u3-4"]
    assert arrow["points"] == [[10 * MM, 10 * MM, "F.Cu"], [20 * MM, 10 * MM, "F.Cu"]]
    assert ghost["ok"] and ghost["kind"] == "cap" and ghost["size_nm"] == MM
    assert ghost["points"] == [[12 * MM, 12 * MM, "B.Cu"]]
    assert polygon["polygon_nm"] == [[0, 0], [MM, 0], [MM, MM]] and polygon["layer"] == "In1.Cu"
    assert zone["ids"] == ["z-gnd"] and zone["ok"] and zone["layer"] == "In1.Cu"
    assert layers["layers"] == ["In1.Cu"] and layers["ok"]
    x0, y0, x1, y1 = result["findings"][0]["bbox_nm"]
    assert (x0, y0) == (0, 0) and x1 == 20.5 * MM and y1 < 40 * MM  # parts and points; the 40 mm plane is not


def test_a_ghost_at_a_point_is_that_point_not_the_copper_near_it(index):
    result = payload(index, finding({"tool": "ghost", "kind": "via", "at": "pt:15,10.4@F.Cu"},
                                    {"tool": "label", "at": "pt:15,10.4@F.Cu", "text": "here"}))
    ghost, label = result["findings"][0]["draws"]
    assert ghost["ok"] and ghost["points"] == [[15 * MM, 10_400_000, "F.Cu"]] and ghost["ids"] == []
    assert ghost["targets"] == [{"text": "pt:15,10.4@F.Cu", "found": True, "note": "", "kind": "point", "net": ""}]
    assert label["targets"][0]["note"].startswith("snapped to track")  # other tools still snap, and say so
    assert label["ids"] == [TRACK] and not result["findings"][0]["faded"]
    alone = payload(index, finding({"tool": "ghost", "kind": "via", "at": "pt:15,10.4@F.Cu"}))
    assert alone["findings"][0]["key"] == finding_key("test.check", ["pt:15,10.4@F.Cu"])
    far = only_draw(payload(index, finding({"tool": "ghost", "kind": "via", "at": "pt:3,3@F.Cu"})))
    assert far["ok"] and far["targets"][0]["note"] == ""  # no "no copper within 0.5 mm" either
    part = only_draw(payload(index, finding({"tool": "ghost", "kind": "cap", "at": "U3"})))
    assert part["ids"] == ["f-u3"] and part["points"] == [[10 * MM, 10 * MM, "F.Cu"]]  # not a point: as before


def test_a_part_measures_from_its_pads_and_still_lights_as_a_part(index):
    result = payload(index, finding({"tool": "distance", "from": "U3", "to": "C12"},
                                    {"tool": "highlight", "targets": ["U3"]}))
    distance, highlight = result["findings"][0]["draws"]
    assert distance["measured_nm"] == round(9.3 * MM)  # pad copper to pad copper, not U3's box (9.0)
    assert [(x, layer) for x, _, layer in distance["points"]] == [(10_200_000, "F.Cu"), (19_500_000, "F.Cu")]
    assert highlight["ids"] == ["f-u3"] and distance["ids"] == ["f-u3", "f-c12"]
    x0, y0, _, _ = result["findings"][0]["bbox_nm"]
    assert (x0, y0) == (9 * MM, 9 * MM)  # framed on the part's box


def test_bbox_frames_items_and_points_not_the_outline(index):
    result = payload(index, finding({"tool": "distance", "from": "net:/~{RST}", "to": "edge"}))
    assert result["findings"][0]["bbox_nm"] == [-5 * MM, 19.9 * MM, 10.1 * MM, 20.1 * MM]


def test_id_caps(index, monkeypatch):
    monkeypatch.setattr(findings, "MAX_IDS_DRAW", 3)
    draw = only_draw(payload(index, finding({"tool": "highlight", "targets": ["net:SIG", "U3"]})))
    assert not draw["ok"] and draw["ids"] == [] and draw["problem"] == "net:SIG has 5 items; add @layer or ~x,y"
    monkeypatch.setattr(findings, "MAX_IDS_DRAW", 5_000)
    monkeypatch.setattr(findings, "MAX_IDS_FILE", 6)
    result = payload(index, finding({"tool": "highlight", "targets": ["net:SIG"]}),
                     finding({"tool": "highlight", "targets": ["net:GND"]}, check="b"))
    first, second = (item["draws"][0] for item in result["findings"])
    assert first["ok"] and not second["ok"] and "100,000" not in second["problem"] and second["ids"] == []


def test_too_many_pairs_to_measure(index, monkeypatch):
    monkeypatch.setattr(findings, "MAX_PAIRS", 4)
    draw = only_draw(payload(index, finding({"tool": "distance", "from": "net:SIG", "to": "net:GND"})))
    assert not draw["ok"] and "name single items" in draw["problem"]


# changed since the file was written

def test_changed_when_the_live_stamp_differs(index):
    item = finding({"tool": "highlight", "targets": ["U3"]})
    same = payload(index, item)
    assert not same["changed"] and same["findings"][0]["draws"]
    assert same["status"] == "ok" and same["connected"] and same["run"] == "4"
    changed = payload(index, item, ctx=context(live="00000000"))
    assert changed["changed"] and changed["findings"][0]["draws"]
    drc = payload(index, item, ctx=context(source="drc", live="00000000"))
    assert drc["changed"]
    assert not payload(index, item, stamp=None)["changed"]
    assert not payload(index, item, ctx=context(live=""))["changed"]


# handshake

def test_handshake_known_changed(index):
    item = finding({"tool": "highlight", "targets": ["U3"]})
    known = payload(index, item)
    assert known["handshake"] == "known" and not known["changed"] and known["findings"][0]["draws"]
    assert known["status"] == "ok" and known["connected"] and known["run"] == "4"
    changed = payload(index, item, ctx=context(live="00000000"))
    assert changed["handshake"] == "known" and changed["changed"]
    drc = payload(index, item, ctx=context(source="drc", runs={}, live="00000000"))
    assert drc["handshake"] == "known" and drc["changed"]


@pytest.mark.parametrize("ctx, top", [
    ({"runs": {"4": "ffffffff"}}, {}),  # our run id, another stamp
    ({"runs": {}}, {}),  # a run this bridge never made
    ({}, {"stamp": None}),  # a run id but no stamp
    ({}, {"run": None}),  # a stamp but no run id
])
def test_handshake_other_run_is_drawn(index, ctx, top):
    item = finding({"tool": "highlight", "targets": ["U3"]})
    result = payload(index, item, ctx=context(**ctx), **top)
    assert result["handshake"] == "other_run" and not result["changed"]
    (drawn,) = result["findings"][0]["draws"]
    assert drawn["ok"] and drawn["ids"] and result["findings"][0]["bbox_nm"] is not None


def test_handshake_outside_is_drawn(index):
    item = finding({"tool": "highlight", "targets": ["U3"]})
    result = payload(index, item, run=None, stamp=None, tool="some script")
    assert result["handshake"] == "outside" and not result["changed"]
    assert result["run"] == "" and result["tool"] == "some script"
    (drawn,) = result["findings"][0]["draws"]
    assert drawn["ok"] and drawn["ids"] and result["findings"][0]["bbox_nm"] is not None
    assert payload(index, item, run=None, stamp=None, ctx=context(source="drc"))["handshake"] == "outside"


def test_not_connected_lists_findings_with_a_note():
    result = payload(None, finding({"tool": "highlight", "targets": ["U3"]},
                                   {"tool": "area", "layer": "F.Cu", "polygon": [[0, 0], [1, 0], [1, 1]]}))
    assert result["connected"] is False
    highlight, area = result["findings"][0]["draws"]
    assert highlight["targets"][0]["note"] == "KiCad not connected" and not highlight["ok"]
    assert highlight["problem"] == "KiCad not connected" and area["ok"]
    assert result["findings"][0]["faded"] is False


def test_removed_target_fades_the_finding(index):
    snapshot = board()
    snapshot = model.BoardSnapshot(**{**snapshot.__dict__, "tracks": snapshot.tracks[1:]})
    result = payload(BoardIndex(snapshot), finding({"tool": "highlight", "targets": [f"uuid:{TRACK[-8:]}"]}))
    assert result["findings"][0]["faded"] is True
    assert payload(index, finding({"tool": "highlight", "targets": [f"uuid:{TRACK[-8:]}"]}))["findings"][0]["faded"] is False


def test_status_payload_shape_matches():
    empty = findings.status_payload("unsaved")
    full = resolve_file(parse(raw()), None, context())
    assert set(empty) == set(full) and empty["findings"] == []


# keys

def test_named_rewrites_uuid_point_and_picked_targets(index):
    def names(text):
        return named(index, resolve(index, text))
    assert names(f"uuid:{TRACK[-8:]}") == ["net:SIG@F.Cu~15,10"]
    assert names("net:SIG@F.Cu~14.8,10.05") == ["net:SIG@F.Cu~15,10"]
    assert names("pt:15.2,10.3@F.Cu") == ["net:SIG@F.Cu~15,10"]
    assert names(f"uuid:{VIA}") == ["net:SIG@F.Cu~15,15"]
    assert names("uuid:z-gnd") == ["net:GND@In1.Cu", "net:GND@In2.Cu"]
    assert names("uuid:p-u3-4") == ["U3.4"] and names("uuid:f-c12") == ["C12"]
    assert resolve(index, "uuid:p-u3-4").items[0].number == "4"  # copper pads are found by id
    assert names("uuid:arc-1") == ["net:SIG@B.Cu~1,1"]
    assert names(" net:SIG ") == ["net:SIG"] and names("U3") == ["U3"] and names("edge") == ["edge"]
    assert names("uuid:nothing-here") == ["uuid:nothing-here"]
    assert names("pt:3,3@F.Cu") == ["pt:3,3@F.Cu"]  # snapped to nothing


def test_key_is_the_same_for_a_uuid_and_a_net_point_naming_one_track(index):
    by_uuid = payload(index, finding({"tool": "highlight", "targets": [f"uuid:{TRACK}", "U3.4"]}))
    by_name = payload(index, finding({"tool": "highlight", "targets": ["U3.4"]},
                                     {"tool": "label", "at": "net:SIG@F.Cu~14.8,10.05", "text": "x"}))
    assert by_uuid["findings"][0]["key"] == by_name["findings"][0]["key"]
    other = payload(index, finding({"tool": "highlight", "targets": [f"uuid:{TRACK}"]}, check="other.check"))
    assert other["findings"][0]["key"] != by_uuid["findings"][0]["key"]
    assert finding_key("c", ["b", "a", "a"]) == finding_key("c", ["a", "b"])
    assert len(finding_key("c", [])) == 16


def test_keys_are_unique_and_carry_state(index):
    first = payload(index, finding(title="one"), finding(title="two"), finding({"tool": "highlight", "targets": ["U3"]}),
                    finding({"tool": "highlight", "targets": ["U3"]}))
    keys = [item["key"] for item in first["findings"]]
    assert len(set(keys)) == 4 and keys[3] == keys[2] + "-2"
    states = {keys[0]: "dismissed", keys[2]: "confirmed"}
    again = payload(index, finding(title="one"), finding(title="two"), finding({"tool": "highlight", "targets": ["U3"]}),
                    ctx=context(states=states))
    assert [item["state"] for item in again["findings"]] == ["dismissed", "", "confirmed"]


@pytest.mark.parametrize("board_name, draws", [
    ("index", [{"tool": "distance", "from": f"uuid:{VIA}", "to": "edge", "limit": 1},
               {"tool": "highlight", "targets": ["net:SIG"]}]),
    ("index", [{"tool": "width", "target": "net:SIG", "required": 0.3},
               {"tool": "clearance", "a": f"uuid:{VIA}", "b": f"uuid:{TRACK}"},
               {"tool": "routed_length", "net": "SIG", "from": "U3.4", "to": "C12.1"},
               {"tool": "edge_gap", "target": "U3"},
               {"tool": "bars", "items": [{"label": "a", "value": 1}], "at": "C12"}]),
    ("split", [{"tool": "layer_change", "via": "uuid:via-lvds-0"},
               {"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:SATA_P"},
               {"tool": "return_path", "signal": "net:USB_D+", "plane": "GND@In1.Cu"},
               {"tool": "plane_islands", "net": "GND"}]),
    ("stitched", [{"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 5},
                  {"tool": "stub", "via": "uuid:st-0"},
                  {"tool": "discontinuity", "net": "USB_D+", "at": [[50, 10]]}]),
    ("index", [{"tool": "ghost", "kind": "part", "part": "C12", "at": "pt:25,30@F.Cu"},
               {"tool": "ghost", "kind": "track", "net": "SIG", "path": [[22, 5], [26, 8]], "layer": "F.Cu"},
               {"tool": "ghost", "kind": "via_row", "from": "pt:2,2@F.Cu", "to": "pt:2,12@F.Cu", "pitch": 3}]),
], ids=["marks", "measured", "planes", "stitching", "ghosts"])
def test_payload_is_json_and_every_draw_has_every_key(request, board_name, draws):
    result = payload(request.getfixturevalue(board_name),finding(*draws, values={"x_mm": 1.5}))
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    keys = set(findings._empty_draw(findings.Draw("highlight", {})))
    assert all(set(draw) == keys and draw["ok"] for draw in result["findings"][0]["draws"])
    assert all(isinstance(v, int) for v in result["findings"][0]["bbox_nm"])


def test_distance_between_items_sharing_a_layer_is_drawn_on_that_layer():
    from kileido_bridge import model as m
    from kileido_bridge.board_index import BoardIndex
    from kileido_bridge.findings import _edge_gap
    from kileido_bridge.targets import resolve
    mm = 1_000_000
    via = m.Via("v1", "A", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu")
    track = m.Track("t1", "In2.Cu", "B", (mm, -mm), (mm, mm), 200_000)
    other = m.Track("t2", "F.Cu", "B", (5 * mm, -mm), (5 * mm, mm), 200_000)
    snapshot = m.BoardSnapshot("t", {}, (track, other), (), (via,), (), (), (), m.Outline(()), m.Stackup(()), (), {})
    index = BoardIndex(snapshot)
    gap, layer_a, layer_b = _edge_gap(index, resolve(index, "uuid:v1"), resolve(index, "uuid:t1"))
    assert (layer_a, layer_b) == ("In2.Cu", "In2.Cu") and gap.distance == 600_000
    assert index.layers_of(via) == ("F.Cu", "In2.Cu", "B.Cu")


def test_an_arrow_between_items_on_a_shared_layer_stays_on_it():
    from kileido_bridge import model as m
    from kileido_bridge.board_index import BoardIndex
    from kileido_bridge.findings import _anchor_pair
    from kileido_bridge.targets import resolve
    mm = 1_000_000
    via = m.Via("v1", "A", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu")
    pad = m.Pad("p1", "", "1", "B", (mm, 0), (400_000, 400_000),
                {"B.Cu": ((((mm - 400_000, -400_000), (mm + 400_000, -400_000), (mm + 400_000, 400_000),
                            (mm - 400_000, 400_000)),),)})
    snapshot = m.BoardSnapshot("t", {}, (), (), (via,), (pad,), (), (), m.Outline(()), m.Stackup(()), (), {})
    index = BoardIndex(snapshot)
    ends = _anchor_pair(index, resolve(index, "uuid:v1"), resolve(index, "uuid:p1"))
    assert [end[2] for end in ends] == ["B.Cu", "B.Cu"]  # where DRC sees the hole gap, not the via's top


def test_a_finding_at_a_point_of_a_plane_is_framed_on_the_point(index):
    found = payload(index, finding({"tool": "highlight", "targets": ["net:GND@In1.Cu~5,5"]}))
    box = found["findings"][0]["bbox_nm"]
    assert box == [5 * MM, 5 * MM, 5 * MM, 5 * MM]  # not the 40 mm plane


# measured geometry

def test_measuring_tool_fields_are_checked():
    draws = [{"tool": "clearance", "a": "U3"},
             {"tool": "clearance", "a": "U3", "b": "C12", "required": -1},
             {"tool": "edge_gap", "target": "U3", "min_mm": "0.5"},
             {"tool": "width"},
             {"tool": "routed_length", "net": "", "from": "U3.4", "to": "C12.1"},
             {"tool": "routed_length", "net": 5, "from": "U3.4", "to": "C12.1"},
             {"tool": "bars", "items": []},
             {"tool": "bars", "items": [{"label": "a", "value": 1}] * 13},
             {"tool": "bars", "items": [{"label": "a", "value": "1"}]},
             {"tool": "bars", "items": [{"label": "a", "value": 1, "limit": True}]},
             {"tool": "bars", "items": [{"value": 1}]},
             {"tool": "bars", "items": [{"label": "a", "value": 1}], "unit": "furlong"},
             {"tool": "clearance", "a": "U3", "b": "C12", "required": 0.2},
             {"tool": "routed_length", "net": "net:SIG", "from": "U3.4", "to": "C12.1"}]
    kept, _ = parsed_draws(draws, len(draws) - 2)
    assert [draw.tool for draw in kept] == ["clearance", "routed_length"]
    assert kept[0].fields == {"a": "U3", "b": "C12", "required": 0.2} and kept[1].fields["net"] == "SIG"
    (bars,), problems = parsed_draws([{"tool": "bars", "items": [{"label": "x" * 41, "value": 2, "limit": 1.5}],
                                       "unit": "PS"}], 0)
    assert bars.fields == {"items": (("x" * 39 + "…", 2.0, 1.5),), "unit": "ps", "at": None}
    assert any("cut to 40" in p for p in problems)


def test_clearance_is_measured_like_an_edge_distance(index):
    draw = only_draw(payload(index, finding({"tool": "clearance", "a": f"uuid:{VIA}", "b": f"uuid:{TRACK}",
                                             "required": 5})))
    gap, _, _ = index.closest(index.records(VIA), index.records(TRACK))
    assert draw["ok"] and draw["what"] == "Clearance" and draw["measured_nm"] == 4_600_000
    assert draw["points"] == [[*gap.a, "F.Cu"], [*gap.b, "F.Cu"]] and draw["ids"] == [VIA, TRACK]
    assert (draw["limit_nm"], draw["limit_is"], draw["over"]) == (5 * MM, "min", True)
    assert [t["text"] for t in draw["targets"]] == [f"uuid:{VIA}", f"uuid:{TRACK}"]
    missing = only_draw(payload(index, finding({"tool": "clearance", "a": "U99", "b": "U3"})))
    assert not missing["ok"] and missing["problem"] == "a: U99 not found on board"


def test_edge_gap_measures_to_the_outline(index):
    draw = only_draw(payload(index, finding({"tool": "edge_gap", "target": "net:/~{RST}", "min_mm": 5})))
    assert draw["what"] == "Edge gap" and draw["measured_nm"] == 5 * MM - 100_000 and draw["over"]
    assert draw["ids"] == ["t-weird"] and draw["limit_is"] == "min"
    assert [end[2] for end in draw["points"]] == ["F.Cu", "F.Cu"]
    on_layer = only_draw(payload(index, finding({"tool": "edge_gap", "target": "net:/~{RST}@F.Cu"})))
    assert [end[2] for end in on_layer["points"]] == ["F.Cu", "F.Cu"]  # not Edge.Cuts: no line through the board
    assert on_layer["limit_nm"] is None and not on_layer["over"]


def test_width_lists_segments_and_marks_the_narrow_ones(index):
    draw = only_draw(payload(index, finding({"tool": "width", "target": "net:SIG", "required": 0.15})))
    assert draw["ok"] and draw["what"] == "Width" and draw["problem"] == ""
    assert draw["segments_nm"][0] == [10 * MM, 10 * MM, 20 * MM, 10 * MM, "F.Cu", 200_000]
    arc = draw["segments_nm"][1:]
    assert len(arc) > 10 and all(segment[4:] == ["B.Cu", 100_000] for segment in arc)
    assert draw["narrow"] == list(range(1, len(draw["segments_nm"])))
    assert draw["min_nm"] == draw["measured_nm"] == 100_000 and draw["points"] == [[MM, MM, "B.Cu"]]
    assert (draw["limit_nm"], draw["limit_is"], draw["over"]) == (150_000, "min", True)
    assert draw["ids"] == [TRACK, "arc-1"]  # the tracks measured, not the net's pads and via
    one = only_draw(payload(index, finding({"tool": "width", "target": "net:SIG@F.Cu~15,10.1"})))
    assert one["narrow"] == [] and one["min_nm"] == 200_000 and one["limit_nm"] is None and not one["over"]
    pad = only_draw(payload(index, finding({"tool": "width", "target": "U3.4"})))
    assert not pad["ok"] and pad["problem"] == "target: U3.4 has no tracks or arcs"


def test_width_caps_the_segments(index, monkeypatch):
    monkeypatch.setattr(findings, "MAX_SEGMENTS", 5)
    draw = only_draw(payload(index, finding({"tool": "width", "target": "net:SIG", "required": 0.15})))
    assert draw["ok"] and len(draw["segments_nm"]) == 5 and draw["narrow"] == [1, 2, 3, 4]
    assert draw["problem"].endswith("segments; the first 5 drawn") and draw["min_nm"] == 100_000


def test_routed_length_lights_the_path(index):
    draw = only_draw(payload(index, finding({"tool": "routed_length", "net": "SIG", "from": "U3.4", "to": "C12.1"})))
    assert draw["ok"] and draw["what"] == "Routed length" and draw["measured_nm"] == 10 * MM
    assert draw["path_nm"] == [[10 * MM, 10 * MM, "F.Cu"], [20 * MM, 10 * MM, "F.Cu"]]
    assert draw["points"] == draw["path_nm"] and draw["ids"] == ["p-u3-4", TRACK, "p-c12-1"]
    assert draw["limit_nm"] is None


@pytest.mark.parametrize("fields, problem", [
    ({"net": "NOPE", "from": "U3.4", "to": "C12.1"}, "net NOPE not found on board"),
    ({"net": "SIG", "from": "U3", "to": "C12.1"}, "from: U3 is not a pad (give one, e.g. U3.4)"),
    ({"net": "GND", "from": "U3.EP", "to": "C12.1"}, "to: C12.1 is not on net GND"),
    ({"net": "SIG", "from": "U3.4", "to": "C12.9"}, "to: C12 has no pad 9"),
])
def test_routed_length_problems(index, fields, problem):
    draw = only_draw(payload(index, finding({"tool": "routed_length", **fields})))
    assert not draw["ok"] and draw["problem"] == problem


def test_routed_length_not_connected_says_why():
    snapshot = board()
    snapshot = model.BoardSnapshot(**{**snapshot.__dict__, "tracks": snapshot.tracks[1:]})
    draw = only_draw(payload(BoardIndex(snapshot), finding({"tool": "routed_length", "net": "net:SIG",
                                                            "from": "U3.4", "to": "C12.1"})))
    assert not draw["ok"] and draw["problem"] == (
        "no copper path between U3.4 and C12.1 on net SIG (tracks, arcs and vias are followed, zones are not)")
    assert draw["ids"] == ["p-u3-4", "p-c12-1"]  # the two pads still light up


def test_bars_stand_at_their_target_or_the_findings_first(index):
    items = [{"label": "D0", "value": 41.2}, {"label": "D1", "value": 43.9, "limit": 42}]
    result = payload(index,
                     finding({"tool": "bars", "items": items, "unit": "mm", "at": "U3"}),
                     finding({"tool": "highlight", "targets": ["U99", "C12.1"]}, {"tool": "bars", "items": items}),
                     finding({"tool": "bars", "items": items}))
    at, after, alone = (item["draws"][-1] for item in result["findings"])
    assert at["ok"] and at["what"] == "" and at["unit"] == "mm" and at["points"] == [[10 * MM, 10 * MM, "F.Cu"]]
    assert at["bars"] == [{"label": "D0", "value": 41.2, "limit": None, "over": False},
                          {"label": "D1", "value": 43.9, "limit": 42.0, "over": True}]
    assert after["ok"] and after["points"] == [[20 * MM, 10 * MM, "F.Cu"]] and after["unit"] == ""
    assert not alone["ok"] and "give at" in alone["problem"]
    assert result["findings"][1]["bbox_nm"] is not None
    offline = payload(None, finding({"tool": "bars", "items": items}))
    assert only_draw(offline)["problem"] == "KiCad not connected"


def test_every_measured_draw_names_what_it_measured(index):
    result = payload(index, finding({"tool": "distance", "from": "U3", "to": "C12", "label": "Cap reach"},
                                    {"tool": "distance", "from": "U3", "to": "C12"},
                                    {"tool": "highlight", "targets": ["U3"]}))
    assert [draw["what"] for draw in result["findings"][0]["draws"]] == ["Cap reach", "", ""]


@pytest.mark.parametrize("values, keys", [({"width_mm": 0.2, "length_mm": 10.0}, []),
                                          ({"width_mm": 0.15, "length_mm": 12.0}, ["width_mm", "length_mm"]),
                                          ({"distance_mm": 3.0}, ["distance_mm"])])
def test_claimed_width_and_length_against_measured(index, values, keys):
    result = payload(index, finding({"tool": "width", "target": "net:SIG@F.Cu"},
                                    {"tool": "routed_length", "net": "SIG", "from": "U3.4", "to": "C12.1"},
                                    {"tool": "clearance", "a": f"uuid:{VIA}", "b": f"uuid:{TRACK}"},
                                    values=values))
    assert [entry["key"] for entry in result["findings"][0]["disagree"]] == keys


# planes and return paths (on the split-plane board, tests/split_board.py)

@pytest.fixture
def split():
    return BoardIndex(split_board.board())


def test_plane_tool_fields_are_checked():
    draws = [{"tool": "layer_change"},
             {"tool": "layer_change", "via": "uuid:v", "planes": "GND@In1.Cu"},
             {"tool": "layer_change", "via": "uuid:v", "planes": ["GND@In1.Cu", "GND@In2.Cu", "GND@B.Cu"]},
             {"tool": "layer_change", "via": "uuid:v", "planes": ["GND"]},
             {"tool": "layer_change", "via": "uuid:v", "planes": ["@In1.Cu"]},
             {"tool": "layer_change", "via": "uuid:v", "planes": ["GND@F.SilkS"]},
             {"tool": "layer_change", "via": "uuid:v", "limit": 0},
             {"tool": "plane_gap", "plane": "GND@In1.Cu"},
             {"tool": "plane_gap", "plane": 5, "crossing": "net:A"},
             {"tool": "return_path", "signal": "net:A"},
             {"tool": "plane_islands"},
             {"tool": "plane_islands", "net": "GND", "layer": "Edge.Cuts"},
             {"tool": "layer_change", "via": "uuid:v"},
             {"tool": "layer_change", "via": "uuid:v", "planes": ["net:GND@in1.cu", "A@B@In2.Cu"], "limit": 2},
             {"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:A"},
             {"tool": "return_path", "signal": "net:A", "plane": "net: GND @ B.Cu"},
             {"tool": "plane_islands", "net": "net:GND"},
             {"tool": "plane_islands", "net": "GND", "layer": "in2.cu"}]
    kept, problems = parsed_draws(draws, 12)
    default, named_planes, gap, path, islands, layer = (draw.fields for draw in kept)
    assert default == {"via": "uuid:v", "limit": 1.0, "planes": ()}
    assert named_planes["planes"] == (("GND", "In1.Cu"), ("A@B", "In2.Cu")) and named_planes["limit"] == 2
    assert gap == {"plane": ("GND", "In1.Cu"), "crossing": "net:A"}
    assert path == {"signal": "net:A", "plane": ("GND", "B.Cu")}
    assert islands == {"net": "GND", "layer": None} and layer["layer"] == "In2.Cu"
    assert any("planes[0] is not net@layer (e.g. GND@In1.Cu)" in p for p in problems)


def test_layer_change_without_a_return_via_is_over_and_suggests_nothing(split):
    draw = only_draw(payload(split, finding({"tool": "layer_change", "via": "uuid:via-lvds-0"})))
    assert draw["ok"] and draw["problem"] == "" and draw["what"] == "Return via"
    assert draw["planes"] == ["GND@In1.Cu", "GND@In2.Cu"]  # the fill next to F.Cu and B.Cu at the via
    assert draw["ids"] == ["via-lvds-0"] and draw["returns"] == []
    assert draw["points"] == [[20 * MM, 20 * MM, "F.Cu"]]
    assert (draw["measured_nm"], draw["limit_nm"], draw["limit_is"], draw["over"]) == (None, MM, "max", True)
    assert "ghosts_nm" not in draw  # where one should go is the LLM's to suggest (a ghost)


@pytest.mark.parametrize("at, size, rotation, problem", [
    ("pt:10,10@F.Cu", None, None, "lands on U3.4 (SIG)"),  # on a pad
    ("pt:10,9.45@F.Cu", 0.4, None, "lands on U3"),  # between its pads: on its body (the box of its pads)
    ("pt:10.45,9.45@F.Cu", 0.6, 0, "lands on U3"),  # along x, it reaches over the body's edge
    ("pt:10.45,9.45@F.Cu", 0.6, 90, ""),  # turned, it fits beside it
    ("pt:10,9.45@B.Cu", 0.4, None, ""),  # U3 is on the top
])
def test_a_suggested_cap_lands_clear_of_parts(index, at, size, rotation, problem):
    ghost = {"tool": "ghost", "kind": "cap", "at": at, **({"size": size} if size else {}),
             **({"rotation": rotation} if rotation is not None else {})}
    draw = only_draw(payload(index, finding(ghost)))
    assert (draw["fits"], draw["problem"], draw["rotation_deg"]) == (not problem, problem, rotation or 0)


def test_a_suggested_via_is_checked_to_fit(split):
    def ghost(at, **fields):
        result = payload(split, finding({"tool": "layer_change", "via": "uuid:via-lvds-0"},
                                        {"tool": "ghost", "kind": "via", "at": at, **fields}))
        return result["findings"][0]["draws"][1]
    fits = ghost("pt:20,19.2@F.Cu", net="GND")  # 0.8 mm from the LVDS via: inside 1 mm, 0.2 mm clear of it
    assert fits["ok"] and fits["fits"] is True and fits["problem"] == ""
    on_track = ghost("pt:15,20.1@F.Cu", net="GND")
    assert on_track["ok"] and on_track["fits"] is False
    assert on_track["problem"] == ("clashes with LVDS_P track (0.00 mm from its copper, 0.10 mm needed); "
                                   "5.00 mm from the via, outside its 1.00 mm limit")
    too_far = ghost("pt:20,18.5@F.Cu", net="GND")
    assert too_far["fits"] is False and too_far["problem"] == "1.50 mm from the via, outside its 1.00 mm limit"
    alone = only_draw(payload(split, finding({"tool": "ghost", "kind": "cap", "at": "pt:15,20.1@F.Cu"})))
    assert alone["fits"] is True  # a capacitor: clear of every part (it may sit on a track)
    assert payload(split, finding({"tool": "ghost", "kind": "via", "at": "pt:1,1@F.Cu", "net": 5}))[
        "findings"][0]["problems"]


@pytest.mark.parametrize("distance, over", [(MM, False), (1_500_000, True)])
def test_layer_change_measures_the_nearest_return_via(distance, over):
    index = BoardIndex(split_board.with_return_via(split_board.board(), distance))
    draw = only_draw(payload(index, finding({"tool": "layer_change", "via": "net:LVDS_P@F.Cu~20,20"})))
    assert draw["ok"] and draw["measured_nm"] == distance and draw["over"] is over
    assert draw["points"] == [[20 * MM, 20 * MM, "F.Cu"], [20 * MM, 20 * MM - distance, "F.Cu"]]
    assert draw["returns"] == [[20 * MM, 20 * MM - distance, 600_000, "via-gnd"]]
    assert draw["ids"] == ["via-lvds-0", "via-gnd"]
    looser = only_draw(payload(index, finding({"tool": "layer_change", "via": "uuid:via-lvds-0", "limit": 2})))
    assert looser["limit_nm"] == 2 * MM and not looser["over"]


def test_layer_change_problems(split):
    def draw(**fields):
        return only_draw(payload(split, finding({"tool": "layer_change", **fields})))
    assert draw(via="uuid:usb-0")["problem"] == "via: uuid:usb-0 is not a via"
    assert draw(via="uuid:nope0000")["problem"] == "via: no item with uuid nope0000"
    wrong = draw(via="uuid:via-lvds-0", planes=["GND@In1.Cu", "+3V3@In2.Cu"])
    assert not wrong["ok"] and wrong["problem"] == "plane +3V3@In2.Cu: +3V3 has no zone fill on In2.Cu"
    unknown = draw(via="uuid:via-lvds-0", planes=["AGND@In1.Cu"])
    assert unknown["problem"] == "plane AGND@In1.Cu: net AGND not found on board"
    given = draw(via="uuid:via-lvds-0", planes=["GND@In2.Cu"])  # the LLM's planes are taken as given
    assert given["ok"] and given["planes"] == ["GND@In2.Cu"] and given["over"]


def test_layer_change_with_one_plane_for_both_layers_needs_no_return_via():
    layer = model.StackupLayer
    stack = model.Stackup((layer("F.Cu", "copper", 35_000, None, None, None),
                           layer("d", "dielectric", 200_000, None, None, None),
                           layer("In1.Cu", "copper", 35_000, None, None, None),
                           layer("e", "dielectric", 200_000, None, None, None),
                           layer("B.Cu", "copper", 35_000, None, None, None)))
    via = model.Via("v", "SIG", (5 * MM, 5 * MM), 600_000, 300_000, "F.Cu", "B.Cu")
    tracks = (model.Track("a", "F.Cu", "SIG", (0, 5 * MM), (5 * MM, 5 * MM), 150_000),
              model.Track("b", "B.Cu", "SIG", (5 * MM, 5 * MM), (9 * MM, 5 * MM), 150_000))
    outline = ((0, 0), (10 * MM, 0), (10 * MM, 10 * MM), (0, 10 * MM))
    antipad = ((4_500_000, 4_500_000), (5_500_000, 4_500_000), (5_500_000, 5_500_000), (4_500_000, 5_500_000))
    plane = model.ZoneFill("z", "GND", "In1.Cu", ((outline, antipad),))

    def board_with(tracks):
        return BoardIndex(model.BoardSnapshot("t", {}, tracks, (), (via,), (), (), (plane,), model.Outline(()),
                                              stack, (), {}))
    draw = only_draw(payload(board_with(tracks), finding({"tool": "layer_change", "via": "uuid:v"})))
    assert draw["ok"] and draw["planes"] == ["GND@In1.Cu"] and draw["ids"] == ["v"]
    assert draw["problem"] == "both layers return on GND@In1.Cu; no return via needed"
    assert draw["measured_nm"] is None and not draw["over"]
    draw = only_draw(payload(board_with(tracks[:1]), finding({"tool": "layer_change", "via": "uuid:v"})))
    assert draw["problem"].startswith("the via meets its net on fewer than two layers")


def test_plane_gap_marks_the_void_and_its_edges(split):
    draw = only_draw(payload(split, finding({"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:SATA_P"})))
    assert draw["ok"] and draw["what"] == "Gap" and draw["layer"] == "In1.Cu" and draw["planes"] == ["GND@In1.Cu"]
    assert draw["ids"] == ["sata-0"] and draw["measured_nm"] == MM
    assert draw["spans_nm"] == [[25 * MM, 35 * MM, 26 * MM, 35 * MM, "F.Cu", split_board.WIDTH]]
    assert draw["points"] == [[25_500_000, 35 * MM, "F.Cu"]]
    assert draw["edges_nm"] == [[[25 * MM, 34 * MM], [26 * MM, 34 * MM], [26 * MM, 36 * MM], [25 * MM, 36 * MM],
                                 [25 * MM, 34 * MM]]]
    assert draw["covered_nm"] == [] and draw["limit_nm"] is None
    crossing = only_draw(payload(split, finding({"tool": "plane_gap", "plane": "GND@In1.Cu",
                                                 "crossing": "net:USB_D+@F.Cu~50,10"})))
    assert crossing["measured_nm"] == 40_250_000  # no GND under it from the split to its end
    assert crossing["edges_nm"] == [[[49_750_000, 9 * MM], [49_750_000, 11 * MM]]]
    clear = only_draw(payload(split, finding({"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:ETH_P"})))
    assert clear["ok"] and clear["measured_nm"] == 0 and clear["spans_nm"] == [] and clear["edges_nm"] == []
    assert clear["points"] == [[25 * MM, 30 * MM, "F.Cu"]]  # the track's middle, for the label


def test_plane_gap_fills_its_own_antipads(split):
    draw = only_draw(payload(split, finding({"tool": "plane_gap", "plane": "GND@In2.Cu", "crossing": "net:LVDS_P"})))
    assert draw["ok"] and draw["ids"] == ["lvds-b-0"] and draw["measured_nm"] == 0  # B.Cu is next to In2.Cu


@pytest.mark.parametrize("fields, problem", [
    ({"plane": "GND@In2.Cu", "crossing": "net:USB_D+"},
     "crossing: net:USB_D+ has no tracks on In1.Cu or B.Cu next to In2.Cu"),
    ({"plane": "GND@In3.Cu", "crossing": "net:USB_D+"}, "plane GND@In3.Cu: GND has no zone fill on In3.Cu"),
    ({"plane": "GND@In1.Cu", "crossing": "uuid:via-lvds-0"}, "crossing: uuid:via-lvds-0 has no tracks or arcs"),
    ({"plane": "GND@In1.Cu", "crossing": "net:NOPE"}, "crossing: net NOPE not found on board"),
])
def test_plane_gap_problems(split, fields, problem):
    draw = only_draw(payload(split, finding({"tool": "plane_gap", **fields})))
    assert not draw["ok"] and draw["problem"] == problem and draw["ids"] == []


def test_return_path_splits_the_track_into_covered_and_not(split):
    draw = only_draw(payload(split, finding({"tool": "return_path", "signal": "net:USB_D+", "plane": "GND@In1.Cu"})))
    assert draw["ok"] and draw["what"] == "Without plane" and draw["layer"] == "In1.Cu"
    assert draw["covered_nm"] == [[10 * MM, 10 * MM, 49_750_000, 10 * MM, "F.Cu", split_board.WIDTH]]
    assert draw["spans_nm"] == [[49_750_000, 10 * MM, 90 * MM, 10 * MM, "F.Cu", split_board.WIDTH]]
    assert draw["measured_nm"] == 40_250_000 and draw["points"] == [[69_875_000, 10 * MM, "F.Cu"]]
    assert draw["ids"] == ["usb-0"] and draw["edges_nm"] == [] and draw["planes"] == ["GND@In1.Cu"]
    other = only_draw(payload(split, finding({"tool": "return_path", "signal": "net:USB_D+",
                                              "plane": "+3V3@In1.Cu"})))
    assert other["measured_nm"] == 40_250_000  # the other side of the split
    assert other["covered_nm"][0][:4] == [50_250_000, 10 * MM, 90 * MM, 10 * MM]
    assert draw["whole_nm"] == 80 * MM


@pytest.mark.parametrize("values, keys", [
    ({"length_mm": 40.25}, []),  # alone: the length without plane
    ({"length_mm": 80.0}, ["length_mm"]),
    ({"coverage_pct": 49.7, "length_mm": 80.0}, []),  # beside coverage_pct: the whole track
    ({"coverage_pct": 49.7, "length_mm": 40.25}, ["length_mm"]),
    ({"coverage_pct": 54.0}, []),  # 4.3 points off
    ({"coverage_pct": 56.0}, ["coverage_pct"]),
])
def test_claimed_coverage_against_return_path(split, values, keys):
    result = payload(split, finding({"tool": "return_path", "signal": "net:USB_D+", "plane": "GND@In1.Cu"},
                                    values=values))
    assert [entry["key"] for entry in result["findings"][0]["disagree"]] == keys


def test_stretches_are_capped(split, monkeypatch):
    monkeypatch.setattr(findings, "MAX_SEGMENTS", 1)
    draw = only_draw(payload(split, finding({"tool": "return_path", "signal": "net:SATA_P", "plane": "GND@In1.Cu"})))
    assert draw["ok"] and len(draw["covered_nm"]) == 1 and draw["problem"] == "2 stretches; the first 1 drawn"


def test_plane_islands_count_the_pieces_and_the_loose_ones(split):
    draw = only_draw(payload(split, finding({"tool": "plane_islands", "net": "GND"})))
    assert draw["ok"] and draw["what"] == "Islands" and draw["label"] == "2 pieces, 1 not tied"
    assert draw["ids"] == ["zone-gnd-in1", "zone-gnd-in2"] and draw["measured_nm"] is None and draw["layer"] == ""
    first, second = draw["islands"]
    assert (first["index"], first["layer"], first["main"], first["tied"], first["area_mm2"]) == \
        (1, "In1.Cu", False, False, 1848.08)
    assert (second["index"], second["layer"], second["main"], second["tied"]) == (2, "In2.Cu", True, True)
    assert first["polygon_nm"] == [[MM, MM], [49_750_000, MM], [49_750_000, 39 * MM], [MM, 39 * MM]]
    one = only_draw(payload(split, finding({"tool": "plane_islands", "net": "GND", "layer": "In1.Cu",
                                            "label": "GND split"})))
    assert one["label"] == "GND split (1 piece, 1 not tied)" and one["layer"] == "In1.Cu"
    assert [island["layer"] for island in one["islands"]] == ["In1.Cu"] and one["ids"] == ["zone-gnd-in1"]
    tied = only_draw(payload(BoardIndex(split_board.with_return_via(split_board.board())),
                             finding({"tool": "plane_islands", "net": "GND"})))
    assert tied["label"] == "2 pieces, 0 not tied" and all(i["main"] and i["tied"] for i in tied["islands"])


def test_plane_islands_problems_and_caps(split, monkeypatch):
    def draw(**fields):
        return only_draw(payload(split, finding({"tool": "plane_islands", **fields})))
    assert draw(net="NOPE")["problem"] == "net NOPE not found on board"
    assert draw(net="GND", layer="F.Cu")["problem"] == "GND has no zone fill on F.Cu"
    assert draw(net="USB_D+")["problem"] == "USB_D+ has no zone fill"
    offline = only_draw(payload(None, finding({"tool": "plane_islands", "net": "GND"})))
    assert not offline["ok"] and offline["problem"] == "KiCad not connected"
    monkeypatch.setattr(findings, "MAX_ISLANDS", 1)
    monkeypatch.setattr(findings, "MAX_ISLAND_POINTS", 2)
    capped = draw(net="GND")
    assert len(capped["islands"]) == 1 and capped["islands"][0]["main"]  # the main one first
    assert capped["islands"][0]["polygon_nm"] == [[MM, MM], [99 * MM, 39 * MM]]  # every 2nd point
    assert capped["problem"] == "2 pieces; 1 drawn, the main ones and the loose ones first"
    assert capped["label"] == "2 pieces, 1 not tied"  # the count is of all of them


@pytest.mark.parametrize("values, keys", [({"gap_mm": 1.0, "length_mm": 1.0, "distance_mm": 1.0}, []),
                                          ({"gap_mm": 2.0, "length_mm": 0.5, "distance_mm": 3.0},
                                           ["distance_mm", "length_mm", "gap_mm"])])
def test_claimed_plane_values_against_measured(values, keys):
    index = BoardIndex(split_board.with_return_via(split_board.board()))
    result = payload(index, finding({"tool": "layer_change", "via": "uuid:via-lvds-0"},
                                    {"tool": "return_path", "signal": "net:SATA_P", "plane": "GND@In1.Cu"},
                                    {"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:SATA_P"},
                                    values=values))
    assert [entry["key"] for entry in result["findings"][0]["disagree"]] == keys


def test_plane_islands_are_framed_on_the_loose_piece(split):
    alone = payload(split, finding({"tool": "plane_islands", "net": "GND"}))
    assert alone["findings"][0]["bbox_nm"] == [MM, MM, 49_750_000, 39 * MM]  # the loose piece
    assert alone["findings"][0]["key"] == finding_key("test.check", ["title:T"])


# stitching, stub, discontinuity

STITCHES = [(5 * MM, 2 * MM), (30 * MM, 2 * MM), (10 * MM, 2 * MM), (20 * MM, 10 * MM), (40 * MM, 500_000)]


@pytest.fixture
def stitched():
    """The split board with GND vias near its top edge (one far from it, one outside the pour)."""
    return BoardIndex(split_board.with_vias(split_board.board(), "GND", STITCHES, "st"))


def necked():
    """The split board with a net NECK on F.Cu over +3V3: 0.20 mm to x = 70 mm, then 0.15 mm."""
    return BoardIndex(split_board.with_tracks(
        split_board.board(), model.Track("neck-0", "F.Cu", "NECK", (60 * MM, 30 * MM), (70 * MM, 30 * MM), 200_000),
        model.Track("neck-1", "F.Cu", "NECK", (70 * MM, 30 * MM), (80 * MM, 30 * MM), 150_000)))


def test_stitching_stub_and_discontinuity_fields_are_checked():
    draws = [{"tool": "stitching", "along": "edge", "spacing_mm": 5},
             {"tool": "stitching", "along": "GND@In1.Cu"},
             {"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 0},
             {"tool": "stitching", "along": "GND@In1.Cu", "net": "VCC", "spacing_mm": 5},
             {"tool": "stitching", "along": "GND", "spacing_mm": 5},
             {"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 5, "max_mm": -1},
             {"tool": "stub"},
             {"tool": "stub", "via": "uuid:a", "pad": "J1.1"},
             {"tool": "discontinuity", "net": "A", "at": []},
             {"tool": "discontinuity", "net": "A", "at": "pt:1,2"},
             {"tool": "discontinuity", "net": "A", "at": [[1]]},
             {"tool": "discontinuity", "net": "A", "at": ["pt:1,2"] * 21},
             {"tool": "discontinuity", "at": ["pt:1,2"]},
             {"tool": "stitching", "along": "EDGE", "net": "net:GND", "spacing_mm": 5},
             {"tool": "stitching", "along": "net:GND@in1.cu", "net": "GND", "spacing_mm": 5, "max_mm": 8},
             {"tool": "stub", "pad": "J1.1"},
             {"tool": "discontinuity", "net": "net:A", "at": [[1.5, -2], "pt:3,4@F.Cu", [0.0000004, 2]]}]
    kept, problems = parsed_draws(draws, 13)
    edge, pour, pad, spots = (draw.fields for draw in kept)
    assert edge == {"along": "edge", "net": "GND", "spacing": 5.0, "max": None}
    assert pour == {"along": ("GND", "In1.Cu"), "net": "GND", "spacing": 5.0, "max": 8.0}
    assert pad == {"via": None, "pad": "J1.1"}
    assert spots == {"net": "A", "at": ("pt:1.5,-2", "pt:3,4@F.Cu", "pt:0,2")}
    for text in ("along edge needs net: the stitching net (e.g. GND)", "net VCC is not the pour's net GND",
                 "spacing_mm is not a positive number in mm", "give via or pad (one of them)",
                 "at is not a list of 1 to 20 points or targets", "at[0] is not [x, y]"):
        assert any(text in p for p in problems), text


def test_stitching_along_a_pour(stitched):
    draw = only_draw(payload(stitched, finding({"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 5,
                                                "max_mm": 8})))
    assert draw["ok"] and draw["problem"] == "" and draw["what"] == "Stitching gap"
    assert draw["layer"] == "In1.Cu" and draw["planes"] == ["GND@In1.Cu"]
    assert draw["returns"] == [[5 * MM, 2 * MM, 600_000, "st-0"], [10 * MM, 2 * MM, 600_000, "st-2"],
                               [30 * MM, 2 * MM, 600_000, "st-1"]]  # in order along the outline
    assert draw["ids"] == ["st-0", "st-2", "st-1"]
    # the widest gap: from (30, 2) round the pour back to (5, 2), measured along its outline
    assert (draw["measured_nm"], draw["limit_nm"], draw["limit_is"], draw["over"]) == (148_500_000, 8 * MM, "max", True)
    assert draw["points"] == [[30 * MM, 2 * MM, "In1.Cu"], [5 * MM, 2 * MM, "In1.Cu"]]
    assert draw["edges_nm"] == [[[30 * MM, MM], [49_750_000, MM], [49_750_000, 39 * MM], [MM, 39 * MM], [MM, MM],
                                 [5 * MM, MM]],
                                [[10 * MM, MM], [30 * MM, MM]]]  # the widest, then the 20 mm one, also over
    assert draw["path_nm"] == [[MM, MM, "In1.Cu"], [49_750_000, MM, "In1.Cu"], [49_750_000, 39 * MM, "In1.Cu"],
                               [MM, 39 * MM, "In1.Cu"], [MM, MM, "In1.Cu"]]
    assert "ghosts_nm" not in draw  # where vias should go is the LLM's to suggest
    looser = only_draw(payload(stitched, finding({"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 30,
                                                  "max_mm": 150})))
    assert not looser["over"] and len(looser["edges_nm"]) == 1  # the widest, not over


def test_stitching_along_the_edge(stitched):
    draw = only_draw(payload(stitched, finding({"tool": "stitching", "along": "edge", "net": "GND",
                                                "spacing_mm": 5})))
    assert draw["ok"] and draw["layer"] == "F.Cu" and draw["planes"] == []
    assert draw["ids"] == ["st-0", "st-2", "st-1", "st-4"]  # (40, 0.5) is outside the pour, inside the board
    assert (draw["measured_nm"], draw["limit_nm"], draw["over"]) == (245 * MM, 5 * MM, True)  # no max: spacing
    assert draw["points"] == [[40 * MM, 500_000, "F.Cu"], [5 * MM, 2 * MM, "F.Cu"]]
    assert len(draw["edges_nm"]) == 3  # the widest, the 20 mm and the 10 mm gaps; the 5 mm one is not over


def test_stitching_with_too_few_vias_goes_round_the_line(stitched):
    none = only_draw(payload(stitched, finding({"tool": "stitching", "along": "+3V3@In1.Cu", "spacing_mm": 5})))
    assert none["ok"] and none["problem"] == "no +3V3 via within 2 mm of the pour; the spacing is the way round it"
    assert none["measured_nm"] == 173_500_000 and none["points"] == [[50_250_000, MM, "In1.Cu"]]
    assert none["returns"] == [] and none["ids"] == [] and len(none["edges_nm"]) == 1
    one = BoardIndex(split_board.with_vias(split_board.board(), "GND", [(5 * MM, 2 * MM)]))
    draw = only_draw(payload(one, finding({"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 50})))
    assert draw["problem"].startswith("one GND via within 2 mm") and draw["points"] == [[5 * MM, 2 * MM, "In1.Cu"]]
    assert draw["measured_nm"] == 173_500_000


def test_stitching_problems_and_caps(stitched, monkeypatch):
    def draw(**fields):
        return only_draw(payload(stitched, finding({"tool": "stitching", "spacing_mm": 5, **fields})))
    assert draw(along="GND@In3.Cu")["problem"] == "along GND@In3.Cu: GND has no zone fill on In3.Cu"
    assert draw(along="edge", net="NOPE")["problem"] == "net NOPE not found on board"
    offline = only_draw(payload(None, finding({"tool": "stitching", "along": "edge", "net": "GND", "spacing_mm": 5})))
    assert not offline["ok"] and offline["problem"] == "KiCad not connected"
    monkeypatch.setattr(findings, "MAX_STITCHES", 1)
    monkeypatch.setattr(findings, "MAX_GAPS", 1)
    capped = draw(along="GND@In1.Cu", max_mm=8)
    assert capped["ok"] and len(capped["returns"]) == 1 and capped["ids"] == ["st-0"] and len(capped["edges_nm"]) == 1
    assert capped["problem"] == "3 vias; the first 1 drawn; 2 stretches over the limit; the first 1 drawn"


def test_stub_of_a_through_via_used_end_to_end_is_none(split):
    draw = only_draw(payload(split, finding({"tool": "stub", "via": "uuid:via-lvds-0"})))
    assert draw["ok"] and draw["problem"] == "" and draw["what"] == "Stub" and draw["ids"] == ["via-lvds-0"]
    assert draw["barrel"] == {"x": 20 * MM, "y": 20 * MM, "size_nm": 300_000, "used": ["F.Cu", "B.Cu"], "stub": []}
    assert draw["measured_nm"] == 0 and draw["points"] == [[20 * MM, 20 * MM, "F.Cu"]]
    picked = only_draw(payload(split, finding({"tool": "stub", "via": "net:LVDS_P@B.Cu~20,20"})))
    assert picked["ids"] == ["via-lvds-0"]  # the via of the net at the point


def test_stub_lengths_from_the_stackup(stitched):
    index = BoardIndex(split_board.with_vias(stitched.snapshot, "CLK", [(60 * MM, 5 * MM)], "clk"))
    ground = only_draw(payload(index, finding({"tool": "stub", "via": "uuid:st-0"})))
    assert ground["barrel"]["used"] == ["In1.Cu", "In2.Cu"] and ground["barrel"]["stub"] == ["In2.Cu", "B.Cu"]
    assert ground["measured_nm"] == 235_000 and ground["points"] == [[5 * MM, 2 * MM, "In2.Cu"]]
    assert ground["problem"] == "also a 0.235 mm stub In1.Cu to F.Cu"
    clock = only_draw(payload(index, finding({"tool": "stub", "via": "uuid:clk-0"})))
    assert clock["barrel"]["used"] == ["F.Cu", "F.Cu"] and clock["barrel"]["stub"] == ["F.Cu", "B.Cu"]
    assert clock["measured_nm"] == 1_505_000 and clock["problem"] == "the via meets its net on F.Cu only"
    bare = BoardIndex(replace(index.snapshot, stackup=model.Stackup(())))
    unknown = only_draw(payload(bare, finding({"tool": "stub", "via": "uuid:clk-0"})))
    assert not unknown["ok"] and unknown["problem"].startswith("the stackup has no layer thicknesses")


def test_stub_of_a_plated_pad():
    land = {layer: ((split_board.square(70 * MM, 25 * MM, MM),),) for layer in ("F.Cu", "B.Cu")}
    hole = model.Pad("pad-hole", "", "1", "SIG", (70 * MM, 25 * MM), (800_000, 800_000), land)
    smd = model.Pad("pad-smd", "", "2", "SIG", (75 * MM, 25 * MM), None,
                    {"F.Cu": ((split_board.square(75 * MM, 25 * MM, MM),),)})
    board = replace(split_board.with_tracks(
        split_board.board(), model.Track("sig", "F.Cu", "SIG", (70 * MM, 25 * MM), (75 * MM, 25 * MM), 150_000)),
        pads=(hole, smd))
    draw = only_draw(payload(BoardIndex(board), finding({"tool": "stub", "pad": "uuid:pad-hole"})))
    assert draw["ok"] and draw["barrel"] == {"x": 70 * MM, "y": 25 * MM, "size_nm": 800_000, "used": ["F.Cu", "F.Cu"],
                                             "stub": ["F.Cu", "B.Cu"]}
    assert draw["measured_nm"] == 1_505_000 and draw["problem"] == "the hole meets its net on F.Cu only"
    smd_draw = only_draw(payload(BoardIndex(board), finding({"tool": "stub", "pad": "uuid:pad-smd"})))
    assert smd_draw["problem"] == "pad: uuid:pad-smd is not a plated hole"
    track = only_draw(payload(BoardIndex(board), finding({"tool": "stub", "via": "uuid:sig"})))
    assert track["problem"] == "via: uuid:sig is not a via"


@pytest.mark.parametrize("claimed, keys", [(1.5, []), (1.0, ["stub_mm"])])
def test_claimed_stub_against_measured(claimed, keys):
    index = BoardIndex(split_board.with_vias(split_board.board(), "CLK", [(60 * MM, 5 * MM)], "clk"))
    result = payload(index, finding({"tool": "stub", "via": "uuid:clk-0"}, values={"stub_mm": claimed}))
    assert [entry["key"] for entry in result["findings"][0]["disagree"]] == keys


def test_discontinuity_marks_a_reference_change_across_a_split(split):
    draw = only_draw(payload(split, finding({"tool": "discontinuity", "net": "USB_D+", "at": [[50, 10]]})))
    assert draw["ok"] and draw["problem"] == "" and draw["what"] == "Discontinuity" and draw["measured_nm"] is None
    assert draw["marks"] == [{"x": 49_750_000, "y": 10 * MM, "layer": "F.Cu", "kind": "reference",
                              "text": "GND@In1.Cu -> +3V3@In1.Cu"}]  # on the split's edge
    assert draw["points"] == [[49_750_000, 10 * MM, "F.Cu"]] and draw["ids"] == ["usb-0"]
    void = only_draw(payload(split, finding({"tool": "discontinuity", "net": "SATA_P", "at": ["pt:24.5,35@F.Cu"]})))
    assert [mark["text"] for mark in void["marks"]] == ["GND@In1.Cu -> no plane"]


def test_discontinuity_marks_a_layer_change_at_a_via(split):
    draw = only_draw(payload(split, finding({"tool": "discontinuity", "net": "LVDS_P", "at": ["uuid:via-lvds-0"]})))
    assert draw["marks"] == [
        {"x": 20 * MM, "y": 20 * MM, "layer": "F.Cu", "kind": "layer", "text": "F.Cu -> B.Cu"},
        {"x": 20 * MM, "y": 20 * MM, "layer": "F.Cu", "kind": "reference", "text": "GND@In1.Cu -> GND@In2.Cu"}]
    assert draw["ids"] == ["via-lvds-0"] and len(draw["points"]) == 2


def test_discontinuity_marks_a_width_step_where_the_tracks_meet():
    index = necked()
    draw = only_draw(payload(index, finding({"tool": "discontinuity", "net": "NECK", "at": [[70.3, 30], [75, 30]]})))
    assert draw["ok"] and draw["marks"] == [{"x": 70 * MM, "y": 30 * MM, "layer": "F.Cu", "kind": "width",
                                             "text": "0.20 -> 0.15 mm"}]
    assert draw["ids"] == ["neck-0", "neck-1"] and draw["points"] == [[70 * MM, 30 * MM, "F.Cu"]]
    assert draw["problem"] == "no change found at 75.00,30.00"
    plain = only_draw(payload(index, finding({"tool": "discontinuity", "net": "NECK", "at": [[75, 30.1]]})))
    assert plain["ok"] and plain["marks"] == [] and plain["points"] == [[75 * MM, 30 * MM, "F.Cu"]]


def test_discontinuity_problems(split):
    def draw(net, *spots):
        return only_draw(payload(necked(), finding({"tool": "discontinuity", "net": net, "at": list(spots)})))
    assert draw("NOPE", [1, 1])["problem"] == "net NOPE not found on board"
    far = draw("NECK", [5, 5])
    assert not far["ok"] and far["problem"] == "5.00,5.00: NECK has no copper within 1.00 mm"
    other = draw("NECK", "pt:70.3,30@B.Cu")  # nothing of NECK on B.Cu
    assert not other["ok"] and other["problem"] == "70.30,30.00: NECK has no copper within 1.00 mm"
    mixed = draw("NECK", "uuid:nope0000", [70.3, 30])
    assert mixed["ok"] and len(mixed["marks"]) == 1 and mixed["problem"] == "at[0]: no item with uuid nope0000"


def test_a_stub_is_framed_on_its_via_and_each_discontinuity_point_is_keyed(stitched):
    alone = payload(stitched, finding({"tool": "stub", "via": "uuid:st-0"}))
    x0, y0, x1, y1 = alone["findings"][0]["bbox_nm"]
    assert x0 <= 5 * MM <= x1 and y0 <= 2 * MM <= y1 and x1 - x0 < MM  # the via
    spots = payload(stitched, finding({"tool": "discontinuity", "net": "USB_D+", "at": [[50, 10], "uuid:usb-0"]}))
    assert spots["findings"][0]["key"] == finding_key("test.check", ["net:USB_D+@F.Cu~50,10"])  # each point keyed


# ghosts: a part moved, a route, a row of vias (on the board of test_board_index)

EMPTY = {"planes": [], "spans_nm": [], "covered_nm": [], "edges_nm": [], "returns": [], "fits": None,  # planes
         "islands": [], "barrel": {}, "marks": [],
         "part": "", "from": [], "moved_nm": None, "pads_nm": [], "width_nm": 0, "length_nm": None, "row": [],  # ghosts
         "pitch_nm": 0}


def ghost(index, **fields):
    return only_draw(payload(index, finding({"tool": "ghost", **fields})))


def with_parts(*extra_pads, side="top"):
    """The board with C7: two small GND pads 10 mm apart around (30, 10) mm, its body between them."""
    snapshot = board()
    c7 = model.Footprint("f-c7", "C7", (30 * MM, 10 * MM), 0.0, side, ())
    pads = tuple(model.Pad(f"p-c7-{n}", "f-c7", str(n), "GND", (30 * MM, y), None,
                           {"B.Cu" if side == "bottom" else "F.Cu": (square(29.9 * MM, y - 100_000, 30.1 * MM,
                                                                              y + 100_000),)})
                 for n, y in ((1, 5 * MM), (2, 15 * MM)))
    return BoardIndex(replace(snapshot, footprints=(*snapshot.footprints, c7), pads=(*snapshot.pads, *pads, *extra_pads)))


def test_every_draw_carries_the_plane_and_ghost_keys_empty(index):
    empty = findings._empty_draw(findings.Draw("highlight", {}))
    assert {key: empty[key] for key in EMPTY} == EMPTY
    draw = only_draw(payload(index, finding({"tool": "highlight", "targets": ["U3"]})))
    assert all(draw[key] == value for key, value in EMPTY.items())


def test_ghost_kind_fields_are_checked():
    bad = [({"kind": "part", "at": "pt:1,1@F.Cu"}, "part missing"),
           ({"kind": "part", "part": "C12"}, "at missing"),
           ({"kind": "part", "part": 5, "at": "pt:1,1@F.Cu"}, "part is not a target"),
           ({"kind": "part", "part": "C12", "at": "pt:1,1@F.Cu", "rotation": "90"}, "rotation is not a number"),
           ({"kind": "track", "at": "pt:1,1@F.Cu", "net": "SIG"}, "a track needs path: 2 to 50 points or targets"),
           ({"kind": "track", "path": "pt:1,1", "net": "SIG"}, "path is not a list of 2 to 50"),
           ({"kind": "track", "path": ["pt:1,1"], "net": "SIG"}, "path is not a list of 2 to 50"),
           ({"kind": "track", "path": ["pt:1,1"] * 51, "net": "SIG"}, "path is not a list of 2 to 50"),
           ({"kind": "track", "path": ["pt:1,1", 5], "net": "SIG"}, "path[1] is not a target"),
           ({"kind": "track", "path": ["pt:1,1", [1, 2, 3]], "net": "SIG"}, "path[1] is not [x, y]"),
           ({"kind": "track", "path": ["pt:1,1", "pt:2,2"]}, "net missing"),
           ({"kind": "track", "path": ["pt:1,1", "pt:2,2"], "net": "SIG", "width": 0}, "width is not a positive"),
           ({"kind": "track", "path": ["pt:1,1", "pt:2,2"], "net": "SIG", "width": 51}, "width is over 50 mm"),
           ({"kind": "track", "path": ["pt:1,1", "pt:2,2"], "net": "SIG", "layer": "F.SilkS"},
            "layer is not a copper layer"),
           ({"kind": "via_row", "from": "pt:1,1", "to": "pt:2,2"}, "pitch missing"),
           ({"kind": "via_row", "from": "pt:1,1", "to": "pt:2,2", "pitch": 0}, "pitch is not a positive"),
           ({"kind": "via_row", "from": "pt:1,1", "to": "pt:2,2", "pitch": True}, "pitch is not a positive"),
           ({"kind": "via_row", "to": "pt:2,2", "pitch": 1}, "from missing"),
           ({"kind": "via_row", "from": "pt:1,1", "to": [2], "pitch": 1}, "to is not [x, y]"),
           ({"kind": "via_row", "from": "pt:1,1", "to": "pt:2,2", "pitch": 1, "net": ""}, "net is not a net name")]
    _, problems = parsed_draws([{"tool": "ghost", **fields} for fields, _ in bad], len(bad))
    assert len(problems) == len(bad)
    for problem, (_, reason) in zip(problems, bad):
        assert "skipped: " + reason in problem, (problem, reason)
    good = [{"kind": "part", "part": "C12", "at": "pt:1,1@F.Cu"},
            {"kind": "part", "part": "C12", "at": "U3", "rotation": -270},
            {"kind": "track", "path": ["U3.4", [22, 8], "pt:26,8@F.Cu"], "net": "net:SIG", "layer": "f.cu",
             "width": 0.25},
            {"kind": "via_row", "from": [2, 2], "to": "pt:2,12@F.Cu", "pitch": 1.5, "size": 0.4, "net": "GND"},
            {"kind": "via", "at": "pt:1,1@F.Cu"}]
    kept, _ = parsed_draws([{"tool": "ghost", **fields} for fields in good], 0)
    part, turned, track, row, via = (draw.fields for draw in kept)
    assert part["part"] == "C12" and part["rotation"] is None  # a part keeps its own rotation unless given
    assert turned["rotation"] == 90 and turned["at"] == "U3"
    assert track["path"] == ("U3.4", "pt:22,8", "pt:26,8@F.Cu") and track["layer"] == "F.Cu"
    assert track["net"] == "SIG" and track["width"] == 0.25
    assert row["from"] == "pt:2,2" and row["pitch"] == 1.5 and row["size"] == 0.4 and row["net"] == "GND"
    assert via["rotation"] == 0.0 and via["at"] == "pt:1,1@F.Cu"  # as before


def test_a_moved_part_carries_its_pads_and_fits(index):
    draw = ghost(index, kind="part", part="C12", at="pt:25,10@F.Cu")
    assert draw["ok"] and draw["fits"] is True and draw["problem"] == ""
    assert (draw["part"], draw["from"], draw["moved_nm"]) == ("C12", [20 * MM, 10 * MM, "F.Cu"], 5 * MM)
    assert draw["points"] == [[25 * MM, 10 * MM, "F.Cu"]] and draw["rotation_deg"] == 0.0 and draw["layer"] == "F.Cu"
    assert draw["pads_nm"] == [{"layer": "F.Cu", "polygon_nm": [[24.5 * MM, 9.5 * MM], [25.5 * MM, 9.5 * MM],
                                                                [25.5 * MM, 10.5 * MM], [24.5 * MM, 10.5 * MM]]}]
    assert draw["polygon_nm"] == []  # one pad: no body
    assert draw["ids"] == ["f-c12"] and draw["targets"][0] == {"text": "pt:25,10@F.Cu", "found": True, "note": "",
                                                              "kind": "point", "net": ""}
    same_net = ghost(index, kind="part", part="C12", at="pt:15,10.6@F.Cu")  # its pad on its own net's track
    assert same_net["fits"] is True


def test_a_moved_part_turns_about_its_anchor(index):
    draw = ghost(index, kind="part", part="U3", at="pt:30,25@F.Cu", rotation=90)
    assert draw["fits"] is True and draw["rotation_deg"] == 90 and draw["moved_nm"] == round(math.hypot(20, 15) * MM)
    centres = sorted((sum(x for x, _ in pad["polygon_nm"]) // 4, sum(y for _, y in pad["polygon_nm"]) // 4)
                     for pad in draw["pads_nm"])
    assert centres == [(29 * MM, 25 * MM), (30 * MM, 25 * MM), (31 * MM, 25 * MM)]  # EP above the anchor: now left
    xs, ys = [x for x, _ in draw["polygon_nm"]], [y for _, y in draw["polygon_nm"]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (28_900_000, 31_100_000, 24_800_000, 25_200_000)  # the body turned


@pytest.mark.parametrize("at, problem", [
    ("pt:5,20@F.Cu", "C12.1 clashes with /~{RST} track (0.00 mm from its copper, 0.10 mm needed)"),
    ("pt:5,20.65@F.Cu", "C12.1 clashes with /~{RST} track (0.05 mm from its copper, 0.10 mm needed)"),
    ("pt:44.6,10@F.Cu", "crosses the board edge"),
    ("pt:60,10@F.Cu", "outside the board"),
    ("pt:10,9@F.Cu", "C12.1 clashes with GND pad (0.00 mm from its copper, 0.10 mm needed)"),
])
def test_a_moved_part_that_does_not_fit_says_why(index, at, problem):
    draw = ghost(index, kind="part", part="C12", at=at)
    assert draw["ok"] and draw["fits"] is False and draw["problem"] == problem


def test_a_moved_part_must_not_land_on_another_parts_body():
    draw = ghost(with_parts(), kind="part", part="U3", at="pt:30,10@F.Cu")  # between C7's pads, clear of them
    assert draw["fits"] is False and draw["problem"] == "lands on C7"
    beneath = ghost(with_parts(side="bottom"), kind="part", part="U3", at="pt:30,10@F.Cu")
    assert beneath["fits"] is True  # C7 is on the other side
    own = ghost(with_parts(), kind="part", part="C7", at="pt:30,11@F.Cu")  # over where it is now: its own body
    assert own["fits"] is True


def test_moved_part_problems(index, monkeypatch):
    for part, problem in (("U3.4", "part: U3.4 is not a part (give its reference, e.g. C12)"),
                          ("R1", "part: 2 parts share reference R1"), ("U99", "part: U99 not found on board")):
        draw = ghost(index, kind="part", part=part, at="pt:25,10@F.Cu")
        assert not draw["ok"] and draw["problem"] == problem
    missing = ghost(index, kind="part", part="C12", at="U99")
    assert not missing["ok"] and missing["problem"] == "at: U99 not found on board"
    monkeypatch.setattr(findings, "MAX_GHOST_PADS", 2)
    many = ghost(index, kind="part", part="U3", at="pt:30,25@F.Cu")
    assert many["fits"] is True and many["pads_nm"] == [] and len(many["polygon_nm"]) == 4
    assert many["problem"] == "3 pads; only the body drawn"


def test_a_suggested_route_is_checked_along_its_path(index):
    draw = ghost(index, kind="track", net="SIG", path=["pt:22,5@F.Cu", "pt:22,8@F.Cu", "pt:26,8@F.Cu"])
    assert draw["ok"] and draw["fits"] is True and draw["problem"] == "" and draw["layer"] == "F.Cu"
    assert draw["path_nm"] == [[22 * MM, 5 * MM, "F.Cu"], [22 * MM, 8 * MM, "F.Cu"], [26 * MM, 8 * MM, "F.Cu"]]
    assert draw["points"] == [[22 * MM, 5 * MM, "F.Cu"], [26 * MM, 8 * MM, "F.Cu"]]
    assert (draw["width_nm"], draw["length_nm"]) == (200_000, 7 * MM)
    near = ghost(index, kind="track", net="SIG", path=["pt:15,10.05@F.Cu", "pt:15,12@F.Cu"])
    assert near["targets"][0]["note"] == "" and near["ids"] == []  # a point as given: not snapped to the track
    inner = ghost(index, kind="track", net="SIG", layer="In1.Cu", path=[[2, 5], [30, 5]])
    assert inner["fits"] is True  # over the GND plane: zones refill around a track


@pytest.mark.parametrize("path, width, problem", [
    (["pt:5,18@F.Cu", "pt:5,22@F.Cu"], None, "clashes with /~{RST} track (0.00 mm from its copper, 0.10 mm needed)"),
    (["pt:2,20.25@F.Cu", "pt:8,20.25@F.Cu"], None,
     "clashes with /~{RST} track (0.05 mm from its copper, 0.10 mm needed)"),
    (["pt:2,20.40@F.Cu", "pt:8,20.40@F.Cu"], 0.5, "clashes with /~{RST} track (0.05 mm from its copper, "
                                                  "0.10 mm needed)"),
    (["pt:40,30@F.Cu", "pt:50,30@F.Cu"], None, "crosses the board edge"),
    (["pt:50,30@F.Cu", "pt:60,30@F.Cu"], None, "outside the board"),
    (["pt:5,18@F.Cu", "pt:5,22@F.Cu", "pt:50,22@F.Cu"], None,
     "clashes with /~{RST} track (0.00 mm from its copper, 0.10 mm needed); crosses the board edge"),
])
def test_a_route_that_does_not_fit_says_why(index, path, width, problem):
    draw = ghost(index, kind="track", net="SIG", path=path, **({"width": width} if width else {}))
    assert draw["ok"] and draw["fits"] is False and draw["problem"] == problem


def test_a_route_without_a_width_takes_its_nets_usual_one(index):
    snapshot = board()
    wide = tuple(model.Track(f"t-wide-{n}", "B.Cu", "SIG", (n * MM, 35 * MM), (n * MM, 38 * MM), 300_000)
                 for n in (1, 2))
    busy = BoardIndex(replace(snapshot, tracks=(*snapshot.tracks, *wide)))
    path = ["pt:22,5@F.Cu", "pt:26,5@F.Cu"]
    assert ghost(busy, kind="track", net="SIG", path=path)["width_nm"] == 300_000  # two of 0.3, one of 0.2
    assert ghost(index, kind="track", net="SIG", path=path)["width_nm"] == 200_000  # 0.2 and 0.1 once each: the wider
    assert ghost(index, kind="track", net="GND", path=path)["width_nm"] == findings.GHOST_TRACK_NM  # no tracks
    assert ghost(index, kind="track", net="GND", path=path, width=0.5)["width_nm"] == 500_000


@pytest.mark.parametrize("fields, problem", [
    ({"path": ["U3.4", "pt:22,8@F.Cu"]}, "layer missing: give layer, or the same @layer on every point of path"),
    ({"path": ["pt:1,1@F.Cu", "pt:2,2@B.Cu"]}, "layer missing: give layer, or the same @layer on every point of path"),
    ({"path": ["pt:1,1@F.Cu", "pt:2,2@B.Cu"], "layer": "F.Cu"}, "path[1] is on B.Cu, the track on F.Cu"),
    ({"path": ["pt:1,1@F.SilkS", "pt:2,2@F.SilkS"]}, "F.SilkS is not a copper layer"),
    ({"path": ["pt:1,1@F.Cu", "U99"]}, "path[1]: U99 not found on board"),
    ({"path": ["pt:1,1@F.Cu", "pt:2,2@F.Cu"], "net": "NOPE"}, "net NOPE not found on board"),
])
def test_route_problems(index, fields, problem):
    draw = ghost(index, kind="track", **{"net": "SIG", **fields})
    assert not draw["ok"] and draw["problem"] == problem


def test_a_route_on_a_pad_takes_the_pads_place_and_layer_given(index):
    draw = ghost(index, kind="track", net="SIG", layer="F.Cu", path=["C12.1", "pt:25,10"])
    assert draw["ok"] and draw["path_nm"][0] == [20 * MM, 10 * MM, "F.Cu"] and draw["fits"] is True


def test_a_row_of_vias_is_spread_evenly_no_further_apart_than_its_pitch(index):
    def row(pitch, **fields):
        return ghost(index, kind="via_row", **{"from": "pt:2,2@F.Cu", "to": "pt:2,12@F.Cu", "pitch": pitch, **fields})
    spread = row(3)
    assert spread["ok"] and spread["fits"] is True and spread["problem"] == ""
    assert spread["row"] == [[2 * MM, round(y * MM), True] for y in (2, 4.5, 7, 9.5, 12)]
    assert spread["pitch_nm"] == 2.5 * MM and spread["points"] == [[2 * MM, 2 * MM, "F.Cu"], [2 * MM, 12 * MM, "F.Cu"]]
    assert len(row(2.5)["row"]) == 5 and len(row(2)["row"]) == 6  # both ends included, never one too many
    sized = row(3, size=0.4)
    assert sized["size_nm"] == 400_000
    one = ghost(index, kind="via_row", **{"from": [2, 2], "to": "pt:2,2@F.Cu", "pitch": 1})
    assert one["row"] == [[2 * MM, 2 * MM, True]] and one["pitch_nm"] == 0


def test_a_row_says_how_many_of_its_vias_clash(index):
    def row(**fields):
        return ghost(index, kind="via_row", **{"from": "pt:5,18@F.Cu", "to": "pt:5,22@F.Cu", "pitch": 1, **fields})
    clashing = row(net="GND")
    assert clashing["ok"] and clashing["fits"] is False
    assert [fits for _, _, fits in clashing["row"]] == [True, True, False, True, True]
    assert clashing["problem"] == "1 of 5 vias clash (first: /~{RST} track 0.00 mm from its copper, 0.10 mm needed)"
    assert row(net="/~{RST}")["fits"] is True  # its own net's track
    assert clashing["targets"][0]["note"] == "" and clashing["ids"] == []  # its ends as given


def test_a_row_past_its_cap_is_refused_not_cut(index, monkeypatch):
    draw = ghost(index, kind="via_row", **{"from": "pt:0,0@F.Cu", "to": "pt:40,0@F.Cu", "pitch": 0.1})
    assert not draw["ok"] and draw["problem"] == "401 vias at 0.1 mm pitch over 40.00 mm; at most 100 in one row"
    monkeypatch.setattr(findings, "MAX_ROW_VIAS", 4)
    draw = ghost(index, kind="via_row", **{"from": "pt:2,2@F.Cu", "to": "pt:2,12@F.Cu", "pitch": 3})
    assert not draw["ok"] and draw["problem"].startswith("5 vias at 3 mm pitch")


FAR = "9" * 150


@pytest.mark.parametrize("draw, role", [
    ({"tool": "ghost", "kind": "via", "at": f"pt:{FAR},5@F.Cu"}, "at"),
    ({"tool": "label", "at": f"pt:5,-{FAR}", "text": "x"}, "at"),
    ({"tool": "highlight", "targets": ["U3", "net:SIG@F.Cu~20000,1"]}, "targets[1]"),
    ({"tool": "ghost", "kind": "track", "net": "SIG", "path": ["pt:1,1@F.Cu", "PT: 10000.5, 1 @F.Cu"]}, "path[1]"),
])
def test_a_point_past_ten_metres_is_refused(index, draw, role):
    item = parse(raw(finding(draw))).findings[0]
    assert not item.draws
    assert item.problems == (f"draw 1 ({draw['tool']}) skipped: {role}: a coordinate past 10000 mm (10 m) is not on "
                             "a board",)
    edge = parse(raw(finding({"tool": "label", "at": "pt:10000,-10000@F.Cu", "text": "x"}))).findings[0]
    assert edge.draws and not edge.problems  # 10 m itself is still a coordinate


def test_a_target_past_ten_metres_resolves_to_nothing(index):
    for target in (f"pt:{FAR},5@F.Cu", "pt:20000,5", "net:SIG@F.Cu~-20000,1"):
        found = resolve(index, target)
        assert not found.found and found.note == "a coordinate past 10000 mm (10 m) is not on a board", target


@pytest.mark.parametrize("fields, problem", [
    ({"kind": "via", "at": "pt:60,10@F.Cu"}, "outside the board"),
    ({"kind": "via", "at": "pt:45,10@F.Cu"}, "crosses the board edge"),
    ({"kind": "pad", "at": "pt:60,10@F.Cu"}, "outside the board"),
    ({"kind": "cap", "at": "pt:60,10@F.Cu"}, "outside the board"),
    ({"kind": "cap", "at": "pt:44.8,10@F.Cu", "rotation": 0}, "crosses the board edge"),
])
def test_a_suggestion_off_the_board_does_not_fit(index, fields, problem):
    draw = ghost(index, **fields)
    assert draw["ok"] and draw["fits"] is False and draw["problem"] == problem
    assert draw["kicad_drc"]["kept"] == [problem] and draw["kicad_drc"]["quick_fits"] is True  # KiCad never judges it
    inside = ghost(index, **{**fields, "at": "pt:30,30@F.Cu"})
    assert inside["fits"] is True and inside["kicad_drc"]["kept"] == []


def test_a_rows_vias_off_the_board_are_held_back(index):
    draw = ghost(index, kind="via_row", **{"from": "pt:40,30@F.Cu", "to": "pt:50,30@F.Cu", "pitch": 2.5})
    assert [fits for _, _, fits in draw["row"]] == [True, True, False, False, False]
    assert draw["fits"] is False and draw["problem"] == "3 of 5 vias not inside the board (first: crosses the board edge)"
    assert draw["kicad_drc"]["kept_row"] == [2, 3, 4] and draw["kicad_drc"]["quick_fits"] is True
    judged = ghost_drc.apply({"findings": [{"draws": [draw]}]}, ghost_drc.Outcome({(0, 0): ghost_drc.Verdict("done")}))
    after = judged["findings"][0]["draws"][0]  # KiCad's DRC found nothing: the vias off the board still do not fit
    assert after["fits"] is False and [fits for _, _, fits in after["row"]] == [True, True, False, False, False]
    assert after["problem"] == "3 of 5 vias not inside the board (first: crosses the board edge)"


def test_off_the_board_stands_after_kicads_drc_for_moves_and_routes(index):
    for draw in (ghost(index, kind="part", part="C12", at="pt:60,10@F.Cu"),
                 ghost(index, kind="track", net="SIG", path=["pt:50,30@F.Cu", "pt:60,30@F.Cu"])):
        assert draw["fits"] is False and draw["problem"] == "outside the board"
        judged = ghost_drc.apply({"findings": [{"draws": [draw]}]},
                                 ghost_drc.Outcome({(0, 0): ghost_drc.Verdict("done")}))["findings"][0]["draws"][0]
        assert judged["fits"] is False and judged["problem"] == "outside the board"


def test_a_ghosts_net_is_cleaned_and_capped():
    item = parse(raw(finding({"tool": "ghost", "kind": "via", "at": "pt:1,1@F.Cu", "net": " net:G\x00N‮D "},
                             {"tool": "ghost", "kind": "via", "at": "pt:1,1@F.Cu", "net": "N" * 201}))).findings[0]
    (draw,) = item.draws
    assert draw.fields["net"] == "G N D"
    assert item.problems == ("draw 2 (ghost) skipped: net is not a net name",)


def test_a_measuring_bug_costs_its_draw_not_the_file(index, monkeypatch):
    def broken(*args):
        raise TypeError("unsupported operand")
    monkeypatch.setattr(findings, "_width", broken)
    result = payload(index, finding({"tool": "width", "target": "net:SIG", "required": 0.15},
                                    {"tool": "highlight", "targets": ["U3"]}))
    width, highlight = result["findings"][0]["draws"]
    assert not width["ok"] and width["problem"] == "could not measure (TypeError: unsupported operand); please report it"
    assert width["ids"] == [] and width["measured_nm"] is None and width["what"] == "Width"
    assert highlight["ok"] and highlight["ids"]


def test_ghost_kinds_are_framed_on_what_they_show(index):
    result = payload(index, finding({"tool": "ghost", "kind": "part", "part": "C12", "at": "pt:25,30@F.Cu"},
                                    {"tool": "ghost", "kind": "track", "net": "SIG", "path": [[22, 5], [26, 8]],
                                     "layer": "F.Cu"},
                                    {"tool": "ghost", "kind": "via_row", "from": "pt:2,2@F.Cu", "to": "pt:2,12@F.Cu",
                                     "pitch": 3}))
    x0, y0, x1, y1 = result["findings"][0]["bbox_nm"]
    assert (x0, y0) == (2 * MM, 2 * MM) and (x1, y1) == (26 * MM, 30.5 * MM)  # the row, the route, both places of C12
    assert not result["findings"][0]["passes"] and result["findings"][0]["disagree"] == []


def test_verdict_is_what_the_board_says(index, split):
    """`verdict`: measured (a measuring draw, nothing against it), refuted (a problem whose
    limits are all met; not an info finding), mismatch (a claim far off, a target not on the
    board, a suggestion that does not fit), unchecked (marks only, or KiCad not connected)."""
    def verdict(board, *draws, **fields):
        return payload(board, finding(*draws, **fields))["findings"][0]["verdict"]
    over = {"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}", "limit": 5, "limit_is": "min"}  # 4.6 mm
    met = dict(over, limit=4)
    highlight = {"tool": "highlight", "targets": [f"uuid:{TRACK}"]}
    assert verdict(index, over) == "measured"
    assert verdict(index, over, highlight) == "measured"
    assert verdict(index, met) == "refuted"
    assert verdict(index, met, severity="info") == "measured"
    assert verdict(index, over, values={"distance_mm": 9.0}) == "mismatch"  # claimed 9, measured 4.6
    assert verdict(index, {"tool": "highlight", "targets": ["uuid:00000000"]}) == "mismatch"  # not on the board
    assert verdict(index, over, {"tool": "label", "at": "uuid:00000000", "text": "x"}) == "mismatch"
    assert verdict(index, highlight) == "unchecked"
    assert verdict(index, {"tool": "bars", "unit": "mm", "items": [["a", 1.0, None]]}) == "unchecked"  # its own numbers
    assert verdict(index) == "unchecked"
    assert verdict(None, over) == "unchecked"
    change = {"tool": "layer_change", "via": "uuid:via-lvds-0"}
    assert verdict(split, change, {"tool": "ghost", "kind": "via", "at": "pt:20,19.2@F.Cu", "net": "GND"}) == "measured"
    assert verdict(split, change, {"tool": "ghost", "kind": "via", "at": "pt:20,18.5@F.Cu", "net": "GND"}) == "mismatch"


def ruled(board, rule, *draws, **fields):
    """The payload's one finding with `rule` applied to its check."""
    ctx = Context("file", {"4": STAMP}, STAMP, {}, {"test.check": (rule,)})
    return payload(board, finding(*draws, **fields), ctx=ctx)["findings"][0]


def test_decap_pair_rule_wants_the_cap_on_the_pins_net(index):
    """The part with fewer pads is the cap; the pin's net must be on one of its pads."""
    good = ruled(index, "decap_pair", {"tool": "distance", "from": "C12.1", "to": "U3.4", "limit": 2})
    assert good["rules"] == [{"rule": "decap_pair", "ok": True, "text": "C12 and U3.4 share SIG"}]
    assert good["verdict"] == "measured"  # 10 mm, over the 2 mm limit; the pair is right
    bad = ruled(index, "decap_pair", {"tool": "distance", "from": "C12.1", "to": "U3.EP", "limit": 2})
    assert bad["rules"] == [{"rule": "decap_pair", "ok": False, "text":
                             "C12.1 is on SIG, U3.EP on GND: C12 does not connect to GND, not a decoupling pair"}]
    assert bad["verdict"] == "mismatch"
    swapped = ruled(index, "decap_pair", {"tool": "arrow", "from": "U3.EP", "to": "C12.1"})  # the IC named first
    assert swapped["rules"][0]["ok"] is False and swapped["rules"][0]["text"].startswith("C12.1 is on SIG")
    snapshot = board()
    loose = replace(snapshot.pads[0], id="p-u3-7", number="7", net="unconnected-(U3-Pad7)")
    spare = BoardIndex(replace(snapshot, pads=snapshot.pads + (loose,)))
    assert ruled(spare, "decap_pair", {"tool": "distance", "from": "C12.1", "to": "U3.7"})["rules"][0] == {
        "rule": "decap_pair", "ok": False, "text": "U3.7 is not connected: nothing to decouple"}
    assert ruled(index, "decap_pair", {"tool": "distance", "from": "C12", "to": "U3"})["rules"] == []  # parts: no pads
    assert ruled(index, "decap_pair", {"tool": "highlight", "targets": ["C12"]})["rules"] == []
    assert ruled(index, "no_such_rule", {"tool": "highlight", "targets": ["C12"]})["rules"] == []
    assert payload(None, finding({"tool": "distance", "from": "C12.1", "to": "U3.4"}),
                   ctx=Context("file", {"4": STAMP}, STAMP, {}, {"test.check": ("decap_pair",)}))["findings"][0]["rules"] == []


def test_same_net_rule(index):
    same = ruled(index, "same_net", {"tool": "distance", "from": "U3.4", "to": "C12.1"})
    assert same["rules"] == [{"rule": "same_net", "ok": True, "text": "U3.4 and C12.1 share SIG"}]
    other = ruled(index, "same_net", {"tool": "clearance", "a": "U3.4", "b": "U3.EP"})
    assert other["rules"] == [{"rule": "same_net", "ok": False, "text": "U3.4 is on SIG, U3.EP on GND: not the same net"}]
    assert other["verdict"] == "mismatch"


def test_signal_via_rule(split):
    change = {"tool": "layer_change", "via": "uuid:via-lvds-0"}
    assert ruled(split, "signal_via", change)["rules"] == [
        {"rule": "signal_via", "ok": True, "text": "uuid:via-lvds-0 is on LVDS_P, no plane of its net next to it"}]
    snapshot = split_board.board()  # a GND via in the GND fill of both planes
    gnd_via = model.Via("via-gnd-2", "GND", (20 * MM, 10 * MM), 600_000, 300_000, "F.Cu", "B.Cu")
    returned = BoardIndex(replace(snapshot, vias=snapshot.vias + (gnd_via,)))
    plane_via = ruled(returned, "signal_via", {"tool": "layer_change", "via": "uuid:via-gnd-2"})
    (rule,) = plane_via["rules"]
    assert rule["rule"] == "signal_via" and rule["ok"] is False
    assert rule["text"].startswith("uuid:via-gnd-2 is on GND, the plane on In") and \
        rule["text"].endswith(" next to it: a return via, not a signal via")
    assert plane_via["verdict"] == "mismatch"
    assert ruled(split, "signal_via", {"tool": "highlight", "targets": ["uuid:via-lvds-0"]})["rules"] == []


def test_diff_pair_rule(split):
    def nets(*targets):
        return ruled(split, "diff_pair", {"tool": "highlight", "targets": list(targets)})["rules"]
    assert nets("net:LVDS_P", "net:LVDS_N@F.Cu") == [{"rule": "diff_pair", "ok": True, "text": "LVDS_P and LVDS_N are a pair"}]
    assert nets("net:LVDS_P") == [{"rule": "diff_pair", "ok": True, "text": "LVDS_P pairs with LVDS_N"}]
    assert nets("net:LVDS_P", "net:ETH_N") == [{"rule": "diff_pair", "ok": False, "text":
                                                "LVDS_P and ETH_N are not a pair (LVDS_P pairs with LVDS_N)"}]
    assert nets("net:GND") == [{"rule": "diff_pair", "ok": False, "text":
                                "GND is not a differential pair net: no partner named like it (+/- or P/N)"}]
    assert nets("net:LVDS_P", "net:LVDS_N", "net:ETH_P")[0]["text"] == "3 nets named (LVDS_P, LVDS_N, ETH_P): a pair is two"
    assert nets("uuid:via-lvds-0") == []  # no net named as such


def test_a_target_entry_names_its_net(index):
    draw = only_draw(payload(index, finding({"tool": "distance", "from": "U3.4", "to": "U3"})))
    assert [target["net"] for target in draw["targets"]] == ["SIG", ""]  # a pad's net; a part has none
    draw = only_draw(payload(index, finding({"tool": "highlight", "targets": ["net:GND", f"uuid:{VIA}", "edge"]})))
    assert [target["net"] for target in draw["targets"]] == ["GND", "SIG", ""]


def test_clean_checks_are_kept_apart_from_findings(index):
    """`clean`: check ids or {"check", "note"}, deduplicated, capped; a check with findings
    stays a finding; the payload lists them beside `skipped`."""
    found = parse(raw(finding({"tool": "highlight", "targets": ["U3"]}, check="decap.distance"),
                      clean=["hs.stub", {"check": "dp.skew", "note": "both pairs within 0.1 mm"}, "hs.stub",
                             {"check": "decap.distance"}, 7, {"note": "no check"}]))
    assert found.clean == (("hs.stub", ""), ("dp.skew", "both pairs within 0.1 mm"))
    assert [p for p in found.problems if "clean" in p] == [
        "decap.distance is listed clean and has findings: the findings stand",
        "a clean entry is not a check id or an object", "a clean entry names no check"]
    assert parse(raw(clean="hs.stub")).clean == () and "clean is not a list" in parse(raw(clean="hs.stub")).problems
    many = parse(raw(clean=[f"g.c{n}" for n in range(findings.MAX_CLEAN + 5)]))
    assert len(many.clean) == findings.MAX_CLEAN and any("left out" in p for p in many.problems)
    assert parse(raw()).clean == ()
    result = resolve_file(found, index, context())
    assert result["clean"] == [{"check": "hs.stub", "note": ""}, {"check": "dp.skew", "note": "both pairs within 0.1 mm"}]
    assert findings.status_payload("none")["clean"] == []
