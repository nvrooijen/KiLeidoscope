"""KiCad's DRC on the LLM's suggestions: ghosts written into a board copy, KiCad's report
mapped back to them, the payload's verdict, and the watcher that runs it in the background."""

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from kileido_bridge import drc_findings, findings, findings_watch, ghost_drc, kls_folder
from kileido_bridge.board_index import BoardIndex
from kileido_bridge.ghost_drc import Ghost, Outcome, Verdict
from kileido_bridge.scrub import _nodes
from test_board_index import board
from test_findings import finding, only_draw, payload
from test_live_link import fixture

MM = 1_000_000
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "drc" / "drc_violations.kicad_pcb"
TEXT = FIXTURE.read_text(encoding="utf-8")
CLI = drc_findings.find_kicad_cli()
UUID = r'\(uuid "([0-9a-f-]{36})"\)'

OLD = """(kicad_pcb
\t(version 20241229)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(1 "F.Mask" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(segment
\t\t(start 0 0)
\t\t(end 1 0)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(net 1)
\t\t(uuid "00000000-0000-4000-8000-000000000001")
\t)
)
"""


def loads(text):
    """The text is one balanced (kicad_pcb ...) node, nothing outside it."""
    tops = [(start, end, name) for start, end, name, depth in _nodes(text) if depth == 0]
    assert [name for _, _, name in tops] == ["kicad_pcb"] and text[tops[0][1]:].strip() == "", tops
    return text


def via(n, x, y, net="GND", size=600_000, finding_index=None):
    return Ghost(n if finding_index is None else finding_index, 0, "via", net, ((x, y),), "F.Cu", size)


# writing the copy

def test_each_kind_goes_into_the_copy_with_fresh_uuids():
    ghosts = [via(0, 21 * MM, 8_500_000),
              Ghost(1, 0, "track", "SIG_B", ((14 * MM, 14 * MM), (14 * MM, 14 * MM), (14 * MM, 17 * MM),
                                              (16 * MM, 17 * MM)), "F.Cu", 0, 250_000),
              Ghost(2, 0, "pad", "GND", ((5 * MM, 5 * MM),), "B.Cu", 800_000, rotation=30),
              Ghost(3, 0, "cap", "", ((5 * MM, 16 * MM),), "F.Cu", 1_000_000, rotation=90),
              Ghost(4, 0, "via_row", "GND", ((3 * MM, 2_500_000), (5 * MM, 2_500_000)), "F.Cu", 400_000),
              Ghost(5, 0, "part", "", ((10 * MM, 12 * MM),), "F.Cu", rotation=90, part="R1")]
    copy, placement = ghost_drc.build(TEXT, ghosts)
    loads(copy)
    assert placement.skipped == {}
    assert copy.startswith(TEXT[:TEXT.index('\t(footprint "Fixture:R"')])  # the board is as it was up to R1
    added = copy[copy.index("(embedded_fonts no)"):]
    assert '\t(via\n\t\t(at 21 8.5)\n\t\t(size 0.6)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "GND")' \
        in added  # the board's 0.6 mm vias have a 0.3 mm drill
    assert added.count("(segment") == 2 and "(start 14 14)\n\t\t(end 14 17)" in added  # no zero-length piece
    assert '(width 0.25)\n\t\t(layer "F.Cu")\n\t\t(net "SIG_B")' in added
    assert '(footprint "KLS:ghost_pad"\n\t\t(layer "B.Cu")' in added and '(at 5 5 30)' in added
    assert '(pad "1" smd rect\n\t\t\t(at 0 0 30)\n\t\t\t(size 0.8 0.8)\n\t\t\t(layers "B.Cu" "B.Mask")' in added
    assert '(footprint "KLS:ghost_cap"' in added and "(start -0.5 -0.25)" in added and '(layer "F.CrtYd")' in added
    assert added.count("(drill 0.3)") == 3  # the 0.4 mm row: as the board's 0.4 mm via
    r1 = copy[copy.index('(footprint "Fixture:R"'):copy.index('(footprint "Fixture:J"')]
    assert "(at 10 12 90)" in r1 and '(at -0.8 0 90) (size 0.8 0.9)' in r1 and "(at 0 -1.2 90)" in r1
    # every uuid written is fresh and names its ghost; a moved part keeps its own (R1's and its pads')
    written = {uid: owner for uid, owner in placement.members.items()}
    fresh = [uid for uid in re.findall(UUID, added)]
    assert len(fresh) == len(set(fresh)) == 1 + 2 + 2 + 2 + 2 and all(uid not in TEXT for uid in fresh)
    assert sorted(written[uid] for uid in fresh if written[uid][0] == 4) == [(4, 0), (4, 1)]
    assert written["d0c00000-0000-4000-8000-000000000100"] == (5, 0)  # R1
    assert written["d0c00000-0000-4000-8000-000000000103"] == (5, 0)  # its pad 1
    assert placement.nets == {0: "GND", 1: "SIG_B", 2: "GND", 4: "GND"}
    assert placement.drills[0] == (300_000, True)


def test_a_via_without_a_board_via_of_its_size_gets_the_usual_ring():
    copy, placement = ghost_drc.build(TEXT, [via(0, MM, MM, size=800_000)])
    assert placement.drills[0] == (500_000, False) and "(size 0.8)\n\t\t(drill 0.5)" in copy
    copy, placement = ghost_drc.build(OLD, [via(0, MM, MM, size=500_000)])
    assert placement.drills[0] == (250_000, False)  # no vias at all: half its size


def test_a_board_with_numbered_nets_gets_numbers_and_new_nets_declared():
    ghosts = [via(0, MM, MM), Ghost(1, 0, "pad", "GND", ((2 * MM, 2 * MM),), "F.Cu", 600_000),
              via(2, 3 * MM, 3 * MM, net="NEW (1)"), via(3, 4 * MM, 4 * MM, net="")]
    copy, placement = ghost_drc.build(OLD, ghosts)
    loads(copy)
    assert '\t(net 1 "GND")\n\t(net 2 "NEW (1)")\n\t(segment' in copy
    vias = re.findall(r"\t\(via\n.*?\n\t\)", copy, re.S)
    assert "(net 1)" in vias[0] and "(net 2)" in vias[1] and "(net" not in vias[2]
    assert '(layers "F.Cu" "F.Mask")\n\t\t\t(net 1 "GND")' in copy


@pytest.mark.parametrize("net", ['GND" (x', "A\\B", "line\nbreak", "bidi‮", "x" * 201])
def test_a_net_name_that_could_break_the_file_is_refused(net):
    copy, placement = ghost_drc.build(TEXT, [via(0, MM, MM, net=net), via(1, 2 * MM, MM)])
    loads(copy)
    assert "cannot be written into a board file" in placement.skipped[0] and 1 not in placement.skipped
    assert copy.count("\t(via\n") == TEXT.count("\t(via\n") + 1 and net not in copy
    assert {owner for owner in placement.members.values()} == {(1, 0)}


def test_ghosts_kicad_cannot_take_are_left_out_whole():
    ghosts = [Ghost(0, 0, "pad", "GND", ((MM, MM),), "In1.Cu", 600_000),
              Ghost(1, 0, "track", "GND", ((MM, MM), (2 * MM, MM)), "In2.Cu", 0, 200_000),
              Ghost(2, 0, "track", "NEWNET", ((MM, MM), (MM, MM)), "F.Cu", 0, 200_000),
              Ghost(3, 0, "part", "", ((MM, MM),), "F.Cu", part="R2"),  # two parts named R2
              Ghost(4, 0, "part", "", ((MM, MM),), "F.Cu", part="U99"),
              Ghost(5, 0, "part", "", ((MM, MM),), "F.Cu", part="J1"),
              Ghost(6, 0, "part", "", ((2 * MM, MM),), "F.Cu", part="J1")]
    copy, placement = ghost_drc.build(TEXT, ghosts)
    loads(copy)
    assert placement.skipped[0].startswith("KiCad has pads and parts on the outer layers only")
    assert placement.skipped[1] == "layer In2.Cu is not on this board"
    assert placement.skipped[2] == "the route has no length" and "NEWNET" not in copy
    assert placement.skipped[3] == "2 footprints are named R2"
    assert placement.skipped[4] == "part U99 is not in the board text"
    assert 5 not in placement.skipped and placement.skipped[6].startswith("another suggestion in this file moves J1")
    assert {gi for gi, _ in placement.members.values()} == {5}


def test_zones_a_moved_part_owns_move_with_it():
    text = TEXT.replace('\t\t(attr through_hole)\n', '\t\t(attr through_hole)\n\t\t(zone (net 0) (layer "F.Cu") '
                        '(uuid "d0c00000-0000-4000-8000-000000000210") (keepout (tracks not_allowed)) '
                        '(polygon (pts (xy 13 13) (xy 15 13) (xy 15 15)))\n\t\t)\n')
    copy, _ = ghost_drc.build(text, [Ghost(0, 0, "part", "", ((20 * MM, 14 * MM),), "F.Cu", rotation=90, part="J1")])
    zone = copy[copy.index("(zone (net 0)"):]
    assert "(pts (xy 19 15) (xy 19 13) (xy 21 13))" in zone  # turned 90 degrees about J1, then moved 6 mm


# reading KiCad's report

def item(uid, description, x=0.0, y=0.0):
    return {"uuid": uid, "description": description, "pos": {"x": x, "y": y}}


def entry(kind, description, *items, severity="error"):
    return {"type": kind, "description": description, "severity": severity, "items": list(items)}


def placed(*ghosts, text=TEXT):
    copy, placement = ghost_drc.build(text, list(ghosts))
    uids = {}
    for uid, owner in placement.members.items():
        uids.setdefault(owner, uid)
    return placement, uids


def test_rules_against_a_ghost_come_back_in_plain_words():
    placement, uid = placed(via(0, 21 * MM, 8_500_000), Ghost(1, 0, "track", "SIG_B", ((14 * MM, 14 * MM),
                                                                                      (14 * MM, 17 * MM)), "F.Cu", 0,
                                                                   250_000))
    track = item("d0c00000-0000-4000-8000-000000000603", "Track [SIG_A] on F.Cu, length 6,0000 mm")
    report = {"violations": [
        entry("clearance", "Clearance violation ( clearance 0,2000 mm; actual 0,0750 mm)",
              item(uid[(0, 0)], "Via [GND] on F.Cu - B.Cu"), track),
        entry("hole_clearance", "Hole clearance violation (board setup constraints hole clearance 0,2500 mm; "
              "actual 0,2250 mm)", track, item(uid[(0, 0)], "Via [GND] on F.Cu - B.Cu")),
        entry("via_dangling", "Via is not connected or connected on only one layer",
              item(uid[(0, 0)], "Via [GND] on F.Cu - B.Cu"), severity="warning"),
        entry("track_dangling", "Track has unconnected end", item(uid[(1, 0)], "Track [SIG_B] on F.Cu, length 3 mm")),
        entry("clearance", "Clearance violation (clearance 0.2 mm; actual 0.1 mm)", track,  # the board's own
              item("d0c00000-0000-4000-8000-000000000602", "Track [SIG_B] on F.Cu")),
        entry("silk_over_copper", "Silkscreen clipped", item(uid[(0, 0)], "Via [GND] on F.Cu - B.Cu")),  # not a fit
        {**entry("clearance", "Clearance violation (clearance 0.2 mm; actual 0.0 mm)", item(uid[(0, 0)], "Via [GND]"),
                 track), "excluded": True}],
        "unconnected_items": [entry("unconnected_items", "Missing connection between items",
                                    item(uid[(1, 0)], "Track [SIG_B] on F.Cu"), item("x", "Pad 2 [SIG_B] of R1"))]}
    outcome = ghost_drc.judge(report, placement)
    first, second = outcome.verdicts[(0, 0)], outcome.verdicts[(1, 0)]
    assert first.state == "done" and first.problems == (
        "clearance 0.20 mm needed, 0.075 mm (Track [SIG_A] on F.Cu)",
        "hole clearance 0.25 mm needed, 0.23 mm (Track [SIG_A] on F.Cu)")
    assert first.notes == ("joins no GND copper, with the zones refilled",
                           "with a 0.30 mm drill, as the board's vias of this size")
    assert first.bad == {0} and first.shorts == frozenset()
    assert second.problems == () and second.notes == ("an end reaches no SIG_B copper",)  # unconnected: left out


def test_a_ghost_kicad_renets_shorts_to_that_net():
    placement, uid = placed(via(0, 17 * MM, 8 * MM), Ghost(1, 0, "track", "SIG_C", ((5 * MM, MM), (5 * MM, 6 * MM)),
                                                           "F.Cu", 0, 250_000))
    report = {"violations": [
        entry("via_dangling", "Via is not connected or connected on only one layer",
              item(uid[(0, 0)], "Via [SIG_A] on F.Cu - B.Cu")),
        entry("shorting_items", "Items shorting two nets", item(uid[(1, 0)], "Track [SIG_C] on F.Cu"),
              item("d0c00000-0000-4000-8000-000000000601", "Track [SIG_A] on F.Cu, length 10 mm"))]}
    outcome = ghost_drc.judge(report, placement)
    renetted, shorted = outcome.verdicts[(0, 0)], outcome.verdicts[(1, 0)]
    assert renetted.problems == ("shorts to SIG_A (KiCad joins it to that net)",) and renetted.notes == ()
    assert renetted.shorts == {"SIG_A"} and renetted.bad == {0}
    assert shorted.problems == ("shorts to SIG_A",) and shorted.shorts == {"SIG_A"}


def test_net_names_with_brackets_are_not_taken_for_renetting():
    placement, uid = placed(via(0, MM, MM, net="D[3]"))
    report = {"violations": [entry("via_dangling", "Via is not connected", item(uid[(0, 0)], "Via [D[3]] on F.Cu"))]}
    verdict = ghost_drc.judge(report, placement).verdicts[(0, 0)]
    assert verdict.problems == () and verdict.notes == ("joins no D[3] copper, with the zones refilled",)


def test_entries_between_suggestions_of_different_findings_are_left_out():
    placement, uid = placed(via(0, MM, MM), via(1, MM + 300_000, MM),
                            Ghost(1, 1, "track", "GND", ((0, 0), (MM, 0)), "F.Cu", 0, 200_000))
    hole = "Hole to hole violation (hole to hole 0,2500 mm; actual 0,0000 mm)"
    report = {"violations": [entry("hole_to_hole", hole, item(uid[(0, 0)], "Via [GND]"),
                                   item(uid[(1, 0)], "Via [GND]")),
                             entry("clearance", "Clearance violation (clearance 0,2 mm; actual 0,1 mm)",
                                   item(uid[(1, 0)], "Via [GND]"), item(uid[(2, 0)], "Track [GND]"))]}
    verdicts = ghost_drc.judge(report, placement).verdicts
    assert verdicts[(0, 0)].problems == ()  # another finding's via: each is judged against the board
    assert verdicts[(1, 0)].problems == ("clearance 0.20 mm needed, 0.10 mm (Track [GND])",)  # its own track counts
    assert verdicts[(1, 1)].problems == ("clearance 0.20 mm needed, 0.10 mm (Via [GND])",)


def test_a_row_says_which_vias():
    row = Ghost(0, 0, "via_row", "GND", tuple((x * MM, 2_500_000) for x in range(1, 9)), "F.Cu", 600_000)
    placement, _ = placed(row)
    members = sorted(placement.members.items(), key=lambda pair: pair[1])
    clearance = "Clearance violation (clearance 0,2000 mm; actual 0,0750 mm)"
    report = {"violations": [entry("clearance", clearance, item(uid, "Via [GND]"),
                                   item("t", "Track [SIG_A] on F.Cu")) for uid, (_, member) in members if member != 2]}
    report["violations"].append(entry("via_dangling", "Via is not connected", item(members[2][0], "Via [GND]")))
    verdict = ghost_drc.judge(report, placement).verdicts[(0, 0)]
    assert verdict.problems == ("vias 1, 2, 4, 5, 6 and 2 more: clearance 0.20 mm needed, 0.075 mm (Track [SIG_A] "
                                "on F.Cu)",)
    assert verdict.notes[0] == "1 of 8 vias join no GND copper, with the zones refilled"
    assert verdict.bad == {0, 1, 3, 4, 5, 6, 7}


def test_skipped_ghosts_say_why_and_empty_runs_need_no_kicad():
    placement, _ = placed(Ghost(0, 0, "pad", "GND", ((MM, MM),), "In1.Cu", 600_000))
    assert not placement.members
    outcome = ghost_drc.check(None, TEXT, placement.ghosts, "kicad-cli", run=None)
    assert outcome.verdicts[(0, 0)] == Verdict("skipped", error=placement.skipped[0])


# the payload

def ghost_draw(kind="via", fits=True, problem="", touches=(), row=(), kept=()):
    draw = findings._empty_draw(findings.Draw("ghost", {}))
    draw.update(kind=kind, fits=fits, problem=problem, points=[[MM, MM, "F.Cu"]], net="GND",
                touches=[list(pair) for pair in touches], row=[list(spot) for spot in row])
    draw["kicad_drc"] = findings.kicad_drc_state(draw)
    draw["kicad_drc"]["kept"] = list(kept)
    if kept:
        draw["fits"], draw["problem"] = False, "; ".join(filter(None, (problem, *kept)))
    return draw


def source(*draws):
    other = findings._empty_draw(findings.Draw("highlight", {}))
    return {"findings": [{"key": "k", "draws": [other, *draws]}, {"key": "plain", "draws": [other]}]}


def test_pending_failed_and_done_on_the_payload():
    near = "clashes with SIG_A track (0.05 mm from its copper, 0.10 mm needed)"
    data = source(ghost_draw(fits=False, problem=near), ghost_draw())
    pending = ghost_drc.apply(data, None)
    a, b = pending["findings"][0]["draws"][1:]
    assert a["kicad_drc"]["state"] == b["kicad_drc"]["state"] == "pending"
    assert (a["fits"], a["problem"], b["fits"]) == (False, near, True)  # the quick check's, meanwhile
    assert pending["findings"][1] is data["findings"][1]  # rows without ghosts stay the same objects
    failed = ghost_drc.apply(pending, Outcome(error="Failed to load board"))
    a = failed["findings"][0]["draws"][1]
    assert a["kicad_drc"]["state"] == "failed" and a["kicad_drc"]["error"] == "Failed to load board"
    assert (a["fits"], a["problem"]) == (False, near)
    done = ghost_drc.apply(failed, Outcome({(0, 1): Verdict("done", notes=("joins no GND copper",)),
                                            (0, 2): Verdict("done", problems=("clearance 0.20 mm needed, 0.05 mm",))}))
    a, b = done["findings"][0]["draws"][1:]
    assert a["fits"] is True and a["problem"] == "" and a["kicad_drc"]["problems"] == []  # KiCad's rules allow it
    assert a["kicad_drc"]["notes"] == ["joins no GND copper", f"the quick check said: {near}; KiCad's rules allow it"]
    assert b["fits"] is False and b["problem"] == "KiCad DRC: clearance 0.20 mm needed, 0.05 mm"
    again = Outcome({(0, 1): Verdict("done", notes=("joins no GND copper",)),
                     (0, 2): Verdict("done", problems=("clearance 0.20 mm needed, 0.05 mm",))})
    assert ghost_drc.apply(done, again) is done  # nothing new: the same dict
    json.dumps(done)


def test_a_touch_stays_a_short_and_reasons_kicad_does_not_judge_stand():
    touch = ghost_draw(fits=False, problem="clashes with SIG_A track (0.00 mm from its copper, 0.10 mm needed)",
                       touches=[(0, "SIG_A")])
    reach = ghost_draw(kept=["1.50 mm from the via, outside its 1.00 mm limit"])
    shorted = ghost_draw(fits=False, problem="clashes", touches=[(0, "SIG_B")])
    outcome = Outcome({(0, 1): Verdict("done"), (0, 2): Verdict("done"),
                       (0, 3): Verdict("done", problems=("shorts to SIG_B",), shorts=frozenset({"SIG_B"}))})
    a, b, c = ghost_drc.apply(source(touch, reach, shorted), outcome)["findings"][0]["draws"][1:]
    assert a["fits"] is False and a["problem"] == "KiCad DRC: touches SIG_A copper: a short"
    assert b["fits"] is False and b["problem"] == "1.50 mm from the via, outside its 1.00 mm limit"
    assert c["kicad_drc"]["problems"] == ["shorts to SIG_B"]  # said once


def test_a_rows_vias_take_kicads_word():
    row = ghost_draw("via_row", fits=False, problem="1 of 3 vias clash",
                     row=[[0, 0, True], [MM, 0, False], [2 * MM, 0, True]])
    outcome = Outcome({(0, 1): Verdict("done", problems=("via 3: clearance",), bad=frozenset({2}))})
    judged = ghost_drc.apply(source(row), outcome)["findings"][0]["draws"][1]
    assert [spot[2] for spot in judged["row"]] == [True, True, False] and judged["fits"] is False
    back = ghost_drc.apply(source(row), Outcome(error="x"))["findings"][0]["draws"][1]
    assert [spot[2] for spot in back["row"]] == [True, False, True]  # the quick check's again


def test_a_caps_quick_reasons_stand_beside_kicads():
    cap = ghost_draw("cap", fits=False, problem="lands on R1.1 (SIG_A)")
    judged = ghost_drc.apply(source(cap), Outcome({(0, 1): Verdict("done")}))["findings"][0]["draws"][1]
    assert judged["fits"] is False and judged["problem"] == "lands on R1.1 (SIG_A)"


def test_resolve_marks_ghosts_with_the_quick_verdict_and_touches():
    index = BoardIndex(board())
    on = only_draw(payload(index, finding({"tool": "ghost", "kind": "via", "at": "pt:15,10@F.Cu", "net": "GND"})))
    assert on["fits"] is False and on["net"] == "GND" and on["touches"] == [[0, "SIG"]]
    assert on["kicad_drc"] == {"state": "skipped", "problems": [], "notes": [], "error": "", "quick_fits": False,
                               "quick_problem": on["problem"], "quick_row": [], "kept": []}
    near = only_draw(payload(index, finding({"tool": "ghost", "kind": "via", "at": "pt:15,10.45@F.Cu", "net": "GND",
                                             "size": 0.6})))  # 0.05 mm from the track
    assert near["fits"] is False and near["touches"] == []  # close, not touching
    label = only_draw(payload(index, finding({"tool": "label", "at": "U3", "text": "x"})))
    assert label["kicad_drc"] is None and label["touches"] == []
    collected = ghost_drc.collect(payload(index, finding({"tool": "ghost", "kind": "via", "at": "pt:15,10@F.Cu",
                                                         "net": "GND"})))
    assert collected == [Ghost(0, 0, "via", "GND", ((15 * MM, 10 * MM),), "F.Cu", findings.GHOST_VIA_NM)]


# KiCad itself

@pytest.mark.skipif(not CLI, reason="no kicad-cli (set KILEIDO_KICAD_CLI)")
def test_kicad_judges_ghosts_on_the_drc_fixture(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text(TEXT, encoding="utf-8")
    folder = kls_folder.KlsFolder(str(board))
    ghosts = [via(0, 21 * MM, 8_500_000),  # 0.075 mm from the SIG_A track
              via(1, 17 * MM, 8 * MM),  # on the SIG_A track: KiCad renets it
              via(2, 5 * MM, 10 * MM),  # nothing near: joins no GND copper
              Ghost(3, 0, "track", "SIG_B", ((14 * MM, 14 * MM), (14 * MM, 17 * MM)), "F.Cu", 0, 250_000),
              Ghost(4, 0, "cap", "", ((20_600_000, 16 * MM),), "F.Cu", 1_000_000),  # on the two R2s' courtyards
              Ghost(5, 0, "part", "", ((10 * MM, 12 * MM),), "F.Cu", rotation=90, part="R1")]
    try:
        outcome = ghost_drc.check(folder, TEXT, ghosts, CLI)
    except drc_findings.DrcFailed as error:  # a kicad-cli older than the fixture's KiCad 10
        pytest.skip(f"this kicad-cli cannot load the KiCad 10 fixture: {error}")
    verdicts = outcome.verdicts
    assert verdicts[(0, 0)].problems == ("clearance 0.20 mm needed, 0.075 mm (Track [SIG_A] on F.Cu)",
                                         "hole clearance 0.25 mm needed, 0.23 mm (Track [SIG_A] on F.Cu)")
    assert verdicts[(1, 0)].problems == ("shorts to SIG_A (KiCad joins it to that net)",)
    assert verdicts[(2, 0)].problems == () and verdicts[(2, 0)].notes[0].startswith("joins no GND copper")
    assert verdicts[(3, 0)].problems == () and verdicts[(3, 0)].notes == ("an end reaches no SIG_B copper",)
    assert verdicts[(4, 0)].problems and all(line.startswith("courtyard overlaps Footprint R2")
                                             for line in verdicts[(4, 0)].problems)
    assert verdicts[(5, 0)] == Verdict("done")
    runs = folder.root / "runs"
    (run,) = runs.iterdir()
    assert json.loads((run / "run.json").read_text(encoding="utf-8"))["kind"] == ghost_drc.RUN_KIND
    assert folder.runs() == {}  # no answer echoes a ghost run


# the watcher

class Gate:
    """A fake kicad-cli run: records the copy, waits until let through, answers `report(text)`."""

    def __init__(self, report=lambda text: {}):
        self.report, self.texts, self.open = report, [], threading.Event()
        self.open.set()

    def __call__(self, cli, run_dir):
        text = (Path(run_dir) / "board.kicad_pcb").read_text(encoding="utf-8")
        self.texts.append(text)
        assert self.open.wait(10)
        return self.report(text)


def clearance(text):
    """A clearance on the last via written (a ghost's)."""
    uid = re.findall(r'\t\(via\n.*?\(uuid "([^"]+)"\)', text, re.S)[-1]
    return {"violations": [entry("clearance", "Clearance violation (clearance 0,2000 mm; actual 0,0500 mm)",
                                 item(uid, "Via [GND] on F.Cu - B.Cu"), item("t", "Track [SIG] on F.Cu"))]}


BOARD_TEXT = "(kicad_pcb (version 20250101))\n"


def watched(tmp_path, run, cli="kicad-cli"):
    project = tmp_path / "project"
    project.mkdir(parents=True)
    board = project / "board.kicad_pcb"
    board.write_text(BOARD_TEXT, encoding="utf-8")
    watch = findings_watch.FindingsWatch(find_cli=lambda hint: cli, run_ghost_drc=run)
    watch.CHECK_S = watch.SETTLE_S = watch.RESOLVE_DEBOUNCE_S = 0.0
    watch.target(str(board))
    folder = watch.folder
    prompt = folder.new_run(BOARD_TEXT, "llm")  # the run the answer echoes: drawn without an accept
    data = {"format": "kls-findings 1", "run": prompt.id, "stamp": prompt.stamp, "tool": "t", "findings": [
        {"check": "plane.return", "severity": "warning", "title": "Return via",
         "draw": [{"tool": "ghost", "kind": "via", "at": "pt:20,2@F.Cu", "net": "GND"}]}]}
    folder.ensure()
    (folder.root / "board.kls-findings.json").write_text(json.dumps(data), encoding="utf-8")
    os.utime(folder.root / "board.kls-findings.json", (time.time() - 5, time.time() - 5))
    return watch, folder, prompt


def ghost_of(watch):
    _, payload = watch.result()
    rows = payload["sources"]["file"]["findings"] if payload else []
    return rows[0]["draws"][0] if rows and rows[0]["draws"] else None


def until(watch, snapshot, test, text=BOARD_TEXT, revision=1, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        watch.tick(time.monotonic(), snapshot, revision, kls_folder.stamp(text), {}, text)
        draw = ghost_of(watch)
        if draw is not None and test(draw):
            return draw
        time.sleep(0.01)
    raise AssertionError(f"not reached: {ghost_of(watch)}")


def test_the_watcher_shows_pending_then_kicads_verdict(tmp_path):
    gate = Gate(clearance)
    gate.open.clear()
    watch, folder, prompt = watched(tmp_path, gate)
    snapshot = fixture()
    draw = until(watch, snapshot, lambda d: d["kicad_drc"]["state"] == "pending")
    quick = draw["fits"]
    gate.open.set()
    draw = until(watch, snapshot, lambda d: d["kicad_drc"]["state"] == "done")
    assert draw["fits"] is False
    assert draw["problem"] == "KiCad DRC: clearance 0.20 mm needed, 0.050 mm (Track [SIG] on F.Cu)"
    assert quick is True and len(gate.texts) == 1 and gate.texts[0].startswith("(kicad_pcb (version 20250101)")
    assert '(net "GND")' in gate.texts[0] and "(at 20 2)" in gate.texts[0]
    for _ in range(5):  # the same ghosts on the same board: never run again
        until(watch, snapshot, lambda d: d["kicad_drc"]["state"] == "done")
    assert len(gate.texts) == 1
    assert prompt.id in folder.runs()  # the answer's run stays known; the ghost run is not one
    kinds = [json.loads((run / "run.json").read_text(encoding="utf-8"))["kind"] for run in folder.runs_dir.iterdir()]
    assert sorted(kinds) == ["ghost_drc", "llm"]


def test_a_board_edit_drops_a_stale_result(tmp_path):
    answers = []

    def report(text):
        answers.append(text)
        return clearance(text) if len(answers) == 1 else {}  # the first board: a clash; the edited one: clear

    gate = Gate(report)
    gate.open.clear()
    watch, _, _ = watched(tmp_path, gate)
    snapshot = fixture()
    until(watch, snapshot, lambda d: d["kicad_drc"]["state"] == "pending")
    edited = BOARD_TEXT.replace("(version", "(generator \"pcbnew\") (version")
    # the board is edited while KiCad works on the first text: the payload is resolved on it, still pending
    until(watch, snapshot, lambda d: d["kicad_drc"]["state"] == "pending"
          and watch._ghost_key["file"][0] == kls_folder.stamp(edited), text=edited, revision=2)
    gate.open.set()
    draw = until(watch, snapshot, lambda d: d["kicad_drc"]["state"] == "done", text=edited, revision=2)
    assert draw["fits"] is True and draw["problem"] == ""  # the edited board's verdict, never the stale clash
    assert len(gate.texts) == 2 and gate.texts[1].startswith(edited.rstrip()[:-1])


def test_without_kicad_cli_or_when_it_fails_the_quick_verdict_stays(tmp_path):
    watch, _, _ = watched(tmp_path, Gate(), cli="")
    draw = until(watch, fixture(), lambda d: d["kicad_drc"]["state"] == "failed")
    assert draw["kicad_drc"]["error"] == ghost_drc.NO_CLI and draw["fits"] is True

    def broken(cli, run_dir):
        raise drc_findings.DrcFailed("Failed to load board")
    watch, _, _ = watched(tmp_path / "second", broken)
    draw = until(watch, fixture(), lambda d: d["kicad_drc"]["state"] == "failed")
    assert draw["kicad_drc"]["error"] == "Failed to load board" and draw["fits"] is True and draw["problem"] == ""


def test_a_ghost_run_is_judged_by_the_projects_own_rules(tmp_path):
    (tmp_path / "x.kicad_pcb").write_text("saved", encoding="utf-8")
    (tmp_path / "x.kicad_pro").write_text('{"board": {"design_settings": {"rules": {"min_clearance": 0.15}}}}',
                                          encoding="utf-8")
    (tmp_path / "x.kicad_dru").write_text("(version 1)", encoding="utf-8")
    folder = kls_folder.KlsFolder(str(tmp_path / "x.kicad_pcb"))
    seen = []

    def run(cli, run_dir):
        seen.append(sorted(path.name for path in Path(run_dir).iterdir()))
        return clearance((Path(run_dir) / "board.kicad_pcb").read_text(encoding="utf-8"))
    outcome = ghost_drc.check(folder, TEXT, [via(0, MM, MM)], "kicad-cli", run=run, lock=threading.Lock())
    assert seen == [["board.kicad_dru", "board.kicad_pcb", "board.kicad_pro", "run.json"]]
    assert outcome.verdicts[(0, 0)].problems == ("clearance 0.20 mm needed, 0.050 mm (Track [SIG] on F.Cu)",)


def test_ghost_runs_keep_their_own_count(tmp_path):
    (tmp_path / "x.kicad_pcb").write_text("saved", encoding="utf-8")
    folder = kls_folder.KlsFolder(str(tmp_path / "x.kicad_pcb"))
    prompt = folder.new_run("(kicad_pcb)", "llm")
    made = [folder.new_run(f"(kicad_pcb {i})", ghost_drc.RUN_KIND).id for i in range(6)]
    assert sorted(int(p.name) for p in folder.runs_dir.iterdir()) == [1, int(made[-2]), int(made[-1])]
    assert folder.runs() == {prompt.id: prompt.stamp}
    for i in range(5):
        folder.new_run(f"(kicad_pcb drc {i})", "drc")
    names = sorted(int(p.name) for p in folder.runs_dir.iterdir())
    assert int(made[-1]) in names and 1 not in names and len(names) == 2 + kls_folder.KEEP_RUNS
