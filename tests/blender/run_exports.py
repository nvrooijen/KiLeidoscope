"""Headless checks of the kicad-cli export workers and what they leave behind: a failed
model export is retried, exports read the bytes their cache key was made from, a new
GLB import drops the one it replaces, re-applied overlays free the plots they replace,
and a reflection HDRI saved by another Blender install is found again. Needs kicad-cli.

blender --background --factory-startup --python tests/blender/run_exports.py
"""

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, cosmetics, kicad_cli, lighting, models  # noqa: E402
from kileido.watcher import Job  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
BOARD_FILE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_pcb"


def job(source):
    return Job(Path(source), str(source), {}, 0, threading.Event(), queue.SimpleQueue())


def check_model_retry(scratch):
    """A failed export keeps no signature (retried on the next change); a good one does,
    and it exported the hashed bytes even though the watched file changed meanwhile."""
    source = scratch / BOARD_FILE.name
    source.write_bytes(BOARD_FILE.read_bytes())
    failing = job(source)
    memory = {}
    real_run, real_lookup = kicad_cli.run, kicad_cli.lookup
    kicad_cli.lookup = lambda kind, key: None  # never served from an earlier run's cache
    try:
        kicad_cli.run = lambda arguments, timeout: subprocess.CompletedProcess(arguments, 1, "", "simulated failure")
        models._on_board_change(failing, memory)
        assert "signature" not in memory, "a failed export was remembered"
        assert "simulated failure" in failing.results.get_nowait().error

        exported = []

        def run(arguments, timeout):
            board_file = Path(arguments[-1])
            exported.append((board_file.name, board_file.read_bytes(),
                             board_file.with_suffix(".kicad_pro").is_file(), list(arguments)))
            source.write_text("(kicad_pcb rewritten meanwhile)", encoding="utf-8")
            return real_run(arguments, timeout)

        kicad_cli.run = run
        good = job(source)
        models._on_board_change(good, memory)
        result = good.results.get_nowait()
        assert result.error is None, result.error
        assert memory.get("signature"), "a good export was not remembered"
        assert {name for name, *_ in exported} == {BOARD_FILE.name}, exported
        assert all(data == BOARD_FILE.read_bytes() for _, data, *_ in exported), "kicad-cli read other bytes"
        glb = next(arguments for *_, arguments in exported if "glb" in arguments)
        assert f"KIPRJMOD={scratch}" in glb, glb
        assert Path(result.data["glb"]).is_file() and result.data["asset"]
    finally:
        kicad_cli.run, kicad_cli.lookup = real_run, real_lookup


def export_glb(path, name, size):
    """A two-mesh GLB, each with a textured material, as a stand-in for KiCad's export."""
    made = []
    bpy.ops.object.select_all(action="DESELECT")
    for index in range(2):
        bpy.ops.mesh.primitive_cube_add(size=size * (index + 1))
        obj = bpy.context.active_object
        obj.name = f"{name} {index}"
        material = bpy.data.materials.new(f"{name} material {index}")
        image = bpy.data.images.new(f"{name} image {index}", 4, 4)
        texture = material.node_tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        material.node_tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
        obj.data.materials.append(material)
        made.append((obj, obj.data, material, image))
    for obj, *_ in made:
        obj.select_set(True)
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True)
    for obj, mesh, material, image in made:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)
        bpy.data.materials.remove(material)
        bpy.data.images.remove(image)


def check_superseded_glb(scratch):
    """Only the current GLB import stays; a mesh something else still uses survives,
    and data that is not KiLeidoscope's is never touched."""
    user_mesh = bpy.data.meshes.new("User mesh")
    user = bpy.data.objects.new("User object", user_mesh)
    bpy.context.scene.collection.objects.link(user)
    first, second = scratch / "first.glb", scratch / "second.glb"
    export_glb(first, "Part one", 0.001)
    export_glb(second, "Part two", 0.002)

    models.load_glb(str(first), [], "asset-one")
    old = bpy.data.collections["KiLeidoscope models asset-one"]
    old_objects = sorted((obj for obj in old.objects if obj.type == "MESH"), key=lambda obj: obj.name)
    assert len(old_objects) == 2, [obj.name for obj in old.objects]
    (kept_mesh, kept_material), (gone_mesh, gone_material) = (
        (obj.data.name, obj.data.materials[0].name) for obj in old_objects)
    gone_images = {node.image.name for node in bpy.data.materials[gone_material].node_tree.nodes
                   if node.type == "TEX_IMAGE" and node.image is not None}
    assert gone_images, "the GLB brought no image along"
    kept = bpy.data.objects.new("User object on a model mesh", bpy.data.meshes[kept_mesh])
    bpy.context.scene.collection.objects.link(kept)

    models.load_glb(str(second), [], "asset-two")
    names = [collection.name for collection in bpy.data.collections if collection.get(models.GLB_ASSET)]
    assert names == ["KiLeidoscope models asset-two"], names
    assert kept_mesh in bpy.data.meshes and kept_material in bpy.data.materials, "data still in use was removed"
    assert gone_mesh not in bpy.data.meshes and gone_material not in bpy.data.materials
    assert not gone_images & set(bpy.data.images.keys()), gone_images
    assert user.name in bpy.data.objects and user_mesh.name in bpy.data.meshes


def check_overlay_images():
    """Overlays applied again free the plots they replace (the silkscreen included)."""
    handled = []
    on_result = cosmetics._watcher.on_result
    cosmetics._watcher.on_result = lambda result: (handled.append(result), on_result(result))[1]
    try:
        deadline = time.monotonic() + 90
        while not handled and time.monotonic() < deadline:
            cosmetics.drain()
            time.sleep(0.2)
    finally:
        cosmetics._watcher.on_result = on_result
    assert handled, cosmetics.status()
    assert any(layer == "F.SilkS" for layer, *_ in handled[0].data["plots"]), "no silkscreen plotted"
    count = len(bpy.data.images)
    for _ in range(3):
        cosmetics._on_result(handled[0])
    orphans = [image.name for image in bpy.data.images if image.users == 0]
    assert not orphans, orphans
    assert len(bpy.data.images) == count, (count, len(bpy.data.images))


def check_reflections():
    """A world saved with another install's HDRI path gets this install's copy back."""
    lighting.ensure_studio_lights()
    texture = bpy.context.scene.world.node_tree.nodes["KLS reflection image"]
    current = texture.image
    stale = bpy.data.images.new("interior.exr", 4, 4)
    stale.source = "FILE"
    stale.filepath = "/opt/blender-other/datafiles/studiolights/world/interior.exr"
    stale_name = stale.name
    texture.image = stale
    lighting.refresh_reflections()
    assert texture.image == current, texture.image
    assert stale_name not in bpy.data.images, "the other install's copy was kept"
    lighting.refresh_reflections()  # this install's copy: left alone
    assert texture.image == current


def main():
    kileido.register()
    scratch = Path(tempfile.mkdtemp())
    try:
        check_model_retry(scratch)
        snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
        apply.load_frames(b"".join(snapshot_frames(snapshot, board_path=str(BOARD_FILE))))
        check_overlay_images()
        check_superseded_glb(scratch)
        check_reflections()
    finally:
        kileido.unregister()
    print("KLS_EXPORTS_OK")


if __name__ == "__main__":
    main()
