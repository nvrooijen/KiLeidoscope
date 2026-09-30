"""Headless checks of the kicad-cli export workers and what they leave behind: a failed
model export is retried, one with a model file missing or unreadable is shown but not
cached, exports read the bytes their cache key was made from, a new GLB import drops
the one it replaces, re-applied overlays free the plots they replace, a reflection
HDRI saved by another Blender install is found again, and no scratch folder outlives
Blender. Needs kicad-cli.

blender --background --factory-startup --python tests/blender/run_exports.py
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, cosmetics, kicad_cli, lighting, models, watcher  # noqa: E402
from kileido.watcher import Job  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
BOARD_FILE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_pcb"


def job(source):
    return Job(Path(source), str(source), {}, 0, threading.Event(), queue.SimpleQueue())


def check_model_retry(scratch):
    """A failed export keeps no key (retried on the next change); a good one does,
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
        assert "key" not in memory, "a failed export was remembered"
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
        assert memory.get("key"), "a good export was not remembered"
        assert {name for name, *_ in exported} == {BOARD_FILE.name}, exported
        assert all(data == BOARD_FILE.read_bytes() for _, data, *_ in exported), "kicad-cli read other bytes"
        glb = next(arguments for *_, arguments in exported if "glb" in arguments)
        assert f"KIPRJMOD={scratch}" in glb, glb
        assert Path(result.data["glb"]).is_file() and result.data["asset"]
    finally:
        kicad_cli.run, kicad_cli.lookup = real_run, real_lookup


def check_missing_models_not_cached(scratch):
    """An export that could not find one model file and could not read another (a
    malformed VRML file: kicad-cli then exits 2 but still writes the GLB) is shown, not
    failed, and not cached (the files may be added or fixed before the next open), yet
    still remembered for this watch so edits do not re-export."""
    text = BOARD_FILE.read_text(encoding="utf-8")
    start = re.search(r'\(footprint\s+"', text).start()
    end = start + len(models._block(text, start)) - 1
    source = scratch / "missing" / BOARD_FILE.name
    source.parent.mkdir()
    (source.parent / "bad.wrl").write_text(  # a point with two coordinates
        "#VRML V2.0 utf8\nShape { geometry IndexedFaceSet {\n"
        "coord Coordinate { point [ 0 0 0, 1 0 0, 1 1 ] }\ncoordIndex [ 0, 1, 2, -1 ] } }\n", encoding="utf-8")
    models_text = '(model "${KLS_UNSET_TEST_3D}/missing.step") (model "${KIPRJMOD}/bad.wrl")'
    source.write_text(text[:end] + models_text + text[end:], encoding="utf-8")
    stored = []
    real_lookup, real_store = kicad_cli.lookup, kicad_cli.store
    kicad_cli.lookup = lambda kind, key: None
    kicad_cli.store = lambda kind, key, files: stored.append(key)
    try:
        exporting = job(source)
        memory = {}
        models._on_board_change(exporting, memory)
        result = exporting.results.get_nowait()
        assert result.error is None, result.error
        problems = [re.split(r"[\\/]", path)[-1] for path in result.data["problems"]]
        assert problems == ["missing.step", "bad.wrl"], result.data["problems"]
        assert models.problems_text(result.data["problems"]) == "; 2 not found or unreadable: missing.step, bad.wrl"
        assert models.problems_text(["a/1.step", "2.wrl", "a\\1.step", "3.wrl", "4.wrl"]) == \
            "; 5 not found or unreadable: 1.step, 2.wrl, 3.wrl, …"
        assert not stored, "an export with a missing model was cached"
        assert Path(result.data["glb"]).parent == Path(result.directory), "not the scratch export"
        assert memory.get("key"), "an export with a missing model is re-run on every edit"
        watcher.remove_later(result.directory)
    finally:
        kicad_cli.lookup, kicad_cli.store = real_lookup, real_store


def check_unsaved_board_follows_live_copy(scratch):
    """A board with no saved file (empty board path) still loads models from the bridge's
    live copy; only with neither copy nor saved board is the status the save-it hint."""
    live = scratch / "live" / BOARD_FILE.name
    live.parent.mkdir()
    live.write_bytes(BOARD_FILE.read_bytes())
    watcher_ = models._watcher
    real_change = watcher_.on_change
    watcher_.on_change = lambda job, memory: None  # only the start-up is checked, no export
    try:
        models.follow_board("", {"path": str(live), "live": True})
        assert watcher_._stop is not None, "an unsaved board with a live copy was not followed"
        assert watcher_.status == watcher_.starting({"live": True}), watcher_.status
        models.follow_board("", {})
        assert watcher_._stop is None and watcher_.status == watcher_.unavailable, watcher_.status
        assert "save the board" in watcher_.status
    finally:
        models.stop_following()
        watcher_.on_change = real_change


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


def check_offset_models():
    """Several model roots of one footprint (KiCad names them U5, U5.001) bind to it even
    when every one sits far from its origin, as CM5 MINIMA's module connectors do; more
    roots than the footprint declares models do not."""
    from kileido.state import board
    target = next(obj for obj in board.collection.all_objects if obj.get("kls_footprint") == 1)
    target["kls_model_paths"] = ["connector.step", "connector.step"]
    root = bpy.data.objects.new("Offset models root", None)
    bpy.context.scene.collection.objects.link(root)
    made = [root]
    for index, offset in enumerate((0.021, 0.040, 0.060)):
        mesh = bpy.data.meshes.new(f"Offset model {index}")
        mesh.vertices.add(1)
        ref = bpy.data.objects.new(target["kls_reference"] + (f".{index:03d}" if index else ""), None)
        part = bpy.data.objects.new(f"Offset model part {index}", mesh)
        for obj in (ref, part):
            bpy.context.scene.collection.objects.link(obj)
        ref.parent, part.parent = root, ref
        ref.location = target.matrix_world.translation + Vector((offset, 0, 0))
        made += [ref, part]
    bpy.context.view_layer.update()
    extra = made[-2:]  # the third root: one more than the two declared models
    extra[0].parent = None
    assert len(models._matches(root, [])) == 2, "offset model roots were not bound"
    extra[0].parent = root
    assert models._matches(root, []) == [], "three roots bound to a footprint declaring two models"
    for obj in made:
        bpy.data.objects.remove(obj)


def check_scratch_cleanup():
    """Result folders nobody handled (here: every check's) go when Python exits, and a
    crashed Blender's old ones on the next start; a fresh one is another Blender's."""
    left = set(watcher._scratch)
    assert left, "the checks' exports registered no scratch folders"
    watcher._remove_unhandled()  # what atexit runs when Blender quits
    assert not any(path.exists() for path in left), "a scratch folder outlived Blender"
    root = Path(tempfile.gettempdir())
    stale, fresh = (Path(tempfile.mkdtemp(prefix=watcher.SCRATCH_PREFIXES[0], dir=root)) for _ in range(2))
    (stale / "models.glb").write_bytes(b"old")
    old = time.time() - watcher.STALE_S - 60
    os.utime(stale, (old, old))
    try:
        swept = watcher.sweep_stale()
        assert stale in swept and fresh not in swept, swept
        deadline = time.monotonic() + 10
        while stale.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not stale.exists(), "a stale scratch folder was not removed"
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(stale, ignore_errors=True)


def main():
    kileido.register()
    scratch = Path(tempfile.mkdtemp())
    try:
        check_model_retry(scratch)
        check_missing_models_not_cached(scratch)
        check_unsaved_board_follows_live_copy(scratch)
        snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
        apply.load_frames(b"".join(snapshot_frames(snapshot, board_path=str(BOARD_FILE))))
        check_overlay_images()
        check_offset_models()
        check_superseded_glb(scratch)
        check_reflections()
        check_scratch_cleanup()
    finally:
        kileido.unregister()
        shutil.rmtree(scratch, ignore_errors=True)
    print("KLS_EXPORTS_OK")


if __name__ == "__main__":
    main()
