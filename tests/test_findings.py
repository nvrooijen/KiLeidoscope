"""Findings files: validation limits, target resolution, measured distances, keys, handshake,
the changed notice."""

import json
import math
from dataclasses import replace

import pytest

import split_board
from kileido_bridge import findings, model
from kileido_bridge.board_index import BoardIndex
from kileido_bridge.findings import Context, FindingsError, finding_key, named, parse, resolve_file
from kileido_bridge.targets import resolve
from test_board_index import MM, board

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


def context(source="file", runs=None, accepted=(), live=STAMP, states=None):
    return Context("x.kls-findings.json", source, {"4": STAMP} if runs is None else runs,
                   frozenset(accepted), live, states or {})


def payload(index, *items, ctx=None, **top):
    return resolve_file(parse(raw(*items, **top)), index, ctx or context())


def only_draw(result):
    (item,) = result["findings"]
    (draw,) = item["draws"]
    return draw


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
    found = parse(raw(*[finding() for _ in range(301)]))
    assert len(found.findings) == 300 and any("past 300" in p for p in found.problems)
    draws = [{"tool": "highlight", "targets": ["U3"]}] * 21
    found = parse(raw(finding(*draws)))
    assert len(found.findings[0].draws) == 20 and any("past 20" in p for p in found.findings[0].problems)


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


def test_unknown_or_bad_check_ids_group_under_other(index):
    result = payload(index, finding(check="my.own_check"), finding(check="plane.gap_crossing"),
                     finding(check="has space"), finding(check="drc.clearance"))
    groups = [(f["check"], f["group"]) for f in result["findings"]]
    assert groups == [("my.own_check", "other"), ("plane.gap_crossing", "plane"),
                      ("other", "other"), ("drc.clearance", "drc")]


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
    assert len(item.draws) == 1 and item.draws[0].step == 0 and item.draws[0].emphasis == "normal"
    assert sum("skipped" in p for p in item.problems) == len(draws) - 1
    assert any("unknown tool explode" in p for p in item.problems)


def test_valid_draw_fields_are_normalised():
    draws = [{"tool": "distance", "from": "U3", "to": "C12", "mode": "center", "limit": 0.2, "step": 3},
             {"tool": "area", "layer": "in1.cu", "targets": ["uuid:z-gnd"], "emphasis": "strong"},
             {"tool": "area", "layer": "in1.cu", "polygon": [[0, 0], [1, 0], [1, 1]]},
             {"tool": "layers", "layers": ["f.cu", "In2.Cu"]},
             {"tool": "ghost", "kind": "via", "at": "pt:1,2@F.Cu", "size": 0.6, "emphasis": "strong"}]
    distance, zone, area, layers, ghost = parse(raw(finding(*draws))).findings[0].draws
    assert distance.fields["mode"] == "centre" and distance.fields["limit_is"] == "max" and distance.step == 3
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
    assert draw["outline"] is True
    assert {"f-u3", "p-c12-1", TRACK, "arc-1", VIA, "z-gnd"} <= set(draw["ids"])
    assert len(draw["ids"]) == len(set(draw["ids"]))
    assert [t["kind"] for t in draw["targets"]][:2] == ["part", "pad"]


def test_missing_targets_are_noted_and_the_rest_is_drawn(index):
    result = payload(index, finding({"tool": "highlight", "targets": ["U3", "U99"]},
                                    {"tool": "label", "at": "U99", "text": "here"}))
    highlight, label = result["findings"][0]["draws"]
    assert highlight["ok"] and highlight["targets"][1] == {"text": "U99", "found": False,
                                                          "note": "U99 not found on board", "kind": ""}
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
    assert draw["measured_nm"] == 5 * MM - 100_000 and draw["outline"] and draw["ids"] == ["t-weird"]
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
    assert ghost["targets"] == [{"text": "pt:15,10.4@F.Cu", "found": True, "note": "", "kind": "point"}]
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
    assert same["stamp"] == same["live_stamp"] == STAMP
    changed = payload(index, item, ctx=context(live="00000000"))
    assert changed["changed"] and changed["findings"][0]["draws"]
    drc = payload(index, item, ctx=context(source="drc", live="00000000"))
    assert drc["changed"] and drc["source"] == "drc"
    assert not payload(index, item, stamp=None)["changed"]
    assert not payload(index, item, ctx=context(live=""))["changed"]


# handshake

def test_handshake_known_changed_unknown_accepted(index):
    item = finding({"tool": "highlight", "targets": ["U3"]})
    known = payload(index, item)
    assert known["handshake"] == "known" and not known["changed"] and known["findings"][0]["draws"]
    assert known["status"] == "ok" and known["connected"] and known["run"] == "4"
    changed = payload(index, item, ctx=context(live="00000000"))
    assert changed["handshake"] == "known" and changed["changed"]
    unknown = payload(index, item, ctx=context(runs={"4": "ffffffff"}))
    assert unknown["handshake"] == "unknown_run" and unknown["findings"][0]["draws"] == []
    assert unknown["findings"][0]["bbox_nm"] is None and unknown["findings"][0]["title"] == "T"
    accepted = payload(index, item, ctx=context(runs={}, accepted=[findings.accept_key("4", STAMP)]))
    assert accepted["handshake"] == "accepted" and accepted["findings"][0]["draws"]
    assert not accepted["changed"]
    drc = payload(index, item, ctx=context(source="drc", runs={}, live="00000000"))
    assert drc["handshake"] == "known" and drc["changed"] and drc["source"] == "drc"
    assert payload(index, item, stamp=None)["handshake"] == "unknown_run"


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


def test_payload_is_json(index):
    result = payload(index, finding({"tool": "distance", "from": f"uuid:{VIA}", "to": "edge", "limit": 1},
                                    {"tool": "highlight", "targets": ["net:SIG"]}, values={"x_mm": 1.5}))
    text = json.dumps(result, allow_nan=False)
    assert json.loads(text) == result
    assert all(isinstance(v, int) for v in result["findings"][0]["bbox_nm"])
    assert not math.isnan(result["findings"][0]["draws"][0]["measured_nm"])


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

def test_stage2_fields_are_checked():
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
    item = parse(raw(finding(*draws))).findings[0]
    assert [draw.tool for draw in item.draws] == ["clearance", "routed_length"]
    assert item.draws[0].fields == {"a": "U3", "b": "C12", "required": 0.2} and item.draws[1].fields["net"] == "SIG"
    assert sum("skipped" in p for p in item.problems) == len(draws) - 2
    bars = parse(raw(finding({"tool": "bars", "items": [{"label": "x" * 41, "value": 2, "limit": 1.5}],
                              "unit": "PS"}))).findings[0]
    assert bars.draws[0].fields == {"items": (("x" * 39 + "…", 2.0, 1.5),), "unit": "ps", "at": None}
    assert any("cut to 40" in p for p in bars.problems)


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
    assert draw["outline"] and draw["ids"] == ["t-weird"] and draw["limit_is"] == "min"
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


def test_stage2_payload_is_json(index):
    result = payload(index, finding({"tool": "width", "target": "net:SIG", "required": 0.3},
                                    {"tool": "clearance", "a": f"uuid:{VIA}", "b": f"uuid:{TRACK}"},
                                    {"tool": "routed_length", "net": "SIG", "from": "U3.4", "to": "C12.1"},
                                    {"tool": "edge_gap", "target": "U3"},
                                    {"tool": "bars", "items": [{"label": "a", "value": 1}], "at": "C12"}))
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    keys = set(findings._empty_draw(findings.Draw("highlight", {})))
    assert all(set(draw) == keys and draw["ok"] for draw in result["findings"][0]["draws"])


# planes and return paths (on the split-plane board, tests/split_board.py)

STAGE3_KEYS = ("planes", "spans_nm", "covered_nm", "edges_nm", "returns", "fits", "islands", "barrel", "marks")


@pytest.fixture
def split():
    return BoardIndex(split_board.board())


def test_every_draw_carries_the_stage3_keys_empty(index):
    empty = findings._empty_draw(findings.Draw("highlight", {}))
    assert {key: empty[key] for key in STAGE3_KEYS} == {
        "planes": [], "spans_nm": [], "covered_nm": [], "edges_nm": [], "returns": [], "fits": None,
        "islands": [], "barrel": {}, "marks": []}
    draw = only_draw(payload(index, finding({"tool": "highlight", "targets": ["U3"]})))
    assert all(draw[key] == empty[key] for key in STAGE3_KEYS)


def test_stage3_fields_are_checked():
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
    item = parse(raw(finding(*draws))).findings[0]
    assert sum("skipped" in p for p in item.problems) == 12 and len(item.draws) == 6
    default, named_planes, gap, path, islands, layer = (draw.fields for draw in item.draws)
    assert default == {"via": "uuid:v", "limit": 1.0, "planes": ()}
    assert named_planes["planes"] == (("GND", "In1.Cu"), ("A@B", "In2.Cu")) and named_planes["limit"] == 2
    assert gap == {"plane": ("GND", "In1.Cu"), "crossing": "net:A"}
    assert path == {"signal": "net:A", "plane": ("GND", "B.Cu")}
    assert islands == {"net": "GND", "layer": None} and layer["layer"] == "In2.Cu"
    assert any("planes[0] is not net@layer (e.g. GND@In1.Cu)" in p for p in item.problems)


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
def test_claimed_stage3_values_against_measured(values, keys):
    index = BoardIndex(split_board.with_return_via(split_board.board()))
    result = payload(index, finding({"tool": "layer_change", "via": "uuid:via-lvds-0"},
                                    {"tool": "return_path", "signal": "net:SATA_P", "plane": "GND@In1.Cu"},
                                    {"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:SATA_P"},
                                    values=values))
    assert [entry["key"] for entry in result["findings"][0]["disagree"]] == keys


def test_stage3_payload_is_json_and_framed_on_what_it_found(split):
    result = payload(split, finding({"tool": "layer_change", "via": "uuid:via-lvds-0"},
                                    {"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:SATA_P"},
                                    {"tool": "return_path", "signal": "net:USB_D+", "plane": "GND@In1.Cu"},
                                    {"tool": "plane_islands", "net": "GND"}))
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    keys = set(findings._empty_draw(findings.Draw("highlight", {})))
    assert all(set(draw) == keys and draw["ok"] for draw in result["findings"][0]["draws"])
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


def test_stage3_wave2_fields_are_checked():
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
    item = parse(raw(finding(*draws))).findings[0]
    assert sum("skipped" in p for p in item.problems) == 13 and len(item.draws) == 4
    edge, pour, pad, spots = (draw.fields for draw in item.draws)
    assert edge == {"along": "edge", "net": "GND", "spacing": 5.0, "max": None}
    assert pour == {"along": ("GND", "In1.Cu"), "net": "GND", "spacing": 5.0, "max": 8.0}
    assert pad == {"via": None, "pad": "J1.1"}
    assert spots == {"net": "A", "at": ("pt:1.5,-2", "pt:3,4@F.Cu", "pt:0,2")}
    for text in ("along edge needs net: the stitching net (e.g. GND)", "net VCC is not the pour's net GND",
                 "spacing_mm is not a positive number in mm", "give via or pad (one of them)",
                 "at is not a list of 1 to 20 points or targets", "at[0] is not [x, y]"):
        assert any(text in p for p in item.problems), text


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


def test_stage3_wave2_payload_is_json_and_framed(stitched):
    result = payload(stitched, finding({"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 5},
                                       {"tool": "stub", "via": "uuid:st-0"},
                                       {"tool": "discontinuity", "net": "USB_D+", "at": [[50, 10]]}))
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    keys = set(findings._empty_draw(findings.Draw("highlight", {})))
    assert all(set(draw) == keys and draw["ok"] for draw in result["findings"][0]["draws"])
    alone = payload(stitched, finding({"tool": "stub", "via": "uuid:st-0"}))
    x0, y0, x1, y1 = alone["findings"][0]["bbox_nm"]
    assert x0 <= 5 * MM <= x1 and y0 <= 2 * MM <= y1 and x1 - x0 < MM  # the via
    spots = payload(stitched, finding({"tool": "discontinuity", "net": "USB_D+", "at": [[50, 10], "uuid:usb-0"]}))
    assert spots["findings"][0]["key"] == finding_key("test.check", ["net:USB_D+@F.Cu~50,10"])  # each point keyed
