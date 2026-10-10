"""Findings files: validation limits, target resolution, measured distances, keys, the changed notice."""

import json
import math

import pytest

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
    return {"check": "test.check", "severity": "error", "title": "T",
            "message": "m", "draw": list(draws), **fields}


def context(source="file", live=STAMP, states=None):
    return Context("x.kls-findings.json", source, live, states or {})


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
    assert found.findings[0].values == {} and any("x_mm" in p for p in found.findings[0].problems)


# limits and plain text

def test_findings_and_draws_past_the_limits_are_dropped():
    found = parse(raw(*[finding() for _ in range(301)]))
    assert len(found.findings) == 300 and any("past 300" in p for p in found.problems)
    draws = [{"tool": "highlight", "targets": ["U3"]}] * 21
    found = parse(raw(finding(*draws)))
    assert len(found.findings[0].draws) == 20 and any("past 20" in p for p in found.findings[0].problems)


def test_text_is_cut_and_controls_become_spaces():
    found = parse(raw(finding(title="a" * 301, message="one\ntwo\x1b[31m‮red")))
    item = found.findings[0]
    assert len(item.title) == 300 and item.title.endswith("…")
    assert any("title cut" in p for p in item.problems)
    assert item.message == "one two [31m red"


def test_values_numbers_with_units_only():
    values = {"distance_mm": 0.2, "area_mm2": 3, "skew_ps": 1.5, "ok_mm": True, "note_mm": "about 2",
              "count": 4, "Bad-Key": 1, "lst_a": [1, 2], "via_density_per_cm2": 2.5}
    item = parse(raw(finding(values=values))).findings[0]
    assert item.values == {"distance_mm": 0.2, "area_mm2": 3.0, "skew_ps": 1.5, "via_density_per_cm2": 2.5}
    for key in ("ok_mm", "note_mm", "count", "lst_a"):
        assert f"value {key} is not a number with a unit; left out" in item.problems
    assert any("Bad-Key" in p for p in item.problems)
    many = {f"v{i}_mm": i for i in range(25)}
    item = parse(raw(finding(values=many))).findings[0]
    assert len(item.values) == 20 and any("past 20" in p for p in item.problems)


def test_bad_enums_fall_back():
    item = parse(raw(finding(severity="fatal"))).findings[0]
    assert item.severity == "warning" and len(item.problems) == 1
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
             {"tool": "area", "layer": "F.SilkS", "targets": ["U3"]},
             {"tool": "area", "layer": "F.Cu"},
             {"tool": "label", "at": "U3"},
             "highlight",
             {"tool": "highlight", "targets": ["U3"], "emphasis": "loud", "label": "ok"}]
    item = parse(raw(finding(*draws))).findings[0]
    assert len(item.draws) == 1 and item.draws[0].emphasis == "normal"
    assert sum("skipped" in p for p in item.problems) == len(draws) - 1
    assert any("unknown tool explode" in p for p in item.problems)


def test_valid_draw_fields_are_normalised():
    draws = [{"tool": "distance", "from": "U3", "to": "C12", "mode": "center", "limit": 0.2},
             {"tool": "area", "layer": "in1.cu", "targets": ["uuid:z-gnd"], "emphasis": "strong"}]
    distance, area = parse(raw(finding(*draws))).findings[0].draws
    assert distance.fields["mode"] == "centre" and distance.fields["limit_is"] == "max"
    assert area.fields == {"layer": "In1.Cu", "targets": ("uuid:z-gnd",)} and area.emphasis == "strong"


def test_header_fields():
    found = parse(raw(stamp="NOTHEX!!", run="r" * 40))
    assert found.stamp == "" and len(found.run) == 32
    assert any("stamp" in p for p in found.problems)
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


@pytest.mark.parametrize("limits, passes", [((5,), False), ((4,), True), ((4, 5), False), ((), False)])
def test_a_finding_its_measurements_do_not_back_up(index, limits, passes):
    """Every limit met (an edge gap of 1.91 mm against a 0.50 mm minimum): `passes`; Blender
    shows it within the limit, not as the problem. No limit given: nothing to judge."""
    draws = [{"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}", "limit": limit, "limit_is": "min"}
             for limit in limits] or [{"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{TRACK}"}]
    assert payload(index, finding(*draws))["findings"][0]["passes"] is passes  # 4.6 mm measured


def test_label_arrow_area(index):
    result = payload(index, finding(
        {"tool": "label", "at": "U3.4", "text": "pin 4", "label": "ignored here"},
        {"tool": "arrow", "from": "U3", "to": "C12.1"},
        {"tool": "area", "layer": "In1.Cu", "targets": ["uuid:z-gnd"]}))
    label, arrow, zone = result["findings"][0]["draws"]
    assert label["text"] == "pin 4" and label["points"] == [[10 * MM, 10 * MM, "F.Cu"]] and label["ids"] == ["p-u3-4"]
    assert arrow["points"] == [[10 * MM, 10 * MM, "F.Cu"], [20 * MM, 10 * MM, "F.Cu"]]
    assert zone["ids"] == ["z-gnd"] and zone["ok"] and zone["layer"] == "In1.Cu"
    x0, y0, x1, y1 = result["findings"][0]["bbox_nm"]
    assert (x0, y0) == (9 * MM, 9 * MM) and x1 == 20.5 * MM and y1 < 40 * MM  # parts and points; the 40 mm plane is not


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


def test_not_connected_lists_findings_with_a_note():
    result = payload(None, finding({"tool": "highlight", "targets": ["U3"]}))
    assert result["connected"] is False
    highlight = only_draw(result)
    assert highlight["targets"][0]["note"] == "KiCad not connected" and not highlight["ok"]
    assert highlight["problem"] == "KiCad not connected"
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
             {"tool": "clearance", "a": "U3", "b": "C12", "required": 0.2}]
    item = parse(raw(finding(*draws))).findings[0]
    assert [draw.tool for draw in item.draws] == ["clearance"]
    assert item.draws[0].fields == {"a": "U3", "b": "C12", "required": 0.2}
    assert sum("skipped" in p for p in item.problems) == len(draws) - 1


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


def test_every_measured_draw_names_what_it_measured(index):
    result = payload(index, finding({"tool": "distance", "from": "U3", "to": "C12", "label": "Cap reach"},
                                    {"tool": "distance", "from": "U3", "to": "C12"},
                                    {"tool": "highlight", "targets": ["U3"]}))
    assert [draw["what"] for draw in result["findings"][0]["draws"]] == ["Cap reach", "", ""]


def test_stage2_payload_is_json(index):
    result = payload(index, finding({"tool": "width", "target": "net:SIG", "required": 0.3},
                                    {"tool": "clearance", "a": f"uuid:{VIA}", "b": f"uuid:{TRACK}"},
                                    {"tool": "edge_gap", "target": "U3"}))
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    keys = set(findings._empty_draw(findings.Draw("highlight", {})))
    assert all(set(draw) == keys and draw["ok"] for draw in result["findings"][0]["draws"])
