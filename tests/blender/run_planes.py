"""End to end: an LLM's findings file with every plane tool on the
split-plane board (tests/split_board.py) goes through the bridge (resolved and measured)
into Blender, and each finding draws: a layer change's return via measured past its limit,
the SATA pair's gap in the GND plane, USB_D+ running off its plane over the split, and
the GND islands, sparse GND stitching, a CLK via's stub and CLK narrowing. A wrong claim turns its finding into the grey "?"; every finding gets a
marker.

blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_planes.py
"""

import json
import sys
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import kileido  # noqa: E402
import split_board  # noqa: E402
from kileido import apply, findings, findings_draw, findings_markers, live, state, transform  # noqa: E402
from kileido.objects import root  # noqa: E402
from kileido_bridge import findings as bridge_findings, model  # noqa: E402
from kileido_bridge.board_index import BoardIndex  # noqa: E402
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

MM = 1_000_000
STAMP = "5eed5eed"
FINDINGS = [
    {"check": "plane.layer_change_no_return", "severity": "warning", "title": "LVDS_P via without return",
     "values": {"distance_mm": 1.4},
     "draw": [{"tool": "layer_change", "via": "uuid:via-lvds-0", "planes": ["GND@In1.Cu", "GND@In2.Cu"],
               "limit": 1.0},
              {"tool": "ghost", "kind": "via", "at": "pt:20,19.2@F.Cu", "net": "GND"}]},  # the LLM's suggestion
    {"check": "plane.layer_change_no_return", "severity": "warning", "title": "A via on a track",
     "draw": [{"tool": "layer_change", "via": "uuid:via-lvds-0"},
              {"tool": "ghost", "kind": "via", "at": "pt:15,20.1@F.Cu", "net": "GND"}]},
    {"check": "plane.gap_crossing", "severity": "error", "title": "SATA over a void",
     "values": {"gap_mm": 1.0},
     "draw": [{"tool": "plane_gap", "plane": "GND@In1.Cu", "crossing": "net:SATA_P"}]},
    {"check": "plane.gap_crossing", "severity": "error", "title": "USB off its plane",
     "values": {"length_mm": 10.0},  # wrong: the bridge measures 40.25 mm
     "draw": [{"tool": "return_path", "signal": "net:USB_D+", "plane": "GND@In1.Cu"}]},
    {"check": "plane.fragmented", "severity": "info", "title": "GND pieces",
     "draw": [{"tool": "plane_islands", "net": "GND"}]},
    {"check": "plane.stitching_sparse", "severity": "warning", "title": "Sparse GND stitching",
     "draw": [{"tool": "stitching", "along": "GND@In2.Cu", "spacing_mm": 5, "max_mm": 10}]},
    {"check": "hs.stub", "severity": "warning", "title": "CLK via stub",
     "draw": [{"tool": "stub", "via": "uuid:clk-via-0"}]},
    {"check": "hs.impedance_step", "severity": "info", "title": "CLK narrows",
     "draw": [{"tool": "discontinuity", "net": "CLK", "at": ["pt:90,5@F.Cu"]}]},
]
STITCHES = [(5 * MM, 2 * MM), (10 * MM, 2 * MM), (40 * MM, 2 * MM)]  # inside the In2.Cu GND pour's edge


def board():
    """The split board with a 1.4 mm return via, three GND stitching vias, a CLK via used
    only on F.Cu (a stub down to B.Cu) and CLK narrowing past x = 90 mm."""
    snapshot = split_board.with_return_via(split_board.board(), 1_400_000)
    snapshot = split_board.with_vias(snapshot, "GND", STITCHES)
    snapshot = split_board.with_vias(snapshot, "CLK", [(60 * MM, 5 * MM)], "clk-via")
    return split_board.with_tracks(snapshot, model.Track("clk-narrow", "F.Cu", "CLK", (90 * MM, 5 * MM),
                                                         (95 * MM, 5 * MM), 100_000))


def findings_frame(index):
    data = {"format": "kls-findings 1", "run": "7", "stamp": STAMP, "tool": "Test LLM", "findings": FINDINGS}
    context = bridge_findings.Context("file", {"7": STAMP}, STAMP, {})
    payload = bridge_findings.resolve_file(bridge_findings.parse(json.dumps(data).encode("utf-8")), index, context)
    drc = bridge_findings.status_payload("none")
    return {"type": "findings", "revision": 1,
            "findings": {"status": "ok", "folder": "C:/boards/.kileidoscope",
                         "drc": {"state": "idle", "error": "", "run": ""}, "sources": {"drc": drc, "file": payload}}}


def named(title):
    return next(f for f in findings.source("file")["findings"] if f["title"] == title)


def drawn(number, tool):
    """The shown finding's objects of draw `number`."""
    return {obj.name: obj for obj in state.board.collection.all_objects
            if obj.get(findings_draw.TAG) is not None and obj.name.startswith(f"KLS finding {number} {tool}")}


def blender_xy(point):
    return transform.xy_m([point[:2]], state.board.origin_nm)[0]


def show(title):
    found = named(title)
    assert bpy.ops.kileido.finding_show(source="file", key=found["key"]) == {"FINISHED"}
    draw = found["draws"][0]
    assert draw["ok"], (title, draw["problem"])
    objects = drawn(1, draw["tool"])
    assert objects, (title, draw["tool"])
    assert any(obj.type == "FONT" and obj.parent == root() for obj in objects.values()), sorted(objects)  # labelled
    return found, draw, objects


def main():
    kileido.register()
    try:
        snapshot = board()
        apply.load_frames(b"".join(snapshot_frames(snapshot)))
        live.linked = lambda: True
        live.request_select = lambda *args, **kwargs: None
        apply.apply_frame(findings_frame(BoardIndex(snapshot)), {})
        payload = findings.source("file")
        assert payload["handshake"] == "known" and len(payload["findings"]) == len(FINDINGS), payload.get("problems")
        top = state.board.heights["F.Cu"]

        found, draw, objects = show("LVDS_P via without return")
        assert draw["measured_nm"] == 1_400_000 and draw["over"] and draw["planes"] == ["GND@In1.Cu", "GND@In2.Cu"]
        assert findings_draw.mark(found) == "warning"  # 1.4 mm claimed and measured
        assert objects["KLS finding 1 layer_change label"].data.body == "Return via\n1.40 mm > 1.00 mm max"
        assert objects["KLS finding 1 layer_change"].data.materials[0].name == "KLS finding over"
        assert state.board.highlight["finding"] == {"via-lvds-0"}
        suggested = found["draws"][1]
        assert suggested["fits"] is True and drawn(2, "ghost")
        assert drawn(2, "ghost")["KLS finding 2 ghost label"].data.body == "Suggested via\nby the LLM"
        assert "KLS finding 1 layer_change limit" in objects
        bpy.ops.kileido.finding_show(source="file", key=found["key"])

        # The LLM's via on top of an LVDS track, 5 mm out: it does not fit, and the finding is a "?".
        found = named("A via on a track")
        assert bpy.ops.kileido.finding_show(source="file", key=found["key"]) == {"FINISHED"}
        bad = found["draws"][1]
        assert bad["fits"] is False and "clashes with LVDS_P track" in bad["problem"], bad["problem"]
        assert findings_draw.mark(found) == "unknown"
        assert drawn(2, "ghost")["KLS finding 2 ghost label"].data.body == "Suggested via\ndoes not fit"
        bpy.ops.kileido.finding_show(source="file", key=found["key"])

        found, draw, objects = show("SATA over a void")
        assert draw["measured_nm"] == 1 * MM and draw["layer"] == "In1.Cu" and draw["edges_nm"]
        assert findings_draw.mark(found) == "error"
        assert objects["KLS finding 1 plane_gap label"].data.body.startswith("Gap\n1.00 mm")
        span = draw["spans_nm"][0]
        assert np.allclose(sorted([span[0], span[2]]), [25 * MM, 26 * MM])

        found, draw, objects = show("USB off its plane")
        assert draw["measured_nm"] == 40_250_000 and draw["covered_nm"] and draw["spans_nm"]
        assert findings_draw.mark(found) == "unknown" and found["disagree"]  # 10 mm claimed: a "?"
        label = next(obj for obj in objects.values() if obj.type == "FONT" and obj.parent == root())
        assert label.data.body.startswith("Without plane\n40.25 mm"), label.data.body

        found, draw, objects = show("GND pieces")
        islands = draw["islands"]
        assert {island["layer"] for island in islands} == {"In1.Cu", "In2.Cu"}, islands
        assert all(island["tied"] for island in islands)  # the GND via ties both planes
        assert sum(1 for name in objects if name.endswith(" label")) >= 1
        assert findings_draw.mark(found) == "info"
        bpy.ops.kileido.finding_show(source="file", key=found["key"])  # hidden again

        # Stitching: measured along the pour's outline, the tube following that stretch, red past 10 mm.
        found, draw, objects = show("Sparse GND stitching")
        assert draw["over"] and draw["measured_nm"] > 10 * MM and len(draw["returns"]) == 3 and "ghosts_nm" not in draw
        assert len(draw["points"]) == 2 and len(draw["edges_nm"]) >= 2  # the widest, and the 30 mm gap
        tube = objects["KLS finding 1 stitching"]
        assert tube.data.materials[0].name == "KLS finding over" and len(tube.data.splines[0].points) >= 2
        assert objects["KLS finding 1 stitching label"].data.body.startswith("Stitching gap\n")
        bpy.ops.kileido.finding_show(source="file", key=found["key"])

        # Stub: the CLK via used on F.Cu only: the barrel from F.Cu's inner face to B.Cu's far face.
        found, draw, objects = show("CLK via stub")
        assert draw["barrel"]["used"] == ["F.Cu", "F.Cu"] and draw["barrel"]["stub"] == ["F.Cu", "B.Cu"], draw["barrel"]
        assert draw["measured_nm"] == 1_505_000, draw["measured_nm"]
        body = objects["KLS finding 1 stub label"].data.body
        assert body == "Stub\n1.50 mm", repr(body)  # 1.505 mm, two decimals
        assert objects["KLS finding 1 stub"].data.materials[0].name == "KLS finding over"
        bpy.ops.kileido.finding_show(source="file", key=found["key"])

        # Discontinuity: CLK narrows from 0.15 to 0.10 mm at x = 90 mm.
        found, draw, objects = show("CLK narrows")
        assert [mark["kind"] for mark in draw["marks"]] == ["width"], draw["marks"]
        assert "0.15" in draw["marks"][0]["text"] and "0.10" in draw["marks"][0]["text"], draw["marks"]
        bpy.ops.kileido.finding_show(source="file", key=found["key"])

        # Markers: one per finding, out from the top (the layer change spans both sides: None).
        findings_markers.invalidate()
        marks = {marker["finding"]["title"]: marker for marker in findings_markers.markers()}
        assert set(marks) == {finding["title"] for finding in FINDINGS}, sorted(marks)
        assert marks["USB off its plane"]["kind"] == "unknown"
        assert all(marker["at"].z > top for marker in marks.values())
        # The column: nothing reported as not drawn.
        texts = []

        class Layout:
            def __getattr__(self, name):
                return lambda *args, **kwargs: self

            def label(self, text="", **kwargs):
                texts.append(text)

            def operator(self, idname, text="", **kwargs):
                texts.append(text)
                return type("Properties", (), {})()

        for finding in payload["findings"]:
            state.board.findings_shown = ("file", finding["key"])
            findings.draw_column(Layout(), bpy.context.scene, lambda text: [text], "LLM")
        state.board.findings_shown = ("", "")
        assert not [text for text in texts if "not drawn" in text], [text for text in texts if "not drawn" in text]
        print("KLS_PLANES_OK")
    finally:
        kileido.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
