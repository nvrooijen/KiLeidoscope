"""Headless Blender check that undo does not crash or desync the add-on.

Applies the fixture board, takes an undo step, changes the board (cut plane, a second full
snapshot, a model-less update), undoes, then touches the scene again the way the live timer
and the panel do. Each stage prints before it runs, so a crash shows where it died.
"""

import json
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, cut, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402
from kileido_bridge.protocol import snapshot_frames  # noqa: E402


def stage(text):
    print("STAGE " + text, flush=True)


def settle():
    bpy.context.view_layer.update()
    bpy.context.evaluated_depsgraph_get().update()


def main():
    assert bpy.app.background
    kileido.register()
    fixture = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
    frames = b"".join(snapshot_frames(snapshot_from_jsonable(json.loads(fixture.read_text(encoding="utf-8")))))
    try:
        stage("load board")
        apply.load_frames(frames)
        settle()
        bpy.ops.ed.undo_push(message="board loaded")

        stage("cut plane on")
        bpy.context.scene.kileido_cut = True
        settle()
        bpy.ops.ed.undo_push(message="cut on")

        stage("full snapshot again (a resync)")
        apply.load_frames(frames)
        settle()
        bpy.ops.ed.undo_push(message="resynced")

        for step in range(3):
            stage(f"undo {step + 1}")
            bpy.ops.ed.undo()
            settle()
            stage(f"after undo {step + 1}: python state vs Blender data")
            group = bpy.data.node_groups.get(cut.GROUP)
            print("  cut property", bpy.context.scene.kileido_cut, "group", group is not None,
                  "plane", bpy.data.objects.get(cut.PLANE) is not None, flush=True)
            for label, probe in (("collection", lambda: state.board.collection.name),
                                 ("materials", lambda: [m.name for m in state.board.materials.values()]),
                                 ("silk images", lambda: [v["image"].name for v in state.board.silk.values()])):
                try:
                    print("  ", label, "ok", probe(), flush=True)
                except ReferenceError as exc:
                    print("  ", label, "DEAD:", exc, flush=True)
            stage("the live timer's recovery: reset, then a full snapshot")
            try:
                apply.load_frames(frames)
            except ReferenceError:
                state.board.fail("Stale")  # as live.tick does
            state.board.reset()  # fail() resets when the collection is gone
            apply.load_frames(frames)
            settle()
            # Recovered: the board is whole again, and still cut open with its section drawn.
            assert len(state.board.collection.all_objects) > 10 and state.board.collection.name
            assert all(material.name for material in state.board.materials.values())
            if bpy.context.scene.kileido_cut:
                face = bpy.data.objects[cut.FACE]
                assert len(face.data.polygons) and not face.hide_get()
            print("   collections:", sorted(c.name for c in bpy.data.collections),
                  "| images:", len(bpy.data.images), "| materials:", len(bpy.data.materials),
                  "| objects:", len(bpy.data.objects), flush=True)
            bpy.ops.ed.undo_push(message=f"recovered {step}")

        print("KLS_UNDO_OK", flush=True)
    finally:
        kileido.unregister()


main()
