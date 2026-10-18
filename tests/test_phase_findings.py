"""Dynamic phase as findings (kileido_bridge/phase_findings.py): what the DRC column's
Dynamic phase button lists, as a file the findings reader takes whole."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase_boards as boards  # noqa: E402

from kileido_bridge import findings, phase, phase_findings, protocol  # noqa: E402
from kileido_bridge.board_index import BoardIndex  # noqa: E402

STAMP = "abcd1234"


def convert(snapshot, settings=phase.Settings()):
    results, warnings = phase.analyze_board(snapshot, boards.delay_model(), settings)
    return phase_findings.convert(results, warnings, settings, protocol.layer_heights_nm(snapshot)[0], STAMP)


def resolved(snapshot, data):
    """The file read back the way the findings watch reads it: parsed, its targets found."""
    parsed = findings.parse(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return findings.resolve_file(parsed, BoardIndex(snapshot), findings.Context("phase", {}, STAMP, {}))


def test_an_excursion_is_a_warning_with_its_copper_lit_and_a_label_at_its_start():
    snapshot = boards.compensated_far()
    data = convert(snapshot)
    assert data["format"] == "kls-findings 1" and data["stamp"] == STAMP and data["run"] == ""
    assert data["tool"] == "Dynamic phase; 1 pair measured; markers above 1 ps for 5 mm"
    (finding,) = data["findings"]
    assert finding["check"] == "phase.excursion" and finding["severity"] == "warning"
    assert "P and N" in finding["title"] and "apart over" in finding["title"]
    assert "walked from J1.1/2" in finding["message"] and "End-to-end skew" in finding["message"]  # J1: the start end
    assert set(finding["values"]) == {"peak_ps", "length_mm", "at_mm", "skew_ps"}
    assert finding["values"]["length_mm"] > 40 and abs(finding["values"]["skew_ps"]) < 0.01
    highlight, label = finding["draw"]
    assert highlight["tool"] == "highlight" and len(highlight["targets"]) >= 2
    assert all(target.startswith("uuid:t-") for target in highlight["targets"])  # both sides' tracks along the stretch
    assert label["tool"] == "label" and label["at"].endswith("@F.Cu") and " ps over " in label["text"]
    payload = resolved(snapshot, data)
    assert payload["status"] == "ok" and payload["tool"] == data["tool"] and not payload["problems"]
    (row,) = payload["findings"]
    assert row["check"] == "phase.excursion" and not row["problems"]
    lit = row["draws"][0]
    assert lit["tool"] == "highlight" and set(lit["ids"]) == {t[5:] for t in highlight["targets"]}
    assert row["draws"][1]["tool"] == "label"


def test_a_short_is_an_error_and_errors_come_first():
    snapshot = boards.shorted()
    data = convert(snapshot)
    checks = [finding["check"] for finding in data["findings"]]
    assert checks[0] == "phase.short" and data["findings"][0]["severity"] == "error"
    short = data["findings"][0]
    assert "shorted" in short["title"] and short["values"] == {"places": 1}
    highlight, label = short["draw"]
    assert len(highlight["targets"]) == 2 and label["text"] == "P touches N" and label["at"].endswith("@F.Cu")
    assert data["tool"].startswith("Dynamic phase; 0 pairs measured")  # a shorted pair has no phase
    payload = resolved(snapshot, data)
    assert payload["findings"][0]["severity"] == "error" and not payload["findings"][0]["problems"]


def test_a_loop_a_branch_and_an_unfinished_side_are_listed_too():
    looped = convert(boards.looped())
    (loop,) = [f for f in looped["findings"] if f["check"] == "phase.loop"]
    assert loop["severity"] == "warning" and "loops beside the P route" in loop["title"]
    assert loop["draw"][0]["targets"] and loop["draw"][1]["tool"] == "label" and loop["draw"][1]["text"].endswith(" mm loop")
    stub = convert(boards.esd_stub())
    branches = [f for f in stub["findings"] if f["check"] == "phase.branch"]
    assert len(branches) == 2  # the ESD diode hangs off both sides
    assert all(b["severity"] == "info" and "branches to D1" in b["title"] and b["draw"][0]["targets"] for b in branches)
    board = boards.ends(boards.Board()).path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (30, 1))
    unfinished = convert(board.snapshot())
    (open_side,) = [f for f in unfinished["findings"] if f["check"] == "phase.unrouted"]
    assert open_side["severity"] == "info" and "N side is not fully routed" in open_side["title"]


def test_a_matched_pair_gives_an_empty_file_that_still_names_what_was_measured():
    data = convert(boards.matched())
    assert data["findings"] == [] and data["tool"] == "Dynamic phase; 1 pair measured; markers above 1 ps for 5 mm"
    payload = resolved(boards.matched(), data)
    assert payload["status"] == "ok" and payload["findings"] == []
