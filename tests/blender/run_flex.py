"""Headless checks of flex mode: the bridge's flex frame reaches the board state, the
panel box lists the stack, the bends against the chosen use, and the checks; the Fold
slider folds the board's copies as foldmath says, parts with them, and unfolds again.

blender --background --factory-startup --python tests/blender/run_flex.py
"""

import json
import math
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
import numpy as np  # noqa: E402
from kileido import apply, fold, foldmath, state  # noqa: E402
from kileido.objects import read_attribute, read_coordinates  # noqa: E402
from kileido_bridge import flex_checks, model  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
STACK = {"layers": ["In1.Cu", "In2.Cu"], "thickness_nm": 86_000}


class Layout:
    """Records what a panel draws: (kind, text, icon) per label or operator."""

    def __init__(self, drawn=None):
        self.drawn = [] if drawn is None else drawn
        self.alignment, self.active, self.enabled = "EXPAND", True, True

    def _child(self, *args, **kwargs):
        return Layout(self.drawn)

    box = split = row = column = _child

    def label(self, text="", icon="NONE"):
        self.drawn.append(("label", text, icon))

    def prop(self, data, name, **kwargs):
        self.drawn.append(("prop", name, ""))

    def menu(self, idname, text="", icon="NONE"):
        self.drawn.append(("menu", text, icon))

    def operator(self, idname, text="", icon="NONE", emboss=True):
        self.drawn.append(("operator", text, icon))
        return type("Properties", (), {})()


def frames(flex=True):
    """The fixture with a flex zone across it at x = 28 mm and one bend, R0.5 (2.7x the flex)."""
    snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
    points = [p for polygon in snapshot.outline.polygons for ring in polygon for p in ring]
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    middle = left + (right - left) * 7 // 10  # x = 28 mm: clear of the cutout at 18..22, J2..J4 beyond it
    zone = ((middle - 3_000_000, top - 1_000_000), (middle + 3_000_000, top - 1_000_000),
            (middle + 3_000_000, bottom + 1_000_000), (middle - 3_000_000, bottom + 1_000_000))
    drawings = (model.Drawing("zone", "User.1", "closed", zone),
                model.Drawing("bend", "User.2", "line", ((middle, top - 500_000), (middle, bottom + 500_000))),
                model.Drawing("label", "User.2", "text", ((middle + 500_000, top),), "90° R0.5"))
    for k, (x, text) in enumerate(((30, "Stainless steel 0.3 mm bottom"), (35, "Kevlar 0.2 mm bottom"))):
        mm = 1_000_000
        drawings += (model.Drawing(f"stiffener{k}", "User.3", "closed",
                                   ((x * mm, 2 * mm), ((x + 3) * mm, 2 * mm), ((x + 3) * mm, 6 * mm), (x * mm, 6 * mm))),
                     model.Drawing(f"stiffener{k} text", "User.3", "text", (((x + 1) * mm, 4 * mm),), text))
    names = dict(snapshot.layer_display_names, **{"User.1": "Flex", "User.2": "Bend", "User.3": "Stiffener"})
    snapshot = model.BoardSnapshot(snapshot.board_name, names, snapshot.tracks, snapshot.arcs, snapshot.vias,
                                   snapshot.pads, snapshot.footprints, snapshot.zones, snapshot.outline,
                                   snapshot.stackup, snapshot.warnings, snapshot.read_timings_ms,
                                   snapshot.graphics, drawings if flex else ())
    report = flex_checks.report(snapshot, STACK if flex else {})
    return b"".join(snapshot_frames(snapshot, flex=report))


def drawn(scene):
    layout = Layout()
    kileido._draw_flex(layout, scene)
    return layout.drawn


TRACK = "22222222-2222-4222-8222-222222222221"  # B.Cu, x = 30 mm: beyond the bend, it folds up


def check_picking_and_highlight(plan, angles):
    """A click on the folded copy selects the KiCad track under it; selecting it in KiCad
    highlights it on the folded copy."""
    from mathutils import Vector
    from kileido import highlight, pick
    middle = foldmath.world_xy([(30_000_000, 12_500_000)], state.board.origin_nm)[0]
    region = int(foldmath.regions_of(middle[None], plan.regions)[0])
    matrix = foldmath.region_matrix(region, angles, plan)
    below = -matrix[:3, 2]  # the board's bottom, where B.Cu is, as folded
    on_board = (matrix @ np.array((middle[0], middle[1], 0.0, 1.0)))[:3]
    origin = Vector(on_board + below * 0.005)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    item = pick.item_at(bpy.context.scene, depsgraph, origin, Vector(-below))
    assert item == TRACK, item
    highlight.apply_selection({"selected": [TRACK], "pair": []})
    folded = fold._folded_collection()
    glow = folded.objects.get("KLS folded B.Cu highlight selected")
    assert glow is not None and not glow.hide_get(), [o.name for o in folded.objects]
    assert state.board.collection.all_objects["KLS B.Cu highlight selected"].hide_viewport
    bpy.context.view_layer.update()
    points = read_coordinates(glow.evaluated_get(bpy.context.evaluated_depsgraph_get()).data)
    assert np.abs(points.mean(axis=0) - on_board).max() < 3e-3, (points.mean(axis=0), on_board)  # folded with it
    highlight.apply_selection({"selected": [], "pair": []})
    assert not any("highlight" in o.name and not o.hide_get() and len(o.data.vertices) for o in folded.objects)


def check_fold(scene):
    """Fold the board: Blender's Geometry Nodes fold matches foldmath point for point."""
    scene.kileido_fold = 1.0
    plan = foldmath.plan(state.board.flex, state.board.origin_nm, state.board.heights, state.board.layer_thickness)
    angles = foldmath.angles_at(plan, 1.0)
    folded = fold._folded_collection()
    assert folded is not None and len(folded.objects) >= 3, folded
    grips = fold._grips()
    assert [round(grip.rotation_euler.z, 6) for grip in grips] == [round(a, 6) for a in angles], grips
    bpy.context.view_layer.update()  # the drivers carry the handles' angles to the Fold modifiers
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in folded.objects:
        original = state.board.collection.all_objects.get(obj["kls_folded_from"])  # stiffeners have none
        assert original is None or original.hide_viewport, original.name
        mesh = obj.data
        flat = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.attributes["kls_flat"].data.foreach_get("vector", flat)
        flat = flat.reshape(-1, 3).astype(np.float64)
        mask = read_attribute(mesh, "kls_fold_mask", np.float32).astype(np.int64)
        zone = read_attribute(mesh, "kls_fold_zone", np.float32).astype(np.int64)
        expected = foldmath.fold(flat, mask, zone, angles, plan)
        moved = read_coordinates(obj.evaluated_get(depsgraph).data).astype(np.float64)
        error = np.abs(moved - expected).max()
        assert error < 1e-6, (obj.name, error)  # float32 in Geometry Nodes: well under a micrometre
        if obj.name == "KLS folded outline":
            assert np.count_nonzero(zone >= 0) > 20, obj.name  # cut and tagged across the strip
    steel = folded.objects["KLS stiffener 1"]
    assert [m.name for m in steel.data.materials] == ["KLS Stiffener metal"], steel.data.materials[:]
    assert [m.name for m in folded.objects["KLS stiffener 2"].data.materials] == ["KLS Board FR4 core"]
    parts = [obj for obj in fold._parts() if obj.get("kls_flat_matrix") is not None]
    assert parts, "parts folded with their regions"
    lifted = [obj for obj in parts if abs(obj.matrix_world.translation.z - obj["kls_flat_matrix"][11]) > 1e-4]
    assert lifted, "a part on the folded side moved"
    check_picking_and_highlight(plan, angles)
    grips[0].rotation_euler.z = 0.5  # turned by hand, as R does in the viewport
    fold.refresh()  # what the depsgraph handler does after a turn
    bpy.context.view_layer.update()
    outline = folded.objects["KLS folded outline"]
    flat = np.empty(len(outline.data.vertices) * 3, np.float32)
    outline.data.attributes["kls_flat"].data.foreach_get("vector", flat)
    mask = read_attribute(outline.data, "kls_fold_mask", np.float32).astype(np.int64)
    zone = read_attribute(outline.data, "kls_fold_zone", np.float32).astype(np.int64)
    expected = foldmath.fold(flat.reshape(-1, 3).astype(np.float64), mask, zone, [0.5], plan)
    moved = read_coordinates(outline.evaluated_get(bpy.context.evaluated_depsgraph_get()).data)
    assert np.abs(moved - expected).max() < 1e-6, np.abs(moved - expected).max()
    frame = grips[0].parent.matrix_world
    assert abs(frame.col[1].z - 1.0) < 1e-6, frame  # a root-side bend's frame stays upright
    scene.kileido_fold = 0.0  # flat again, still in flex mode: the thin copies stay, the flat originals hidden
    assert not folded.hide_viewport and state.board.collection.all_objects["KLS outline"].hide_viewport
    outline = folded.objects["KLS folded outline"]
    materials = [slot.name for slot in outline.data.materials]
    assert "KLS Flex polyimide" in materials, materials  # the flex's faces: amber polyimide
    flex_faces = sum(1 for face in outline.data.polygons if materials[face.material_index] == "KLS Flex polyimide")
    assert 0 < flex_faces < len(outline.data.polygons), flex_faces
    check_section(scene, plan)


def check_section(scene, plan):
    """The cut plane across the flex: there its section is the thin flex stack, plain
    polyimide (no glass weave) between copper, from coverlay to coverlay."""
    from kileido import cut, section
    scene.kileido_cut = True
    cut.place("Y", centered=True)  # along x through the middle: across the flex zone at x = 25..31 mm
    rects, (line, _) = cut.rectangles()
    zone = fold._crossed(line, plan.zones)
    assert len(zone), "the cut crosses the flex"
    in_flex = [r for r in rects if section.intersect(np.array([[r[0], r[1]]]), zone).size]
    low, high = plan.z_range
    # Laminate only: this fixture's B.Cu track runs into the flex (a fault the checks report);
    # its copper hangs under the flex in the section as in 3D.
    bad = [r for r in in_flex if r[4] not in section.METALS and not (low - 1e-9 <= r[2] and r[3] <= high + 1e-9)]
    assert in_flex and not bad, (low, high, bad)
    assert any(r[4] == section.POLYIMIDE for r in in_flex)
    assert not any(r[4] in section.WOVEN for r in in_flex)
    scene.kileido_cut = False
    assert not any(obj.get("kls_flat_matrix") is not None for obj in fold._parts())


def twist_frames():
    """The fixture with its right part (x 23..40 mm) flex and a 90° twist along x = 30..38 mm."""
    snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
    mm = 1_000_000
    drawings = (model.Drawing("zone", "User.1", "closed", ((23 * mm, -mm), (41 * mm, -mm), (41 * mm, 31 * mm),
                                                           (23 * mm, 31 * mm))),
                model.Drawing("twist", "User.2", "line", ((30 * mm, 15 * mm), (38 * mm, 15 * mm))),
                model.Drawing("twist text", "User.2", "text", ((31 * mm, 16 * mm),), "twist 90°"))
    names = dict(snapshot.layer_display_names, **{"User.1": "Flex", "User.2": "Bend"})
    snapshot = model.BoardSnapshot(snapshot.board_name, names, snapshot.tracks, snapshot.arcs, snapshot.vias,
                                   snapshot.pads, snapshot.footprints, snapshot.zones, snapshot.outline,
                                   snapshot.stackup, snapshot.warnings, snapshot.read_timings_ms,
                                   snapshot.graphics, drawings)
    return b"".join(snapshot_frames(snapshot, flex=flex_checks.report(snapshot, STACK)))


def cone_frames():
    """The fixture with its right part flex and a 90° cone: an area whose sides, x 26..28
    at the top and 30..29 at the bottom, converge far below the board."""
    snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
    mm = 1_000_000
    drawings = (model.Drawing("zone", "User.1", "closed", ((23 * mm, -mm), (41 * mm, -mm), (41 * mm, 31 * mm),
                                                           (23 * mm, 31 * mm))),
                model.Drawing("cone", "User.2", "closed", ((26 * mm, -mm), (30 * mm, -mm), (29 * mm, 31 * mm),
                                                           (28 * mm, 31 * mm))),
                model.Drawing("cone text", "User.2", "text", ((28 * mm, 15 * mm),), "90°"))
    names = dict(snapshot.layer_display_names, **{"User.1": "Flex", "User.2": "Bend"})
    snapshot = model.BoardSnapshot(snapshot.board_name, names, snapshot.tracks, snapshot.arcs, snapshot.vias,
                                   snapshot.pads, snapshot.footprints, snapshot.zones, snapshot.outline,
                                   snapshot.stackup, snapshot.warnings, snapshot.read_timings_ms,
                                   snapshot.graphics, drawings)
    return b"".join(snapshot_frames(snapshot, flex=flex_checks.report(snapshot, STACK)))


def compare_copies(angles):
    """Every folded copy: Blender's Geometry Nodes put each point where foldmath does."""
    plan = foldmath.plan(state.board.flex, state.board.origin_nm, state.board.heights, state.board.layer_thickness)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in fold._folded_collection().objects:
        mesh = obj.data
        flat = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.attributes["kls_flat"].data.foreach_get("vector", flat)
        mask = read_attribute(mesh, "kls_fold_mask", np.float32).astype(np.int64)
        zone = read_attribute(mesh, "kls_fold_zone", np.float32).astype(np.int64)
        expected = foldmath.fold(flat.reshape(-1, 3).astype(np.float64), mask, zone, angles(plan), plan)
        moved = read_coordinates(obj.evaluated_get(depsgraph).data).astype(np.float64)
        assert np.abs(moved - expected).max() < 1e-6, (obj.name, np.abs(moved - expected).max())


def check_cone(scene):
    """A cone: the panel lists it with its radii; Blender's nodes roll it as foldmath does."""
    apply.load_frames(cone_frames())
    (cone,) = state.board.flex["bends"]
    assert cone["kind"] == "cone" and 0 < cone["radius_nm"] < cone["radius_max_nm"], cone
    texts = [text for _, text, _ in drawn(scene)]
    assert any(text.startswith("Cone 1  90° R") and "–" in text for text in texts), texts
    scene.kileido_fold = 1.0
    compare_copies(lambda plan: foldmath.angles_at(plan, 1.0))
    scene.kileido_fold = 0.5
    compare_copies(lambda plan: foldmath.angles_at(plan, 0.5))
    scene.kileido_fold = 0.0


def bends_frames(*bends):
    """The fixture with its right part (x 23..40 mm) flex and bend lines (x, text) across it."""
    snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
    mm = 1_000_000
    drawings = [model.Drawing("zone", "User.1", "closed", ((23 * mm, -mm), (41 * mm, -mm), (41 * mm, 31 * mm),
                                                           (23 * mm, 31 * mm)))]
    for k, (x, text) in enumerate(bends):
        drawings += [model.Drawing(f"bend{k}", "User.2", "line", ((round(x * mm), -mm), (round(x * mm), 31 * mm))),
                     model.Drawing(f"bend{k} text", "User.2", "text", ((round(x * mm) + 500_000, -2 * mm),), text)]
    names = dict(snapshot.layer_display_names, **{"User.1": "Flex", "User.2": "Bend"})
    snapshot = model.BoardSnapshot(snapshot.board_name, names, snapshot.tracks, snapshot.arcs, snapshot.vias,
                                   snapshot.pads, snapshot.footprints, snapshot.zones, snapshot.outline,
                                   snapshot.stackup, snapshot.warnings, snapshot.read_timings_ms,
                                   snapshot.graphics, tuple(drawings))
    return b"".join(snapshot_frames(snapshot, flex=flex_checks.report(snapshot, STACK)))


def check_steps(scene):
    """The folding sequence in the panel, and what runs into what at the end of each step."""
    apply.load_frames(bends_frames((26, "90° R1 #1"), (33, "90° R1 #2")))
    scene.kileido_fold = 0.5  # the end of step 1
    drawn_now = drawn(scene)
    assert ("operator", "Step 1: Bend 1", "CHECKMARK") in drawn_now, drawn_now
    assert ("operator", "Step 2: Bend 2", "BLANK1") in drawn_now, drawn_now
    assert state.board.fold_findings == [], state.board.fold_findings  # a step: up, then over; nothing hits
    scene.kileido_fold = 0.0
    apply.load_frames(bends_frames((31, "180° R0.1")))  # folded flat back onto the board, too tight
    scene.kileido_fold = 1.0
    assert state.board.fold_findings == ["Folded: the part past bend 1 runs into the board"],         state.board.fold_findings
    texts = " ".join(text for _, text, _ in drawn(scene))  # the panel wraps long lines
    assert "Folded: the part past bend 1 runs into the board" in texts, texts
    scene.kileido_fold = 0.0


def check_twist(scene):
    """A twist: Blender's Geometry Nodes turn the copies as foldmath does, and the panel
    lists it with its length."""
    apply.load_frames(twist_frames())
    (twist,) = state.board.flex["bends"]
    assert twist["kind"] == "twist" and twist["width_nm"] == 8_000_000, twist
    texts = [text for _, text, _ in drawn(scene)]
    assert "Twist 1  90°" in texts and "over 8 mm" in texts, texts
    scene.kileido_fold = 1.0
    plan = foldmath.plan(state.board.flex, state.board.origin_nm, state.board.heights, state.board.layer_thickness)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in fold._folded_collection().objects:
        mesh = obj.data
        flat = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.attributes["kls_flat"].data.foreach_get("vector", flat)
        mask = read_attribute(mesh, "kls_fold_mask", np.float32).astype(np.int64)
        zone = read_attribute(mesh, "kls_fold_zone", np.float32).astype(np.int64)
        expected = foldmath.fold(flat.reshape(-1, 3).astype(np.float64), mask, zone, [math.pi / 2], plan)
        moved = read_coordinates(obj.evaluated_get(depsgraph).data).astype(np.float64)
        assert np.abs(moved - expected).max() < 1e-6, (obj.name, np.abs(moved - expected).max())
    outline = fold._folded_collection().objects["KLS folded outline"]
    assert np.ptp(read_coordinates(outline.evaluated_get(depsgraph).data)[:, 2]) > 3e-3  # the end stands on edge
    scene.kileido_fold = 0.0


def main():
    kileido.register()
    try:
        scene = bpy.context.scene
        apply.load_frames(frames())
        found = state.board.flex
        assert found["layers"] == ["In1.Cu", "In2.Cu"] and found["total_nm"] == 186_000, found
        assert [bend["radius_nm"] for bend in found["bends"]] == [500_000], found["bends"]
        static = drawn(scene)
        texts = [text for _, text, _ in static]
        assert ("prop", "kileido_flex_use", "") in static
        assert "In1.Cu + In2.Cu" in texts and "186 µm" in texts, texts
        assert ("label", "2.7×", "ERROR") in static and "Static flex needs 10× or more" in texts, static
        assert any(text.startswith("Bend 1: R0.5 mm is 2.7×") for text in texts), texts
        assert not any("dynamic flex needs" in text for text in texts), texts
        # Stiffeners as their KiCad text says, nothing to change here; an unknown material says so.
        assert "Stainless steel 0.3 mm bottom" in texts and "Stiffener 1" in texts, texts
        assert "Kevlar 0.2 mm bottom" in texts and any("shown as FR4" in t for t in texts), texts
        assert not any(kind == "prop" and "stiffener" in name for kind, name, _ in static)
        # Coverlay from KiCad (amber without a text), with buttons copying the other colours' texts.
        assert ("label", "Amber", "NONE") in static and ("menu", "", "COPYDOWN") in static, static
        assert not any(kind == "prop" and "coverlay" in name for kind, name, _ in static)
        copies = [entry for entry in static if entry == ("operator", "", "COPYDOWN")]
        assert len(copies) == 3, static  # bend 1, stiffeners 1 and 2
        scene.kileido_flex_use = "DYNAMIC"
        texts = [text for _, text, _ in drawn(scene)]
        assert "Dynamic flex needs 150× or more" in texts and any("dynamic flex needs" in t for t in texts), texts
        check_fold(scene)
        check_twist(scene)
        check_cone(scene)
        check_steps(scene)
        apply.load_frames(frames(flex=False))  # a board without flex: no box
        assert state.board.flex == {} and drawn(scene) == []
        print("KLS_FLEX_OK")
    finally:
        kileido.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
