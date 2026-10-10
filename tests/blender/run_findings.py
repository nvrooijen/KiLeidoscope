"""Headless checks of the DRC column: the bridge's findings frame reaches the board
state, the column lists both sources by check, a shown finding draws its tools at the
right Blender coordinates (highlight, label, distance, arrow), confirm and dismiss update
the rows, a hole finding cuts the board open through both holes and puts the user's cut
back after, and a loaded file purges the drawing. What an LLM's file adds: isolate, a
ghost checked to fit, a polygon area, layers, the fix and the names it mentions, a value
the bridge measured differently; the LLM prompt box ticks checks, sends Copy prompt with
the user's notes, copies a ready prompt once and shows how far the LLM is (waiting,
working, stalled, done). The measured tools (hand-built payloads, as the bridge measures
them): clearance, edge gap, width ribbons and callout, a routed path through its vias, a
bar chart on its label's plate; a layer change's return via tube and rings, a plane gap's
plane edges and red spans, a return path's covered share, plane islands by colour and
number; stitching vias ringed with the largest spacing measured, a stub's barrel red past
its last layer used, discontinuity markers by kind. Labels (label_place): light text on
a dark plate, beside the finding's box, above the surface the view sees with a leader to
their anchor, sized to the finding, apart on screen; on a hole finding's cut face, flat
on it, within the board, beside the holes.

blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_findings.py
"""

import json
import sys
import textwrap
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import (apply, columns, cut, findings, findings_draw, focus, hole_cut, label_place, live,  # noqa: E402
                     state, transform)
from kileido.objects import read_coordinates  # noqa: E402
from kileido_bridge import findings as bridge_findings  # noqa: E402
from kileido_bridge.board_index import BoardIndex  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
STAMP = "a1b2c3d4"
TRACK = "11111111-1111-4111-8111-111111111112"  # F.Cu, (5, 5) to (12, 5) mm
OTHER = "11111111-1111-4111-8111-111111111113"  # F.Cu, (5, 11) to (10, 11) mm
VIA = "44444444-4444-4444-8444-444444444444"
ZONE = "88888888-8888-4888-8888-888888888888"
J1 = "66666666-6666-4666-8666-666666666666"


class Layout:
    """Records what a panel draws: (kind, text, icon) per label or operator."""

    def __init__(self, drawn=None):
        self.drawn = [] if drawn is None else drawn
        self.alignment, self.active, self.enabled, self.alert = "EXPAND", True, True, False

    def _child(self, *args, **kwargs):
        return type(self)(self.drawn)

    box = split = row = column = _child

    def label(self, text="", icon="NONE"):
        self.drawn.append(("label", text, icon))

    def prop(self, data, name, **kwargs):
        self.drawn.append(("prop", name, ""))

    def operator(self, idname, text="", icon="NONE", emboss=True, depress=False, icon_value=0):
        self.drawn.append(("operator", text, icon_value or icon))
        return type("Properties", (), {})()


class BarLayout(Layout):
    """Blender 5.1's layout: it has progress bars (recorded with their factor)."""

    def progress(self, factor=0.0, text="", **kwargs):
        self.drawn.append(("progress", text, round(factor, 4)))


def wrap(text):
    return textwrap.wrap(text, 70) or [""]


def column(scene, layout_type=BarLayout, tabs=("DRC", "LLM")):
    """What the tabs' columns draw, one after the other (both by default)."""
    layout = layout_type()
    for tab in tabs:
        findings.draw_column(layout, scene, wrap, tab)
    return layout.drawn


HOLES = [  # as KiCad's DRC converter draws them: a highlight and a dimension between the holes
    {"check": "drc.hole_clearance", "severity": "error", "title": "Hole near J1",
     "draw": [{"tool": "highlight", "targets": [f"uuid:{VIA}", "J1"]},
              {"tool": "distance", "from": f"uuid:{VIA}", "to": "J1", "mode": "centre", "label": "Hole clearance"}]},
    {"check": "drc.hole_to_hole", "severity": "error", "title": "One hole",  # no direction: squarely to the view
     "draw": [{"tool": "distance", "from": f"uuid:{VIA}", "to": f"uuid:{VIA}", "mode": "centre",
               "label": "Hole to hole"}]},
    {"check": "drc.annular_width", "severity": "error", "title": "Thin ring",  # a size check: a dimension across the ring
     "draw": [{"tool": "highlight", "targets": [f"uuid:{VIA}"]},
              {"tool": "distance", "from": "pt:20.175,20@F.Cu", "to": "pt:20.45,20@F.Cu", "mode": "centre",
               "label": "Annular ring", "limit": 0.3, "limit_is": "min"}]},
]


BELOW = [  # labels on the bottom copper and an inner layer, one finding: seen from above, on top with leaders
    {"check": "fab.note", "severity": "warning", "title": "Under the board",
     "draw": [{"tool": "label", "at": "pt:8,8@B.Cu", "text": "Bottom pad here"},
              {"tool": "label", "at": "pt:9,8.5@In2.Cu", "text": "Inner plane 0.20 mm"}]},
]


SUGGESTED = [  # what an LLM's answer adds: a ghost, a polygon area, layers, a fix, a claim, isolate
    {"check": "rf.clearance", "severity": "error", "confidence": "high", "title": "Suggested fixes",
     "message": "RF_A runs close to itself near J1 (via 420d87bb)", "fix": "Move the lower track down",
     "values": {"distance_mm": 1.0, "net_name": "RF_A"},  # the bridge measures 5.3 mm: they disagree
     "draw": [{"tool": "highlight", "targets": [f"uuid:{TRACK}", f"uuid:{OTHER}"]},
              {"tool": "distance", "from": f"uuid:{TRACK}", "to": f"uuid:{OTHER}", "limit": 6.0, "limit_is": "min"},
              {"tool": "label", "at": "J1", "text": "J1 feeds this"},
              {"tool": "arrow", "from": "J1", "to": f"uuid:{VIA}", "label": "return path"},
              {"tool": "ghost", "kind": "via", "at": "pt:15,15@F.Cu"},
              {"tool": "area", "layer": "F.Cu", "polygon": [[14, 14], [18, 14], [18, 18], [14, 18]]},
              {"tool": "layers", "layers": ["F.Cu", "In1.Cu"]}]},
    {"check": "plane.isolated", "severity": "warning", "title": "Plane isolated",
     "draw": [{"tool": "isolate", "targets": [f"uuid:{ZONE}", f"uuid:{VIA}"]}]},
]


def findings_file(extra=()):
    """A findings file on the fixture board: one finding per tool family."""
    items = [
        {"check": "drc.clearance", "severity": "error", "title": "Tracks too close",
         "message": "RF_A runs close to itself near J1 (via 420d87bb)",
         "values": {"distance_mm": 5.3},
         "draw": [{"tool": "highlight", "targets": [f"uuid:{TRACK}", f"uuid:{OTHER}"]},
                  {"tool": "distance", "from": f"uuid:{TRACK}", "to": f"uuid:{OTHER}", "limit": 6.0, "limit_is": "min"},
                  {"tool": "label", "at": "J1", "text": "J1 feeds this"},
                  {"tool": "arrow", "from": "J1", "to": f"uuid:{VIA}", "label": "return path"}]},
        {"check": "plane.gap_crossing", "severity": "warning", "title": "Plane under the via",
         "draw": [{"tool": "highlight", "targets": [f"uuid:{ZONE}", f"uuid:{VIA}"]}]},
        {"check": "plane.gap_crossing", "severity": "warning", "title": "A second gap",
         "draw": [{"tool": "highlight", "targets": [f"uuid:{VIA}"]}]},
        {"check": "fab.missing", "severity": "info", "title": "Gone",
         "draw": [{"tool": "highlight", "targets": ["uuid:00000000-dead-4bee-8f00-000000000000"]}]},
        *extra,
    ]
    data = {"format": "kls-findings 1", "run": "4", "stamp": STAMP, "tool": "Test tool", "findings": items,
            "skipped": [{"check": "emc.ground", "reason": "no stackup"}]}
    return json.dumps(data).encode("utf-8")


def frame(index, drop=(), extra=()):
    """The bridge's findings frame: the findings file resolved (the board as it is, a known
    run), no DRC run yet."""
    context = bridge_findings.Context("x.kls-findings.json", "file", {"4": STAMP}, frozenset(), STAMP, {})
    payload = bridge_findings.resolve_file(bridge_findings.parse(findings_file(extra)), index, context)
    payload["findings"] = [f for f in payload["findings"] if f["title"] not in drop]
    drc = bridge_findings.status_payload("none", bridge_findings.Context("x.drc.json", "drc", {}, frozenset(), STAMP, {}))
    return {"type": "findings", "revision": 1,
            "findings": {"status": "ok", "folder": "C:/boards/.kileidoscope",
                         "drc": {"state": "idle", "error": "", "run": ""}, "sources": {"drc": drc, "file": payload}}}


CATALOGUE = [{"id": "plane.gap_crossing", "group": "plane", "title": "Signal crossing a plane gap", "v1": True},
             {"id": "decap.distance", "group": "decap", "title": "Decoupling cap far from its pin", "v1": True},
             {"id": "hs.stub", "group": "hs", "title": "Stubs on fast nets", "v1": False}]
GROUPS = [{"id": "plane", "title": "Return paths and planes"}, {"id": "decap", "title": "Decoupling"},
          {"id": "hs", "title": "High speed"}]
QUICK = {"plane.gap_crossing", "decap.distance"}
PROMPT = "KiLeidoscope LLM Visualizer: board review request\nRun: 5   Stamp: a1b2c3d4\n\nRead request.md first."


def prompt_frame(index, state="idle", run="", text="", error="", status="ok"):
    """The findings frame with the bridge's check catalogue and a prompt's state."""
    header = frame(index)
    header["findings"].update(status=status, catalogue=CATALOGUE, groups=GROUPS,
                              prompt={"state": state, "run": run, "text": text, "error": error})
    return header


def finding_named(title):
    return next(f for f in findings.source("file")["findings"] if f["title"] == title)


def objects():
    return {obj.name: obj for obj in state.board.collection.all_objects if obj.get(findings_draw.TAG) is not None}


def blender_xy(point):
    return transform.xy_m([point[:2]], state.board.origin_nm)[0]


def show(title):
    found = finding_named(title)
    assert bpy.ops.kileido.finding_show(source="file", key=found["key"]) == {"FINISHED"}
    return found


def check_drawing(scene):
    found = show("Tracks too close")
    assert state.board.findings_shown == ("file", found["key"]) and columns.tab_state("LLM") == "on"
    assert columns.tab_state("DRC") == "off"  # the file's findings are the LLM tab's
    framed = framed_centre(finding_box(found))
    for space in kileido.objects.view3d_spaces():  # the views framed on the finding and its labels
        assert np.allclose(space.region_3d.view_location[:2], framed, atol=1e-6)
    drawn = objects()
    names = sorted(drawn)
    assert {"KLS finding 2 distance", "KLS finding 2 distance label", "KLS finding 3 label", "KLS finding 4 arrow",
            "KLS finding 4 arrow head", "KLS finding 4 arrow label", findings_draw.EYE} <= set(names), names
    group = next(child for child in state.board.collection.children if child.get("kls_group") == "findings")
    assert all(obj.name in group.objects for obj in drawn.values())
    draws = found["draws"]
    # Distance: a tube between the bridge's two measured points, on F.Cu's surface, red past its 6 mm minimum.
    distance = draws[1]
    assert distance["over"] and distance["measured_nm"] == 5_300_000, distance
    tube = drawn["KLS finding 2 distance"]
    (line, *ticks) = tube.data.splines
    assert len(ticks) == 2
    ends = np.array([tuple(point.co)[:3] for point in line.points])
    expected = np.array([blender_xy(point) for point in distance["points"]])
    assert np.allclose(ends[:, :2], expected, atol=1e-7), (ends, expected)
    surface = state.board.heights["F.Cu"]
    assert np.all(ends[:, 2] > surface) and np.all(ends[:, 2] < surface + 1e-4), ends
    assert tube.data.materials[0].name == "KLS finding over"
    assert drawn["KLS finding 2 distance label"].data.body == "Distance\n5.30 mm < 6.00 mm min"
    # Label: beside the box, its leader to J1 (the target is a footprint), facing the eye.
    label = drawn["KLS finding 3 label"]
    leader = drawn["KLS finding 3 label leader"].data.splines[0].points
    assert np.allclose(tuple(leader[-1].co)[:2], blender_xy(draws[2]["points"][0]), atol=1e-7)
    assert label.data.body == "J1 feeds this"
    (track,) = label.constraints
    assert track.type == "TRACK_TO" and track.target == drawn[findings_draw.EYE] and track.use_target_z
    check_labels(found, 3)  # the distance's, the label, the arrow's
    # A highlighted via's lands and inner rings open over its hole, as the selection's do.
    for key in ("highlight_finding", "highlight_selected"):
        assert state.board.materials[key].node_tree.nodes.get("KLS holes plot") is not None, key
    # A still view leaves the eye where it is: each move would restart a rendered view.
    findings_draw._follow()  # the view framed after the drawing was placed: one move
    laid, lay_out = [], findings_draw._lay_out
    findings_draw._lay_out = lambda matrix, *_: laid.append(matrix)
    try:
        for _ in range(5):  # each tick after a depsgraph update, as in the UI: the eye's matrix read back rounded
            bpy.context.view_layer.update()
            findings_draw._follow()
        assert not laid, len(laid)
    finally:
        findings_draw._lay_out = lay_out
    # Arrow: a tube and a head.
    assert len(drawn["KLS finding 4 arrow head"].data.polygons) > 0
    # Highlight: the two tracks' copper copied in the finding's colour; no X-ray (the tick box is off).
    copy = state.board.collection.all_objects["KLS F.Cu highlight finding"]
    assert len(copy.data.edges) == 2 and not copy.hide_get(), len(copy.data.edges)
    source = state.board.collection.all_objects["KLS F.Cu tracks"]
    ids = list(source["kls_ids"])
    wanted = {ids.index(TRACK), ids.index(OTHER)}
    item = np.empty(len(source.data.vertices), np.int32)
    source.data.attributes["item"].data.foreach_get("value", item)
    expected = read_coordinates(source.data)[np.isin(item, list(wanted))]
    assert np.allclose(np.sort(read_coordinates(copy.data), axis=0), np.sort(expected, axis=0))
    amount = bpy.data.node_groups[focus.GROUP].nodes["Amount"].outputs[0]
    assert amount.default_value == 0.0
    # The column: the shown finding opens with its message, values and measurements.
    drawn = column(scene)
    texts = [text for _, text, _ in drawn]
    assert "RF_A runs close to itself near J1 (via 420d87bb)" in texts and "distance: 5.3 mm" in texts, texts
    assert "Measured 5.30 mm < 6.00 mm min" in texts, texts
    # A zone and a via highlighted; no X-ray with the tick box off.
    show("Plane under the via")
    assert amount.default_value == 0.0
    zone = state.board.collection.all_objects[f"KLS In1.Cu zone {ZONE}"]
    assert zone["kls_zone_highlight"] == "finding"
    assert not state.board.collection.all_objects["KLS vias highlight finding"].hide_get()
    assert not state.board.collection.all_objects.get("KLS F.Cu highlight finding").visible_get()
    assert "KLS finding 2 distance" not in objects() and findings_draw.EYE not in objects()
    show("Plane under the via")  # a second click hides it
    assert state.board.findings_shown == ("", "") and amount.default_value == 0.0 and not objects()
    assert zone["kls_zone_highlight"] == "" and columns.tab_state("DRC") == "off"


def check_suggestions(scene, index):
    """What an LLM's file adds: a ghost via checked to fit and labelled, a polygon area just
    over its layer, layers listed; in the column the fix, a button per name the message
    mentions, the claimed value against the measured one (the grey "?"); isolate's X-ray."""
    apply.apply_frame(frame(index, extra=SUGGESTED), {})
    found = show("Suggested fixes")
    drawn = objects()
    assert {"KLS finding 2 distance", "KLS finding 5 ghost", "KLS finding 5 ghost label", "KLS finding 6 area",
            findings_draw.EYE} <= set(drawn), sorted(drawn)
    check_labels(found, 4)  # the ghost labelled too: the LLM's suggestion, here on an RF_A arc
    assert drawn["KLS finding 5 ghost label"].data.body == "Suggested via\ndoes not fit"
    assert drawn["KLS finding 5 ghost"].data.materials[0].name == "KLS finding over ghost"
    # Ghost via: a cylinder through the board at (15, 15) mm.
    ghost = drawn["KLS finding 5 ghost"]
    assert np.allclose(ghost.matrix_world.translation[:2], blender_xy([15_000_000, 15_000_000]), atol=1e-7)
    corners_ = np.array([tuple(v.co) for v in ghost.data.vertices])
    assert np.isclose(np.ptp(corners_[:, 2]), state.board.heights["F.Cu"] - state.board.heights["B.Cu"], atol=1e-7)
    assert np.isclose(np.ptp(corners_[:, 0]), findings_draw.VIA_GHOST_M, atol=1e-6)
    # Area: one face just over F.Cu.
    area = drawn["KLS finding 6 area"]
    assert len(area.data.polygons) == 1 and area.matrix_world.translation.z > state.board.heights["F.Cu"]
    # The column: the message, the fix, the claimed value against the measured one, the layers listed.
    drawn = column(scene)
    texts = [text for _, text, _ in drawn]
    assert "RF_A runs close to itself near J1 (via 420d87bb)" in texts and "Fix: Move the lower track down" in texts
    # A button per name it mentions, in order; a made-up code greyed, not a button.
    chips = [(kind, text) for kind, text, _ in drawn if text.split(":")[0] in ("RF_A", "J1", "420d87bb")]
    assert [text.split(":")[0] for _, text in chips] == ["RF_A", "J1", "420d87bb"], chips
    assert [kind for kind, _ in chips] == ["operator", "operator", "label"] and \
        chips[2][1] == "420d87bb: not on the board", chips
    assert "distance: claimed 1 mm, measured 5.3 mm" in texts, texts
    assert "Layers: F.Cu, In1.Cu" in texts and "Measured 5.30 mm < 6.00 mm min" in texts, texts
    # Not confirmed, a "?": the bridge measured the distance differently (1.0 claimed, 5.3 mm).
    unknown = next(iter(findings.severity_icon("unknown").values()))
    assert ("operator", "Suggested fixes", unknown) in drawn
    assert findings_draw.mark(finding_named("Suggested fixes")) == "unknown"
    # Isolate: a zone and a via, X-ray on whatever the tick box says.
    amount = bpy.data.node_groups[focus.GROUP].nodes["Amount"].outputs[0]
    show("Plane isolated")
    assert state.board.findings_isolate and amount.default_value == 1.0
    zone = state.board.collection.all_objects[f"KLS In1.Cu zone {ZONE}"]
    assert zone["kls_zone_highlight"] == "finding"
    show("Plane isolated")  # a second click hides it
    assert state.board.findings_shown == ("", "") and amount.default_value == 0.0 and not objects()
    assert zone["kls_zone_highlight"] == ""
    apply.apply_frame(frame(index), {})


def corners(obj):
    return [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]


def framed_centre(box):
    """Where a shown finding is framed: the middle of its box and its labels together."""
    reach = findings_draw.label_reach()
    assert reach is not None
    low = np.minimum(box[:2], reach[:2])
    high = np.maximum(box[2:], reach[2:])
    return (low + high) / 2


def finding_box(found):
    (x0, y0), (x1, y1) = blender_xy(found["bbox_nm"][:2]), blender_xy(found["bbox_nm"][2:])
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def apart(a, b, axes):
    """Two point sets apart on one of `axes` (2D): a separating axis."""
    for axis in axes:
        pa, pb = [p.xy.dot(axis) for p in a], [p.xy.dot(axis) for p in b]
        if max(pa) < min(pb) or max(pb) < min(pa):
            return True
    return False


def in_text(part):
    """A label part's vertices in its text's own axes."""
    return np.array([tuple(part.matrix_basis @ v.co) for v in part.data.vertices])


def check_plate(text, drawn, severity):
    """Light text on its dark plate, the severity's icon at the left: the plate a child
    a hair behind the text, around the icon and all the text; the icon (circle or
    triangle, "!" or "i"; a green tick within a limit) on it, before the text; none of it
    clipped by the cut."""
    plate, icon, glyph = (drawn[text.name + part] for part in (" plate", " icon", " icon glyph"))
    assert all(part.parent == text and part.show_in_front for part in (plate, icon, glyph))
    local, mark = in_text(plate), in_text(icon)
    assert np.all(local[:, 2] < 0) and np.all(local[:, 2] > -0.1 * text.data.size), local  # a hair behind
    assert np.all(mark[:, 2] > local[:, 2].max()) and np.all(mark[:, 2] < 0)  # in front of the plate
    bound = np.array([tuple(c) for c in text.bound_box])
    for inner in (bound, mark):  # the plate around the text and the icon
        assert local[:, 0].min() < inner[:, 0].min() and local[:, 0].max() > inner[:, 0].max()
        assert local[:, 1].min() < inner[:, 1].min() and local[:, 1].max() > inner[:, 1].max()
    assert mark[:, 0].max() < bound[:, 0].min()  # at the left, before the text
    if icon.data.materials[0].name == "KLS finding icon pass":  # a measurement within its limit
        severity = "pass"
    shape, _, letter, _ = findings_draw.ICONS[severity]
    assert (len(icon.data.vertices) == 3) == (shape == "triangle"), (severity, len(icon.data.vertices))
    if letter == "✓":  # drawn: two strokes
        assert glyph.type == "MESH" and len(glyph.data.polygons) == 2
    else:
        assert glyph.type == "FONT" and glyph.data.body == letter
    assert icon.data.materials[0].name == f"KLS finding icon {severity}"
    (dark,) = plate.data.materials
    assert dark.name == "KLS finding plate" and max(dark.diffuse_color[:3]) < 0.01, tuple(dark.diffuse_color)
    assert not any(node.type == "BSDF_TRANSPARENT" for node in dark.node_tree.nodes)  # opaque: no grain behind it
    for part in (text, plate, icon, glyph):  # an overlay: no shadows or light cast on the board
        assert part.visible_camera and not (part.visible_shadow or part.visible_diffuse or part.visible_glossy)
    light = text.data.materials[0]
    emission = next(node for node in light.node_tree.nodes if node.type == "EMISSION")
    assert light.name == "KLS finding text" and min(emission.inputs["Color"].default_value[:3]) > 0.8
    for material in (light, dark, icon.data.materials[0], glyph.data.materials[0]):
        assert material.node_tree.nodes.get(cut.NODE) is None, material.name
    return plate


def check_labels(found, count, below=False):
    """Every label of the shown finding: sized to its box, outside it, out from the surface
    the view sees with a leader to its anchor, on its plate, the plates apart on screen."""
    bpy.context.view_layer.update()
    drawn = objects()
    texts = [obj for obj in drawn.values() if obj.type == "FONT" and obj.parent is None]  # not a glyph, a bar's
    assert len(texts) == count, sorted(drawn)
    box = finding_box(found)
    rect = [Vector((x, y, 0.0)) for x in (box[0], box[2]) for y in (box[1], box[3])]
    eye = drawn[findings_draw.EYE].matrix_world
    right, up = eye.col[0].xyz.normalized(), eye.col[2].xyz.normalized()
    flat = [Vector((right.x, right.y)), Vector((-right.y, right.x)), Vector((1.0, 0.0)), Vector((0.0, 1.0))]
    flat = [axis.normalized() for axis in flat if axis.length > 1e-6]
    top, bottom = findings_draw._surface("F.Cu")[0], findings_draw._surface("B.Cu")[0]
    plates = []
    for text in texts:
        assert np.isclose(text.data.size, label_place.size(box), rtol=1e-6), (text.data.size, box)  # none strong
        plate = check_plate(text, drawn, found["severity"])
        heights = [corner.z for corner in corners(text) + corners(plate)]
        if below:
            assert max(heights) < bottom, (text.name, heights, bottom)
        else:
            assert min(heights) > top, (text.name, heights, top)
        assert apart(corners(plate), rect, flat), text.name  # beside the finding, not on it
        leader = drawn[text.name + " leader"]
        foot, anchor = (Vector(tuple(point.co)[:3]) for point in leader.data.splines[0].points)
        assert (foot - text.matrix_world.translation).length < plate.dimensions.x, text.name
        assert leader.data.bevel_depth < findings_draw.TUBE_SHARE * text.data.size
        plates.append([Vector((c.dot(right), c.dot(up), 0.0)) for c in corners(plate)])  # in screen terms
    for i, a in enumerate(plates):
        for b in plates[i + 1:]:
            assert apart(a, b, [Vector((1.0, 0.0)), Vector((0.0, 1.0))]), "labels overlap on screen"
    return texts


def eye_side(space):
    """+1 when a view looks at the board from above, -1 from below."""
    return 1 if (space.region_3d.view_rotation @ Vector((0.0, 0.0, 1.0))).z > 0 else -1


def check_markers(scene):
    """The board's markers: one per finding with a place, out from its side, the "?" for
    one not confirmed; merged on screen worst first; the filter and dismissed leave some
    out; a click on one shows its finding, on a shared one frames them and opens their groups."""
    from kileido import findings_markers
    findings_markers.invalidate()
    placed = {marker["finding"]["title"]: marker for marker in findings_markers.markers()}
    assert set(placed) == {"Tracks too close", "Plane under the via", "A second gap"}, sorted(placed)  # "Gone": no box
    top = findings_draw._surface("F.Cu")[0]
    for title, marker in placed.items():
        assert marker["kind"] == findings_draw.mark(finding_named(title)) and marker["side"] in (1.0, None), title
        assert marker["at"].z > top and np.allclose(marker["at"].xy, framed_centre_box(finding_box(finding_named(title))))
    # Merged on screen: the worst first, nearer than `reach` shares one.
    a, b, c = (placed[title] for title in ("A second gap", "Tracks too close", "Plane under the via"))
    kinds = [{"kind": kind} for kind in ("info", "warning", "error", "unknown")]
    groups = findings_markers.merge([(10.0, 10.0, kinds[0]), (14.0, 10.0, kinds[1]), (60.0, 10.0, kinds[3]),
                                     (16.0, 12.0, kinds[2])], 10.0)
    assert [[m["kind"] for m in members] for _, _, members in groups] == [["error", "warning", "info"], ["unknown"]]
    assert groups[0][:2] == (16.0, 12.0)  # at the worst one's place
    # The filter: errors only; dismissed: none.
    assert {m["finding"]["title"] for m in findings_markers.shown(scene)} == set(placed)
    scene.kileido_markers_level = "error"
    assert [m["finding"]["title"] for m in findings_markers.shown(scene)] == ["Tracks too close"]
    scene.kileido_markers_level = "all"
    a["finding"]["state"] = "dismissed"
    assert "A second gap" not in {m["finding"]["title"] for m in findings_markers.shown(scene)}
    # "Show dismissed" off while a dismissed finding is shown: the list hides it, so does the board.
    scene.kileido_findings_dismissed = True
    show("A second gap")
    assert state.board.findings_shown == (a["source"], a["key"])
    scene.kileido_findings_dismissed = False
    assert state.board.findings_shown == ("", "") and findings_draw.EYE not in objects()
    a["finding"]["state"] = ""
    # A click: hit where it was drawn; it shows the finding and opens its group. A stacked
    # marker shows its first (worst) finding, the next one each further click, round again.
    findings_markers._drawn[1] = [((0.0, 0.0, 20.0, 20.0), [b]), ((30.0, 0.0, 50.0, 20.0), [c, a])]
    assert findings_markers.hit(1, 25.0, 5.0) is None and findings_markers.hit(1, 5.0, 5.0) == [b]
    findings_markers.open_markers(bpy.context, findings_markers.hit(1, 40.0, 5.0))
    assert state.board.findings_shown == (c["source"], c["key"])
    findings_markers.open_markers(bpy.context, findings_markers.hit(1, 40.0, 5.0))
    assert state.board.findings_shown == (a["source"], a["key"])
    findings_markers.open_markers(bpy.context, findings_markers.hit(1, 40.0, 5.0))
    assert state.board.findings_shown == (c["source"], c["key"])
    findings_markers.open_markers(bpy.context, [c])  # the shown one again: hidden
    assert state.board.findings_shown == ("", "")
    opened = [text for kind, text, icon in column(scene) if kind == "operator" and icon == "DISCLOSURE_TRI_DOWN"]
    assert "plane.gap_crossing (2)" in opened, opened
    scene.kileido_findings_open, fold_past = "", findings.FOLD_PAST
    findings.FOLD_PAST = 2  # a long list: its groups start folded
    try:
        assert ("operator", "plane.gap_crossing (2)", "DISCLOSURE_TRI_RIGHT") in column(scene)
        findings_markers.open_markers(bpy.context, [c])  # one: its group opened too, its details under its row
        assert state.board.findings_shown == ("file", c["key"])
        drawn = column(scene)
        assert ("operator", "plane.gap_crossing (2)", "DISCLOSURE_TRI_DOWN") in drawn, drawn
        assert any(text == c["finding"]["title"] for _, text, _ in drawn)
    finally:
        findings.FOLD_PAST = fold_past
        scene.kileido_findings_open = ""
    findings_markers.open_markers(bpy.context, [c])
    findings_markers.open_markers(bpy.context, [b])
    assert state.board.findings_shown == ("file", b["key"])
    findings_markers.open_markers(bpy.context, [b])  # again: hidden, as its title does
    assert state.board.findings_shown == ("", "")
    findings_markers._drawn.clear()


def framed_centre_box(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def check_pull():
    """A label with a part's box around it comes towards the eye, in front of the box,
    scaled by the share of the way so it covers the same pixels; orthographic, along the
    view, unscaled; without the box, back where it was."""
    show("Tracks too close")
    findings_draw._place_eye(force=True)  # laid out for the framed view
    bpy.context.view_layer.update()
    label = objects()["KLS finding 3 label"]
    home = label.matrix_world.translation.copy()
    reach = label.data.size * 2
    part_boxes = findings_draw.part_boxes
    findings_draw.part_boxes = lambda: [(home - Vector((reach,) * 3), home + Vector((reach,) * 3))]
    findings_draw._state["parts"] = None  # the boxes are read once per drawing: again now
    try:
        for ortho in (False, True):
            spaces = list(kileido.objects.view3d_spaces())
            perspectives = [space.region_3d.view_perspective for space in spaces]
            for space in spaces:
                space.region_3d.view_perspective = "ORTHO" if ortho else "PERSP"
            findings_draw._place_eye(force=True)
            bpy.context.view_layer.update()
            eye = objects()[findings_draw.EYE].matrix_world.translation
            moved = label.matrix_world.translation
            low, high = home - Vector((reach,) * 3), home + Vector((reach,) * 3)
            assert not all(low[i] <= moved[i] <= high[i] for i in range(3)), (home, moved)  # out of the box
            if ortho:
                back = -objects()[findings_draw.EYE].matrix_world.col[1].xyz.normalized()
                assert np.isclose(label.scale.x, 1.0) and (moved - home).cross(back).length < 1e-6 * (moved - home).length
                assert (moved - home).dot(back) > reach
            else:
                share = (moved - eye).length / (home - eye).length
                assert share < 1 and np.isclose(label.scale.x, share, rtol=1e-6), (share, label.scale.x)
                assert (moved - eye).normalized().cross((home - eye).normalized()).length < 1e-6  # same pixels
            for space, perspective in zip(spaces, perspectives):
                space.region_3d.view_perspective = perspective
    finally:
        findings_draw.part_boxes = part_boxes
        findings_draw._state["parts"] = None
    findings_draw._place_eye(force=True)
    bpy.context.view_layer.update()
    assert np.isclose(label.scale.x, 1.0) and (label.matrix_world.translation - home).length < 1e-9
    show("Tracks too close")


def check_fold(scene, index):
    """A finding shown while the flex board is folded is not drawn (the column says so);
    unfolding draws it, a hole finding's cut included, without a new findings frame."""
    apply.apply_frame(frame(index, extra=HOLES), {})
    if bpy.app.timers.is_registered(findings_draw._follow):
        bpy.app.timers.unregister(findings_draw._follow)
    folded = findings_draw.fold.folded
    findings_draw.fold.folded = lambda: True
    try:
        found = show("Hole near J1")
        assert state.board.findings_shown == ("file", found["key"]) and not objects()
        assert not scene.kileido_cut and hole_cut.KEY not in scene  # no cut on a folded board
        assert bpy.app.timers.is_registered(findings_draw._follow)  # it watches for the unfolding
        findings_draw.fold.folded = lambda: False
        assert findings_draw._follow() == findings_draw.FOLLOW_S
        assert "KLS finding 2 distance" in objects() and scene.kileido_cut and hole_cut.KEY in scene
        show("Hole near J1")
        assert not objects() and not scene.kileido_cut and hole_cut.KEY not in scene
    finally:
        findings_draw.fold.folded = folded
    apply.apply_frame(frame(index), {})


def check_placement(scene, index):
    """Labels for the bottom copper and an inner layer, seen from above: above the top
    surface, leaders down to their anchors; from below: under the bottom surface. Their
    size follows the finding's box, not the board."""
    apply.apply_frame(frame(index, extra=BELOW), {})
    views = [(space.region_3d.view_rotation.copy(), space.region_3d.view_perspective)
             for space in kileido.objects.view3d_spaces()]
    assert views and all(eye_side(space) > 0 for space in kileido.objects.view3d_spaces())
    found = show("Under the board")
    # A finding on the bottom side only: the views turn to look at it from below, mirrored
    # through the board (the same compass direction and tilt), and its labels follow.
    assert findings_draw.finding_side(found) == -1.0
    for space, (rotation, _) in zip(kileido.objects.view3d_spaces(), views):
        before, after = (r @ Vector((0.0, 0.0, 1.0)) for r in (rotation, space.region_3d.view_rotation))
        assert np.allclose(after, (before.x, before.y, -before.z), atol=1e-6), (before, after)
        right = (rotation @ Vector((1.0, 0.0, 0.0)), space.region_3d.view_rotation @ Vector((1.0, 0.0, 0.0)))
        assert np.allclose(right[1], (right[0].x, right[0].y, -right[0].z), atol=1e-6), right
    findings_draw._follow()
    check_labels(found, 2, below=True)
    for space, (rotation, perspective) in zip(kileido.objects.view3d_spaces(), views):  # seen from above again
        space.region_3d.view_rotation, space.region_3d.view_perspective = rotation, perspective
    findings_draw._follow()
    drawn = objects()
    for number, layer in ((1, "B.Cu"), (2, "In2.Cu")):
        leader = drawn[f"KLS finding {number} label leader"].data.splines[0].points
        anchor = tuple(leader[-1].co)[:3]
        assert np.allclose(anchor[:2], blender_xy(found["draws"][number - 1]["points"][0]), atol=1e-7)
        assert np.isclose(anchor[2], findings_draw._surface(layer)[0], atol=1e-9), (layer, anchor)
    texts = check_labels(found, 2)
    small = texts[0].data.size
    assert np.isclose(small, label_place.FRAME_MIN_M * label_place.LABEL_SHARE), small  # a point: the framed minimum
    bounds = kileido.objects.outline_bounds()
    assert small < (bounds[2] - bounds[0]) / 100  # not a share of the 40 mm board
    for space in kileido.objects.view3d_spaces():  # from below: the labels follow under the board
        space.region_3d.view_rotation = Vector((0.3, -1.0, -0.8)).to_track_quat("Z", "Y")
    findings_draw._follow()
    check_labels(found, 2, below=True)
    for space, (rotation, perspective) in zip(kileido.objects.view3d_spaces(), views):
        space.region_3d.view_rotation, space.region_3d.view_perspective = rotation, perspective
    findings_draw._follow()
    check_labels(found, 2)
    show("Under the board")
    # The bigger box draws bigger text, by its extent.
    large = show("Tracks too close")
    box = finding_box(large)
    size = objects()["KLS finding 3 label"].data.size
    assert np.isclose(size / small, max(box[2] - box[0], box[3] - box[1]) / label_place.FRAME_MIN_M, rtol=1e-6)
    show("Tracks too close")
    # Every label reads the same: what, then the value against the limit with units on both.
    distance = {"label": "Clearance", "measured_nm": 80_000, "limit_nm": 200_000, "limit_is": "min"}
    assert findings_draw.label_text(distance) == "Clearance\n0.080 mm < 0.20 mm min"
    assert findings_draw.label_text(dict(distance, label="Edge clearance", measured_nm=12_000_000,
                                         limit_nm=10_000_000, limit_is="max")) == \
        "Edge clearance\n12.00 mm > 10.00 mm max"
    assert findings_draw.label_text({"label": "Not connected", "measured_nm": 3_200_000}) == "Not connected\n3.20 mm"
    assert findings_draw._short("Hole gap 0.23 mm < 0.25 mm min") == "Hole gap\n0.23 mm < 0.25 mm min"
    assert findings_draw._short("Width 0.25 mm") == "Width\n0.25 mm"
    assert findings_draw._short("Clearance\n0.080 mm < 0.20 mm min") == "Clearance\n0.080 mm < 0.20 mm min"
    assert findings_draw._short("Tracks cross") == "Tracks cross"
    apply.apply_frame(frame(index), {})


MM = 1_000_000


def measured_draw(tool, what, **fields):
    """A measured draw as the bridge sends it: the common fields, `what`, the tool's own."""
    return {"tool": tool, "ok": True, "problem": "", "ids": [], "targets": [], "label": "", "step": 0,
            "emphasis": "normal", "what": what, **fields}


MEASURED = {  # hand-built (the bridge measures these): one finding with every DRC measured tool
    "key": "measured-1", "check": "hs.length_match", "severity": "warning", "title": "Measured geometry", "state": "",
    "bbox_nm": [5 * MM, 1 * MM, 20 * MM, 20 * MM],
    "draws": [
        measured_draw("clearance", "Clearance", points=[[6 * MM, 5 * MM, "F.Cu"], [6 * MM, 5 * MM + 80_000, "F.Cu"]],
                      measured_nm=80_000, limit_nm=200_000, limit_is="min", over=True),
        measured_draw("edge_gap", "Edge gap", points=[[10 * MM, 1 * MM, "Edge.Cuts"], [10 * MM, 3 * MM, "B.Cu"]],
                      measured_nm=2 * MM, limit_nm=300_000, limit_is="min", over=False),
        measured_draw("width", "Width", segments_nm=[[5 * MM, 5 * MM, 8 * MM, 5 * MM, "F.Cu", 300_000],
                                                     [8 * MM, 5 * MM, 12 * MM, 5 * MM, "F.Cu", 200_000],
                                                     [12 * MM, 5 * MM, 12 * MM, 8 * MM, "In1.Cu", 300_000]],
                      narrow=[1], min_nm=200_000, limit_nm=300_000, limit_is="min", over=True,
                      points=[[10 * MM, 5 * MM, "F.Cu"]]),
        measured_draw("width", "Width", ok=False, problem="no track named"),
    ]}


def spline_points(obj, index=0):
    return np.array([tuple(point.co)[:3] for point in obj.data.splines[index].points])


def check_measured(scene, index):
    """The measured tools: clearance and edge gap as dimension lines (red past the limit,
    an edge gap's outline tick longer), width ribbons (the narrow one red) with a callout
    across the thinnest; every label `what` over the value against the limit."""
    header = frame(index)
    header["findings"]["sources"]["file"]["findings"].append(MEASURED)
    apply.apply_frame(header, {})
    found = show("Measured geometry")
    drawn = objects()
    top, bottom = state.board.heights["F.Cu"], state.board.heights["B.Cu"]
    severity = "KLS finding warning"
    # Clearance: a tube between the two closest points, red (over), its label what over value.
    clearance = drawn["KLS finding 1 clearance"]
    ends = spline_points(clearance)
    assert np.allclose(ends[:, :2], [blender_xy(p) for p in MEASURED["draws"][0]["points"]], atol=1e-7), ends
    assert np.all(ends[:, 2] > top) and clearance.data.materials[0].name == "KLS finding over"
    assert drawn["KLS finding 1 clearance label"].data.body == "Clearance\n0.080 mm < 0.20 mm min"
    # Edge gap: on B.Cu at both ends (the outline end takes the copper's layer), within its limit: green with a
    # tick on its label, not the finding's colour; its edge tick longer.
    gap = drawn["KLS finding 2 edge_gap"]
    ends = spline_points(gap)
    assert np.all(ends[:, 2] < bottom) and np.all(ends[:, 2] > bottom - 1e-4), ends
    assert gap.data.materials[0].name == "KLS finding pass"
    assert drawn["KLS finding 2 edge_gap label icon"].data.materials[0].name == "KLS finding icon pass"
    assert drawn["KLS finding 1 clearance label icon"].data.materials[0].name == "KLS finding icon warning"
    edge, copper = (np.ptp(spline_points(gap, i)[:, 0]) for i in (1, 2))  # ticks across the y-going gap
    assert np.isclose(edge / copper, findings_draw.EDGE_TICK), (edge, copper)
    assert drawn["KLS finding 2 edge_gap label"].data.body == "Edge gap\n2.00 mm > 0.30 mm min"
    # Width: ribbons at the real width just over their copper; the narrow one red, a hair further out.
    wide, narrow = drawn["KLS finding 3 width"], drawn["KLS finding 3 width narrow"]
    assert wide.data.materials[0].name == severity and narrow.data.materials[0].name == "KLS finding over"
    corners_ = np.array([tuple(v.co) for v in narrow.data.vertices])
    (x0, y0), (x1, _) = blender_xy([8 * MM, 5 * MM]), blender_xy([12 * MM, 5 * MM])
    assert np.isclose(np.ptp(corners_[:, 1]), 0.2e-3, atol=1e-7) and np.isclose(corners_[:, 1].mean(), y0, atol=1e-7)
    assert np.isclose(corners_[:, 0].min(), x0 - 0.1e-3, atol=1e-7) and np.isclose(corners_[:, 0].max(), x1 + 0.1e-3,
                                                                                     atol=1e-7)
    assert np.all(corners_[:, 2] > top) and corners_[:, 2].min() > np.array([v.co.z for v in wide.data.vertices
                                                                              if v.co.z > top]).max()
    inner = [v.co.z for v in wide.data.vertices if v.co.z < top]  # the In1.Cu segment on its own layer
    assert inner and np.allclose(inner, findings_draw._surface("In1.Cu", findings_draw.AREA_LIFT_M)[0])
    across = spline_points(drawn["KLS finding 3 width callout"])  # edge to edge, poking out past both
    assert np.allclose(across[:, 0], blender_xy([10 * MM, 5 * MM])[0], atol=1e-7)
    assert np.ptp(across[:, 1]) > 0.2e-3 and np.isclose(across[:, 1].mean(), y0, atol=1e-7)
    ticks = [spline_points(drawn["KLS finding 3 width callout"], i) for i in (1, 2)]
    assert np.isclose(abs(ticks[0][0, 1] - ticks[1][0, 1]), 0.2e-3, atol=1e-7)  # the ticks on the track's edges
    assert drawn["KLS finding 3 width label"].data.body == "Width\n0.20 mm < 0.30 mm min"
    assert "KLS finding 4 width" not in drawn  # not drawn: the bridge found no track
    check_labels(found, 3)  # every label beside the box, above the board, apart on screen
    # The draw not drawn and the measured values in the column.
    texts = [text for _, text, _ in column(scene)]
    for line in ("Clearance: 0.080 mm < 0.20 mm min", "Edge gap: 2.00 mm > 0.30 mm min",
                 "Width: 0.20 mm < 0.30 mm min", "1 of 3 segments too narrow", "width not drawn: no track named"):
        assert line in texts, (line, texts)
    # `what` first, then the draw's label, then the tool's name.
    assert findings_draw.label_text({"tool": "distance", "what": "Clearance", "label": "too close",
                                     "measured_nm": 80_000}) == "Clearance\n0.080 mm"
    assert findings_draw.label_text({"tool": "clearance", "measured_nm": 80_000}) == "Clearance\n0.080 mm"
    assert findings_draw.label_text({"tool": "width", "min_nm": 150_000, "limit_nm": 100_000, "limit_is": "min"}) == \
        "Width\n0.15 mm > 0.10 mm min"
    show("Measured geometry")
    apply.apply_frame(frame(index), {})


ROUTED = {  # hand-built (the bridge measures these): a routed length and a bar chart
    "key": "measured-2", "check": "hs.length_match", "severity": "warning", "title": "Routed and charted",
    "state": "", "bbox_nm": [5 * MM, 1 * MM, 20 * MM, 20 * MM],
    "draws": [
        measured_draw("routed_length", "Routed length",  # down a via at (8, 11), up one at (14, 13) mm
                      path_nm=[[5 * MM, 11 * MM, "F.Cu"], [8 * MM, 11 * MM, "F.Cu"], [8 * MM, 11 * MM, "B.Cu"],
                               [12 * MM, 13 * MM, "B.Cu"], [14 * MM, 13 * MM, "F.Cu"]],
                      measured_nm=23_400_000, points=[[5 * MM, 11 * MM, "F.Cu"], [14 * MM, 13 * MM, "F.Cu"]]),
        measured_draw("bars", "Length mismatch", unit="mm", points=[[20 * MM, 20 * MM, "F.Cu"]],
                      bars=[{"label": "CLK_P", "value": 12.0, "limit": 10.0, "over": True},
                            {"label": "CLK_N", "value": 8.0, "limit": 10.0, "over": False},
                            {"label": "DATA0", "value": 5.0, "limit": 10.0}]),
    ]}


def check_routed(scene, index):
    """A routed path stepping through the board at its vias, ends marked; a bar chart on
    its label's plate, each bar scaled to the largest, the over one red, the limit across;
    the values in the column."""
    header = frame(index)
    header["findings"]["sources"]["file"]["findings"].append(ROUTED)
    apply.apply_frame(header, {})
    found = show("Routed and charted")
    drawn = objects()
    top, bottom = state.board.heights["F.Cu"], state.board.heights["B.Cu"]
    severity = "KLS finding warning"
    # Routed length: along the path just out from the copper, straight through the board at each via.
    path = spline_points(drawn["KLS finding 1 routed_length"])
    radius = drawn["KLS finding 1 routed_length"].data.bevel_depth
    expected = [([5, 11], top), ([8, 11], top), ([8, 11], bottom), ([12, 13], bottom), ([14, 13], bottom),
                ([14, 13], top)]  # the second via reached on B.Cu, then up
    assert len(path) == len(expected), path
    for point, (xy, z) in zip(path, expected):
        assert np.allclose(point[:2], blender_xy([xy[0] * MM, xy[1] * MM]), atol=1e-7), (point, xy)
        assert np.isclose(point[2], z + (radius if z == top else -radius), atol=2e-5), (point, z)
    for end, at in (("start", path[0]), ("end", path[-1])):
        assert np.allclose(drawn[f"KLS finding 1 routed_length {end}"].matrix_world.translation, at, atol=1e-7)
    assert drawn["KLS finding 1 routed_length label"].data.body == "Routed length\n23.40 mm"
    # Bars: under the title on its plate, one bar each scaled to the largest, the over one red, the limit across.
    title = drawn["KLS finding 2 bars"]
    assert title.data.body == "Length mismatch\nlimit 10 mm", title.data.body
    size = title.data.size
    bars = [drawn[f"KLS finding 2 bars bar {n}"] for n in (1, 2, 3)]
    assert "KLS finding 2 bars bar 4" not in drawn
    assert all(bar.parent == title and bar.show_in_front for bar in bars)
    heights = [np.ptp([v.co.y for v in bar.data.vertices]) for bar in bars]
    assert np.allclose(heights, np.array([12, 8, 5]) / 12 * findings_draw.BAR_HEIGHT * size, rtol=1e-4), heights
    assert [bar.data.materials[0].name for bar in bars] == ["KLS finding over", severity, severity]
    base = min(v.co.y + bar.location.y for bar in bars for v in bar.data.vertices)
    limit = in_text(drawn["KLS finding 2 bars limit"])
    assert np.isclose(limit[:, 1].mean(), base + 10 / 12 * findings_draw.BAR_HEIGHT * size, rtol=1e-4)
    lows = [bar.location.x - findings_draw.BAR * size / 2 for bar in bars]
    assert limit[:, 0].min() <= min(lows) and limit[:, 0].max() >= max(lows) + findings_draw.BAR * size  # across all
    for number, (value, name) in enumerate((("12", "CLK_P"), ("8", "CLK_N"), ("5", "DATA0")), start=1):
        over = drawn[f"KLS finding 2 bars bar {number} value"]
        assert over.data.body == value and over.location.y > bars[number - 1].location.y + heights[number - 1] / 2
        assert drawn[f"KLS finding 2 bars bar {number} name"].data.body == name
        assert drawn[f"KLS finding 2 bars bar {number} name"].location.y < base
    plate = in_text(drawn["KLS finding 2 bars plate"])
    for part in [*bars, drawn["KLS finding 2 bars limit"], drawn["KLS finding 2 bars base"]]:
        local = in_text(part)
        assert plate[:, 0].min() < local[:, 0].min() and local[:, 0].max() < plate[:, 0].max(), part.name
        assert plate[:, 1].min() < local[:, 1].min() and local[:, 1].max() < plate[:, 1].max(), part.name
        assert local[:, 2].max() > 0  # stands out of the plate, towards the eye
    check_labels(found, 2)  # the path's label and the chart's, beside the box, above the board, apart on screen
    texts = [text for _, text, _ in column(scene)]
    for line in ("Routed length: 23.40 mm", "Length mismatch", "CLK_P: 12 mm", "CLK_N: 8 mm", "DATA0: 5 mm",
                 "limit: 10 mm"):
        assert line in texts, (line, texts)
    bars_column = []
    findings._draw_bars(Layout(bars_column), wrap, {"unit": "ps", "bars": [{"label": "A", "value": 3.5, "limit": 2},
                                                                           {"label": "B", "value": 1.0}]})
    assert [text for _, text, _ in bars_column] == ["A: 3.5 ps (limit 2)", "B: 1 ps"], bars_column
    show("Routed and charted")
    apply.apply_frame(frame(index), {})


def plane_draw(tool, what, **fields):
    """A plane draw as the bridge sends it: the measured fields and the plane keys, empty
    unless given (kileido_bridge findings._empty_draw)."""
    return measured_draw(tool, what, **{"planes": [], "spans_nm": [], "covered_nm": [], "edges_nm": [], "returns": [],
                                        "fits": None, "islands": [], "barrel": {}, "marks": [], "layer": "",
                                        "measured_nm": None, "limit_nm": None, "limit_is": "", "over": False,
                                        **fields})


C_SHAPE = [[14, 1], [20, 1], [20, 2], [15, 2], [15, 6], [20, 6], [20, 7], [14, 7]]  # mm: its centroid in the notch
PLANES = {  # hand-built (the bridge measures these): one finding with every plane tool
    "key": "planes-1", "check": "plane.gap_crossing", "severity": "warning", "title": "Planes and returns", "state": "",
    "bbox_nm": [5 * MM, 1 * MM, 20 * MM, 16 * MM],
    "draws": [
        plane_draw("layer_change", "Return via", ids=[VIA, "return-1", "return-2"],
                   points=[[8 * MM, 8 * MM, "F.Cu"], [9_400_000, 8 * MM, "In2.Cu"]],  # drawn on the via's layer
                   measured_nm=1_400_000, limit_nm=1 * MM, limit_is="max", over=True,
                   planes=["GND@In1.Cu", "GND@In2.Cu"],
                   returns=[[9_400_000, 8 * MM, 600_000, "return-1"], [6 * MM, 10 * MM, 400_000, "return-2"]]),
        plane_draw("plane_gap", "Gap", layer="In1.Cu", ids=[TRACK], planes=["GND@In1.Cu"],
                   spans_nm=[[10 * MM, 5 * MM, 11_200_000, 5 * MM, "F.Cu", 250_000],
                             [13 * MM, 5 * MM, 13_500_000, 5 * MM, "F.Cu", 250_000]],
                   edges_nm=[[[10 * MM, 3 * MM], [10 * MM, 7 * MM]], [[11_200_000, 3 * MM], [11_200_000, 7 * MM]]],
                   points=[[10_600_000, 5 * MM, "F.Cu"], [13_250_000, 5 * MM, "F.Cu"]], measured_nm=1_200_000),
        plane_draw("return_path", "Without plane", layer="In1.Cu", ids=[OTHER],
                   covered_nm=[[5 * MM, 15 * MM, 13 * MM, 15 * MM, "F.Cu", 200_000]],
                   spans_nm=[[13 * MM, 15 * MM, 15 * MM, 15 * MM, "F.Cu", 200_000]], measured_nm=2 * MM,
                   planes=["GND@In1.Cu"]),
        plane_draw("plane_islands", "Islands", ids=[ZONE], islands=[
            {"index": 1, "layer": "In1.Cu", "polygon_nm": [[x * MM, y * MM] for x, y in C_SHAPE], "area_mm2": 16.0,
             "main": True, "tied": True},
            {"index": 2, "layer": "In1.Cu", "polygon_nm": [[16 * MM, 9 * MM], [17 * MM, 9 * MM], [17 * MM, 10 * MM],
                                                           [16 * MM, 10 * MM], [16 * MM, 9 * MM]],  # closed
             "area_mm2": 1.0, "main": False, "tied": False},
            {"index": 3, "layer": "In2.Cu", "polygon_nm": [[18 * MM, 9 * MM], [20 * MM, 9 * MM], [19 * MM, 11 * MM]],
             "area_mm2": 2.0, "main": False, "tied": True}]),
    ]}
NO_RETURN = {
    "key": "planes-2", "check": "plane.layer_change_no_return", "severity": "error", "title": "No return via",
    "state": "", "bbox_nm": [7 * MM, 7 * MM, 9 * MM, 9 * MM],
    "draws": [plane_draw("layer_change", "Return via", ids=[VIA], points=[[8 * MM, 8 * MM, "F.Cu"]], limit_nm=1 * MM,
                         limit_is="max", planes=["GND@In1.Cu"])]}


def leader_anchor(drawn, label):
    return np.array(tuple(drawn[label + " leader"].data.splines[0].points[-1].co)[:3])


def check_planes(scene, index):
    """A layer change's via highlighted with a measured tube on its layer to the nearest
    return via (red past the limit), the others ringed, no ghost of its own; a plane
    gap's plane edges lit up on the plane's layer and named, the spans without plane red
    with end ticks and the gap labelled; a return path's covered stretches faint, the rest
    red, with the share covered; plane islands as areas on their layers, each its own
    colour, the loose one outlined red, numbered labels and the count; the column's lines."""
    header = frame(index)
    header["findings"]["sources"]["file"]["findings"] += [PLANES, NO_RETURN]
    apply.apply_frame(header, {})
    found = show("Planes and returns")
    drawn = objects()
    top = state.board.heights["F.Cu"]
    severity, over = "KLS finding warning", "KLS finding over"
    assert state.board.highlight["finding"] == {VIA}, state.board.highlight["finding"]  # the via, not its returns
    assert not state.board.collection.all_objects["KLS vias highlight finding"].hide_get()
    # Layer change: the tube on F.Cu (the return's In2.Cu ignored), red past 1 mm; one ring, at the other return.
    tube = drawn["KLS finding 1 layer_change"]
    ends = spline_points(tube)
    assert np.allclose(ends[:, :2], [blender_xy([8 * MM, 8 * MM]), blender_xy([9_400_000, 8 * MM])], atol=1e-7), ends
    assert np.all(ends[:, 2] > top) and np.all(ends[:, 2] < top + 1e-4), ends
    assert tube.data.materials[0].name == over
    assert drawn["KLS finding 1 layer_change label"].data.body == "Return via\n1.40 mm > 1.00 mm max"
    rings = drawn["KLS finding 1 layer_change returns"]
    assert len(rings.data.splines) == 1 and rings.data.materials[0].name == severity
    ring = spline_points(rings)
    centre = blender_xy([6 * MM, 10 * MM])
    assert np.allclose(np.hypot(ring[:, 0] - centre[0], ring[:, 1] - centre[1]), 0.2e-3 * findings_draw.RING, atol=1e-7)
    assert np.all(ring[:, 2] > top)
    # The limit: a thin circle of 1 mm round the via on its layer, where a return via must be.
    limit = drawn["KLS finding 1 layer_change limit"]
    circle, centre = spline_points(limit), blender_xy([8 * MM, 8 * MM])
    assert np.allclose(np.hypot(circle[:, 0] - centre[0], circle[:, 1] - centre[1]), 1e-3, atol=1e-7)
    assert np.all(circle[:, 2] > top) and limit.data.bevel_depth < tube.data.bevel_depth
    assert "KLS finding 1 layer_change ghost" not in drawn  # suggesting one is the LLM's (a ghost draw)
    # Plane gap: the plane's edges on In1.Cu, named; the spans red on the track with a tick across each end.
    edges = drawn["KLS finding 2 plane_gap edges"]
    assert len(edges.data.splines) == 2 and edges.data.materials[0].name == severity
    inner = findings_draw._surface("In1.Cu")[0]
    for number, x in enumerate((10 * MM, 11_200_000)):
        line = spline_points(edges, number)
        assert np.allclose(line[:, 0], blender_xy([x, 0])[0], atol=1e-7), line
        assert np.all(np.abs(line[:, 2] - inner) < 1e-4) and np.all(line[:, 2] < top), (line, inner)
    assert drawn["KLS finding 2 plane_gap edges label"].data.body == "Plane edge\nGND@In1.Cu"
    spans = drawn["KLS finding 2 plane_gap spans"]
    assert spans.data.materials[0].name == over and spans.show_in_front
    corners_ = np.array([tuple(v.co) for v in spans.data.vertices])
    (x0, y0), (x1, _) = blender_xy([10 * MM, 5 * MM]), blender_xy([13_500_000, 5 * MM])
    assert np.isclose(np.ptp(corners_[:, 1]), 0.25e-3, atol=1e-7) and np.isclose(corners_[:, 1].mean(), y0, atol=1e-7)
    assert np.isclose(corners_[:, 0].min(), x0 - 0.125e-3, atol=1e-7) and np.isclose(corners_[:, 0].max(),
                                                                                       x1 + 0.125e-3, atol=1e-7)
    assert np.all(corners_[:, 2] > top)
    ends = drawn["KLS finding 2 plane_gap span ends"]
    assert len(ends.data.splines) == 4 and ends.data.materials[0].name == over
    assert all(np.ptp(spline_points(ends, i)[:, 1]) > 0.25e-3 for i in range(4))  # across, past the ribbon
    assert drawn["KLS finding 2 plane_gap label"].data.body == "Gap\n1.20 mm"
    anchor = leader_anchor(drawn, "KLS finding 2 plane_gap label")
    assert np.allclose(anchor[:2], blender_xy([10_600_000, 5 * MM]), atol=1e-7) and anchor[2] > top  # the longest
    # Return path: covered faint, uncovered red; the share covered on the label.
    assert drawn["KLS finding 3 return_path covered"].data.materials[0].name == severity + " ghost"
    assert drawn["KLS finding 3 return_path spans"].data.materials[0].name == over
    assert drawn["KLS finding 3 return_path label"].data.body == "Without plane\n2.00 mm (80 % covered)"
    anchor = leader_anchor(drawn, "KLS finding 3 return_path label")
    assert np.allclose(anchor[:2], blender_xy([14 * MM, 15 * MM]), atol=1e-7)
    # Plane islands: an area per piece on its layer, its own colour; the loose one outlined red.
    name = "KLS finding 4 plane_islands"
    heights = {1: "In1.Cu", 2: "In1.Cu", 3: "In2.Cu"}
    colours = set()
    for number, layer in heights.items():
        area = drawn[f"{name} island {number}"]
        assert len(area.data.polygons) == 1 and area.show_in_front
        assert np.isclose(area.matrix_world.translation.z, findings_draw._surface(layer, findings_draw.AREA_LIFT_M)[0])
        assert area.data.materials[0].name == f"KLS finding island {number}"
        colours.add(tuple(round(c, 4) for c in area.data.materials[0].diffuse_color[:3]))
        assert (f"{name} island {number} outline" in drawn) == (number == 2), number
    assert len(colours) == 3 and len(drawn[f"{name} island 2"].data.vertices) == 4  # the closing point dropped
    assert drawn[f"{name} island 2 outline"].data.materials[0].name == over
    for number, body in ((1, "Island 1, In1.Cu\n16.0 mm², main"), (2, "Island 2, In1.Cu\n1.0 mm², not tied"),
                         (3, "Island 3, In2.Cu\n2.0 mm², tied")):
        assert drawn[f"{name} island {number} label"].data.body == body, number
    assert drawn[f"{name} label"].data.body == "Islands\n3 pieces, 1 not tied"
    anchor = leader_anchor(drawn, f"{name} island 1 label")  # the C's centroid is in its notch: a corner instead
    assert any(np.allclose(anchor[:2], blender_xy([x * MM, y * MM]), atol=1e-7) for x, y in C_SHAPE), anchor
    anchor = leader_anchor(drawn, f"{name} island 2 label")
    assert np.allclose(anchor[:2], blender_xy([16_500_000, 9_500_000]), atol=1e-7)
    check_labels(found, 8)
    texts = [text for _, text, _ in column(scene)]
    for line in ("Return via: 1.40 mm > 1.00 mm max", "Planes: GND@In1.Cu, GND@In2.Cu", "Gap: 1.20 mm",
                 "Planes: GND@In1.Cu", "Without plane: 2.00 mm", "80 % covered", "Islands: 3 pieces, 1 not tied",
                 "1: 16.0 mm², main (In1.Cu)", "2: 1.0 mm², not tied (In1.Cu)", "3: 2.0 mm², tied (In2.Cu)"):
        assert line in texts, (line, texts)
    # No return via at all: the label at the via says so, red; the limit round it.
    found = show("No return via")
    drawn = objects()
    assert "KLS finding 1 layer_change" not in drawn and "KLS finding 1 layer_change returns" not in drawn
    label = drawn["KLS finding 1 layer_change label"]
    assert label.data.body == "Return via\nnone found"
    assert drawn["KLS finding 1 layer_change label leader"].data.materials[0].name == over
    assert np.allclose(leader_anchor(drawn, "KLS finding 1 layer_change label")[:2], blender_xy([8 * MM, 8 * MM]),
                       atol=1e-7)
    assert "KLS finding 1 layer_change limit" in drawn
    assert ("label", "Return via: none found", "DRIVER_DISTANCE") in column(scene)
    # Texts alone: the share never 100 % while a stretch is without plane; islands counted.
    nearly = {"covered_nm": [[0, 0, 999, 0, "F.Cu", 1]], "spans_nm": [[0, 0, 1, 0, "F.Cu", 1]]}
    assert findings_draw.covered_text(nearly) == "99 % covered"
    assert findings_draw.covered_text({}) == ""
    assert findings_draw.islands_text({"islands": [{"main": True}]}) == "1 piece, 0 not tied"
    assert findings_draw.island_text({"area_mm2": 1234.5, "tied": False}) == "1234 mm², not tied"
    show("No return via")
    apply.apply_frame(frame(index), {})


MARKS = [{"x": 13 * MM, "y": 5 * MM, "layer": "F.Cu", "kind": "width", "text": "0.20 -> 0.15 mm"},
         {"x": 14 * MM, "y": 12 * MM, "layer": "F.Cu", "kind": "layer", "text": "F.Cu -> In2.Cu"},
         {"x": 16 * MM, "y": 14 * MM, "layer": "In2.Cu", "kind": "reference", "text": "GND@In1.Cu -> GND@In2.Cu"}]
VIAS = {  # hand-built (the bridge measures these): one finding with every via and step tool
    "key": "planes-3", "check": "plane.stitching_sparse", "severity": "warning", "title": "Stitching and stubs",
    "state": "", "bbox_nm": [4 * MM, 2 * MM, 16 * MM, 14 * MM],
    "draws": [
        plane_draw("stitching", "Stitching gap", layer="In1.Cu", ids=["via-a", "via-b", "via-c", "via-d"],
                   path_nm=[[4 * MM, 2 * MM, "In1.Cu"], [16 * MM, 2 * MM, "In1.Cu"]],
                   returns=[[5 * MM, 2 * MM, 600_000, "via-a"], [6 * MM, 2 * MM, 600_000, "via-b"],
                            [9_200_000, 2 * MM, 600_000, "via-c"], [11 * MM, 2 * MM, 600_000, "via-d"]],
                   points=[[6 * MM, 2 * MM, "In1.Cu"], [9_200_000, 2 * MM, "In1.Cu"]],
                   edges_nm=[[[6 * MM, 2 * MM], [9_200_000, 2 * MM]],  # the widest, then the other over the limit
                             [[11 * MM, 2 * MM], [16 * MM, 2 * MM], [16 * MM, 5 * MM]]],
                   measured_nm=3_200_000, limit_nm=2 * MM, limit_is="max", over=True),
        plane_draw("stub", "Stub", ids=[VIA], points=[[8 * MM, 8 * MM, "F.Cu"]], measured_nm=850_000,
                   barrel={"x": 8 * MM, "y": 8 * MM, "size_nm": 600_000, "used": ["F.Cu", "In1.Cu"],
                           "stub": ["In1.Cu", "B.Cu"]}),
        plane_draw("discontinuity", "Discontinuity", ids=[TRACK], marks=MARKS,
                   points=[[mark["x"], mark["y"], mark["layer"]] for mark in MARKS]),
    ]}
MANY = [{"x": (4 + n) * MM, "y": 16 * MM, "layer": "F.Cu", "kind": "width", "text": f"0.{20 + n} -> 0.15 mm"}
        for n in range(10)]
NO_STUB = {
    "key": "planes-4", "check": "hs.stub", "severity": "info", "title": "No stub", "state": "",
    "bbox_nm": [4 * MM, 2 * MM, 16 * MM, 16 * MM],
    "draws": [plane_draw("stub", "Stub", ids=[VIA], points=[[8 * MM, 8 * MM, "F.Cu"]], measured_nm=0,
                         barrel={"x": 8 * MM, "y": 8 * MM, "size_nm": 600_000, "used": ["F.Cu", "B.Cu"], "stub": []}),
              plane_draw("discontinuity", "Discontinuity", ids=[TRACK], marks=MANY),
              plane_draw("stitching", "Stitching gap", layer="F.Cu", path_nm=[[4 * MM, 2 * MM], [16 * MM, 2 * MM]],
                         points=[[10 * MM, 2 * MM, "F.Cu"]], measured_nm=12 * MM, limit_nm=2 * MM,
                         limit_is="max", over=True)]}


def check_vias_and_steps(scene, index):
    """Stitching's vias ringed on its layer, no ghost of its own where one is missing, the
    stretches over the limit along its line, the widest measured between its neighbouring
    vias (red past the limit); a stub's unused barrel red between its layers' copper, the used part faint;
    a discontinuity's markers by kind (bar, ring, diamond) on their layers, labelled with
    their text, the first MARK_LABELS only; a via with no stub says "none"; the column's lines."""
    header = frame(index)
    header["findings"]["sources"]["file"]["findings"] += [VIAS, NO_STUB]
    apply.apply_frame(header, {})
    found = show("Stitching and stubs")
    drawn = objects()
    top, bottom = state.board.heights["F.Cu"], state.board.heights["B.Cu"]
    severity, over = "KLS finding warning", "KLS finding over"
    inner, deeper = findings_draw._surface("In1.Cu")[0], findings_draw._surface("In2.Cu")[0]
    # Stitching: four vias ringed on In1.Cu; each stretch over the limit a thin red line along
    # the pour edge with end ticks, the widest (3.2 mm, between the 2nd and 3rd vias) labelled.
    assert "KLS finding 1 stitching path" not in drawn and "KLS finding 1 stitching ghost" not in drawn
    rings = drawn["KLS finding 1 stitching vias"]
    assert len(rings.data.splines) == 4 and rings.data.materials[0].name == severity
    for number, x in enumerate((5 * MM, 6 * MM, 9_200_000, 11 * MM)):
        ring, centre = spline_points(rings, number), blender_xy([x, 2 * MM])
        assert np.allclose(np.hypot(ring[:, 0] - centre[0], ring[:, 1] - centre[1]), 0.3e-3 * findings_draw.RING,
                           atol=1e-7), number
        assert np.all(np.abs(ring[:, 2] - inner) < 1e-4)
    tube = drawn["KLS finding 1 stitching"]
    assert len(tube.data.splines) == 2 + 4 and tube.data.materials[0].name == over  # two stretches, four ticks
    ends = spline_points(tube)
    assert np.allclose(ends[:, :2], [blender_xy([6 * MM, 2 * MM]), blender_xy([9_200_000, 2 * MM])], atol=1e-7), ends
    assert np.all(np.abs(ends[:, 2] - inner) < 1e-4) and np.all(ends[:, 2] < top)
    assert len(spline_points(tube, 1)) == 3  # along the pour edge round its corner
    assert tube.data.bevel_depth < rings.data.bevel_depth  # a hairline, as a label's leader
    assert drawn["KLS finding 1 stitching label"].data.body == "Stitching gap\n3.20 mm > 2.00 mm max"
    # Stub: red from In1.Cu down to B.Cu, the used part faint from F.Cu to In1.Cu, just around the via.
    stub, used = drawn["KLS finding 2 stub"], drawn["KLS finding 2 stub used"]
    centre = blender_xy([8 * MM, 8 * MM])
    upper, lower = findings_draw._surface("F.Cu")[0], findings_draw._surface("B.Cu")[0]  # the copper's outer faces
    for part, (low, high), material in ((stub, (lower, inner), over), (used, (inner, upper), severity + " ghost")):
        world = np.array([tuple(part.matrix_world @ v.co) for v in part.data.vertices])
        assert np.isclose(world[:, 2].min(), min(low, high), atol=1e-6) and \
            np.isclose(world[:, 2].max(), max(low, high), atol=1e-6), (part.name, world[:, 2].min(), low, high)
        assert np.isclose(np.hypot(world[:, 0] - centre[0], world[:, 1] - centre[1]).max(),
                          0.3e-3 * findings_draw.BARREL, atol=1e-7), part.name
        assert part.data.materials[0].name == material and part.show_in_front
    assert drawn["KLS finding 2 stub label"].data.body == "Stub\n0.85 mm"
    assert drawn["KLS finding 2 stub label leader"].data.materials[0].name == over
    anchor = leader_anchor(drawn, "KLS finding 2 stub label")
    assert np.allclose(anchor, [*centre, (lower + inner) / 2], atol=1e-7), anchor
    # Discontinuity: a bar, a ring and a diamond on their layers, each labelled with its kind over its text.
    size = drawn["KLS finding 2 stub label"].data.size
    half = size * findings_draw.MARK / 2
    shapes = {1: 2, 2: findings_draw.RING_SEGMENTS + 1, 3: 5}
    for number, mark in enumerate(MARKS, start=1):
        marker = spline_points(drawn[f"KLS finding 3 discontinuity mark {number}"])
        centre = blender_xy([mark["x"], mark["y"]])
        assert len(marker) == shapes[number], (number, len(marker))
        assert np.allclose(np.hypot(marker[:, 0] - centre[0], marker[:, 1] - centre[1]).max(), half, rtol=1e-4)
        z = findings_draw._surface(mark["layer"])[0]
        assert np.all(np.abs(marker[:, 2] - z) < 1e-4), (number, marker[:, 2], z)
    assert np.all(spline_points(drawn["KLS finding 3 discontinuity mark 3"])[:, 2] < inner)  # on In2.Cu
    assert abs(deeper - inner) > 1e-5
    for number, body in ((1, "Width step\n0.20 -> 0.15 mm"), (2, "Layer change\nF.Cu -> In2.Cu"),
                         (3, "Reference change\nGND@In1.Cu -> GND@In2.Cu")):
        assert drawn[f"KLS finding 3 discontinuity mark {number} label"].data.body == body, number
    check_labels(found, 5)
    texts = [text for _, text, _ in column(scene)]
    for line in ("Stitching gap: 3.20 mm > 2.00 mm max", "Stub: 0.85 mm", "width: 0.20 -> 0.15 mm",
                 "layer: F.Cu -> In2.Cu", "reference: GND@In1.Cu -> GND@In2.Cu"):
        assert line in texts, (line, texts)
    # No stub: only the faint used barrel, through the board, and "none"; marks past MARK_LABELS
    # unlabelled in 3D but all in the column; stitching without vias labelled at its line's middle.
    show("No stub")
    drawn = objects()
    assert "KLS finding 1 stub" not in drawn and drawn["KLS finding 1 stub used"].dimensions.z > top - bottom - 1e-5
    assert drawn["KLS finding 1 stub label"].data.body == "Stub\nnone"
    assert all(f"KLS finding 2 discontinuity mark {n}" in drawn for n in range(1, 11))
    labelled = [n for n in range(1, 11) if f"KLS finding 2 discontinuity mark {n} label" in drawn]
    assert labelled == list(range(1, findings_draw.MARK_LABELS + 1)), labelled
    assert "KLS finding 3 stitching" not in drawn and "KLS finding 3 stitching vias" not in drawn
    assert drawn["KLS finding 3 stitching label"].data.body == "Stitching gap\n12.00 mm > 2.00 mm max"
    anchor = leader_anchor(drawn, "KLS finding 3 stitching label")
    assert np.allclose(anchor[:2], blender_xy([10 * MM, 2 * MM]), atol=1e-7) and anchor[2] > top, anchor
    drawn_column = column(scene)
    texts = [text for _, text, _ in drawn_column]
    assert ("label", "Stub: none", "DRIVER_DISTANCE") in drawn_column
    assert not any(text.startswith("Stub: 0") for text in texts), texts
    assert all(findings_draw.mark_text(mark) in texts for mark in MANY), texts
    assert findings_draw.mark_text({"kind": "layer"}) == "layer" and not findings_draw.stubbed({"measured_nm": 0})
    show("No stub")
    apply.apply_frame(frame(index), {})


def check_severity_icons():
    """The rows' severity icons coloured as their 3D labels': the shape's colour at the
    edge, the glyph's ink in the middle, see-through corners."""
    for severity, (shape, color, mark, ink) in findings_draw.ICONS.items():
        if severity in findings.SEVERITY_ICONS:
            findings.severity_icon(severity)
        else:  # "pass" is a 3D label's icon only: the column never asks for it, painted here to check it
            findings._paint_icon(findings._icons.new(severity), severity)
        preview = findings._icons[severity]
        size = findings.ICON_PX
        pixels = np.array(preview.icon_pixels_float[:]).reshape(size, size, 4)
        assert pixels[0, 0, 3] == 0 and pixels[-1, -1, 3] == 0, severity  # corners clear
        side = pixels[size // 2 - 1 if shape == "circle" else 5, 6 if shape == "circle" else 9]
        assert side[3] == 1 and np.allclose(side[:3], color, atol=1 / 255), (severity, side)
        if mark == "✓":  # the tick's corner
            corner = findings_draw.CHECK[1]
            middle = pixels[round(16 + corner[1] * 14), round(16 + corner[0] * 14)]
        else:
            middle = pixels[16 if shape == "circle" else 15, size // 2]  # the bar of the "!" or "i"
        assert np.allclose(middle[:3], ink, atol=1 / 255), (severity, middle)
    assert sorted(findings._icons) == sorted(findings_draw.ICONS)


def check_column(scene):
    drawn = column(scene)
    texts = [text for _, text, _ in drawn]
    assert "Test tool, run 4: 4 findings" in texts, texts
    assert ("operator", "KiCad DRC", "PLAY") in drawn, drawn
    drc, llm = ([text for _, text, _ in column(scene, tabs=(tab,))] for tab in ("DRC", "LLM"))
    assert "KiCad DRC" in drc and "KiCad DRC" not in llm  # the DRC button only in the DRC tab
    assert "Test tool, run 4: 4 findings" in llm and "Test tool, run 4: 4 findings" not in drc
    assert not any(text.startswith("LLM prompt") for text in drc) and \
        any(text.startswith("LLM prompt") for text in llm)  # the prompt box only in the LLM tab
    assert ("operator", "plane.gap_crossing (2)", "DISCLOSURE_TRI_DOWN") in drawn, drawn  # under 20: open
    order = [text for kind, text, _ in drawn if kind == "operator" and text in ("Tracks too close",
                                                                                 "plane.gap_crossing (2)", "Gone")]
    assert order == ["Tracks too close", "plane.gap_crossing (2)", "Gone"], order  # errors first
    warning, unknown = (next(iter(findings.severity_icon(kind).values())) for kind in ("warning", "unknown"))
    assert ("operator", "A second gap", warning) in drawn
    # Not confirmed, a "?": its items gone.
    assert ("operator", "Gone", unknown) in drawn and findings_draw.mark(finding_named("Gone")) == "unknown"
    assert findings_draw.mark(finding_named("Tracks too close")) == "error"
    check_severity_icons()
    # The checks the file says it skipped, with why.
    assert "emc.ground: no stackup" in texts and not any(text.startswith("KiCad DRC,") for text in texts), texts
    gone = finding_named("Gone")
    assert gone["faded"] and not gone["draws"][0]["ok"]
    # Confirm, then dismiss: the rows at once (the bridge would write its state file).
    key = finding_named("Tracks too close")["key"]
    assert bpy.ops.kileido.finding_state(source="file", key=key, action="confirm") == {"FINISHED"}
    assert ("operator", "Tracks too close", "CHECKMARK") in column(scene)
    assert bpy.ops.kileido.finding_state(source="file", key=key, action="confirm") == {"FINISHED"}
    assert finding_named("Tracks too close")["state"] == ""
    assert bpy.ops.kileido.finding_state(source="file", key=key, action="dismiss") == {"FINISHED"}
    texts = [text for _, text, _ in column(scene)]
    assert "Test tool, run 4: 4 findings, 1 dismissed" in texts and "Tracks too close" not in texts, texts
    scene.kileido_findings_dismissed = True
    assert "Tracks too close" in [text for _, text, _ in column(scene)]
    scene.kileido_findings_dismissed = False
    bpy.ops.kileido.finding_state(source="file", key=key, action="dismiss")
    assert sent == [("confirm", key, "file"), ("reset", key, "file"), ("dismiss", key, "file"),
                    ("reset", key, "file")], sent
    assert bpy.ops.kileido.findings_group(group="file:plane.gap_crossing") == {"FINISHED"}
    assert ("operator", "plane.gap_crossing (2)", "DISCLOSURE_TRI_RIGHT") in column(scene)
    scene.kileido_findings_open = ""


def check_drc_header(scene, index):
    """The DRC source's header reads the tool's name and the run; the notes the bridge
    puts after the name ("2 library notices left out", "saved file, unsaved edits not
    checked") each take a line under it."""
    header = frame(index)
    sources = header["findings"]["sources"]
    sources["drc"] = {**sources["file"], "source": "drc", "run": "3",
                      "tool": "KiCad DRC 10.0.6; 2 library notices left out; saved file, unsaved edits not checked"}
    apply.apply_frame(header, {})
    texts = [text for _, text, _ in column(scene)]
    assert "KiCad DRC 10.0.6, run 3: 4 findings" in texts, texts
    assert "2 library notices left out" in texts and "Saved file, unsaved edits not checked" in texts, texts
    apply.apply_frame(frame(index), {})
    assert not any(text.startswith("KiCad DRC 10") for _, text, _ in column(scene))
def check_prompt(scene, index):
    """The LLM prompt box above the LLM's findings: v1 checks ticked to start with, groups
    folded, Quick and All, Copy prompt sends the ticked ids, a ready prompt is copied once."""
    copied = []
    findings.set_clipboard = copied.append  # background Blender has no clipboard
    apply.apply_frame(prompt_frame(index), {})
    drawn = column(scene)
    texts = [text for _, text, _ in drawn]
    assert "LLM prompt: 2 of 3 checks" in texts, texts  # the quick (v1) checks to start with
    assert texts.index("KiCad DRC") < texts.index("LLM prompt: 2 of 3 checks") < \
        texts.index("Test tool, run 4: 4 findings"), texts  # above the file's section
    for group in ("Return paths and planes (1/1)", "Decoupling (1/1)", "High speed (0/1)"):
        assert ("operator", group, "DISCLOSURE_TRI_RIGHT") in drawn, drawn  # folded
    assert "Stubs on fast nets" not in texts and ("operator", "Copy prompt", "COPYDOWN") in drawn
    assert bpy.ops.kileido.findings_group(group="prompt:hs") == {"FINISHED"}
    assert ("operator", "Stubs on fast nets", "CHECKBOX_DEHLT") in column(scene)
    assert bpy.ops.kileido.prompt_check(check="hs.stub") == {"FINISHED"}
    assert findings.chosen(scene) == QUICK | {"hs.stub"}
    drawn = column(scene)
    assert ("operator", "Stubs on fast nets", "CHECKBOX_HLT") in drawn and ("operator", "High speed (1/1)",
                                                                           "DISCLOSURE_TRI_DOWN") in drawn, drawn
    assert bpy.ops.kileido.prompt_preset(preset="quick") == {"FINISHED"}
    assert findings.chosen(scene) == QUICK and scene.kileido_prompt_checks == ""
    assert bpy.ops.kileido.prompt_preset(preset="all") == {"FINISHED"}
    assert findings.chosen(scene) == {row["id"] for row in CATALOGUE}
    for row in CATALOGUE:  # none ticked: stays none (not back to quick), and nothing to ask for
        bpy.ops.kileido.prompt_check(check=row["id"])
    assert findings.chosen(scene) == set() and "LLM prompt: 0 of 3 checks" in [t for _, t, _ in column(scene)]
    assert not bpy.ops.kileido.findings_prompt.poll()
    bpy.ops.kileido.prompt_preset(preset="quick")
    # Copy prompt: the ticked ids in the catalogue's order, then Making prompt… until the bridge answers.
    assert bpy.ops.kileido.findings_prompt() == {"FINISHED"}
    assert sent[-1] == ("prompt", "", "file", ("plane.gap_crossing", "decap.distance"), ""), sent[-1]  # no notes
    assert ("operator", "Making prompt…", "TIME") in column(scene) and not bpy.ops.kileido.findings_prompt.poll()
    apply.apply_frame(prompt_frame(index, "making"), {})
    assert not copied
    apply.apply_frame(prompt_frame(index, "ready", "5", PROMPT), {})
    assert copied == [PROMPT]
    drawn = column(scene)
    texts = [text for _, text, _ in drawn]
    assert "Prompt for run 5 copied — paste it into your LLM tool" in texts, texts
    assert ("prop", "kileido_prompt_show", "") in drawn and "Read request.md first." not in texts
    apply.apply_frame(prompt_frame(index, "ready", "5", PROMPT), {})  # sent again (a resync): not copied again
    assert copied == [PROMPT]
    scene.kileido_prompt_show = True
    texts = [text for _, text, _ in column(scene)]
    assert "Read request.md first." in texts and "Run: 5   Stamp: a1b2c3d4" in texts, texts
    apply.apply_frame(prompt_frame(index, "ready", "6", PROMPT), {})  # a run not asked for here: not copied
    assert copied == [PROMPT] and "Prompt for run 6 made" in [t for _, t, _ in column(scene)]
    # The press after a ready run: that run sent again is not the new one.
    assert bpy.ops.kileido.findings_prompt() == {"FINISHED"}
    apply.apply_frame(prompt_frame(index, "ready", "6", PROMPT), {})
    assert copied == [PROMPT]
    apply.apply_frame(prompt_frame(index, "failed", error="disk full"), {})
    assert "Prompt failed: disk full" in [t for _, t, _ in column(scene)] and len(copied) == 1
    apply.apply_frame(prompt_frame(index, "ready", "7", PROMPT + "!"), {})  # the press's run, after all
    assert copied == [PROMPT, PROMPT + "!"]
    apply.apply_frame(prompt_frame(index, status="unsaved"), {})
    assert not bpy.ops.kileido.findings_prompt.poll()
    scene.kileido_findings_open, scene.kileido_prompt_show = "", False
    apply.apply_frame(frame(index), {})


CHEAP = "Keep it cheap: no sub-agents, as few tokens as possible"
CONFIDENT = "Report only findings you are confident about"


def check_notes(scene, index):
    """Notes for the LLM, folded under the checks: none ticked to start with; the ticked
    sentences one per line, then the user's line, capped; Copy prompt sends them."""
    apply.apply_frame(prompt_frame(index), {})
    assert all(not getattr(scene, name) for name, _ in findings.NOTE_TOGGLES) and scene.kileido_prompt_note == ""
    assert findings.notes(scene) == ""
    drawn = column(scene)
    texts = [text for _, text, _ in drawn]
    assert ("operator", "Notes", "DISCLOSURE_TRI_RIGHT") in drawn, drawn  # folded, nothing given
    assert texts.index("High speed (0/1)") < texts.index("Notes") < texts.index("Copy prompt"), texts
    assert not any(kind == "prop" and name.startswith("kileido_note") for kind, name, _ in drawn)
    assert bpy.ops.kileido.findings_group(group=findings.NOTES_TAG) == {"FINISHED"}
    drawn = column(scene)
    for name in (*(name for name, _ in findings.NOTE_TOGGLES), "kileido_prompt_note"):
        assert ("prop", name, "") in drawn, (name, drawn)
    scene.kileido_note_cheap = scene.kileido_note_confident = True
    scene.kileido_prompt_note = "  Only the RF side  "
    assert findings.notes(scene) == f"{CHEAP}\n{CONFIDENT}\nOnly the RF side", findings.notes(scene)
    assert ("operator", "Notes (3)", "DISCLOSURE_TRI_DOWN") in column(scene)
    # Copy prompt sends them with the checks.
    assert bpy.ops.kileido.findings_prompt() == {"FINISHED"}
    assert sent[-1] == ("prompt", "", "file", ("plane.gap_crossing", "decap.distance"),
                        f"{CHEAP}\n{CONFIDENT}\nOnly the RF side"), sent[-1]
    # Capped: 500 characters, 8 lines.
    scene.kileido_prompt_note = "x" * 600
    capped = findings.notes(scene)
    assert len(capped) == findings.NOTES_MAX and capped.startswith(f"{CHEAP}\n{CONFIDENT}\nxxx"), len(capped)
    scene.kileido_prompt_note = "\n".join(f"line {n}" for n in range(10))  # set from Python: still 8 lines
    assert findings.notes(scene).split("\n") == [CHEAP, CONFIDENT, *(f"line {n}" for n in range(6))]
    scene.kileido_note_tools = True
    scene.kileido_note_cheap = scene.kileido_note_confident = False
    scene.kileido_prompt_note = ""
    assert findings.notes(scene) == "Use any KiCad analysis tools or skills you have installed"
    assert ("operator", "Notes (1)", "DISCLOSURE_TRI_DOWN") in column(scene)
    scene.kileido_note_tools = False
    scene.kileido_findings_open = ""
    state.board.prompt_wait = None
    apply.apply_frame(frame(index), {})


NOW = 1_800_000_000.0
STALLED = "No update for 5 min — the LLM may have stalled or be waiting for your approval"


def progress_frame(index, **progress):
    """The findings frame with the LLM's progress (as the bridge reads it from the run's folder)."""
    header = prompt_frame(index)
    header["findings"]["progress"] = {"state": "none", "run": "", "done": 0, "total": 0, "check": "", "note": "",
                                      "updated_unix": 0, "made_unix": 0, **progress}
    return header


def check_progress(scene, index):
    """How far the LLM is, under Copy prompt: waiting since the prompt was made, a bar while
    it works with its note and age, an alert past 5 min without word, its answer arrived;
    nothing before. The 1 s redraw timer runs while it waits or works, and only then."""
    findings.now = lambda: NOW  # the ages count to this
    ticking = findings._progress_tick
    apply.apply_frame(progress_frame(index), {})
    texts = [text for _, text, _ in column(scene)]
    assert not any(text.startswith(("Waiting for the LLM", "Run ", "LLM answer", "No update")) for text in texts)
    assert not bpy.app.timers.is_registered(ticking) and ticking() is None
    # Waiting: since the prompt was made.
    apply.apply_frame(progress_frame(index, state="waiting", run="4", made_unix=NOW - 40), {})
    drawn = column(scene)
    texts = [text for _, text, _ in drawn]
    assert ("label", "Waiting for the LLM… prompt copied 40 s ago", "TIME") in drawn, drawn
    assert texts.index("Copy prompt") < texts.index("Waiting for the LLM… prompt copied 40 s ago"), texts
    assert bpy.app.timers.is_registered(ticking) and ticking() == findings.PROGRESS_TICK_S
    findings.now = lambda: NOW + 19  # a later redraw: older
    assert ("label", "Waiting for the LLM… prompt copied 59 s ago", "TIME") in column(scene)
    findings.now = lambda: NOW + 20
    assert ("label", "Waiting for the LLM… prompt copied 1 min ago", "TIME") in column(scene)
    findings.now = lambda: NOW
    # Working: a bar for the checks done, the one under way named, the LLM's note and how long ago.
    apply.apply_frame(progress_frame(index, state="working", run="4", done=1, total=3, check="decap.distance",
                                     note="Measuring cap distances", updated_unix=NOW - 12, made_unix=NOW - 400), {})
    drawn = column(scene)
    assert ("progress", "Run 4: check 2 of 3 — decap.distance", round(1 / 3, 4)) in drawn, drawn
    assert ("label", "Measuring cap distances — 12 s ago", "BLANK1") in drawn, drawn
    assert not any(text.startswith("No update") for _, text, _ in drawn)  # its age is the update's, not the prompt's
    assert bpy.app.timers.is_registered(ticking) and ticking() == findings.PROGRESS_TICK_S
    drawn = column(scene, Layout)  # a layout without progress bars: a line instead
    assert ("label", "Run 4: check 2 of 3 — decap.distance", "TIME") in drawn, drawn
    # Exactly 5 min: not yet; past it: an alert, with an icon and words.
    apply.apply_frame(progress_frame(index, state="working", run="4", done=1, total=3, updated_unix=NOW - 300), {})
    assert not any(kind == "alert" for kind, *_ in findings.progress_lines())
    apply.apply_frame(progress_frame(index, state="working", run="4", done=1, total=3, updated_unix=NOW - 301), {})
    assert ("alert", STALLED, "ERROR", None) in findings.progress_lines(), findings.progress_lines()
    assert ("label", wrap(STALLED)[0], "ERROR") in column(scene)
    assert ("label", "5 min ago", "BLANK1") in column(scene)  # no note: the age alone
    apply.apply_frame(progress_frame(index, state="waiting", run="4", made_unix=NOW - 2 * 3600), {})
    lines = findings.progress_lines()
    assert lines[0][1] == "Waiting for the LLM… prompt copied 2 h ago" and lines[-1][0] == "alert", lines
    assert lines[-1][1].startswith("No update for 2 h — "), lines
    # Done: its answer arrived (listed below); the timer stops.
    apply.apply_frame(progress_frame(index, state="done", run="4", done=3, total=3, updated_unix=NOW - 900), {})
    drawn = column(scene)
    assert ("label", "LLM answer for run 4 arrived", "CHECKMARK") in drawn, drawn
    assert not any(text.startswith("No update") for _, text, _ in drawn)
    assert not bpy.app.timers.is_registered(ticking) and ticking() is None
    # Working again, then none (a new board): the timer stops.
    apply.apply_frame(progress_frame(index, state="working", run="5", updated_unix=NOW), {})
    assert bpy.app.timers.is_registered(ticking)
    assert ("progress", "Run 5: working", 0.0) in column(scene)
    apply.apply_frame(progress_frame(index), {})
    assert not bpy.app.timers.is_registered(ticking)
    apply.apply_frame(frame(index), {})  # no progress at all: nothing, no timer
    assert findings.progress_lines() == [] and not bpy.app.timers.is_registered(ticking)


def cut_settings(scene):
    """Everything the cut uses, and each view's direction, as plain values."""
    views = [(tuple(space.region_3d.view_rotation), space.region_3d.view_perspective)
             for space in kileido.objects.view3d_spaces()]
    return hole_cut._cut_state(scene), views


def same(a, b):
    return hole_cut._same(json.loads(json.dumps(a)), json.loads(json.dumps(b)))


def plane_normal():
    plane = bpy.data.objects[cut.PLANE]
    return plane.matrix_world.translation, (plane.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()


HOLE_RADII_M = (0.175e-3, 1.0e-3)  # the via's drill and J1's pad drill (2 x 1 mm), halved


def check_cut_label(found, text, drawn, origin, normal, ends, radii=HOLE_RADII_M):
    """A hole finding's label written on the cut face: flat in it (no turning to the eye),
    a hair in front, within the board's thickness, beside both holes, on its plate."""
    bpy.context.view_layer.update()
    assert not text.constraints and text.name + " leader" not in drawn
    axis = (text.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
    assert axis.dot(normal) > 1 - 1e-6, (axis, normal)  # flat in the cut plane, facing the viewer
    plate = check_plate(text, drawn, found["severity"])
    low, high = state.board.heights["B.Cu"], state.board.heights["F.Cu"]
    for corner in corners(text) + corners(plate):
        ahead = (corner - origin).dot(normal)
        assert 0 < ahead < 0.2e-3, ahead  # in front of the face, by a hair
        assert low <= corner.z <= high, (corner.z, low, high)
    across = Vector((-normal.y, normal.x, 0.0))
    span = [corner.dot(across) for corner in corners(plate)]
    for centre, radius in zip(ends, radii):  # never across a barrel
        hole = centre.dot(across)
        assert hole + radius < min(span) or hole - radius > max(span), (hole, span)


def check_hole_cut(scene, index):
    """A hole finding cuts upright through both holes, the half facing the view removed,
    the views turned onto the cut face; hiding it, or showing any other finding, puts the
    user's cut and view directions back exactly; a cut the user changed meanwhile stays."""
    apply.apply_frame(frame(index, extra=HOLES), {})
    # The user's own cut: on, across X, moved, flipped, no section face, cut light on.
    scene.kileido_cut = True
    cut.place("X")
    bpy.data.objects[cut.PLANE].location.x += 0.003
    scene.kileido_cut_flip, scene.kileido_cut_face, scene.kileido_cut_light = True, False, True
    bpy.context.view_layer.update()
    for space in kileido.objects.view3d_spaces():  # looking from the front, a little from above
        space.region_3d.view_rotation = Vector((0.0, -1.0, 0.5)).to_track_quat("Z", "Y")
        space.region_3d.view_perspective = "PERSP"
    before = cut_settings(scene)
    found = show("Hole near J1")
    a, b = (Vector((*blender_xy(point), 0.0)) for point in found["draws"][-1]["points"])
    assert found["draws"][-1]["tool"] == "distance" and (b - a).length > 1e-3
    assert scene.kileido_cut and not scene.kileido_cut_flip and scene.kileido_cut_face and scene.kileido_cut_light
    assert cut.upright(scene)
    origin, normal = plane_normal()
    assert abs(normal.z) < 1e-6, normal  # upright: the board's vertical axis lies in the plane
    for point in (a, b):  # both hole centres on the plane
        assert abs((point - Vector((origin.x, origin.y, 0.0))).dot(normal)) < 1e-6, (point, origin, normal)
    assert abs(normal.dot((b - a).normalized())) < 1e-6
    assert normal.y < 0  # the half towards the front view removed
    face = bpy.data.objects[cut.FACE]
    assert not face.hide_get() and len(face.data.polygons)
    for space in kileido.objects.view3d_spaces():  # every view on the gap, looking into the cut face
        view = space.region_3d
        assert np.allclose(view.view_location[:2], framed_centre((*np.minimum(a.xy, b.xy), *np.maximum(a.xy, b.xy))),
                           atol=1e-6)  # the gap and its labels
        eye = view.view_rotation @ Vector((0.0, 0.0, 1.0))
        assert eye.dot(normal) > 0.9 and 0 < eye.z < 0.5, eye
    # The finding's tubes and labels are not clipped by the cut (the board's highlight is).
    drawn = objects()
    arrow = drawn["KLS finding 2 distance"]
    assert arrow.data.materials[0].node_tree.nodes.get(cut.NODE) is None and not arrow.hide_get()
    check_cut_label(found, drawn["KLS finding 2 distance label"], drawn, origin, normal, (a, b))
    # A second hole finding keeps the first one's saved cut; its points coincide: a one-hole cut.
    facing = hole_cut._facing()  # the views look at the first cut's face: a one-hole cut faces them squarely
    show("One hole")
    origin, normal = plane_normal()
    assert abs(normal.z) < 1e-6 and np.allclose(normal.xy, facing, atol=1e-6), (normal, facing)
    assert np.allclose(origin[:2], blender_xy(finding_named("One hole")["draws"][-1]["points"][0]), atol=1e-7)
    # A via size finding: the cut holds its dimension (along x here), the half towards the view removed;
    # the dimension's tube with its end ticks lies in the face, its label is written on the face.
    facing = hole_cut._facing()
    found = show("Thin ring")
    origin, normal = plane_normal()
    ends = [Vector((*blender_xy(point), 0.0)) for point in found["draws"][-1]["points"]]
    assert found["draws"][-1]["tool"] == "distance" and (ends[1] - ends[0]).length > 1e-4
    assert cut.upright(scene) and abs(normal.z) < 1e-6 and abs(abs(normal.y) - 1) < 1e-6, normal
    assert np.sign(normal.y) == np.sign(facing.y), (normal, facing)
    for point in ends:
        assert abs((point - Vector((origin.x, origin.y, 0.0))).dot(normal)) < 1e-6, (point, origin, normal)
    drawn = objects()
    assert not drawn["KLS finding 2 distance"].hide_get()
    centre = Vector((*blender_xy((20_000_000, 20_000_000)), 0.0))  # the via: 0.9 mm wide
    check_cut_label(found, drawn["KLS finding 2 distance label"], drawn, origin, normal, (centre, centre),
                    (0.45e-3, 0.45e-3))
    show("Thin ring")
    assert same(cut_settings(scene), before), (cut_settings(scene), before)
    # Hidden (a second click): the user's cut and view directions back, exactly.
    show("One hole")
    show("One hole")
    assert same(cut_settings(scene), before) and hole_cut.KEY not in scene, (cut_settings(scene), before)
    # A non-hole finding shown after a hole one: back too; shown alone, it leaves the cut be.
    show("Hole near J1")
    show("Tracks too close")
    assert same(cut_settings(scene), before), (cut_settings(scene), before)
    show("Tracks too close")
    assert same(cut_settings(scene), before)
    # The list cleared, or a file loaded (the same restore): back.
    show("Hole near J1")
    apply.apply_frame(frame(index), {})
    assert state.board.findings_shown == ("", "") and same(cut_settings(scene), before)
    apply.apply_frame(frame(index, extra=HOLES), {})
    show("Hole near J1")
    findings_draw.clear()  # a file loaded: the drawing purged, then the cut put back
    hole_cut.restore()  # what _file_loaded calls
    assert same(cut_settings(scene), before)
    # Changed by the user while shown: that switch is theirs and stays; the rest goes back.
    show("Hole near J1")
    scene.kileido_cut_light = False
    show("Hole near J1")
    assert same(hole_cut._cut_state(scene), {**before[0], "light": False}), hole_cut._cut_state(scene)
    scene.kileido_cut_light = True
    # The plane moved by the user while shown: it stays where they put it; the switches go back.
    show("Hole near J1")
    bpy.data.objects[cut.PLANE].location.x += 0.001
    moved = hole_cut._cut_state(scene)["plane"]
    show("Hole near J1")
    assert same(hole_cut._cut_state(scene), {**before[0], "plane": moved}), hole_cut._cut_state(scene)
    hole_cut._set_cut(scene, before[0])  # the user's plane again, as `before` has it
    bpy.context.view_layer.update()
    assert same(cut_settings(scene), before), (cut_settings(scene), before)
    # The plane deleted while shown: hiding brings the user's own plane and switches back.
    show("Hole near J1")
    bpy.data.objects.remove(bpy.data.objects[cut.PLANE])
    show("Hole near J1")
    assert same(cut_settings(scene), before), (cut_settings(scene), before)
    # No cut before: the plane is gone again, so the next Cut plane starts across the middle.
    scene.kileido_cut = False
    bpy.data.objects.remove(bpy.data.objects[cut.PLANE])
    show("Hole near J1")
    show("Hole near J1")
    assert not scene.kileido_cut and bpy.data.objects.get(cut.PLANE) is None
    apply.apply_frame(frame(index), {})


sent = []


def main():
    kileido.register()
    try:
        scene = bpy.context.scene
        snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
        apply.load_frames(b"".join(snapshot_frames(snapshot)))
        index = BoardIndex(snapshot)
        # Without a live link: the tab is greyed, Run DRC off, the list still shown.
        assert columns.tab_state("DRC") == "unavailable"
        assert [key for key, _, _ in columns.TABS] == ["STUDIO", "IMS", "FLEX", "DRC", "LLM"]
        # A newer findings frame replaces a queued older one.
        link = live.LiveLink()
        for revision in (1, 2):
            link._queue({"type": "findings", "revision": revision}, {})
        assert [header["revision"] for header, _ in link.pending] == [2]
        apply.apply_frame(frame(index), {})
        assert state.board.findings["sources"]["file"]["handshake"] == "known"
        assert not objects()
        texts = [text for _, text, _ in column(scene)]
        assert any(text.startswith("KiCad not connected") for text in texts), texts
        live.linked = lambda: True  # as if live; requests are recorded instead of sent
        live.request_findings = lambda action, key="", source="drc", checks=None, notes=None: sent.append(
            (action, key, source) if checks is None else (action, key, source, tuple(checks), notes))
        assert columns.tab_state("DRC") == "off"
        check_column(scene)
        check_drc_header(scene, index)
        check_markers(scene)
        check_drawing(scene)
        check_pull()
        check_hole_cut(scene, index)
        check_fold(scene, index)
        check_placement(scene, index)
        check_measured(scene, index)
        check_routed(scene, index)
        check_suggestions(scene, index)
        check_planes(scene, index)
        check_vias_and_steps(scene, index)
        check_prompt(scene, index)
        check_notes(scene, index)
        check_progress(scene, index)
        # Run DRC: asked of the bridge, the button says so until the bridge answers.
        assert bpy.ops.kileido.findings_run_drc() == {"FINISHED"} and sent[-1] == ("run_drc", "", "drc")
        assert ("operator", "Running KiCad DRC…", "TIME") in column(scene)
        # The shown finding gone from a newer list: nothing drawn.
        show("Tracks too close")
        apply.apply_frame(frame(index, drop=("Tracks too close",)), {})
        assert state.board.findings_shown == ("", "") and not objects()
        # A snapshot keeps the drawing (made again after the sweep); a loaded file drops it.
        apply.apply_frame(frame(index), {})
        show("Tracks too close")
        apply.load_frames(b"".join(snapshot_frames(snapshot)))
        assert "KLS finding 2 distance" in objects() and len(objects()["KLS finding 4 arrow head"].data.vertices)
        findings_draw.purge()
        assert not any(obj.get(findings_draw.TAG) is not None for obj in bpy.data.objects)
        # A file loaded while the LLM works: the redraw timer stops (the bridge sends its progress again).
        apply.apply_frame(progress_frame(index, state="working", run="4", updated_unix=NOW), {})
        assert bpy.app.timers.is_registered(findings._progress_tick)
        kileido._file_loaded(None)
        assert not bpy.app.timers.is_registered(findings._progress_tick)
        print("KLS_FINDINGS_OK")
    finally:
        kileido.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
