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
from kileido import apply, cut, focus, holes, packages, section, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

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
    else:
        assert np.abs(got - np.array(wanted)).max() < TOLERANCE, (label, got, wanted)


def main():
    assert bpy.app.background, "run with Blender --background"
    kileido.register()
    scene = bpy.context.scene
    try:
        apply.load_frames(b"".join(snapshot_frames(snapshot_from_jsonable(layered_snapshot()))))
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
        assert not face.hide_get() and len(face.data.polygons) > 10
        materials = [m for m in cut.materials() if m.node_tree]
        assert materials and all(cut.NODE in m.node_tree.nodes for m in materials)
        focus.add_to(board.materials["board"])
        output = next(n for n in board.materials["board"].node_tree.nodes if n.type == "OUTPUT_MATERIAL")
        last = output.inputs["Surface"].links[0].from_node
        assert last.name == cut.NODE and last.inputs[0].links[0].from_node.name == focus.NODE
        assert cut.upright(scene)

        scene.view_settings.view_transform = "Standard"  # rendered colours are the section's own sRGB
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 8
        scene.cycles.use_denoising = False
        scene.kileido_via_plug = "RESIN"
        assert len(holes._sources["vias"]) == 0  # a plugged through via is closed from above

        # Head-on: the stackup across the board.
        camera(scene, ortho_scale=0.044)
        front = render(scene, "front")
        for x_mm, z_um, wanted, label in (
                (-15, 500, section.PREPREG, "bottom prepreg"), (-15, 1000, section.CORE, "core"),
                (-15, 1400, section.PREPREG, "top prepreg"), (21, 800, None, "beyond the board edge")):
            expect(scene, front, (x_mm * MM, 0, z_um * UM), wanted, label)

        # Zoomed on the blind via: its walls, plug, lands and the In1 pour at their thickness.
        camera(scene, ortho_scale=0.0006, location=(-0.010, -0.2, 1400 * UM), size=(1400, 700))
        via = render(scene, "blind_via")
        for dx_um, z_um, wanted, label in (
                (0, 1400, section.RESIN, "plug"), (162, 1400, section.COPPER, "barrel wall"),
                (250, 1400, section.PREPREG, "laminate beside the barrel"),
                (-250, 1290, section.COPPER, "In1 copper"), (250, 1522, section.COPPER, "F.Cu land")):
            expect(scene, via, (-0.010 + dx_um * UM, 0, z_um * UM), wanted, label)
        scene.kileido_via_plug = "NONE"
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

        # Off: stages gone, plane and face hidden, nothing drawn.
        scene.kileido_cut = False
        assert all(cut.NODE not in m.node_tree.nodes for m in cut.materials() if m.node_tree)
        assert plane.hide_get() and face.hide_get() and not len(face.data.polygons)
        print("KLS_CUT_OK=" + json.dumps({"renders": str(OUT)}))
    finally:
        kileido.unregister()


main()
