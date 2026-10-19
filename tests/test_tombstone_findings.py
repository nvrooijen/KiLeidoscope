"""The tombstone check as findings (kileido_bridge/tombstone_findings.py): what the DRC
column's Tombstone button lists, as a file the findings reader takes whole."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_tombstone as boards  # noqa: E402  (its synthetic two-pad boards)

from kileido_bridge import findings, model, tombstone, tombstone_findings  # noqa: E402
from kileido_bridge.board_index import BoardIndex  # noqa: E402

STAMP = "abcd1234"
GND_TRACE = model.Track("t", "F.Cu", "GND", (boards.PITCH // 2, 0), (5 * boards.MM, 0), 200_000)


def convert(snapshot, stencil_nm=tombstone.DEFAULT_STENCIL_NM):
    checked, found = tombstone.check(snapshot, stencil_nm)
    return tombstone_findings.convert(checked, found, stencil_nm, STAMP)


def resolved(snapshot, data):
    """The file read back the way the findings watch reads it: parsed, its targets found."""
    parsed = findings.parse(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return findings.resolve_file(parsed, BoardIndex(snapshot), findings.Context("tombstone", {}, STAMP, {}))


def test_a_pad_in_a_plane_is_a_warning_with_the_pads_lit_and_the_part_lifted():
    snapshot = boards.board(tracks=[boards.SIG_TRACE], zones=[boards.SOLID])
    data = convert(snapshot)
    assert data["format"] == "kls-findings 1" and data["stamp"] == STAMP and data["run"] == ""
    assert data["tool"] == "Tombstone check; 1 part checked (01005, 0201, 0402); stencil 0.12 mm"
    (finding,) = data["findings"]
    assert finding["check"] == "tombstone.heat" and finding["severity"] == "warning"
    assert finding["confidence"] == "medium"  # the heat thresholds are a guess
    assert finding["title"].startswith("R1: pad 2 (GND) draws heat away") and finding["title"].endswith("pad 1 melts first")
    assert finding["message"].startswith("0402 at HIGH tombstone risk: pad 2 (GND) draws heat away")
    assert finding["message"].endswith("pad 1 melts first. The end on pad 2 would lift.")
    assert finding["fix"].startswith("Give the pad in the plane a thermal relief")
    assert finding["values"] == {"level": "HIGH", "package": "0402", "lifts": "pad 2"}
    highlight, label, lift = finding["draw"]
    assert highlight == {"tool": "highlight", "targets": ["uuid:a", "uuid:b"]}  # the part stands clear
    assert label == {"tool": "label", "at": "pt:0,0@F.Cu", "text": "tombstone HIGH: pad 2 lifts"}
    # Turned about pad 1's outer edge (0.25 mm out from its centre at -0.5 mm), pad 2's end rising
    assert lift == {"tool": "lift", "targets": ["uuid:R1"], "about": "pt:-0.75,0@F.Cu", "toward": "pt:0.5,0@F.Cu",
                    "angle_deg": 45}
    payload = resolved(snapshot, data)
    assert payload["status"] == "ok" and payload["tool"] == data["tool"] and not payload["problems"]
    (row,) = payload["findings"]
    assert row["check"] == "tombstone.heat" and not row["problems"]
    assert set(row["draws"][0]["ids"]) == {"a", "b"} and row["draws"][1]["tool"] == "label"
    lifted = row["draws"][2]
    assert lifted["tool"] == "lift" and lifted["ok"] and lifted["ids"] == ["R1"]
    assert lifted["points"] == [[-750_000, 0, "F.Cu"], [500_000, 0, "F.Cu"]] and lifted["rotation_deg"] == 45


def test_two_factors_are_both_told_and_the_worst_names_the_end_that_lifts():
    pads = (boards.pad("a", "SIG", -boards.PITCH // 2), boards.pad("b", "GND", boards.PITCH // 2, paste=0.7))
    data = convert(boards.board(pads=pads, tracks=[boards.SIG_TRACE], zones=[boards.SOLID]))
    (finding,) = data["findings"]
    assert finding["check"] == "tombstone.heat" and finding["confidence"] == "medium"
    assert finding["message"].startswith("0402 at HIGH tombstone risk, 2 factors raised: HIGH: pad 2 (GND) draws heat")
    assert "; HIGH: pad 1 has 2.04x the paste of the other." in finding["message"]
    assert finding["message"].endswith("The end on pad 2 would lift.")
    # An open via alone: that end's solder drains, so that end lifts, about the other pad's outer edge
    via = model.Via("v", "SIG", (-boards.PITCH // 2, 0), 400_000, 200_000, "F.Cu", "B.Cu", model.FROM_RULES)
    data = convert(boards.board(tracks=[boards.SIG_TRACE, GND_TRACE], vias=[via]))
    (finding,) = data["findings"]
    assert finding["check"] == "tombstone.via_in_pad" and finding["confidence"] == "high"
    assert finding["message"] == ("0402 at HIGH tombstone risk: open via in pad 1 only: solder drains into it. "
                                  "The end on pad 1 would lift.")
    assert finding["fix"] == "Fill or cap the via, or move it out of the pad."
    assert finding["draw"][2]["about"] == "pt:0.75,0@F.Cu" and finding["draw"][2]["toward"] == "pt:-0.5,0@F.Cu"


def test_medium_risk_is_info_lifted_less_and_a_dry_pad_lifts():
    relief = convert(boards.board(tracks=[boards.SIG_TRACE], zones=[boards.RELIEVED], graphics=boards.SPOKES))
    (finding,) = relief["findings"]
    assert finding["severity"] == "info" and finding["values"]["level"] == "MEDIUM"
    assert finding["draw"][1]["text"] == "tombstone MEDIUM: pad 2 lifts" and finding["draw"][2]["angle_deg"] == 25
    pads = (boards.pad("a", "SIG", -boards.PITCH // 2), boards.pad("b", "GND", boards.PITCH // 2, paste=0))
    dry = convert(boards.board(pads=pads, tracks=[boards.SIG_TRACE, GND_TRACE]))
    (finding,) = dry["findings"]
    assert finding["check"] == "tombstone.paste" and finding["values"]["lifts"] == "pad 2"
    assert finding["message"] == "0402 at HIGH tombstone risk: paste on pad 1 only. The end on pad 2 would lift."


def test_a_factor_that_singles_out_no_end_lights_the_part_instead_of_lifting_it():
    def pad(pad_id, number, net, x):  # 0.8 x 0.9 mm pads: far over an 0402's nominal
        shape = ((boards.rect(x, 0, 800_000, 900_000),),)
        return model.Pad(pad_id, "R1", number, net, (x, 0), None, {"F.Cu": shape, "F.Paste": shape})
    pads = (pad("a", "1", "SIG", -boards.PITCH // 2), pad("b", "2", "GND", boards.PITCH // 2))
    data = convert(boards.board(pads=pads, tracks=[boards.SIG_TRACE, GND_TRACE]))
    (finding,) = data["findings"]
    assert finding["check"] == "tombstone.pad_size" and finding["values"]["lifts"] == "either end"
    highlight, label = finding["draw"]
    assert highlight["targets"] == ["uuid:R1", "uuid:a", "uuid:b"] and label["text"] == "tombstone HIGH"


def test_a_clean_board_lists_nothing_but_says_how_many_parts_were_checked():
    data = convert(boards.board(tracks=[boards.SIG_TRACE, GND_TRACE]), stencil_nm=100_000)
    assert data["findings"] == []
    assert data["tool"] == "Tombstone check; 1 part checked (01005, 0201, 0402); stencil 0.1 mm"
