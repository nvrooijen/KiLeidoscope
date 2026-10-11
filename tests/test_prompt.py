"""The review prompt (spec §3, §4): request.md, the clipboard text, a frozen LLM run."""

import json
import sys
import types

import pytest

from kileido_bridge import findings, prompt
from kileido_bridge.checks import CATALOGUE, QUICK, by_id
from kileido_bridge.kls_folder import KlsFolder, NotAvailable, stamp

TEXT = "(kicad_pcb (version 20250101)\r\n  (gr_text \"µ\")\n)\n"
ANSWER = r"C:\proj\.kileidoscope\demo.kls-findings.json"


def tools_offered(text: str) -> set[str]:
    section = text.split("## How to show each finding", 1)[1].split("## Answer format", 1)[0]
    return {line[2:].split(" [", 1)[0] for line in section.splitlines() if line.startswith("- ")}


def test_request_lists_what_kicads_drc_already_found():
    chosen = by_id(QUICK)
    text = prompt.request_md("7", "5a222dbd", ANSWER, chosen, drc={"drc.clearance": 3, "drc.unconnected_items": 2001})
    section = text.split("## Already found by KiCad's DRC", 1)[1].split("## Checks to run", 1)[0]
    assert "2004 entries (drc.unconnected_items 2001, drc.clearance 3)" in section  # most first
    assert "Do not report them again" in section
    many = {f"drc.kind_{i:02}": 1 for i in range(35)}
    assert "and 5 more kinds" in prompt.request_md("7", "5a222dbd", ANSWER, chosen, drc=many)


def test_request_says_when_drc_found_nothing_or_could_not_run():
    chosen = by_id(QUICK)
    clean = prompt.request_md("7", "5a222dbd", ANSWER, chosen, drc={})
    assert "## KiCad's DRC\nKiCad's DRC ran on this exact board and found nothing" in clean
    failed = prompt.request_md("7", "5a222dbd", ANSWER, chosen, drc="kicad-cli not found")
    assert "could not run for this request (kicad-cli not found)" in failed
    assert "DRC" not in prompt.request_md("7", "5a222dbd", ANSWER, chosen)  # none asked for: nothing said


def test_request_names_run_stamp_rules_and_every_chosen_check():
    chosen = by_id(QUICK + ("hs.stub", "fab.copper_balance"))
    text = prompt.request_md("7", "5a222dbd", ANSWER, chosen)
    assert text.startswith("# KiLeidoscope review request 7\n")
    assert "Run: 7   Stamp: 5a222dbd   Format: kls-findings 1" in text and f"Answer file: {ANSWER}" in text
    assert '"run": "7"' in text and '"stamp": "5a222dbd"' in text and f'"format": "{findings.FORMAT}"' in text
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Rules", "## Checks to run", "## How to show each finding", "## Answer format (JSON)"]
    for rule in ("Do not modify", "uuid:5a222dbd", "net:USB_D+@F.Cu~42.10,18.75", "skipped", "confidence",
                 "distance_mm", f"under {findings.MAX_TEXT} characters", "y points down"):
        assert rule in text, rule
    for check in chosen:
        assert f"- {check.id}: {check.instruction}\n  Evidence wanted: {check.evidence}\n" in text
    others = [check.id for check in CATALOGUE if check not in chosen]
    assert not any(f"- {check_id}:" in text for check_id in others)
    assert "Draw with: nothing" in text  # fab.copper_balance is board-wide
    assert "  Values: gap_mm, coverage_pct, length_mm\n  Draw with: plane_gap, return_path, highlight, area, label, " \
        "ghost\n" in text
    assert "  Values: stub_mm, length_mm\n  Draw with: stub, highlight, label\n" in text  # hs.stub
    assert "_cm2" in text and 'under\n  "Values"' in text


def test_checks_with_no_value_keys_list_none():
    text = prompt.request_md("1", "00000000", ANSWER, by_id(["emc.io_ground"]))
    assert "  Values:" not in text.split("## Checks to run", 1)[1]


def test_request_offers_only_drawn_tools_with_their_fields():
    every = prompt.request_md("1", "00000000", ANSWER, CATALOGUE)
    assert tools_offered(every) == set(findings.DRAWN) and "layers" not in findings.DRAWN
    assert "limit_is: min|max" in every and "mode: edge|centre" in every
    quick = prompt.request_md("1", "00000000", ANSWER, by_id(QUICK))
    assert tools_offered(quick) == {"highlight", "label", "area", "distance", "ghost", "plane_gap", "return_path",
                                    "layer_change"}
    stackup = prompt.request_md("1", "00000000", ANSWER, by_id(["plane.stackup"]))
    assert tools_offered(stackup) == {"highlight", "label"} and "Draw with: highlight\n" in stackup
    assert "No checks were chosen" in prompt.request_md("1", "00000000", ANSWER, ())


def test_every_drawn_tool_has_a_line_and_its_example_parses():
    assert findings.TOOLS <= set(prompt._TOOLS)
    for tool in prompt._TOOLS:
        for example in prompt.examples(tool):
            parsed = findings.parse(json.dumps({"format": findings.FORMAT, "findings": [
                {"check": "x.y", "severity": "info", "title": "t", "draw": [json.loads(example)]}]}).encode())
            assert parsed.findings[0].draws[0].tool == tool and not parsed.findings[0].problems, (tool, example)
    kinds = [json.loads(example)["kind"] for example in prompt.examples("ghost")]
    assert kinds == ["via", "part", "track", "via_row"]  # one example per kind beyond cap and pad
    assert prompt.drawing_tools_short(["layers", "nope"]) == ""


def test_stage2_tools_are_offered_with_the_checks_that_use_them():
    text = prompt.request_md("1", "00000000", ANSWER, by_id(["hs.bus_match", "fab.clearance", "emc.esd_far"]))
    assert tools_offered(text) == {"highlight", "label", "bars", "clearance", "edge_gap", "routed_length",
                                   "distance"}
    assert f"items: list of 1 to {findings.MAX_BARS}" in text and "mm|ps|mv|v|a|ma|pct" in text
    assert "not zones" in text and '"tool": "routed_length", "net": "net:CLK"' in text
    assert ("KiLeidoscope measures distance, clearance, edge_gap, routed_length on the board itself" in text
            and "(distance_mm, length_mm)" in text)
    stackup = prompt.request_md("1", "00000000", ANSWER, by_id(["plane.stackup"]))
    assert "KiLeidoscope measures" not in stackup  # nothing measured offered
    order = [line[2:].split(" [", 1)[0] for line in prompt.drawing_tools_short(findings.DRAWN).splitlines()
             if line.startswith("- ")]
    assert order[-12:] == ["clearance", "width", "routed_length", "edge_gap", "bars",  # spec §6.2 order,
                           "return_path", "plane_gap", "layer_change", "plane_islands",  # then §6.3
                           "stitching", "stub", "discontinuity"]


def test_stage3_tools_are_offered_with_the_checks_that_use_them():
    text = prompt.request_md("1", "00000000", ANSWER, by_id(["plane.gap_crossing", "plane.fragmented"]))
    assert tools_offered(text) == {"highlight", "label", "area", "plane_gap", "return_path", "plane_islands",
                                   "ghost"}  # a ghost track: a route that stays over the plane
    assert '"tool": "plane_gap", "plane": "GND@In1.Cu"' in text and "plane: net@layer" in text
    assert '"kind": "track"' in text and "ghost track (path, layer, net)" in text
    assert ("KiLeidoscope measures return_path, plane_gap on the board itself" in text
            and "(length_mm, gap_mm)" in text)
    vias = prompt.request_md("1", "00000000", ANSWER, by_id(["plane.layer_change_no_return"]))
    assert "layer_change" in tools_offered(vias) and "default 1.0" in vias
    assert "measures distance, layer_change on the board itself" in vias


def test_stage3_wave2_tools_are_offered_with_the_checks_that_use_them():
    text = prompt.request_md("1", "00000000", ANSWER, by_id(["plane.stitching_sparse", "plane.edge_unstitched"]))
    assert {"stitching", "ghost", "distance", "edge_gap"} <= tools_offered(text)
    assert '"tool": "stitching", "along": "GND@In1.Cu", "spacing_mm": 5' in text and "or edge" in text
    assert '"kind": "via_row"' in text and f"at most {findings.MAX_ROW_VIAS} vias" in text
    stub = prompt.request_md("1", "00000000", ANSWER, by_id(["hs.stub"]))
    assert tools_offered(stub) == {"stub", "highlight", "label"} and '"tool": "stub", "via": "uuid:5a222dbd"' in stub
    assert "KiLeidoscope measures stub on the board itself" in stub and "(stub_mm)" in stub
    step = prompt.request_md("1", "00000000", ANSWER, by_id(["hs.impedance_step"]))
    assert tools_offered(step) == {"discontinuity", "width", "highlight", "label"}
    assert f"at: list of 1 to {findings.MAX_SPOTS} points" in step and '"tool": "discontinuity"' in step


def test_clipboard_is_short_and_names_the_files():
    text = prompt.clipboard("7", "5a222dbd", r"C:\proj\.kileidoscope\runs\7", ANSWER)
    lines = text.splitlines()
    assert len(lines) <= 15 and lines[0].startswith("KiLeidoscope LLM Visualizer")
    assert "Run: 7   Stamp: 5a222dbd   Format: kls-findings 1" in text
    for name in (prompt.REQUEST, prompt.DIGEST, prompt.BOARD, r"C:\proj\.kileidoscope\runs\7", ANSWER):
        assert name in text, name
    assert "Read request.md first." in text and "Modify nothing else (the progress file excepted)." in text
    assert [line for line in lines if "progress file" in line][0] ==         "request.md also asks for a small progress file beside the answer; it is optional."
    assert "Notes from the user" not in text
    assert prompt.clipboard("7", "5a222dbd", "d", ANSWER, " \n\t\n") == prompt.clipboard("7", "5a222dbd", "d", ANSWER)


def test_notes_go_in_the_clipboard_before_its_last_line():
    text = prompt.clipboard("7", "5a222dbd", r"C:\proj\.kileidoscope\runs\7", ANSWER,
                            "Focus on the USB pair.\r\nIgnore J5 (test only).")
    assert text.rstrip("\n").splitlines()[-1].startswith("Read request.md first.")
    assert ("\n\nNotes from the user (the rules in request.md come first):\n"
            "Focus on the USB pair.\nIgnore J5 (test only).\n\nRead request.md first.") in text
    assert text.index(prompt.BOARD) < text.index("progress file") < text.index("Notes from the user")


def test_notes_are_plain_text_and_short():
    assert prompt.clean_notes("") == prompt.clean_notes(None) == prompt.clean_notes(5) == ""
    assert prompt.clean_notes("\n  \r\n") == ""
    assert prompt.clean_notes("\n a\x00b\x1b[31m\u202ec\t \r\nnext\rline \n") == "a b [31m c\nnext\nline"
    assert prompt.clean_notes("one\u2028two") == "one two"  # a line separator is no line break
    many = prompt.clean_notes("\n".join(f"line {n}" for n in range(12)))
    assert many.splitlines() == [f"line {n}" for n in range(7)] + ["line 7 line 8 line 9 line 10 line 11"]
    assert prompt.clean_notes("x" * 900) == "x" * prompt.MAX_NOTES
    assert prompt.clean_notes("a\n\nb") == "a\n\nb"  # a blank line inside stays


def test_request_asks_for_progress_and_ends_with_the_notes():
    progress = r"C:\proj\.kileidoscope\demo.kls-progress.json"
    text = prompt.request_md("7", "5a222dbd", ANSWER, by_id(QUICK), progress, "Focus on USB.\n\x07Skip J5.")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Rules", "## Checks to run", "## How to show each finding", "## Answer format (JSON)",
                        "## Progress", "## Notes from the user"]
    section = text.split("## Progress\n", 1)[1].split("\n## ", 1)[0]
    assert f"Progress file: {progress}\n" in section and "skip it if you were asked to keep token use low" in section
    assert ('{"format": "kls-progress 1", "run": "7", "stamp": "5a222dbd", "done": <checks finished>,\n'
            ' "total": <checks to run, here 3>, "check": "<id of the check you are on>",\n'
            ' "note": "<what you are doing now, under 120 characters>"}\n') in section
    assert "after each check" in section and "Besides the answer file, it is the only file you write." in section
    assert "answer file (and the\n  progress file below, if you keep one). Write it whole" in text
    assert text.endswith("\n## Notes from the user\nThe rules in request.md come first.\nFocus on USB.\n Skip J5.\n")
    bare = prompt.request_md("7", "5a222dbd", ANSWER, by_id(QUICK), progress)
    assert "Notes from the user" not in bare
    assert bare == prompt.request_md("7", "5a222dbd", ANSWER, by_id(QUICK), progress, "  \n")
    assert "## Progress" not in prompt.request_md("7", "5a222dbd", ANSWER, by_id(QUICK))


@pytest.fixture
def fake_digest(monkeypatch):
    calls = []
    module = types.ModuleType("kileido_bridge.digest")

    def build(snapshot, run, stamp, board_file="board.kicad_pcb"):
        calls.append((snapshot, run, stamp, board_file))
        return {"run": run, "stamp": stamp, "board": board_file, "units": "mm"}

    module.build = build
    module.dumps = lambda digest: json.dumps(digest, indent=1)
    monkeypatch.setitem(sys.modules, "kileido_bridge.digest", module)
    return calls


def test_make_run_writes_the_three_files_and_returns_the_prompt(tmp_path, fake_digest):
    (tmp_path / "demo.kicad_pcb").write_text("saved", encoding="utf-8")
    folder = KlsFolder(str(tmp_path / "demo.kicad_pcb"))
    snapshot = object()
    run, text = prompt.make_run(folder, TEXT, snapshot, by_id(QUICK), notes="Mind J5.")
    assert run.id == "1" and run.stamp == stamp(TEXT) and fake_digest == [(snapshot, "1", run.stamp, "board.kicad_pcb")]
    files = {path.name for path in run.directory.iterdir()}
    assert {prompt.BOARD, prompt.DIGEST, prompt.REQUEST} <= files and not any(n.endswith(".partial") for n in files)
    assert json.loads((run.directory / "run.json").read_text(encoding="utf-8"))["kind"] == "llm"
    assert json.loads((run.directory / prompt.DIGEST).read_text(encoding="utf-8"))["stamp"] == run.stamp
    answer = str(folder.path_for("file"))
    assert answer.endswith("demo.kls-findings.json") and folder.path_for("file").is_absolute()
    progress = str(folder.progress_path)
    assert progress.endswith("demo.kls-progress.json") and folder.progress_path.parent == folder.path_for("file").parent
    request = (run.directory / prompt.REQUEST).read_text(encoding="utf-8")
    assert request == prompt.request_md(run.id, run.stamp, answer, by_id(QUICK), progress, "Mind J5.")
    assert f"Progress file: {progress}" in request and request.endswith("\nMind J5.\n")
    assert text == prompt.clipboard(run.id, run.stamp, str(run.directory), answer, "Mind J5.")
    assert "(the rules in request.md come first):\nMind J5.\n" in text
    # an answer echoing the run and stamp is ours
    echoed = findings.parse(json.dumps({"format": findings.FORMAT, "run": run.id, "stamp": run.stamp,
                                        "findings": []}).encode())
    context = findings.Context(path=answer, source="file", runs=folder.runs(), accepted=frozenset(),
                               live_stamp=run.stamp, states={})
    assert findings.handshake(echoed, context) == "known"
    stale = findings.parse(json.dumps({"format": findings.FORMAT, "run": run.id, "stamp": "deadbeef"}).encode())
    assert findings.handshake(stale, context) == "unknown_run"


def test_make_run_needs_a_saved_board(fake_digest):
    with pytest.raises(NotAvailable):
        prompt.make_run(KlsFolder(""), TEXT, None, by_id(QUICK))
    assert fake_digest == []
