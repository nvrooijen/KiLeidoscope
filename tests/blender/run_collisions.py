"""Headless checks of the collision check between the live board and a view-only board.

blender --background --factory-startup --python tests/blender/run_collisions.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, collisions, packages, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def frames(name):
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = name
    for footprint in snapshot["footprints"]:  # a model not loaded yet: its 3 x 2 mm envelope shows
        x, y = footprint["pos"]
        footprint["bbox_nm"] = [x - 1_500_000, y - 1_000_000, 3_000_000, 2_000_000]
        footprint["model_paths"] = ["part.step"]
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def place(root, x=0.0, y=0.0, z=0.0, rotation_x=0.0):
    root.location = (x, y, z)
    root.rotation_euler = (rotation_x, 0.0, 0.0)
    bpy.context.view_layer.update()
    return collisions.run()


def idle_check_schedules_another():
    """One scheduled check with nothing changed, then the event loop's next depsgraph
    update: does that schedule another check? The check moves the studio softboxes
    and its boxes itself; reacting to those re-ran it every DELAY_S, re-rendering the
    viewport ~3 times a second on a static scene."""
    bpy.context.view_layer.update()
    if bpy.app.timers.is_registered(collisions._timer):
        bpy.app.timers.unregister(collisions._timer)
    collisions._timer()
    bpy.context.view_layer.update()
    return bpy.app.timers.is_registered(collisions._timer)


def main():
    kileido.register()
    try:
        apply.load_frames(frames("alpha"))
        package = Path(tempfile.mkdtemp()) / "alpha.blend"
        packages.export_board(str(package))
        apply.load_frames(frames("beta"))
        root = packages.import_board(str(package))
        thickness = state.board.thickness_m
        placed_x = root.location.x

        assert place(root, x=placed_x) == 0, "boards side by side collide"
        assert collisions.status() == "No collisions"
        assert not idle_check_schedules_another(), "an idle check schedules the next (no collisions)"

        same = place(root)  # exactly on top of the live board: boards and every part overlap
        parts = sum(1 for obj in state.board.collection.all_objects if obj.get("kls_footprint") == 1)
        assert same >= parts + 1, (same, parts)
        boxes = bpy.data.collections[collisions.COLLECTION].objects
        assert len(boxes) == same and all(not box.hide_get() for box in boxes)
        assert not idle_check_schedules_another(), "an idle check schedules the next (with boxes)"

        # Stacked just above: its board solid sits in the live board's top parts (0.4 mm
        # envelopes), not in the live board; its bottom parts reach into the live board.
        stacked = place(root, z=thickness + 0.2e-3)
        assert 0 < stacked < same, (stacked, same)

        assert place(root, z=0.02) == 0, "boards 20 mm apart collide"
        assert len(bpy.data.collections[collisions.COLLECTION].objects) == 0

        tilted = place(root, x=placed_x / 2, z=0.0, rotation_x=1.5708)  # upright through the live board
        assert tilted >= 1

        started = time.perf_counter()
        for _ in range(10):
            collisions.find()
        check_ms = (time.perf_counter() - started) * 100

        bpy.context.scene.kileido_collisions = False
        collisions.run()
        assert collisions.status() is None and not bpy.data.collections[collisions.COLLECTION].objects
        packages.remove(root[packages.ROOT_TAG])
        bpy.context.scene.kileido_collisions = True
        assert collisions.run() == 0  # nothing view-only: nothing to check
    finally:
        kileido.unregister()
    print("KLS_COLLISIONS_OK=" + json.dumps({"on_top": same, "stacked": stacked, "tilted": tilted,
                                             "check_ms": round(check_ms, 2)}))


if __name__ == "__main__":
    main()
