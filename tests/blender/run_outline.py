"""Headless checks of a malformed board outline: the warning, the red outline and X-ray.

blender --background --factory-startup --python tests/blender/run_outline.py
"""

import json
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, focus, packages, state  # noqa: E402
from kileido_bridge.geometry import outline_warning  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def frames(broken):
    """The fixture, or the same board with its outer edge left open (one side missing)."""
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if broken:
        outer = snapshot["outline"]["polygons"][0][0]
        snapshot["outline"]["strokes"] = [outer[:-1] + [outer[-1]]]  # the ring without its closing side
        snapshot["outline"]["problems"] = [outer[0], outer[-1]]
        snapshot["outline"]["polygons"] = snapshot["outline"]["polygons"][1:]  # only the cutout closes
        snapshot["warnings"] = [outline_warning("open Edge.Cuts chain left out", tuple(outer[-1]))]
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def main():
    kileido.register()
    try:
        scene = bpy.context.scene
        scene.kileido_focus = False  # a fault ticks X-ray mode itself
        apply.load_frames(frames(broken=True))
        assert scene.kileido_focus
        red = state.board.collection.all_objects[apply.OUTLINE_PROBLEM]
        assert state.board.outline_problem and not red.hide_get() and len(red.data.edges) >= 3
        modifier = next(m for m in red.modifiers if m.type == "NODES")
        assert state.board.materials["highlight_outline"] in modifier.values()
        assert red.location.z < 0 and "KLS_Tracks" in modifier.node_group.name  # a wall through the board
        warnings = packages.outline_warnings()
        assert len(warnings) == 1 and warnings[0].startswith(packages.OUTLINE_PROBLEM)
        assert focus._active, "X-ray mode did not fade the rest"
        assert "KLS focus" not in state.board.materials["highlight_outline"].node_tree.nodes  # stays vivid

        apply.load_frames(frames(broken=False))  # fixed in KiCad
        assert not state.board.outline_problem and red.hide_get() and not len(red.data.edges)
        assert not packages.outline_warnings() and not focus._active
        assert not scene.kileido_focus, "X-ray mode stayed ticked after the fix"

        scene.kileido_focus = True  # ticked by the user: a fault and its fix leave it on
        apply.load_frames(frames(broken=True))
        apply.load_frames(frames(broken=False))
        assert scene.kileido_focus
    finally:
        kileido.unregister()
    print("KLS_OUTLINE_OK")


if __name__ == "__main__":
    main()
