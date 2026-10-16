"""Headless Blender check: a saved scene reopens with its hole and board mask, not see-through."""

import json
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, holes  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402
from kileido_bridge.protocol import snapshot_frames  # noqa: E402


def pixels():
    image = bpy.data.images[holes.IMAGE]
    found = np.empty(len(image.pixels), np.float32)
    image.pixels.foreach_get(found)
    return found.reshape(-1, 4)


def main():
    assert bpy.app.background, "run with Blender --background"
    kileido.register()
    snapshot = json.loads((ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json")
                          .read_text(encoding="utf-8"))
    apply.load_frames(b"".join(snapshot_frames(snapshot_from_jsonable(snapshot))))
    drawn = pixels()
    assert drawn[:, 3].mean() < 0.5 and drawn[:, 2].max() == 1.0  # few holes; a board area
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "saved.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        assert bpy.data.images[holes.IMAGE].packed_file is not None
        bpy.ops.wm.open_mainfile(filepath=path)
        reopened = pixels()
        assert reopened.shape == drawn.shape and np.abs(reopened - drawn).max() < 1 / 255 + 1e-6
        # Redrawn after the save: packed again on the next one, not the stale copy.
        image = bpy.data.images[holes.IMAGE]
        changed = reopened.copy()
        changed[:, 2] = 0.0
        image.pixels.foreach_set(changed.ravel())
        image.update()
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        assert pixels()[:, 2].max() == 0.0
    print("run_save: ok")


main()
