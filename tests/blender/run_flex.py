"""Headless checks of flex mode: the bridge's flex frame reaches the board state, the
panel box lists the stack, the bends against the chosen use, and the checks; the Fold
slider folds the board's copies as foldmath says, parts with them, and unfolds again.

blender --background --factory-startup --python tests/blender/run_flex.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
import numpy as np  # noqa: E402
from kileido import apply, columns, fold, foldmath, state  # noqa: E402
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
    drawings += (model.Drawing("opening", "User.3", "closed",  # a window in the steel one
                               ((31_500_000, 2_500_000), (32_500_000, 2_500_000), (32_500_000, 3_500_000),
                                (31_500_000, 3_500_000))),)
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
    # A zone takes the highlight material: its copy is baked again from the original, which
    # the fold hides (Blender evaluates no hidden object: baked hidden, the copy came out bare).
    zone = next(o for o in state.board.collection.all_objects if (o.get("kls_copper") or ("", ""))[1] == "zones")
    zone_id = list(zone["kls_ids"])[0]

    def zone_copy():
        return next(c for c in folded.objects if c.get("kls_folded_from") == zone.name)
    faces = len(zone_copy().data.polygons)
    highlight.apply_selection({"selected": [zone_id], "pair": []})
    copy = zone_copy()
    assert [m.name for m in copy.data.materials] == ["KLS Highlight selected"], copy.data.materials[:]
    assert len(copy.data.polygons) == faces and zone.hide_viewport, (len(copy.data.polygons), faces)
    highlight.apply_selection({"selected": [], "pair": []})
    assert [m.name for m in zone_copy().data.materials] == ["KLS In1.Cu copper"], zone_copy().data.materials[:]


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
    tree = BVHTree.FromPolygons([v.co for v in steel.data.vertices], [p.vertices[:] for p in steel.data.polygons])
    window, solid = foldmath.world_xy([(32_000_000, 3_000_000), (30_500_000, 5_000_000)], state.board.origin_nm)
    assert tree.ray_cast(Vector((*window, 1.0)), Vector((0, 0, -1)))[0] is None, "the opening goes through"
    assert tree.ray_cast(Vector((*solid, 1.0)), Vector((0, 0, -1)))[0] is not None, "the steel is solid beside it"
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


def castellation_frames():
    """The fixture with its right part (x 23..40 mm) flex, a bend across it, a plated 1 mm
    drill on its right edge at y = 15 mm (a castellated pad) and a polyimide stiffener."""
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mm = 1_000_000
    pad = next(p for p in snapshot["pads"] if p.get("drill"))
    square = [[[[39 * mm, 14 * mm], [41 * mm, 14 * mm], [41 * mm, 16 * mm], [39 * mm, 16 * mm]]]]
    snapshot["pads"].append({**pad, "id": "55555555-5555-4555-8555-555555555556", "number": "2",
                             "pos": [40 * mm, 15 * mm], "drill": [mm, mm], "drill_shape": "circle",
                             "polygons": {"F.Cu": square, "B.Cu": square}})
    snapshot = snapshot_from_jsonable(snapshot)
    drawings = (model.Drawing("zone", "User.1", "closed", ((23 * mm, -mm), (41 * mm, -mm), (41 * mm, 31 * mm),
                                                           (23 * mm, 31 * mm))),
                model.Drawing("bend", "User.2", "line", ((26 * mm, -mm), (26 * mm, 31 * mm))),
                model.Drawing("bend text", "User.2", "text", ((26 * mm + 500_000, -2 * mm),), "90° R1"),
                model.Drawing("stiffener", "User.3", "closed",
                              ((34 * mm, 2 * mm), (37 * mm, 2 * mm), (37 * mm, 6 * mm), (34 * mm, 6 * mm))),
                model.Drawing("stiffener text", "User.3", "text", ((35 * mm, 4 * mm),), "Polyimide 0.1 mm bottom"))
    names = dict(snapshot.layer_display_names, **{"User.1": "Flex", "User.2": "Bend", "User.3": "Stiffener"})
    snapshot = model.BoardSnapshot(snapshot.board_name, names, snapshot.tracks, snapshot.arcs, snapshot.vias,
                                   snapshot.pads, snapshot.footprints, snapshot.zones, snapshot.outline,
                                   snapshot.stackup, snapshot.warnings, snapshot.read_timings_ms,
                                   snapshot.graphics, drawings)
    return b"".join(snapshot_frames(snapshot, flex=flex_checks.report(snapshot, STACK)))


def check_castellation(scene):
    """A drill on the flex's board edge: the flex's faces and outline walls take the hole
    mask as the rigid board's do, so the half hole and the wall across it are open (they
    were amber film). A polyimide stiffener keeps a solid material: its holes are cut in it."""
    from kileido import holes
    apply.load_frames(castellation_frames())
    scene.kileido_fold = 0.0
    folded = fold._folded_collection()
    outline = folded.objects["KLS folded outline"]
    flex = state.board.materials["flex"]
    plot = flex.node_tree.nodes.get("KLS holes plot")
    assert plot is not None and plot.image == holes.mask_image(), "the flex is holed"
    centre = foldmath.world_xy([(40_000_000, 15_000_000)], state.board.origin_nm)[0]
    materials = [slot.name for slot in outline.data.materials]
    walls = [face for face in outline.data.polygons if abs(face.normal.z) < 0.5
             and abs(face.center.x - centre[0]) < 1e-6 and min(v for v in (
                 outline.data.vertices[k].co.y for k in face.vertices)) < centre[1] < max(
                 outline.data.vertices[k].co.y for k in face.vertices)]
    assert walls and {materials[face.material_index] for face in walls} == {flex.name}, walls
    mask = holes.mask_image()
    pixels = np.empty(mask.size[0] * mask.size[1] * 4, np.float32)
    mask.pixels.foreach_get(pixels)
    pixels = pixels.reshape(mask.size[1], mask.size[0], 4)
    xmin, ymin, xmax, ymax = holes._m().bounds
    pixel = max(xmax - xmin, ymax - ymin) / holes.RESOLUTION
    column, row = int((centre[0] - 0.25e-3 - xmin) / pixel), int((centre[1] - ymin) / pixel)
    assert pixels[row, column, 3] > 0.99, pixels[row, column]  # inside the board, inside the drill
    # The mask reaches past the outline: the wall on it samples a whole hole, not the image's
    # last half pixel (half open: a see-through band across the hole, in flex and FR4 alike).
    edge = int((centre[0] - xmin) / pixel)
    around = pixels[row, edge - 1:edge + 2, 3]  # the pixels either side of the outline: all in the image, all open
    assert len(around) == 3 and around.min() > 0.99, (edge, mask.size[0], around)
    stiffener = folded.objects["KLS stiffener 1"]
    assert [m.name for m in stiffener.data.materials] == ["KLS Stiffener polyimide"], stiffener.data.materials[:]
    assert "KLS holes plot" not in stiffener.data.materials[0].node_tree.nodes
    # Seen straight down on a white background: the half hole is clear (not seen through
    # amber film, which read dark brown), the film beside it amber.
    hole, film = (centre[0] - 0.25e-3, centre[1]), (centre[0] - 1.0e-3, centre[1] + 1.6e-3)
    in_hole, on_film = rendered_colors(scene, (hole, film))
    assert in_hole.min() > 0.95, in_hole
    assert on_film[2] < 0.6 < on_film[0], on_film


def rendered_colors(scene, points):
    """Cycles, straight down on a glowing white sheet under the board: the sRGB colour at
    each world xy."""
    from bpy_extras.object_utils import world_to_camera_view
    middle = np.mean(points, axis=0)
    bpy.ops.mesh.primitive_plane_add(size=0.01, location=(*middle, -0.01))
    sheet = bpy.context.active_object
    white = bpy.data.materials.new("KLS flex test white")
    white.use_nodes = True
    glow = white.node_tree.nodes.new("ShaderNodeEmission")
    glow.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    white.node_tree.links.new(glow.outputs[0], next(n for n in white.node_tree.nodes
                                                    if n.type == "OUTPUT_MATERIAL").inputs["Surface"])
    sheet.data.materials.append(white)
    data = bpy.data.cameras.new("KLS flex test camera")
    data.type, data.ortho_scale, data.clip_start, data.clip_end = "ORTHO", 0.004, 1e-4, 1.0
    camera = bpy.data.objects.new("KLS flex test camera", data)
    scene.collection.objects.link(camera)
    camera.location = (*middle, 0.005)  # looking down -z, inside the factory scene's cube
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.samples, scene.cycles.use_denoising = 8, False
    scene.view_settings.view_transform = "Standard"  # rendered colours as they are
    scene.render.resolution_x = scene.render.resolution_y = 200
    path = Path(tempfile.gettempdir()) / "kileido_flex_castellation.png"
    scene.render.filepath = str(path)
    try:
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(str(path), check_existing=False)
        width, height = image.size
        pixels = np.array(image.pixels[:], np.float32).reshape(height, width, 4)[:, :, :3]
        bpy.data.images.remove(image)
        found = []
        for x, y in points:
            u, v, _ = world_to_camera_view(scene, camera, Vector((x, y, 0.0)))
            found.append(pixels[int(v * height), int(u * width)])
        return found
    finally:
        scene.camera = None
        bpy.data.objects.remove(camera)
        bpy.data.objects.remove(sheet)


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
    assert state.board.fold_findings == ["Folded: the part past bend 1 runs into the board"], state.board.fold_findings
    texts = " ".join(text for _, text, _ in drawn(scene))  # the panel wraps long lines
    assert "Folded: the part past bend 1 runs into the board" in texts, texts
    scene.kileido_fold = 0.0
    scene.kileido_flex = False  # flex mode off: the copies go once edits settle, and the findings with them
    fold._settled()
    assert fold._folded_collection() is None and state.board.fold_findings == [], state.board.fold_findings
    scene.kileido_flex = True


def places():
    """Where every part stands, by name."""
    return {obj.name: obj.matrix_world.copy() for obj in fold._parts()}


def assert_places(found, expected, what):
    assert found.keys() == expected.keys(), what
    for name, matrix in expected.items():
        off = max(abs(a - b) for row, other in zip(found[name], matrix) for a, b in zip(row, other))
        assert off < 1e-6, (what, name, off)


def check_uninstall(scene):
    """The add-on switched off: the board as without flex mode (no copies, no handles, the
    originals and parts back); on again, it folds as before."""
    scene.kileido_fold = 0.0
    flat = places()
    scene.kileido_fold = 1.0
    folded = places()
    outline = state.board.collection.all_objects["KLS outline"]
    assert outline.hide_viewport and fold._grips() and fold._folded_collection() is not None
    assert any(obj.get("kls_flat_matrix") is not None for obj in fold._parts())
    fold.uninstall()
    assert fold._folded_collection() is None and not fold._grips() and bpy.data.collections.get(fold.GRIPS) is None
    assert not outline.hide_viewport and not outline.hide_render
    assert not any(obj.get("kls_hidden_by_fold") for obj in state.board.collection.all_objects)
    assert not any(obj.get("kls_flat_matrix") is not None for obj in fold._parts())
    assert_places(places(), flat, "parts flat after uninstall")
    assert fold._on_depsgraph not in bpy.app.handlers.depsgraph_update_post
    fold.install()
    scene.kileido_fold = 1.0
    assert outline.hide_viewport and fold._folded_collection() is not None
    assert_places(places(), folded, "parts folded again after install")
    scene.kileido_fold = 0.0


def check_animation(scene):
    """The panel's Key fold animation: the handles and parts keyed per frame (a render moves
    nothing itself), the Fold slider scrubbing the timeline, and back to live folding."""
    apply.load_frames(bends_frames((26, "90° R1 #1"), (33, "90° R1 #2")))
    scene.kileido_fold = 0.0
    flat = places()
    scene.kileido_fold = 0.5
    half = places()
    scene.kileido_fold = 0.0
    assert ("operator", "Key fold animation", "KEYFRAME") in drawn(scene), drawn(scene)
    scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end = 30, 2.0, 10, 250
    scene.render.use_lock_interface = False
    before = (30, 2.0, 10, 250, False)
    scene.kileido_fold_frames, scene.kileido_fold_fps = 72, 24
    assert bpy.ops.kileido.fold_animation(action="KEY") == {"FINISHED"}
    assert fold.keyed() and (scene.frame_start, scene.frame_end, scene.render.fps) == (1, 72, 24)
    assert ("operator", "Re-key fold animation", "KEYFRAME_HLT") in drawn(scene), drawn(scene)
    plan = fold._plan()
    targets = foldmath.handle_targets(plan)
    for frame, expected in ((1, [0.0, 0.0]), (72, targets)):
        scene.frame_set(frame)
        assert [g.rotation_euler.z for g in fold._grips()] == pytest_approx(expected), (frame, expected)
    keyed_parts = [obj for obj in fold._parts() if obj.animation_data and obj.animation_data.action]
    assert keyed_parts and len(keyed_parts) == len(fold._parts())
    folded_at = {obj.name: obj.matrix_world.translation.copy() for obj in keyed_parts}
    scene.frame_set(1)
    assert any((obj.matrix_world.translation - folded_at[obj.name]).length > 1e-4 for obj in keyed_parts)  # with the board
    scene.kileido_fold = 0.5  # the slider scrubs the timeline while keyed
    assert scene.frame_current == 1 + round(0.5 * 71), scene.frame_current
    assert fold._render_starts in bpy.app.handlers.render_init  # nothing moves while Blender renders
    assert bpy.ops.kileido.fold_animation(action="CLEAR") == {"FINISHED"}
    assert not fold.keyed() and not any(g.animation_data for g in fold._grips())
    assert not any(obj.animation_data and obj.animation_data.action for obj in fold._parts())
    # Cleared half way along the timeline: the parts fold live from their true flat places,
    # not from where that frame left them; and the scene's frame settings are as before.
    assert_places(places(), half, "parts after clearing at frame 36")
    assert (scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end,
            scene.render.use_lock_interface) == before
    scene.kileido_fold = 0.0
    assert_places(places(), flat, "parts flat after clearing at frame 36")
    bpy.ops.kileido.fold_animation(action="KEY")  # keyed with the slider at 0, cleared at a middle frame: flat
    scene.frame_set(36)
    bpy.ops.kileido.fold_animation(action="CLEAR")
    assert_places(places(), flat, "parts flat after clearing at frame 36 with the slider at 0")
    bpy.ops.kileido.fold_animation(action="KEY")
    scene.frame_set(50)
    bpy.ops.kileido.fold_animation(action="KEY")  # keyed again from a middle frame: frame 1 is still flat
    scene.frame_set(1)
    assert_places(places(), flat, "parts flat at frame 1 after keying again")
    bpy.ops.kileido.fold_animation(action="CLEAR")
    scene.kileido_fold = 1.0  # live again: the slider turns the handles
    assert [g.rotation_euler.z for g in fold._grips()] == pytest_approx(targets)
    scene.kileido_fold = 0.0
    bpy.ops.kileido.fold_animation(action="KEY")
    scene.frame_set(36)
    apply.load_frames(bends_frames((28, "90° R1")))  # the board changed: its keys no longer fit
    scene.kileido_fold = 1.0
    assert not fold.keyed() and not any(g.animation_data for g in fold._grips())
    scene.kileido_fold = 0.0
    assert not any(obj.get("kls_flat_matrix") is not None for obj in fold._parts())


def pytest_approx(values, tolerance=1e-6):
    class Near(list):
        def __eq__(self, other):
            return len(other) == len(self) and all(abs(a - b) <= tolerance for a, b in zip(other, self))
    return Near(values)


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


def check_ribbon(scene):
    """A bend whose curve runs into a twist along the same tail: one ribbon, folded by the
    nodes as foldmath does, at either step and both."""
    snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
    mm = 1_000_000
    drawings = (model.Drawing("zone", "User.1", "closed", ((23 * mm, -mm), (41 * mm, -mm), (41 * mm, 31 * mm),
                                                           (23 * mm, 31 * mm))),
                model.Drawing("bend", "User.2", "line", ((31 * mm, -mm), (31 * mm, 31 * mm))),
                model.Drawing("bend text", "User.2", "text", ((31 * mm + 500_000, -2 * mm),), "90° R3 #1"),
                model.Drawing("twist", "User.2", "line", ((32 * mm, 15 * mm), (40 * mm, 15 * mm))),
                model.Drawing("twist text", "User.2", "text", ((33 * mm, 16 * mm),), "twist 60° #2"))
    names = dict(snapshot.layer_display_names, **{"User.1": "Flex", "User.2": "Bend"})
    snapshot = model.BoardSnapshot(snapshot.board_name, names, snapshot.tracks, snapshot.arcs, snapshot.vias,
                                   snapshot.pads, snapshot.footprints, snapshot.zones, snapshot.outline,
                                   snapshot.stackup, snapshot.warnings, snapshot.read_timings_ms,
                                   snapshot.graphics, drawings)
    apply.load_frames(b"".join(snapshot_frames(snapshot, flex=flex_checks.report(snapshot, STACK))))
    assert state.board.flex["ribbons"] == [[0, 1]] and not state.board.flex["problems"], state.board.flex["problems"]
    for progress in (0.5, 0.75, 1.0):
        scene.kileido_fold = progress
        compare_copies(lambda plan: foldmath.angles_at(plan, progress))
    scene.kileido_fold = 0.0


def check_switch(scene):
    """Flex mode is off until its column's Enable: no folded copy, no handles, the tab unlit;
    on, the tab lights and the board folds. IMS and Flex are never on together."""
    assert not scene.kileido_flex and fold._plan() is None
    assert columns.tab_state("FLEX") == "off"
    column = Layout()
    kileido._draw_flex_column(column, scene)
    assert ("prop", "kileido_flex", "") in column.drawn, column.drawn
    assert ("prop", "kileido_fold", "") not in column.drawn, column.drawn
    scene.kileido_flex = True
    assert fold._plan() is not None and columns.tab_state("FLEX") == "on"
    column = Layout()
    kileido._draw_flex_column(column, scene)
    assert ("prop", "kileido_fold", "") in column.drawn, column.drawn
    scene.kileido_ims = True  # IMS on turns Flex off, and says so in the IMS column
    assert not scene.kileido_flex and fold._plan() is None
    assert kileido._switched_off == {"IMS": "Flex"}, kileido._switched_off
    scene.kileido_flex = True  # and the other way round
    assert not scene.kileido_ims and kileido._switched_off == {"FLEX": "IMS"}, kileido._switched_off
    scene.kileido_column = "IMS"  # the note goes with the column it was shown in
    assert kileido._switched_off == {}
    scene.kileido_fold = 1.0
    scene.kileido_flex = False  # off and on again, the board comes back flat: the slider too
    assert fold._plan() is None and scene.kileido_fold == 0.0
    scene.kileido_column = "NONE"


def main():
    kileido.register()
    try:
        scene = bpy.context.scene
        apply.load_frames(frames())
        check_switch(scene)
        scene.kileido_flex = True
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
        check_ribbon(scene)
        check_cone(scene)
        check_castellation(scene)
        check_steps(scene)
        check_animation(scene)
        check_uninstall(scene)
        apply.load_frames(frames(flex=False))  # a board without flex: no box, a greyed tab
        assert state.board.flex == {} and drawn(scene) == []
        assert columns.tab_state("FLEX") == "unavailable"
        column = Layout()
        kileido._draw_flex_column(column, scene)
        assert ("label", "Enable", "CHECKBOX_DEHLT") in column.drawn, column.drawn
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
