"""KiCad DRC as findings (`kileido_bridge/drc_findings.py`): kicad-cli, dedupe, targets, numbers.

The fixture board (tests/fixtures/drc/drc_violations.kicad_pcb) is synthetic, written for
these tests with one deliberate violation of each kind; drc_violations.drc.json is
kicad-cli 10.0.3's report of it (`--format json --units mm --all-track-errors
--severity-error --severity-warning`). Its BoardIndex is rebuilt from the board text:
tracks and vias by `board_text.copper_items`, parts and pads by the small reader below
(every part in the fixture sits at 0°).
"""

import json
import math
import re
import shutil
import sys
from pathlib import Path

import pytest

from kileido_bridge import board_text, drc_findings, model
from kileido_bridge.board_index import BoardIndex
from kileido_bridge.drc_findings import (DrcFailed, convert, dedupe, drc_command, find_kicad_cli, parse_limits,
                                         run_drc)
from kileido_bridge.targets import resolve

MM = 1_000_000
FIXTURES = Path(__file__).parent / "fixtures" / "drc"
BOARD = FIXTURES / "drc_violations.kicad_pcb"
REPORT = FIXTURES / "drc_violations.drc.json"
KICAD_CLI = find_kicad_cli("")


def uid(n: int) -> str:
    return f"d0c00000-0000-4000-8000-{n:012x}"


_FOOTPRINT = re.compile(r'\n\t\(footprint "[^"]*"\n\t\t\(layer "([^"]+)"\)\n\t\t\(uuid "([^"]+)"\)\n'
                        r'\t\t\(at ([\d.-]+) ([\d.-]+)\)(.*?)\n\t\)', re.S)
_PAD = re.compile(r'\(pad "([^"]*)" (\w+) rect \(at ([\d.-]+) ([\d.-]+)\) \(size ([\d.]+) ([\d.]+)\)'
                  r'(?: \(drill ([\d.]+)\))? \(layers ([^)]*)\)(?: \(net "([^"]*)"\))? \(uuid "([^"]+)"\)\)')
_COURTYARD = re.compile(r'\(fp_rect \(start ([\d.-]+) ([\d.-]+)\) \(end ([\d.-]+) ([\d.-]+)\)')


def nm(text: str) -> int:
    return round(float(text) * MM)


def rect(x0, y0, x1, y1):
    return (((x0, y0), (x1, y0), (x1, y1), (x0, y1)),)


def snapshot(text: str) -> model.BoardSnapshot:
    tracks, arcs, vias = board_text.copper_items(text)
    footprints, pads = [], []
    for side, fid, fx, fy, body in _FOOTPRINT.findall(text):
        x, y = nm(fx), nm(fy)
        reference = re.search(r'\(property "Reference" "([^"]+)"', body).group(1)
        cx0, cy0, cx1, cy1 = (nm(v) for v in _COURTYARD.search(body).groups())
        footprints.append(model.Footprint(fid, reference, (x, y), 0.0, "top" if side == "F.Cu" else "bottom", (),
                                          (x + cx0, y + cy0, cx1 - cx0, cy1 - cy0)))
        for number, _, px, py, sx, sy, drill, layers, net, pid in _PAD.findall(body):
            px, py, hx, hy = x + nm(px), y + nm(py), nm(sx) // 2, nm(sy) // 2
            copper = ("F.Cu", "B.Cu") if '"*.Cu"' in layers else ("F.Cu",)
            pads.append(model.Pad(pid, fid, number, net, (px, py), (nm(drill), nm(drill)) if drill else None,
                                  {layer: (rect(px - hx, py - hy, px + hx, py + hy),) for layer in copper},
                                  "round" if drill else None))
    outline = model.Outline((rect(0, 0, 30 * MM, 20 * MM),))
    return model.BoardSnapshot("drc_violations", {"F.SilkS": "F.Silkscreen"}, tracks, arcs, vias, tuple(pads),
                               tuple(footprints), (), outline, model.Stackup(()), (), {})


@pytest.fixture(scope="module")
def index():
    return BoardIndex(snapshot(BOARD.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def report():
    return json.loads(REPORT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def converted(report, index):
    return convert(report, index, "4", "a1b2c3d4")


def by_check(converted, check):
    return [finding for finding in converted["findings"] if finding["check"] == f"drc.{check}"]


def tools(finding):
    return [draw["tool"] for draw in finding["draw"]]


def draw(finding, tool):
    return next(item for item in finding["draw"] if item["tool"] == tool)


def test_fixture_reader_finds_every_part_and_pad(index):
    snap = index.snapshot
    assert sorted(f.reference for f in snap.footprints) == ["J1", "R1", "R2", "R2", "TP1", "TP2"]
    assert len(snap.pads) == 7 and len(snap.tracks) == 6 and len(snap.vias) == 5


# file shape, dedupe, filter

def test_file_is_kls_findings(converted):
    assert converted["format"] == "kls-findings 1"
    assert (converted["run"], converted["stamp"]) == ("4", "a1b2c3d4")
    assert converted["tool"] == "KiCad DRC 10.0.3; 6 library notices left out"
    for finding in converted["findings"]:
        assert finding["severity"] in ("error", "warning")
        assert finding["check"].startswith("drc.") and len(finding["title"]) <= 300 and len(finding["message"]) <= 300
        assert all(re.fullmatch(r"[a-z0-9_]{1,40}", key) for key in finding["values"])
    json.dumps(converted)  # plain JSON


def test_dedupe_and_filter_counts(report, converted):
    entries = [*report["violations"], *report["unconnected_items"]]
    assert len(entries) == 43
    kept, dropped = dedupe(report)
    assert dropped == 6  # lib_footprint_issues, one per part
    assert len(kept) == 34  # the J1/via pair: clearance twice, hole clearance three times
    assert sorted(count for _, count in kept if count > 1) == [2, 3]
    assert len(converted["findings"]) == 34
    severities = [finding["severity"] for finding in converted["findings"]]
    assert severities == sorted(severities, key=drc_findings.SEVERITY_ORDER.get)  # errors first
    hole = [f for f in by_check(converted, "hole_clearance") if "J1" in f["draw"][0]["targets"][0]]
    assert hole and hole[0]["message"].endswith("(reported 3 times)")
    assert len(by_check(converted, "clearance")) == 3 and len(by_check(converted, "hole_clearance")) == 2


def test_excluded_and_library_entries_are_left_out(index):
    item = {"description": "Via [GND] on F.Cu - B.Cu", "pos": {"x": 24.0, "y": 12.0}, "uuid": uid(0x702)}
    report = {"violations": [{"type": "via_dangling", "severity": "warning", "description": "x", "items": [item],
                              "excluded": True},
                             {"type": "lib_footprint_mismatch", "severity": "warning", "description": "y",
                              "items": [item]}],
              "unconnected_items": [], "kicad_version": "10.0.3"}
    out = convert(report, index, "1", "00000000")
    assert out["findings"] == [] and out["tool"] == "KiCad DRC 10.0.3; 1 library notice left out"


# every target resolves on the board

def test_every_target_resolves(converted, index):
    for finding in converted["findings"]:
        for item in finding["draw"]:
            named = item.get("targets", []) + [item[key] for key in ("from", "to", "at", "a", "b", "target", "via")
                                               if key in item]
            assert named, finding["check"]
            for target in named:
                assert resolve(index, target).found, (finding["check"], target)


def test_target_naming(index):
    def target(n, description, x=0.0, y=0.0):
        return drc_findings._item(index, {"uuid": uid(n), "description": description,
                                          "pos": {"x": x, "y": y}}).target
    assert target(0x104, "Pad 2 [SIG_B] of R1 on F.Cu") == "R1.2"
    assert target(0x200, "Footprint J1") == "J1"
    assert target(0x300, "Footprint R2") == f"uuid:{uid(0x300)}"  # two parts named R2
    assert target(0x303, "Pad 1 of R2 on F.Cu") == f"uuid:{uid(0x303)}"  # R2.1 names two pads
    assert target(0x601, "Track [SIG_A] on F.Cu, length 10,0000 mm") == f"uuid:{uid(0x601)}"
    assert target(0x500, "Rectangle on Edge.Cuts") == "edge"
    assert target(0x501, "PCB text 'EDGE' on F.Silkscreen", 29, 10) == "pt:29,10@F.Silkscreen"
    assert target(0x999, "Zone [GND] on F.Cu, B.Cu and 2 more, priority 0", 1.25, -0.5) == "pt:1.25,-0.5@F.Cu"
    assert target(0x998, "PCB text 'on top on B.Cu' on F.Silkscreen", 1, 1) == "pt:1,1@F.Silkscreen"


def test_renamed_copper_layer_gets_its_canonical_name():
    snap = model.BoardSnapshot("t", {"F.Cu": "TOP", "Edge.Cuts": "Outline"}, (), (), (), (), (), (),
                               model.Outline(()), model.Stackup(()), (), {})
    index = BoardIndex(snap)
    assert drc_findings._item(index, {"uuid": "x", "description": "Zone [A] on TOP", "pos": {"x": 1, "y": 2}}
                              ).target == "pt:1,2@F.Cu"
    assert drc_findings._item(index, {"uuid": "x", "description": "Line on Outline", "pos": {"x": 1, "y": 2}}
                              ).target == "edge"


# numbers

def test_board_index_matches_kicad_actual(converted, index, report):
    """Every copper and hole gap KiCad reports, measured on the index: equal to 4 decimals."""
    checked = 0
    for entry, _ in dedupe(report)[0]:
        kind = entry["type"]
        limit, actual = parse_limits(kind, entry["description"])
        records = [index.records(item["uuid"]) for item in entry["items"]]
        if kind == "clearance":
            measured = index.closest(records[0], records[1])[0].distance
        elif kind == "copper_edge_clearance":
            copper = next(r for r in records if r)
            measured = index.closest(copper, (index.snapshot.outline,))[0].distance
        elif kind in ("hole_clearance", "hole_to_hole"):
            measured = drc_findings._hole_span(index, records[0][0], records[1][0], kind == "hole_to_hole")[0]
        else:
            continue
        assert measured / MM == pytest.approx(actual, abs=5e-5), entry["description"]
        checked += 1
    assert checked == 7  # 3 clearances, 2 hole clearances, hole to hole, edge


def test_hole_gaps_by_hand(index):
    via = {v.id: v for v in index.snapshot.vias}
    pad = next(p for p in index.snapshot.pads if p.id == uid(0x203))
    track = next(t for t in index.snapshot.tracks if t.id == uid(0x603))
    assert drc_findings._hole_span(index, pad, via[uid(0x704)])[0] == pytest.approx(0.2 * MM)  # 1.15 - 0.15 - 0.8
    assert drc_findings._hole_span(index, track, via[uid(0x701)])[0] == pytest.approx(0.225 * MM)  # 0.5 - 0.15 - 0.125
    assert drc_findings._hole_span(index, via[uid(0x702)], via[uid(0x703)], True)[0] == pytest.approx(0.2 * MM)
    assert drc_findings._hole_span(index, track, track) is None
    # the two pairs checked by hand on a KiCad 10 report (PLAN-step1 §2.4)
    a = model.Via("a", "N1", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu")
    b = model.Via("b", "N2", (600_000, 200_000), 600_000, 300_000, "F.Cu", "B.Cu")
    c = model.Via("c", "N1", (500_000, 0), 600_000, 300_000, "F.Cu", "B.Cu")
    empty = BoardIndex(model.BoardSnapshot("t", {}, (), (), (a, b, c), (), (), (), model.Outline(()),
                                           model.Stackup(()), (), {}))
    assert drc_findings._hole_span(empty, a, b)[0] == pytest.approx(632_456 - 150_000 - 300_000, abs=1)  # 0.1825
    assert drc_findings._hole_span(empty, a, c, True)[0] == pytest.approx(200_000)


@pytest.mark.parametrize("kind, description, expected", [
    ("clearance", "Clearance violation ( clearance 0,2000 mm; actual 0,1000 mm)", (0.2, 0.1)),
    ("clearance", "Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.0750 mm)", (0.2, 0.075)),
    ("clearance", "Clearance violation (clearance 7,87 mils; actual 3,94 mils)", (0.199898, 0.100076)),
    ("hole_to_hole", "Drilled hole too close to other hole (board setup constraints min 0,2495 mm; "
                     "actual 0,2000 mm)", (0.2495, 0.2)),
    ("track_width", "Track width (netclass 'Default' min width 0,2000 mm; actual 3,111e-05 mm)", (0.2, 3.111e-05)),
    ("annular_width", "Annular width (min annular width 100 µm; actual 50 um)", (0.1, 0.05)),
    ("clearance", "Clearance violation (rule 'P2.77x2.84mm (x)' clearance 0,2 mm; actual 0,1 mm)", (0.2, 0.1)),
    ("clearance", "Clearance violation of P2.77x2.84mm", (None, None)),  # a library name is no number
    # KiCad in another UI language: no "actual", the last bracket still reads "limit; word value"
    ("clearance", "Abstandsverletzung (Abstand 0,2000 mm; tatsächlich 0,1000 mm)", (0.2, 0.1)),
    ("hole_to_hole", "Bohrung zu nah an anderer Bohrung (Mindestabstand 0,2495 mm; tatsächlich 0,2000 mm)",
     (0.2495, 0.2)),
    ("clearance", "Abstandsverletzung von P2.77x2.84mm", (None, None)),
    ("track_dangling", "Track [A] on F.Cu, length 3,111e-05 mm (actual 1 mm)", (None, None)),  # not measured
    ("text_thickness", "Text thickness out of range (TrueType font characters with insufficient stroke weight)",
     (None, None)),
])
def test_parse_limits(kind, description, expected):
    limit, actual = parse_limits(kind, description)
    if expected[0] is None:
        assert (limit, actual) == expected
    else:
        assert (limit, actual) == pytest.approx(expected)


# the type table, on the fixture

def test_clearance(converted):
    finding = next(f for f in by_check(converted, "clearance") if f["values"]["distance_mm"] == 0.1)
    assert finding["title"] == "Clearance SIG_A / SIG_B" and finding["severity"] == "error"
    assert tools(finding) == ["highlight", "clearance"]
    assert draw(finding, "highlight")["targets"] == [f"uuid:{uid(0x601)}", f"uuid:{uid(0x602)}"]
    assert draw(finding, "clearance") == {"tool": "clearance", "a": f"uuid:{uid(0x601)}",
                                          "b": f"uuid:{uid(0x602)}", "required": 0.2}
    assert finding["values"] == {"limit_mm": 0.2, "distance_mm": 0.1}
    pad = next(f for f in by_check(converted, "clearance") if f["values"]["distance_mm"] == 0.05)
    assert pad["draw"][0]["targets"] == ["J1.1", f"uuid:{uid(0x704)}"]


def test_hole_findings(converted):
    hole = {f["values"]["hole_gap_mm"]: f for f in by_check(converted, "hole_clearance")}
    assert set(hole) == {0.2, 0.225}
    assert tools(hole[0.2]) == ["highlight", "distance"]
    assert draw(hole[0.2], "distance") == {  # a dimension from the drill wall to the copper, Blender measures it
        "tool": "distance", "from": "pt:13,14@F.Cu", "to": "pt:13.2,14@F.Cu", "mode": "centre",
        "label": "Hole clearance", "limit_is": "min", "limit": 0.25}
    assert hole[0.2]["values"]["limit_mm"] == 0.25
    (to_hole,) = by_check(converted, "hole_to_hole")
    assert to_hole["values"] == {"limit_mm": 0.2495, "hole_gap_mm": 0.2} and to_hole["severity"] == "warning"
    dimension = draw(to_hole, "distance")  # wall to wall: as long as the gap
    ends = [[float(v) for v in dimension[end][3:].split("@")[0].split(",")] for end in ("from", "to")]
    assert math.dist(*ends) == pytest.approx(0.2)
    assert dimension["label"] == "Hole to hole" and dimension["limit"] == 0.2495 and dimension["limit_is"] == "min"


def test_edge_clearance(converted):
    (finding,) = by_check(converted, "copper_edge_clearance")
    assert draw(finding, "highlight")["targets"] == [f"uuid:{uid(0x604)}"]
    assert draw(finding, "edge_gap") == {"tool": "edge_gap", "target": f"uuid:{uid(0x604)}", "min_mm": 0.5}
    assert finding["values"] == {"limit_mm": 0.5, "distance_mm": 0.175}


def test_unconnected_and_dangling(converted):
    pads = next(f for f in by_check(converted, "unconnected_items") if draw(f, "highlight")["targets"] ==
                ["R1.2", "J1.1"])
    assert draw(pads, "distance") == {"tool": "distance", "from": "R1.2", "to": "J1.1", "mode": "edge",
                                      "label": "Not connected"}
    labels = {draw(f, "highlight")["targets"][0]: draw(f, "label")["at"] for f in by_check(converted, "track_dangling")}
    assert labels[f"uuid:{uid(0x606)}"] == "pt:24,4@F.Cu"  # its start is on TP1; DRC gives the start
    assert labels[f"uuid:{uid(0x605)}"] == "pt:26,2@F.Cu"  # its end is on TP2
    assert labels[f"uuid:{uid(0x601)}"] == "pt:2,3@F.Cu"  # both ends open: DRC's position
    vias = by_check(converted, "via_dangling")
    assert len(vias) == 5 and all(draw(f, "label")["text"] == "Unconnected via" for f in vias)
    for via in vias:  # the via lit, the label at its position
        assert tools(via) == ["highlight", "label"]
        assert draw(via, "highlight")["targets"][0].startswith("uuid:") and draw(via, "label")["at"].startswith("pt:")


def test_a_dangling_via_is_highlighted_and_labelled():
    from kileido_bridge import findings
    via_id = "d0c00000-0000-4000-8000-00000000beef"
    via = model.Via(via_id, "GND", (5 * MM, 5 * MM), 600_000, 300_000, "F.Cu", "B.Cu")
    board = BoardIndex(model.BoardSnapshot("dangle", {}, (), (), (via,), (), (), (), model.Outline(()),
                                           model.Stackup(()), (), {}))
    report = {"violations": [entry("via_dangling", "Via is not connected or connected on only one layer",
                                   (via_id, "Via [GND] on F.Cu - B.Cu", 5, 5))]}
    (dangling,) = convert(report, board, "1", "00000000")["findings"]
    assert tools(dangling) == ["highlight", "label"]
    assert draw(dangling, "highlight")["targets"] == [f"uuid:{via_id}"]
    assert draw(dangling, "label") == {"tool": "label", "at": "pt:5,5@F.Cu", "text": "Unconnected via"}
    context = findings.Context("drc.kls-findings.json", "drc", "00000000", {})
    payload = findings.resolve_file(findings.parse(json.dumps(convert(report, board, "1", "00000000")).encode()),
                                    board, context)
    lit, label = payload["findings"][0]["draws"]
    assert lit["ok"] and lit["ids"] == [via_id] and label["ok"] and label["points"] == [[5 * MM, 5 * MM, "F.Cu"]]


def test_crossing_label_sits_on_the_crossing(converted):
    (finding,) = by_check(converted, "tracks_crossing")
    assert draw(finding, "label") == {"tool": "label", "at": "pt:26,4@F.Cu", "text": "Tracks cross"}
    assert finding["title"] == "Tracks cross SIG_B / SIG_C"


def _pt_xy(text):
    """("pt:20.175,20@F.Cu") -> (20.175, 20.0, "F.Cu")."""
    coordinates, _, layer = text[len("pt:"):].partition("@")
    x, y = coordinates.split(",")
    return float(x), float(y), layer


def test_size_checks_measure_the_model(converted):
    (annular,) = by_check(converted, "annular_width")
    assert annular["values"] == {"limit_mm": 0.1, "annular_width_mm": 0.05}
    ring = draw(annular, "distance")  # a dimension from the drill wall to the copper's edge, along +x
    assert ring["label"] == "Annular ring" and ring["mode"] == "centre"
    assert ring["limit"] == 0.1 and ring["limit_is"] == "min"
    (xa, ya, la), (xb, yb, lb) = _pt_xy(ring["from"]), _pt_xy(ring["to"])
    assert la == lb and ya == yb and xb - xa == pytest.approx(0.05)
    assert "label" not in tools(annular)  # the dimension's own label says it
    (diameter,) = by_check(converted, "via_diameter")
    assert diameter["values"] == {"limit_mm": 0.5, "diameter_mm": 0.4}
    across = draw(diameter, "distance")  # across the via
    (xa, ya, _), (xb, yb, _) = _pt_xy(across["from"]), _pt_xy(across["to"])
    assert across["label"] == "Diameter" and ya == yb and xb - xa == pytest.approx(0.4)


def test_courtyards_and_silkscreen(converted):
    (court,) = by_check(converted, "courtyards_overlap")
    assert draw(court, "highlight")["targets"] == [f"uuid:{uid(0x300)}", f"uuid:{uid(0x400)}"]
    assert draw(court, "label")["at"] == "pt:20.6,16@F.Courtyard"
    (edge,) = by_check(converted, "silk_edge_clearance")
    assert tools(edge) == ["label"]  # nothing on copper to highlight
    assert draw(edge, "label") == {"tool": "label", "at": "pt:29,10@F.Silkscreen",
                                   "text": "Silkscreen clipped by board edge"}
    over = by_check(converted, "silk_over_copper")
    assert sorted(draw(f, "highlight")["targets"][0] for f in over) == ["R1.1", "R1.2"]
    assert all(draw(f, "label")["at"] == "pt:6.5,8@F.Silkscreen" for f in over)


def test_converted_file_resolves_and_measures_what_drc_says(converted, index):
    """The converter's file through the findings path: every clearance, edge gap and width
    draws, measured over its limit as KiCad says."""
    from kileido_bridge import findings
    context = findings.Context("drc.kls-findings.json", "drc", "a1b2c3d4", {})
    payload = findings.resolve_file(findings.parse(json.dumps(converted).encode()), index, context)
    measured = [d for f in payload["findings"] for d in f["draws"] if d["tool"] in ("clearance", "edge_gap", "width")]
    assert sorted(d["tool"] for d in measured) == ["clearance"] * 3 + ["edge_gap"]
    assert all(d["ok"] and d["what"] and d["measured_nm"] is not None and d["limit_is"] == "min" for d in measured)
    assert all(d["over"] for d in measured)  # every one is a violation


# the rest of the table, on hand-written entries

def entry(kind, description, *items, severity="warning"):
    return {"type": kind, "severity": severity, "description": description,
            "items": [{"description": text, "pos": {"x": x, "y": y}, "uuid": u} for u, text, x, y in items]}


def test_types_not_on_the_fixture(index):
    track_a = (uid(0x601), "Track [SIG_A] on F.Cu, length 10,0000 mm", 2, 3)
    track_b = (uid(0x602), "Track [SIG_B] on F.Cu, length 10,0000 mm", 2, 3.35)
    zone = ("d0c00000-0000-4000-8000-00000000abcd", "Zone [GND] on In1.Cu, priority 0", 1, 1)
    report = {"violations": [
        entry("shorting_items", "Items shorting two nets (nets SIG_A and SIG_B)", track_a, track_b, severity="error"),
        entry("isolated_copper", "Isolated copper fill", zone), entry("isolated_copper", "Isolated copper fill", zone),
        entry("track_width", "Track width (netclass 'Default' min width 0,3000 mm; actual 0,2500 mm)", track_a),
        entry("connection_width", "Connection width (min 0,3000 mm; actual 0,1000 mm)", track_a),
        entry("starved_thermal", "Thermal relief connection to zone incomplete", zone,
              (uid(0x203), "PTH pad 1 [SIG_B] of J1", 14, 14)),
        entry("invalid_outline", "Board has malformed outline", (uid(0x500), "Rectangle on Edge.Cuts", 3, 4)),
        entry("footprint_symbol_mismatch", "R1 doesn't match", (uid(0x100), "Footprint R1", 8, 8)),
        entry("something_new", "A check from a later KiCad", track_a)]}
    found = {f["check"]: f for f in convert(report, index, "1", "00000000")["findings"]}
    short = found["drc.shorting_items"]
    assert short["severity"] == "error" and draw(short, "label")["text"] == "Short: SIG_A / SIG_B"
    assert draw(short, "label")["at"] == f"uuid:{uid(0x601)}"  # apart on this board: at the first item
    island = found["drc.isolated_copper"]
    assert tools(island) == ["label"] and draw(island, "label") == {
        "tool": "label", "at": "pt:1,1@In1.Cu", "text": "Isolated copper (2 islands)"}
    assert island["message"] == "Isolated copper fill (reported 2 times)"
    width = found["drc.track_width"]
    assert width["values"] == {"limit_mm": 0.3, "width_mm": 0.25}
    assert tools(width) == ["highlight", "width"]  # the width draw shows the value against the limit
    assert draw(width, "width") == {"tool": "width", "target": f"uuid:{uid(0x601)}", "required": 0.3}
    connection = found["drc.connection_width"]
    assert connection["values"] == {"limit_mm": 0.3, "connection_width_mm": 0.1}  # no model value: parsed
    assert tools(connection) == ["highlight", "width", "label"]  # the track's width is not the connection's
    assert draw(connection, "label")["text"] == "Connection width 0.10 mm < 0.30 mm min"
    thermal = found["drc.starved_thermal"]
    assert draw(thermal, "highlight")["targets"] == ["J1.1"] and draw(thermal, "label")["at"] == "J1.1"
    outline = found["drc.invalid_outline"]
    assert outline["draw"] == [{"tool": "highlight", "targets": ["edge"]},
                               {"tool": "label", "at": "pt:3,4@Edge.Cuts", "text": "Board has malformed outline"}]
    assert draw(found["drc.footprint_symbol_mismatch"], "highlight")["targets"] == ["R1"]
    other = found["drc.something_new"]
    assert other["title"] == "Something new SIG_A" and tools(other) == ["highlight", "label"]
    assert draw(other, "label")["at"] == "pt:2,3@F.Cu"


def test_isolated_copper_of_a_zone_on_the_board_lights_the_zone():
    from kileido_bridge import findings
    zone_id = "d0c00000-0000-4000-8000-00000000abcd"
    pieces = ((rect(0, 0, 10 * MM, 10 * MM)[0],), (rect(20 * MM, 0, 22 * MM, 2 * MM)[0],))
    zones = (model.ZoneFill(zone_id, "GND", "In1.Cu", pieces), model.ZoneFill(zone_id, "GND", "In2.Cu", pieces[:1]))
    via = model.Via("v", "GND", (5 * MM, 5 * MM), 600_000, 300_000, "F.Cu", "B.Cu")
    board = BoardIndex(model.BoardSnapshot("iso", {}, (), (), (via,), (), (), zones, model.Outline(()),
                                           model.Stackup(()), (), {}))
    report = {"violations": [entry("isolated_copper", "Isolated copper fill",
                                   (zone_id, "Zone [GND] on In1.Cu, priority 0", 21, 1))]}
    (island,) = convert(report, board, "1", "00000000")["findings"]
    assert tools(island) == ["highlight", "label"]
    assert draw(island, "highlight") == {"tool": "highlight", "targets": [f"uuid:{zone_id}"]}
    assert draw(island, "label") == {"tool": "label", "at": "pt:21,1@In1.Cu", "text": "Isolated copper"}
    context = findings.Context("drc.kls-findings.json", "drc", "00000000", {})
    payload = findings.resolve_file(findings.parse(json.dumps(convert(report, board, "1", "00000000")).encode()),
                                    board, context)
    lit, label = payload["findings"][0]["draws"]
    assert lit["ok"] and lit["ids"] == [zone_id] and label["ok"]


def test_long_description_is_cut_with_its_repeat_count(index):
    item = (uid(0x601), "Track [SIG_A] on F.Cu", 2, 3)
    long = entry("something_new", "x" * 400, item)
    out = convert({"violations": [long, long]}, index, "1", "00000000")
    message = out["findings"][0]["message"]
    assert len(message) <= 300 and message.endswith("… (reported 2 times)")


# kicad-cli

def test_drc_command():
    assert drc_command("kicad-cli", "run/board.kicad_pcb", "run/drc.json") == [
        "kicad-cli", "pcb", "drc", "--format", "json", "--units", "mm", "--all-track-errors",
        "--severity-error", "--severity-warning", "--output", "run/drc.json", "run/board.kicad_pcb"]


def test_layer_from_a_description_in_another_language(index):
    assert drc_findings._layer(index, "Via [GND] on F.Cu - B.Cu") == "F.Cu"
    assert drc_findings._layer(index, "Durchkontaktierung [GND] auf F.Cu - B.Cu") == "F.Cu"  # no "on": a known name
    assert drc_findings._layer(index, "Leiterbahn [SIG] auf B.Cu") == "B.Cu"
    assert drc_findings._layer(index, "Leiterbahn [F.Cu] ohne Lage") == ""  # a net name in brackets is not a layer


@pytest.mark.skipif(sys.platform != "win32", reason="the per-user KiCad install is a Windows layout")
def test_installed_finds_a_per_user_kicad(tmp_path, monkeypatch):
    user = tmp_path / "local" / "Programs" / "KiCad" / "10.0" / "bin" / "kicad-cli.exe"
    system = tmp_path / "pf" / "KiCad" / "9.0" / "bin" / "kicad-cli.exe"
    for path in (user, system):
        path.parent.mkdir(parents=True)
        path.write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramW6432", str(tmp_path / "pf"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert drc_findings._installed() == [user, system]  # the newest first


def test_find_kicad_cli_order(tmp_path, monkeypatch):
    hint, env = tmp_path / "hint-cli", tmp_path / "env-cli"
    hint.write_text("")
    env.write_text("")
    monkeypatch.setenv("KILEIDO_KICAD_CLI", str(env))
    monkeypatch.setattr(drc_findings.shutil, "which", lambda name: None)
    monkeypatch.setattr(drc_findings, "_installed", lambda: [])
    assert find_kicad_cli(str(hint)) == str(hint)
    assert find_kicad_cli(str(tmp_path / "missing")) == str(env)
    monkeypatch.delenv("KILEIDO_KICAD_CLI")
    assert find_kicad_cli("") == ""


def test_run_drc_failures(tmp_path):
    with pytest.raises(DrcFailed, match="could not start"):
        run_drc(str(tmp_path / "no-such-kicad-cli"), tmp_path)
    with pytest.raises(DrcFailed):  # python runs, fails on "pcb": non-zero exit, no report
        run_drc(sys.executable, tmp_path)


@pytest.mark.skipif(not KICAD_CLI, reason="no kicad-cli found")
def test_kicad_cli_drc_on_the_fixture(tmp_path, index, report):
    shutil.copyfile(BOARD, tmp_path / drc_findings.BOARD)
    try:
        live = run_drc(KICAD_CLI, tmp_path)
    except DrcFailed as exc:
        if "version" in str(exc).lower() or "load" in str(exc).lower():
            pytest.skip(f"kicad-cli cannot read a KiCad 10 board: {exc}")
        raise
    assert (tmp_path / drc_findings.REPORT).is_file()
    keys = lambda found: sorted((entry["type"], tuple(sorted(i["uuid"] for i in entry["items"])))  # noqa: E731
                                for entry, _count in dedupe(found)[0])
    # The same entries as the checked-in report (10.0.3): the repeat counts of one pair differ
    # between KiCad 10.0.x patch versions, the deduplicated entries do not.
    assert keys(live) == keys(report)
    text = BOARD.read_text(encoding="utf-8")
    for entry in (*live["violations"], *live["unconnected_items"]):
        assert all(f'(uuid "{item["uuid"]}")' in text for item in entry["items"])  # report uuid == file uuid
    out = convert(live, index, "1", "00000000")
    assert len(out["findings"]) == 34


def test_a_hole_near_a_zone_measures_to_its_closest_layer():
    """A zone is one record per copper layer: the hole gap is to the layer where its fill
    comes closest, not to whichever record comes first."""
    via = model.Via("v", "SIG", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu")

    def band(x0, x1):
        return ((((x0, -MM), (x1, -MM), (x1, MM), (x0, MM)),),)

    zones = (model.ZoneFill("z", "GND", "F.Cu", band(MM, 2 * MM)),  # 0.85 mm from the drill wall
             model.ZoneFill("z", "GND", "B.Cu", band(500_000, 2 * MM)))  # 0.35 mm
    snapshot = model.BoardSnapshot("t", {}, (), (), (via,), (), (), zones, model.Outline(()), model.Stackup(()), (), {})
    report = {"kicad_version": "10.0.3", "violations": [{
        "type": "hole_clearance", "severity": "error",
        "description": "Hole clearance violation (clearance 0,5000 mm; actual 0,3500 mm)",
        "items": [{"uuid": "v", "description": "Via [SIG] on F.Cu - B.Cu", "pos": {"x": 0, "y": 0}},
                  {"uuid": "z", "description": "Zone [GND] on B.Cu", "pos": {"x": 0.5, "y": 0}}]}],
        "unconnected_items": [], "schematic_parity": []}
    (finding,) = convert(report, BoardIndex(snapshot), "1", "00000000")["findings"]
    assert finding["values"]["hole_gap_mm"] == pytest.approx(0.35)
    assert draw(finding, "distance")["to"].endswith("@B.Cu")


def test_a_hole_gap_measured_at_or_over_its_limit_still_reads_as_a_minimum():
    """KiCad's number can be stale (the board moved on since its run): the dimension keeps
    `limit_is` min, so Blender shows the gap within its limit, not over a maximum."""
    via = model.Via("v", "SIG", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu")
    other = model.Via("w", "GND", (1_000_000, 0), 600_000, 300_000, "F.Cu", "B.Cu")  # 0.55 mm wall to copper
    snapshot = model.BoardSnapshot("t", {}, (), (), (via, other), (), (), (), model.Outline(()), model.Stackup(()),
                                   (), {})
    report = {"kicad_version": "10.0.3", "violations": [{
        "type": "hole_clearance", "severity": "error",
        "description": "Hole clearance violation (clearance 0,5000 mm; actual 0,2000 mm)",
        "items": [{"uuid": "v", "description": "Via [SIG] on F.Cu - B.Cu", "pos": {"x": 0, "y": 0}},
                  {"uuid": "w", "description": "Via [GND] on F.Cu - B.Cu", "pos": {"x": 1, "y": 0}}]}],
        "unconnected_items": []}
    (finding,) = convert(report, BoardIndex(snapshot), "1", "00000000")["findings"]
    assert finding["values"]["hole_gap_mm"] == pytest.approx(0.55)
    dimension = draw(finding, "distance")
    assert dimension["limit_is"] == "min" and dimension["limit"] == 0.5
