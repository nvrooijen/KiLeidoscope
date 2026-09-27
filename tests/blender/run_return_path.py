"""Headless Blender check of the return-path marks on the synthetic split board (tests/split_board.py)."""

import math
import sys
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import kileido  # noqa: E402
import split_board  # noqa: E402
from kileido import apply, return_path, state  # noqa: E402
from kileido_bridge.protocol import encode_frame, return_path_message, snapshot_frames  # noqa: E402
from kileido_bridge.return_path import ReturnPathCheck, checked_nets  # noqa: E402

COLLECTION = "KiLeidoscope: split_board.kicad_pcb"


def marks(layer):
    return bpy.data.collections[COLLECTION].all_objects.get(f"KLS {layer} highlight return path")


def evaluated_points(obj):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), depsgraph=depsgraph)
    try:
        points = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", points)
        return points.reshape(-1, 3) @ np.asarray(obj.matrix_world)[:3, :3].T + np.asarray(obj.location), \
            len(mesh.polygons)
    finally:
        bpy.data.meshes.remove(mesh)


def main():
    assert bpy.app.background
    kileido.register()
    try:
        snapshot = split_board.board()
        issues = ReturnPathCheck().check(snapshot, checked_nets(snapshot))
        check = return_path_message(checked_nets(snapshot), issues, revision=1, elapsed_ms=3.0)
        apply.load_frames(b"".join(snapshot_frames(snapshot)) + check)
        scene = bpy.context.scene
        assert scene.kileido_return_path
        front, back = marks("F.Cu"), marks("B.Cu")
        rows = {layer: sum(max(1, len(points) - 1) for issue in issues for on, points, _ in issue.marks
                           if on == layer) for layer in ("F.Cu", "B.Cu")}
        assert len(front.data.vertices) == 2 * rows["F.Cu"] and len(back.data.vertices) == 2 * rows["B.Cu"]
        assert not front.hide_get() and not back.hide_get()
        assert front.modifiers[0].node_group.name.startswith("KLS_Tracks")
        material = state.board.materials["highlight_return_path"]
        assert material.name == "KLS Highlight return path"
        # Red glow over the outer surface of the tracks (1 um out from the copper), under
        # the bottom copper's; the via discs have faces too.
        points, faces = evaluated_points(front)
        assert faces and np.isfinite(points).all()
        assert points[:, 2].max() > state.board.heights["F.Cu"] + 1e-6 + 0.5 * return_path.LIFT_M
        points, faces = evaluated_points(back)
        assert faces and np.isfinite(points).all()
        assert points[:, 2].min() < state.board.heights["B.Cu"] - 1e-6 - 0.5 * return_path.LIFT_M
        # The split under USB_D+ sits at x = 50 mm, 0 in Blender (the board is 0..100 mm).
        split = next(issue for issue in issues if issue.item == "usb-0")
        index = issues.index(split)
        width = front.data.attributes["width"].data
        item = front.data.attributes["item"].data
        ours = [n for n in range(len(item)) if item[n].value == index]
        xs = [front.data.vertices[n].co.x for n in ours]
        assert min(xs) < -0.0002 and max(xs) > 0.0002
        assert math.isclose(width[ours[0]].value, split_board.WIDTH * 1e-9 + 2 * return_path.HALO_M, rel_tol=1e-5)
        assert return_path.summary() == f"{len(issues)} issues on 8 nets (3 ms)"

        scene.kileido_return_path = False
        assert front.hide_get() and not len(front.data.vertices) and back.hide_get()
        scene.kileido_return_path = True
        assert not front.hide_get() and len(front.data.vertices) == 2 * rows["F.Cu"]

        scene.kileido_show_F_Cu = False  # the Layers list's F.Cu eye hides its marks too
        assert front.hide_get() and not back.hide_get()
        scene.kileido_show_F_Cu = True
        assert not front.hide_get()

        # A resync snapshot drops the marks until the bridge sends the check again;
        # a check with no issues hides them.
        apply.load_frames(b"".join(snapshot_frames(snapshot, revision=2)))
        assert front.hide_get() and state.board.return_path == {}
        apply.load_frames(check)
        assert not front.hide_get()
        apply.load_frames(return_path_message(checked_nets(snapshot), (), revision=3))
        assert front.hide_get() and back.hide_get()
        assert return_path.summary().startswith("0 issues")
        apply.load_frames(encode_frame({"type": "return_path", "revision": 4, "nets": [], "layers": [],
                                        "issues": [], "error": "Return-path check failed: test"}))
        assert return_path.summary() == "Return-path check failed: test"
    finally:
        kileido.unregister()
    print("KLS_RETURN_PATH_OK")


if __name__ == "__main__":
    main()
