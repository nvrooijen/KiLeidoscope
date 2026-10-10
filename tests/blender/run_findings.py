"""Headless checks of the DRC column: the bridge's findings frame reaches the board
state, the column lists both sources by check, a shown finding draws its tools at the
right Blender coordinates (highlight, label, distance, arrow), confirm and dismiss update
the rows, a hole finding cuts the board open through both holes and puts the user's cut
back after, and a loaded file purges the drawing. The measured tools (a hand-built
payload, as the bridge measures them): clearance, edge gap, width ribbons and callout.
Labels (label_place): light text on a dark plate, beside the finding's box, above the
surface the view sees with a leader to their anchor, sized to the finding, apart on
screen; on a hole finding's cut face, flat on it, within the board, beside the holes.

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


def wrap(text):
    return textwrap.wrap(text, 70) or [""]


def column(scene):
    layout = Layout()
    findings.draw_column(layout, scene, wrap)
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
    data = {"format": "kls-findings 1", "run": "4", "stamp": STAMP, "tool": "Test tool", "findings": items}
    return json.dumps(data).encode("utf-8")


def frame(index, drop=(), extra=()):
    """The bridge's findings frame: the findings file resolved (the board as it is), no DRC run yet."""
    context = bridge_findings.Context("x.kls-findings.json", "file", STAMP, {})
    payload = bridge_findings.resolve_file(bridge_findings.parse(findings_file(extra)), index, context)
    payload["findings"] = [f for f in payload["findings"] if f["title"] not in drop]
    drc = bridge_findings.status_payload("none", bridge_findings.Context("x.drc.json", "drc", STAMP, {}))
    return {"type": "findings", "revision": 1,
            "findings": {"status": "ok", "folder": "C:/boards/.kileidoscope",
                         "drc": {"state": "idle", "error": "", "run": ""}, "sources": {"drc": drc, "file": payload}}}


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
    assert state.board.findings_shown == ("file", found["key"]) and columns.tab_state("DRC") == "on"
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
    texts = [obj for obj in drawn.values() if obj.type == "FONT" and obj.parent is None]  # not a glyph
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
    return {"tool": tool, "ok": True, "problem": "", "ids": [], "targets": [], "label": "",
            "emphasis": "normal", "what": what, **fields}


MEASURED = {  # hand-built (the bridge measures these): one finding with every measured tool
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
    assert not any(text.startswith("KiCad DRC,") for text in texts), texts
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
        assert [key for key, _, _ in columns.TABS] == ["STUDIO", "IMS", "FLEX", "DRC"]
        # A newer findings frame replaces a queued older one.
        link = live.LiveLink()
        for revision in (1, 2):
            link._queue({"type": "findings", "revision": revision}, {})
        assert [header["revision"] for header, _ in link.pending] == [2]
        apply.apply_frame(frame(index), {})
        assert not objects()
        texts = [text for _, text, _ in column(scene)]
        assert any(text.startswith("KiCad not connected") for text in texts), texts
        live.linked = lambda: True  # as if live; requests are recorded instead of sent
        live.request_findings = lambda action, key="", source="drc": sent.append((action, key, source))
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
