"""Headless Blender checks of the cut plane on the synthetic fixture board, with a blind,
a buried and a through via added: the section is a solid face on the plane, with the
stackup, copper and vias where they belong, seen head-on and from above."""

import json
import math
import os
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import (apply, cut, focus, highlight, holes, laminate, metal, nodes, packages, pick, section,  # noqa: E402
                     shading, state)
from kileido.objects import outline_bounds  # noqa: E402
from kileido.placement import CAP_PLATING_M  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames, vias_message  # noqa: E402

MM, UM = 1e-3, 1e-6
OUT = Path(os.environ.get("KLS_CUT_DIR") or tempfile.gettempdir())  # renders, to look at
TOLERANCE = 0.05  # per channel, sRGB


def layered_snapshot():
    """The fixture (4 layers, In1 poured) with a blind via F.Cu-In1 at x = -10 mm, a buried
    In1-In2 at -6 mm and a through via at +10 mm, all on Blender y = 0 (KiCad y = 15 mm),
    where the default cut plane runs."""
    snapshot = json.loads((ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json")
                          .read_text(encoding="utf-8"))
    template = snapshot["vias"][0]  # 0.9 mm land, 0.35 mm drill
    snapshot["vias"] = [
        {**template, "id": f"44444444-4444-4444-8444-44444444444{index}", "pos": [x_mm * 1_000_000, 15_000_000],
         "layer_top": top, "layer_bottom": bottom}
        for index, (x_mm, top, bottom) in enumerate((
            (10, "F.Cu", "In1.Cu"), (14, "In1.Cu", "In2.Cu"), (30, "F.Cu", "B.Cu")), start=1)]
    return snapshot


OPEN = (0,) * 8
PLUGGED = (0, 0, 0, 0, 1, 1, 0, 0)  # IPC-4761 type III-b
PLUGGED_TOP = (0, 0, 0, 0, 1, 0, 0, 0)  # III-a
FILLED = (0, 0, 0, 0, 0, 0, 0, 1)  # V
FILLED_CAPPED = (0, 0, 0, 0, 0, 0, 1, 1)  # VII


def protect(snapshot, codes, revision=[100]):
    """KiCad sends the vias again with each via's protection (blind, buried, through)."""
    for via, code in zip(snapshot["vias"], codes):
        via["protection"] = list(code)
    revision[0] += 1
    apply.load_frames(vias_message(snapshot_from_jsonable(snapshot), revision[0]))
    cut.rebuild()  # what cut.invalidate's timer does, soon after (timers do not run headless)


def attribute(name, obj=None):
    """A per-via attribute's values, of the via mesh unless `obj` is given."""
    obj = obj or state.board.collection.all_objects["KLS vias"]
    return [round(value.value, 9) for value in obj.data.attributes[name].data]


def camera(scene, ortho_scale=None, location=(0, -0.2, 0.0008), rotation=(math.pi / 2, 0, 0), size=(1400, 300)):
    data = bpy.data.cameras.get("cut test") or bpy.data.cameras.new("cut test")
    obj = bpy.data.objects.get("cut test") or bpy.data.objects.new("cut test", data)
    if obj.name not in scene.collection.objects:
        scene.collection.objects.link(obj)
    data.type = "ORTHO" if ortho_scale else "PERSP"
    if ortho_scale:
        data.ortho_scale = ortho_scale
    data.clip_start, data.clip_end = 1e-4, 10.0
    obj.location, obj.rotation_euler = location, rotation
    scene.camera = obj
    scene.render.resolution_x, scene.render.resolution_y = size
    return obj


def render(scene, name):
    path = OUT / f"kileido_cut_{name}.png"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, height = image.size
        return np.array(image.pixels[:], np.float32).reshape(height, width, 4)[:, :, :3]
    finally:
        bpy.data.images.remove(image)


def color_at(scene, pixels, world):
    """The rendered sRGB colour at a world point (on the cut plane)."""
    u, v, _ = world_to_camera_view(scene, scene.camera, Vector(world))
    height, width = pixels.shape[:2]
    return pixels[int(v * height), int(u * width)]


def expect(scene, pixels, world, wanted, label):
    got = color_at(scene, pixels, world)
    if wanted is None:  # nothing drawn here: no section colour
        for color in (section.COPPER, section.CORE, section.PREPREG, section.RESIN):
            assert np.abs(got - np.array(color)).max() > 0.15, (label, got, color)
    elif wanted == section.RESIN:  # milky: its colour over the barrel seen through it
        assert np.abs(got - np.array(wanted)).max() < 1 - cut.RESIN_OPACITY + TOLERANCE, (label, got, wanted)
        assert got.mean() > 0.4, (label, got)  # light, not the dark open bore
    else:  # laminate or copper: its colour, lighter or darker where the weave or the polish is
        ratio = got / np.array(wanted)
        look = metal if wanted == section.COPPER else laminate
        low, high = look.DARKEST - TOLERANCE, look.LIGHTEST + TOLERANCE
        assert low < ratio.min() and ratio.max() < high, (label, got, wanted)
        assert np.ptp(ratio) < TOLERANCE, (label, got, wanted)  # lighter, not another colour


def modifier_value_named(obj, name):
    """An input of the object's Geometry Nodes modifier, by its name."""
    modifier = obj.modifiers[0]
    item = next(item for item in modifier.node_group.interface.items_tree
                if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name)
    return nodes.modifier_value(modifier, item.identifier)


def patch(scene, pixels, low, high):
    """Brightness of the pixels between two world points (opposite corners on the plane)."""
    (u0, v0, _), (u1, v1, _) = (world_to_camera_view(scene, scene.camera, Vector(p)) for p in (low, high))
    height, width = pixels.shape[:2]
    rows = slice(int(min(v0, v1) * height), int(max(v0, v1) * height))
    columns = slice(int(min(u0, u1) * width), int(max(u0, u1) * width))
    return pixels[rows, columns].mean(axis=2)


def main():
    assert bpy.app.background, "run with Blender --background"
    kileido.register()
    scene = bpy.context.scene
    try:
        layered = layered_snapshot()
        apply.load_frames(b"".join(snapshot_frames(snapshot_from_jsonable(layered))))
        board = state.board
        # Two prepregs around a core (what the bridge reads from a saved board's stackup).
        board.appearance["dielectrics"] = [{"type": "prepreg", "material": "FR4"},
                                           {"type": "core", "material": "FR4"},
                                           {"type": "prepreg", "material": "FR4"}]
        assert not cut.enabled() and bpy.data.objects.get(cut.FACE) is None
        assert all(cut.NODE not in m.node_tree.nodes for m in cut.materials() if m.node_tree)

        # On: every material clips (last, after X-ray mode), the plane and the face appear.
        scene.kileido_cut = True
        plane, face = bpy.data.objects[cut.PLANE], bpy.data.objects[cut.FACE]
        assert plane.hide_render and not plane.hide_get() and face.data.materials[0].name == cut.FACE
        assert not plane.visible_camera and not plane.visible_shadow  # a Cycles viewport drew its sheet otherwise
        assert not face.hide_get() and len(face.data.polygons) > 10
        materials = [m for m in cut.materials() if m.node_tree]
        assert materials and all(cut.NODE in m.node_tree.nodes for m in materials)
        focus.add_to(board.materials["board"])
        output = next(n for n in board.materials["board"].node_tree.nodes if n.type == "OUTPUT_MATERIAL")
        last = output.inputs["Surface"].links[0].from_node
        assert last.name == cut.NODE and last.inputs[0].links[0].from_node.name == focus.NODE
        assert cut.upright(scene)
        section_nodes = bpy.data.materials[cut.FACE].node_tree.nodes  # fades in X-ray mode, never clipped itself
        assert focus.NODE in section_nodes and cut.NODE not in section_nodes

        scene.view_settings.view_transform = "Standard"  # rendered colours are the section's own sRGB
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 8
        scene.cycles.use_denoising = False
        protect(layered, [OPEN] * 3)
        # Holes: the through via both sides, the blind one from the top; the buried one is none.
        assert holes._sources["vias"][:, 3].tolist() == [holes.TOP, holes.THROUGH]
        mask = bpy.data.images[holes.IMAGE]
        pixels = np.empty(mask.size[0] * mask.size[1] * 4, np.float32)
        mask.pixels.foreach_get(pixels)
        pixels = pixels.reshape(mask.size[1], mask.size[0], 4)
        xmin, ymin, xmax, ymax = holes._bounds
        pixel = max(xmax - xmin, ymax - ymin) / holes.RESOLUTION

        def channels_at(index):
            """(top, bottom, through) hole coverage at a via's centre."""
            x, y = board.collection.all_objects["KLS vias"].data.vertices[index].co[:2]
            red, green, _, alpha = pixels[int((y - ymin) / pixel), int((x - xmin) / pixel)]
            return round(float(red)), round(float(green)), round(float(alpha))

        assert channels_at(0) == (1, 0, 0) and channels_at(1) == (0, 0, 0) and channels_at(2) == (1, 1, 1)
        protect(layered, [PLUGGED] * 3)
        assert len(holes._sources["vias"]) == 2  # still holes: their lands are rings, the plug shows in them
        assert attribute("bare_barrel") == [1, 1, 1]  # the plug kept the finish out
        # In 3D too the barrels are plugged: a core in the plug's material, and a highlighted
        # via's core in the highlight's (solid in X-ray mode, where everything else fades).
        vias = board.collection.all_objects["KLS vias"]

        def via_faces():
            """(instances, their materials' names): the group's lands, barrels and cores are instances."""
            bpy.context.view_layer.update()
            depsgraph = bpy.context.evaluated_depsgraph_get()
            count, names = 0, set()
            for instance in depsgraph.object_instances:  # read each while iterating: Blender reuses them
                if instance.is_instance and instance.parent and instance.parent.original == vias:
                    count += 1
                    names |= {material.name for material in instance.object.data.materials if material}
            return count, names

        plugged_faces, plugged_materials = via_faces()
        assert board.materials["via_plug"].name in plugged_materials, (plugged_faces, plugged_materials)
        assert board.materials["via_resin"].name in plugged_materials  # the buried via: the prepreg's resin
        ink = kileido.materials.plug_ink()
        assert any(rect[4] == ink for rect in cut.rectangles(scene)[0])  # plugs are mask ink in the section too
        assert attribute("core_top") == [1, 1, 1] and attribute("core_bottom") == [1, 1, 1]  # buried: always resin
        highlight.apply_selection({"selected": ["44444444-4444-4444-8444-444444444443"], "pair": []})
        marked = board.collection.all_objects["KLS vias highlight selected"]
        assert attribute("core_top", marked) == [1]
        assert modifier_value_named(marked, "Fill Material") == board.materials["highlight_selected_barrel"]
        highlight.apply_selection({"selected": [], "pair": []})
        protect(layered, [PLUGGED_TOP] * 3)  # the blind via's one open end: still full; the through via: half
        assert attribute("core_top") == [1, 1, 1] and attribute("core_bottom") == [1, 1, 0]
        protect(layered, [OPEN] * 3)
        assert attribute("bare_barrel") == [0, 1, 0]  # open: finished like the pads (buried: never reached)
        protect(layered, [(1, 1, 0, 0, 0, 0, 0, 0)] * 3)  # tented, type I-b
        assert attribute("tent_top") == [1, 0, 1]  # 0.35 mm drill, 25 um wall: a 0.30 mm hole, tented
        scene.kileido_via_plating_um = 10.0
        assert attribute("tent_top") == [0, 0, 0] and board.via_too_big == 2  # a 0.33 mm hole: too large
        scene.kileido_via_plating_um = 25.0
        assert len(holes._sources["vias"]) == 2 and attribute("tent_top") == [1, 0, 1]  # holes, closed by tents
        assert attribute("tent_bottom") == [0, 0, 1]  # the blind via has no bottom end to tent
        assert {board.materials["tent_F"].name, board.materials["tent_B"].name} <= via_faces()[1]
        assert board.materials["plating_bare"].name in via_faces()[1]
        protect(layered, [OPEN] * 3)
        open_faces, open_materials = via_faces()
        assert open_faces < plugged_faces and attribute("core_top") == [0, 1, 0]  # the buried via stays filled
        protect(layered, [FILLED_CAPPED] * 3)
        assert attribute("cap") == [round(CAP_PLATING_M, 9), 0, round(CAP_PLATING_M, 9)]  # buried: nothing to cap

        def z_ranges(x):
            """(low, high) z of the via group's instances at x, by material name."""
            bpy.context.view_layer.update()
            found = {}
            for instance in bpy.context.evaluated_depsgraph_get().object_instances:
                if instance.is_instance and instance.parent and instance.parent.original == vias:
                    corners = [instance.matrix_world @ Vector(corner) for corner in instance.object.bound_box]
                    if abs(sum(c.x for c in corners) / 8 - x) < 1e-4:
                        name = next((m.name for m in instance.object.data.materials if m), "")
                        low, high = min(c.z for c in corners), max(c.z for c in corners)
                        found.setdefault(name, []).append((low, high))
            return found

        through = z_ranges(vias.data.vertices[2].co.x)
        (bottom_land, top_land), (core,) = sorted(through["KLS Vias"]), through["KLS Via resin"]
        surface = board.heights["F.Cu"]
        assert top_land[1] > surface + CAP_PLATING_M and top_land[1] - top_land[0] > CAP_PLATING_M  # a solid land
        assert core[1] < top_land[0] and core[0] > bottom_land[1]  # the fill stays under both lands
        blind = z_ranges(vias.data.vertices[0].co.x)  # F.Cu to In1: one outer land, a floor on In1
        assert len(blind["KLS Vias"]) == 1
        # Its floor: plated like its barrel, flat on In1, not mask-covered.
        (floor,) = [z for z in blind[board.materials["plating_bare"].name] if z[0] == z[1]]
        assert abs(floor[0] - board.heights["In1.Cu"]) < 5e-6, floor

        # KiCad's "Annular rings": the 3D outer lands and the section's rings follow the layers
        # the bridge gives a ring (kileido_bridge.via_rings). Start and end layers only:
        layered["vias"][2]["rings"] = 4  # model.RINGS_ENDS
        protect(layered, [FILLED_CAPPED] * 3)
        names = list(vias["kls_ring_layers"])
        through = int(vias.data.attributes["ringed"].data[2].value) & 0xFFFFFFFF
        assert {name for i, name in enumerate(names) if through >> i & 1} == {"F.Cu", "B.Cu"}
        assert attribute("ring_top")[2] == 1 and attribute("ring_bottom")[2] == 1
        layered["vias"][2]["rings"] = 2  # model.RINGS_CONNECTED, with no copper touching it on F.Cu or B.Cu
        protect(layered, [FILLED_CAPPED] * 3)
        assert attribute("ring_top")[2] == 0 and attribute("ring_bottom")[2] == 0
        assert len(z_ranges(vias.data.vertices[2].co.x)["KLS Vias"]) == 2  # no lands: its caps over the drill
        protect(layered, [OPEN] * 3)
        assert "KLS Vias" not in z_ranges(vias.data.vertices[2].co.x)  # uncapped and ringless: nothing outside
        # Its inner rings (In1, In2) are flat disks of their own, here none: none connect either.
        rings = board.collection.all_objects["KLS vias rings"]
        assert 2 not in [value.value for value in rings.data.attributes["via"].data]
        protect(layered, [FILLED_CAPPED] * 3)
        layered["vias"][2]["rings"] = 1  # model.RINGS_ALL again
        protect(layered, [FILLED_CAPPED] * 3)
        assert len(z_ranges(vias.data.vertices[2].co.x)["KLS Vias"]) == 2
        inner = [z for z, via in zip((v.co.z for v in rings.data.vertices),
                                     (value.value for value in rings.data.attributes["via"].data)) if via == 2]
        assert sorted(round(z * 1e6) for z in inner) == sorted(round((board.heights[name] + 3e-6) * 1e6)
                                                               for name in ("In1.Cu", "In2.Cu"))
        # Inner copper is never exposed: the rings are bare copper in a material of their own,
        # whatever the board's finish (the lands' material takes it where the mask is open).
        ring_material = modifier_value_named(rings, "Material")
        assert ring_material == board.materials["via_rings"] and ring_material != board.materials["vias"]
        before = dict(board.appearance)
        board.appearance.update(copper_finish="ENIG", viewer={**before.get("viewer", {}), "copper": (0.83, 0.68, 0.30)})
        for mode in ("REALISTIC", "FAB"):
            scene.kileido_color_mode = mode
            assert "KLS finish mix" not in ring_material.node_tree.nodes
            shown = np.array(ring_material.diffuse_color[:3])
            assert np.allclose(shown, shading.srgb_to_linear(kileido.materials.BARE_COPPER), atol=1e-4), (mode, shown)
            assert not np.allclose(shown, board.materials["vias"].diffuse_color[:3], atol=0.02)  # ENIG there
        # Outer copper's face on the laminate has no mask on it and never got the finish: seen
        # from inside the board (cut open, hidden or see-through) it is bare copper, while its
        # outside keeps the mask's colour over it.
        colors = kileido.materials
        no_openings = bpy.data.images.new("KLS test mask", 4, 4, alpha=True)
        no_openings.pixels.foreach_set(np.zeros(4 * 4 * 4, np.float32))  # alpha 0: the mask covers everything
        for side in "FB":
            colors.set_mask_image(side, no_openings, (-0.03, -0.02, 0.03, 0.02))
        scene.kileido_color_mode = "FAB"  # flat (Shaded: lit head-on by a softbox): the materials' own colours
        rows = [f"kileido_show_{row}" for row in ("board", "vias", "F_Cu", "In1_Cu", "In2_Cu", "B_Cu")]
        for row in rows:
            setattr(scene, row, False)
        for layer, inside in (("B.Cu", 1), ("F.Cu", -1)):  # the side its laminate face is seen from: above, below
            shown = "kileido_show_" + layer.replace(".", "_")
            setattr(scene, shown, True)
            tracks = board.collection.all_objects[f"KLS {layer} tracks"]
            ends = [tracks.matrix_world @ vertex.co for vertex in tracks.data.vertices]
            point = next((ends[a] + ends[b]) / 2 for a, b in (edge.vertices for edge in tracks.data.edges)
                         if min(ends[a].y, ends[b].y) > 1 * MM)  # on the side the cut leaves
            covered = colors.seen_through(colors.mask_color(layer[0]), colors.COPPER_UNDER_MASK)
            for side, wanted in ((inside, colors.BARE_COPPER), (-inside, covered)):
                camera(scene, ortho_scale=0.002, location=(point.x, point.y, side * 0.05),
                       rotation=(0 if side > 0 else math.pi, 0, 0), size=(200, 200))
                got = color_at(scene, render(scene, f"{layer}_from_{'above' if side > 0 else 'below'}"), point)
                assert np.abs(got - np.array(wanted[:3])).max() < TOLERANCE, (layer, side, got, wanted)
            setattr(scene, shown, False)
        for material in ("copper:F.Cu", "copper:B.Cu", "vias"):  # the lands too: top above mid-board, bottom below
            assert colors.EXPOSED in board.materials[material].node_tree.nodes
        for row in rows:
            setattr(scene, row, True)
        board.mask_images.clear()
        colors.refresh_mask_colors()
        bpy.data.images.remove(no_openings)
        board.appearance.clear()
        board.appearance.update(before)
        scene.kileido_color_mode = "FAB"
        highlight.apply_selection({"selected": ["44444444-4444-4444-8444-444444444443"], "pair": []})
        marked = board.collection.all_objects["KLS vias rings highlight selected"]
        assert len(marked.data.vertices) == 2 and not marked.hide_get()  # X-ray mode shows them in its colour
        highlight.apply_selection({"selected": [], "pair": []})
        scene.kileido_via_fill_material = "COPPER"
        assert attribute("fill_copper") == [1, 1, 1]
        assert board.materials["via_resin"].name not in via_faces()[1]
        scene.kileido_via_fill_material = "RESIN"
        protect(layered, [FILLED] * 3)  # resin, for the renders below

        # Head-on: the stackup across the board.
        camera(scene, ortho_scale=0.044)
        front = render(scene, "front")
        for x_mm, z_um, wanted, label in (
                (-15, 500, section.PREPREG, "bottom prepreg"), (-15, 1000, section.CORE, "core"),
                (-15, 1400, section.PREPREG, "top prepreg"), (21, 800, None, "beyond the board edge")):
            expect(scene, front, (x_mm * MM, 0, z_um * UM), wanted, label)

        # The laminate's weave, away from vias: a picture to look at (both engines below check it).
        camera(scene, ortho_scale=0.003, location=(-0.015, -0.2, 770 * UM), size=(1200, 760))
        render(scene, "weave")

        # Zoomed on the blind via: its walls, plug, lands and the In1 pour at their thickness,
        # in both engines (EEVEE is the panel's Preview).
        camera(scene, ortho_scale=0.0006, location=(-0.010, -0.2, 1400 * UM), size=(1400, 700))
        scene.eevee.taa_render_samples = 8
        for engine in ("CYCLES", "BLENDER_EEVEE"):
            scene.render.engine = engine
            via = render(scene, f"blind_via_{engine.lower()}")
            for dx_um, z_um, wanted, label in (
                    (0, 1400, section.RESIN, "fill"), (162, 1400, section.COPPER, "barrel wall"),
                    (250, 1400, section.PREPREG, "laminate beside the barrel"),
                    (-250, 1290, section.COPPER, "In1 copper"), (250, 1522, section.COPPER, "F.Cu land")):
                expect(scene, via, (-0.010 + dx_um * UM, 0, z_um * UM), wanted, f"{engine}: {label}")
            woven = patch(scene, via, (-0.010 + 200 * UM, 0, 1320 * UM), (-0.010 + 290 * UM, 0, 1490 * UM))
            polished = patch(scene, via, (-0.010 - 290 * UM, 0, 1275 * UM), (-0.010 - 200 * UM, 0, 1300 * UM))
            assert woven.std() > 0.01 and polished.std() > 0.004, (
                engine, woven.std(), polished.std())  # weave in laminate, polish in copper
        # Filled and capped (type VII): plated over at the outer copper, in the section and in 3D.
        scene.render.engine = "BLENDER_EEVEE"
        protect(layered, [FILLED_CAPPED] * 3)
        camera(scene, ortho_scale=0.0006, location=(-0.010, -0.2, 1450 * UM), size=(1400, 700))  # up to the cap
        capped = render(scene, "blind_via_capped")
        expect(scene, capped, (-0.010, 0, 1522 * UM), section.COPPER, "cap over the fill, in F.Cu")
        expect(scene, capped, (-0.010, 0, 1550 * UM), section.COPPER, "cap plating above F.Cu")
        expect(scene, capped, (-0.010, 0, 1400 * UM), section.RESIN, "fill under the cap")
        scene.render.engine = "CYCLES"
        protect(layered, [OPEN] * 3)
        open_bore = render(scene, "blind_via_open")
        assert np.abs(color_at(scene, open_bore, (-0.010, 0, 1400 * UM)) - section.RESIN).max() > 0.1

        # From above and in front, looking down at the cut: the face, not the In1 pour behind it.
        camera(scene, location=(-0.015, -0.012, 0.010), rotation=(math.radians(50), 0, 0), size=(1000, 700))
        scene.camera.data.lens = 50
        above = render(scene, "above")
        for z_um, wanted, label in ((500, section.PREPREG, "bottom prepreg"), (1000, section.CORE, "core"),
                                    (1400, section.PREPREG, "top prepreg")):
            expect(scene, above, (-0.015, 0, z_um * UM), wanted, f"seen from above: {label}")

        # Moving the plane moves the section; tilted, there is none.
        plane.location.y = 0.004
        bpy.context.view_layer.update()
        cut._on_depsgraph(scene, None)
        ys = {round(v.co.y, 7) for v in face.data.vertices}
        assert ys == {round(0.004 - cut.FACE_OFFSET_M, 7)}, ys
        plane.rotation_euler.x += 0.3
        bpy.context.view_layer.update()
        cut._on_depsgraph(scene, None)
        assert not cut.upright(scene) and not len(face.data.polygons) and face.hide_get()
        cut.place("Y", centered=True)
        assert cut.upright(scene) and len(face.data.polygons)

        # A layer's eye: the section draws what is shown, as soon as the eye changes (not only
        # at the board's next edit), and gets it back with the eye.
        def drawn(name):
            flags = attribute(name, face)
            return sum(polygon.area for polygon in face.data.polygons if flags[polygon.vertices[0]] > 0.5)

        copper_shown, laminate_shown = drawn(cut.FACE_METAL), drawn(cut.FACE_WEAVE)
        if bpy.app.timers.is_registered(cut._rebuild_soon):  # left from the frames above
            bpy.app.timers.unregister(cut._rebuild_soon)
        scene.kileido_show_In1_Cu = False
        assert bpy.app.timers.is_registered(cut._rebuild_soon)  # redrawn soon, once per burst of eyes
        cut.rebuild()  # what that timer does (timers do not run headless)
        assert drawn(cut.FACE_METAL) < 0.9 * copper_shown  # In1's pour is gone from the section
        scene.kileido_show_In1_Cu = True
        cut.rebuild()
        assert math.isclose(drawn(cut.FACE_METAL), copper_shown, rel_tol=1e-9)
        scene.kileido_show_board = False
        cut.rebuild()
        assert drawn(cut.FACE_WEAVE) == 0 and drawn(cut.FACE_METAL) > 0  # no board solid: copper alone
        scene.kileido_show_board = True
        cut.rebuild()
        assert math.isclose(drawn(cut.FACE_WEAVE), laminate_shown, rel_tol=1e-9)

        # Clicks pick only on the side that is shown: the removed side is see-through, not gone.
        _, at, removed = cut._state(scene)
        at, removed = Vector(at), Vector(removed)
        xmin, ymin, xmax, ymax = outline_bounds()

        def picked(origin, direction):
            return pick.item_at(scene, bpy.context.evaluated_depsgraph_get(), origin, direction)

        def down(point):
            return picked(Vector((point[0], point[1], 0.05)), Vector((0, 0, -1)))

        def well_inside(side):
            """(point, its item) on one side of the plane (+1: the removed side), the same item
            0.1 mm around it, as picked from straight above with the board whole."""
            for x in np.linspace(xmin, xmax, 80):
                for y in np.linspace(ymin, ymax, 60):
                    if side * (Vector((x, y, at.z)) - at).dot(removed) < 1 * MM:
                        continue
                    found = down((x, y))
                    if found is not None and all(down((x + dx, y + dy)) == found for dx, dy in (
                            (0.1 * MM, 0), (-0.1 * MM, 0), (0, 0.1 * MM), (0, -0.1 * MM))):
                        return Vector((x, y, board.thickness_m)), found
            raise AssertionError("no item to pick on this side")

        scene.kileido_cut = False
        (gone, gone_item), (kept, kept_item) = well_inside(1), well_inside(-1)
        scene.kileido_cut = True
        cut.place("Y", centered=True)
        assert down(gone) is None and down(kept) == kept_item

        def across(point):
            """Picked by a slanted ray at `point` from over the plane's other side: it passes
            the plane 5 mm above the board, half way there."""
            origin = point - removed * (2 * (point - at).dot(removed)) + Vector((0, 0, 0.010))
            return picked(origin, point - origin)

        # A view ray counts only where it runs through the shown side.
        assert across(kept) == kept_item  # from over the removed side, onto the shown side
        assert across(gone) is None  # from over the shown side, into the removed side
        scene.kileido_cut_flip = True  # the other side removed instead
        assert down(gone) == gone_item and down(kept) is None
        assert across(gone) == gone_item and across(kept) is None
        scene.kileido_cut_flip = False
        scene.kileido_cut = False
        assert down(gone) == gone_item and down(kept) == kept_item  # whole again: both pick
        scene.kileido_cut = True

        # The drawn plane reaches past the board however it is turned about Z, stands well
        # clear of it, and sits on the board's middle. Turned by hand it keeps doing so.
        diagonal = math.hypot(xmax - xmin, ymax - ymin)

        def sheet():
            """(its length along the board, how far it stands) from its corners."""
            corners = [plane.matrix_world @ plane.data.vertices[index].co for index in range(4)]
            flat = [Vector((corner.x, corner.y)) for corner in corners]
            return (max((a - b).length for a in flat for b in flat),
                    max(corner.z for corner in corners) - min(corner.z for corner in corners))

        for turn in ("Y", "X"):
            cut.place(turn, centered=True)
            length, height = sheet()
            assert length > diagonal and height >= 10 * board.thickness_m and height > 0.3 * diagonal, (turn, length)
            assert np.allclose(plane.location[:2], ((xmin + xmax) / 2, (ymin + ymax) / 2), atol=1e-9)
        plane.rotation_euler.z += math.radians(35)  # by hand
        plane.scale = (0.001, 0.001, 0.001)
        scene.kileido_cut = False
        scene.kileido_cut = True  # sized to the board again, the way it stands now
        bpy.context.view_layer.update()
        length, height = sheet()
        assert length > diagonal and math.isclose(height, max(10 * board.thickness_m, cut.SHEET_HEIGHT * diagonal),
                                                  rel_tol=1e-6), (length, height)
        assert cut.upright(scene)

        # A click on the plane as drawn (its border, its arrow) selects it, so it can be moved
        # while KiCad is connected and every other click selects in KiCad.
        cut.place("Y", centered=True)

        def front(point):  # the front view, 10 pixels a millimetre
            return Vector((point.x * 1e4, point.z * 1e4))

        corner, middle = (front(plane.matrix_world @ Vector(local)) for local in ((-0.5, -0.5, 0), (0, 0, 0)))
        assert cut.clicked(front, corner + Vector((3, 0))) and not cut.clicked(front, corner - Vector((30, 30)))
        assert not cut.clicked(front, (corner + middle) / 2)  # inside its sheet: the board behind it
        assert cut.clicked(lambda point: Vector((point.x * 1e4, point.y * 1e4)),  # from above: along its arrow
                           Vector((middle.x, (plane.location.y - 0.4 * plane.scale.z) * 1e4)))
        assert not cut.clicked(lambda point: None, corner)  # behind the view
        cut.select()
        assert plane.select_get() and bpy.context.view_layer.objects.active == plane
        assert [obj.name for obj in bpy.context.selected_objects] == [cut.PLANE]
        plane.select_set(False)
        scene.kileido_cut = False
        assert not cut.clicked(front, corner + Vector((3, 0)))  # hidden: nothing to click
        scene.kileido_cut = True

        # An exported package never carries the clip stage; the live board keeps it.
        package = OUT / "kileido_cut_package.blend"
        packages.export_board(str(package))
        with bpy.data.libraries.load(str(package)) as (data_from, _):
            assert cut.GROUP not in data_from.node_groups, data_from.node_groups
        assert all(cut.NODE in m.node_tree.nodes for m in materials)

        # Realistic mode: the board's edges show the stackup and the weave; other modes keep KiCad's colour.
        core = board.materials["board_core"]
        principled = next(n for n in core.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        scene.kileido_color_mode = "REALISTIC"
        assert principled.inputs["Base Color"].links[0].from_node.name == laminate.EDGE_NODE
        section_nodes = bpy.data.materials[cut.FACE].node_tree.nodes  # its copper: lit metal, as all copper here
        assert section_nodes["KLS realistic"].outputs[0].default_value == 1.0
        assert section_nodes["KLS cut copper"].inputs["Metallic"].default_value == 1.0
        scene.kileido_cut = False
        camera(scene, ortho_scale=0.002, location=(-0.012, -0.05, 800 * UM), size=(1000, 500))
        scene.cycles.samples = 64  # lit, so noisier than the flat section
        render(scene, "edge_realistic")
        scene.cycles.samples = 8
        scene.render.engine = "BLENDER_EEVEE"
        edge = render(scene, "edge_realistic_eevee")
        scene.render.engine = "CYCLES"
        assert edge.std() > 0.005  # bands and weave, not one flat colour
        scene.kileido_cut = True
        scene.kileido_color_mode = "FAB"
        assert not principled.inputs["Base Color"].is_linked
        assert section_nodes["KLS realistic"].outputs[0].default_value == 0.0  # flat, like the board's copper

        # Seeing into the cut: the section face can be hidden, and a light shines into the cut
        # from the removed side, along the plane's normal (and follows a flip).
        scene.kileido_cut_face = False
        assert face.hide_get() and not len(face.data.polygons)
        scene.kileido_cut_face = True
        assert not face.hide_get() and len(face.data.polygons) > 10
        assert bpy.data.objects.get(cut.LIGHT) is None
        for flip in (False, True):
            scene.kileido_cut_flip = flip
            scene.kileido_cut_light = True
            light = bpy.data.objects[cut.LIGHT]
            bpy.context.view_layer.update()
            _, origin, normal = cut._state(scene)
            shines = light.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))  # an area light's
            assert (light.location - Vector(origin)).dot(Vector(normal)) > 0, flip  # on the removed side
            assert shines.dot(Vector(normal)) < -0.999, (flip, shines, normal)  # into the cut
            assert not light.hide_render and not light.hide_get() and light.data.energy > 0
            scene.kileido_cut_light = False
            assert light.hide_render and light.hide_get()
        scene.kileido_cut_flip = False

        # Shaded: Board stackup and PCB Editor colours lit (by the softboxes and the cut
        # light) instead of flat, in the same colours; off, flat as KiCad draws them.
        copper = board.materials["copper:F.Cu"]
        lit = copper.node_tree.nodes[shading.LIT_FLAT]
        emission = next(n for n in copper.node_tree.nodes if n.type == "EMISSION")
        assert scene.kileido_shaded and lit.outputs[0].is_linked and not emission.outputs[0].is_linked
        assert (lit.inputs["Color"].links[0].from_socket == emission.inputs["Color"].links[0].from_socket
                if emission.inputs["Color"].is_linked else
                np.allclose(lit.inputs["Color"].default_value, emission.inputs["Color"].default_value))
        scene.kileido_shaded = False
        assert emission.outputs[0].is_linked and not lit.outputs[0].is_linked
        scene.kileido_shaded = True

        # Off: stages gone, plane and face hidden, nothing drawn.
        scene.kileido_cut_light = True
        scene.kileido_cut = False
        assert all(cut.NODE not in m.node_tree.nodes for m in cut.materials() if m.node_tree)
        assert plane.hide_get() and face.hide_get() and not len(face.data.polygons)
        assert bpy.data.objects[cut.LIGHT].hide_render  # no cut, no cut light
        scene.kileido_cut_light = False
        print("KLS_CUT_OK=" + json.dumps({"renders": str(OUT)}))
    finally:
        kileido.unregister()


main()
