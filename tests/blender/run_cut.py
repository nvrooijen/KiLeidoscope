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
from kileido import apply, cut, focus, highlight, holes, laminate, metal, nodes, packages, section, state  # noqa: E402
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
FILLED_CAPPED = (0, 0, 0, 0, 0, 0, 1, 1)  # VII


def protect(snapshot, codes, revision=[100]):
    """KiCad sends the vias again with each via's protection (blind, buried, through)."""
    for via, code in zip(snapshot["vias"], codes):
        via["protection"] = list(code)
    revision[0] += 1
    apply.load_frames(vias_message(snapshot_from_jsonable(snapshot), revision[0]))
    cut.rebuild()  # what cut.invalidate's timer does, soon after (timers do not run headless)


def attribute_values(name):
    mesh = state.board.collection.all_objects["KLS vias"].data
    return [round(value.value, 9) for value in mesh.attributes[name].data]


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
        assert len(holes._sources["vias"]) == 1  # the through via is see-through; blind and buried never
        protect(layered, [PLUGGED] * 3)
        assert len(holes._sources["vias"]) == 1  # still a hole: its lands are rings, the plug shows in it
        assert attribute_values("bare_barrel") == [1, 1, 1]  # the plug kept the finish out
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

        def attribute(name, obj=vias):
            return [round(value.value, 9) for value in obj.data.attributes[name].data]

        plugged_faces, plugged_materials = via_faces()
        assert board.materials["via_resin"].name in plugged_materials, (plugged_faces, plugged_materials)
        assert attribute("core_top") == [1, 0, 1] and attribute("core_bottom") == [1, 0, 1]  # buried: no outer end
        highlight.apply_selection({"selected": ["44444444-4444-4444-8444-444444444443"], "pair": []})
        marked = board.collection.all_objects["KLS vias highlight selected"]
        assert attribute("core_top", marked) == [1] and             modifier_value_named(marked, "Fill Material") == board.materials["highlight_selected_barrel"]
        highlight.apply_selection({"selected": [], "pair": []})
        protect(layered, [PLUGGED_TOP] * 3)  # the blind via's one open end: still full; the through via: half
        assert attribute("core_top") == [1, 0, 1] and attribute("core_bottom") == [1, 0, 0]
        protect(layered, [OPEN] * 3)
        assert attribute("bare_barrel") == [0, 1, 0]  # open: finished like the pads (buried: never reached)
        protect(layered, [(1, 1, 0, 0, 0, 0, 0, 0)] * 3)  # tented, type I-b
        assert attribute("tent_top") == [0, 0, 0] and board.via_too_big == 2  # 0.35 mm: too large to tent
        scene.kileido_max_tent_mm = 0.4
        assert len(holes._sources["vias"]) == 1 and attribute("tent_top") == [0, 0, 1]  # a hole, closed by tents
        assert {board.materials["tent_F"].name, board.materials["tent_B"].name} <= via_faces()[1]
        assert board.materials["plating_bare"].name in via_faces()[1]
        scene.kileido_max_tent_mm = 0.3
        protect(layered, [OPEN] * 3)
        open_faces, open_materials = via_faces()
        assert open_faces < plugged_faces and board.materials["via_resin"].name not in open_materials
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
        scene.kileido_via_fill_material = "COPPER"
        assert attribute("fill_copper") == [1, 1, 1]
        assert board.materials["via_resin"].name not in via_faces()[1]
        scene.kileido_via_fill_material = "RESIN"
        protect(layered, [PLUGGED] * 3)

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
                    (0, 1400, section.RESIN, "plug"), (162, 1400, section.COPPER, "barrel wall"),
                    (250, 1400, section.PREPREG, "laminate beside the barrel"),
                    (-250, 1290, section.COPPER, "In1 copper"), (250, 1522, section.COPPER, "F.Cu land")):
                expect(scene, via, (-0.010 + dx_um * UM, 0, z_um * UM), wanted, f"{engine}: {label}")
            woven = patch(scene, via, (-0.010 + 200 * UM, 0, 1320 * UM), (-0.010 + 290 * UM, 0, 1490 * UM))
            polished = patch(scene, via, (-0.010 - 290 * UM, 0, 1275 * UM), (-0.010 - 200 * UM, 0, 1300 * UM))
            assert woven.std() > 0.01 and polished.std() > 0.004, (
                engine, woven.std(), polished.std())  # weave in laminate, polish in copper
        scene.render.engine = "CYCLES"
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

        # Off: stages gone, plane and face hidden, nothing drawn.
        scene.kileido_cut = False
        assert all(cut.NODE not in m.node_tree.nodes for m in cut.materials() if m.node_tree)
        assert plane.hide_get() and face.hide_get() and not len(face.data.polygons)
        print("KLS_CUT_OK=" + json.dumps({"renders": str(OUT)}))
    finally:
        kileido.unregister()


main()
