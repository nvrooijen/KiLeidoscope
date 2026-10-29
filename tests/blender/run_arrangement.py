"""Headless checks of the files that keep boards (multi-board M5): an arrangement (.usda)
saved with where each board stands is valid USD that places each board's KiCad frame as the
pose says, opening it places the boards shown and each that joins later, and a saved .blend
reopens with its boards back in their slots as saved copies that KiCad's Open in Blender
takes over in place.

blender --background --factory-startup --python tests/blender/run_arrangement.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, arrangement, join, objects, poses, saved, slots, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
FOLDER = Path(tempfile.mkdtemp())
MAIN, DAUGHTER = FOLDER / "Main" / "Main.kicad_pcb", FOLDER / "Daughter" / "Daughter.kicad_pcb"


def frames(path):
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = path.name
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot), board_path=str(path)))


def load(slot, path):
    with state.using(slot):
        apply.load_frames(frames(path))


def pose(slot):
    return state.boards[slot].pose


def close(a, b, tolerance=1e-9):
    return np.abs(np.array(a, dtype=np.float64) - np.array(b, dtype=np.float64)).max() <= tolerance


def same_pose(a, b, tolerance=1e-9):
    return close(a[0], b[0], tolerance) and close(a[1], b[1], tolerance)


def check_save(file):
    """Saved: valid USD, millimetres and Z up, each board's frame placed as its pose says,
    its KiCad file relative to the arrangement."""
    with state.using(2):
        poses.set_shown(offset=(0.02, -0.005, 0.0), angles=(math.pi, 0.0, math.radians(30)))
    slots.set_hidden(2, True)
    assert arrangement.save(file) == 2
    text = file.read_text(encoding="utf-8")
    assert "metersPerUnit = 0.001" in text and 'upAxis = "Z"' in text, text[:300]
    assert "@Main/Main.kicad_pcb@" in text and "@Daughter/Daughter.kicad_pcb@" in text, text
    from pxr import Usd, UsdGeom  # Blender's own USD reads it
    stage = Usd.Stage.Open(str(file))
    assert UsdGeom.GetStageMetersPerUnit(stage) == 0.001 and UsdGeom.GetStageUpAxis(stage) == "Z"
    prims = {prim.GetAttribute("kileidoscope:name").Get(): prim for prim in stage.Traverse()
             if prim.GetAttribute("kileidoscope:board")}
    assert set(prims) == {"Main.kicad_pcb", "Daughter.kicad_pcb"}, prims
    for slot, name in ((1, "Main.kicad_pcb"), (2, "Daughter.kicad_pcb")):
        matrix = np.array(UsdGeom.Xformable(prims[name]).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T
        matrix[:3, 3] *= 1e-3  # millimetres
        with state.using(slot):
            assert close(matrix, poses.pose_matrix(), 1e-9), (slot, matrix, poses.pose_matrix())
    assert prims["Daughter.kicad_pcb"].GetAttribute("kileidoscope:hidden").Get() is True
    entries = arrangement.read(file)
    assert [Path(entry["path"]) for entry in entries] == [MAIN, DAUGHTER], entries
    assert same_pose((entries[1]["translation"], entries[1]["angles"]), pose(2))
    assert arrangement._read_text(file)[0][1][4].round(9).tolist() == np.array(
        [*poses_matrix_rows(2)]).round(9).tolist(), "the reader without USD differs"


def poses_matrix_rows(slot):
    with state.using(slot):
        return poses.pose_matrix()


def check_open(file):
    """Opened: the boards shown are put back where the file says; one that joins later too."""
    kept = pose(2)
    with state.using(2):
        poses.set_shown(offset=(0.0, 0.0, 0.05), angles=(0.0, 0.0, 0.0))
    slots.set_hidden(2, False)
    assert arrangement.load(file) == (2, 0)
    assert same_pose(pose(2), kept) and state.boards[2].hidden and arrangement.waiting() == []
    slots.remove(2)
    assert arrangement.waiting() == ["Daughter.kicad_pcb"]
    saved_over = file.with_name("boards saved over.usda")  # saved while Daughter waits: it stays, where it was
    assert arrangement.save(saved_over) == 2
    entries = arrangement.read(saved_over)
    assert [entry["name"] for entry in entries] == ["Main.kicad_pcb", "Daughter.kicad_pcb"], entries
    assert same_pose((entries[1]["translation"], entries[1]["angles"]), kept) and entries[1]["hidden"]
    assert arrangement.waiting() == ["Daughter.kicad_pcb"], "the save forgot the waiting board"
    load(2, DAUGHTER)  # Open in Blender in its KiCad: placed as the file says, not stacked
    assert same_pose(pose(2), kept) and state.boards[2].collection.hide_viewport
    assert arrangement.waiting() == []
    slots.set_hidden(2, False)


def check_open_first(file):
    """A session that opens the file before any board: the first to come is centred where the
    file puts it, and the next keeps their places relative to each other."""
    relative = np.linalg.inv(poses_matrix_rows(1)) @ poses_matrix_rows(2)
    slots.remove(1)
    slots.remove(2)
    assert not slots.loaded()
    arrangement.load(file)
    load(2, DAUGHTER)
    with state.using(2):
        assert close(poses.root_matrix()[:2, 3], (0.0, 0.0), 1e-12), "the first board is not centred"
    load(1, MAIN)
    assert close(np.linalg.inv(poses_matrix_rows(1)) @ poses_matrix_rows(2), relative, 1e-9)


def check_blend():
    """A saved .blend reopens with both boards in their slots as saved copies; Open in Blender
    in a board's KiCad takes it over in place."""
    with state.using(2):
        poses.set_shown(offset=(0.01, 0.0, 0.02), angles=(0.0, 0.0, math.radians(45)))
    poses_before = {slot: pose(slot) for slot in (1, 2)}
    roots = {}
    for slot in (1, 2):
        with state.using(slot):
            roots[slot] = np.array(objects.root().matrix_world)
    path = str(FOLDER / "two boards.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    for slot, name, file in ((1, "Main.kicad_pcb", MAIN), (2, "Daughter.kicad_pcb", DAUGHTER)):
        restored = state.boards[slot]
        assert restored.loaded and restored.saved_copy and restored.status == saved.SAVED_STATUS, slot
        assert restored.board_name == name and Path(restored.board_path) == file
        assert same_pose(restored.pose, poses_before[slot]), (slot, restored.pose, poses_before[slot])
        assert restored.materials and restored.stackup and restored.heights, slot
    assert [value for value, _ in kileido._board_rows_wanted()] == ["ALL", "S1", "S2"]
    slot, reason = join.choose_slot({"board_path": str(DAUGHTER), "board_name": "Daughter.kicad_pcb"})
    assert (slot, reason) == (2, "already shown"), (slot, reason)
    load(2, DAUGHTER)  # the takeover's first snapshot
    with state.using(2):
        assert np.abs(np.array(objects.root().matrix_world) - roots[2]).max() < 1e-6, "moved by the takeover"
    assert not state.boards[2].saved_copy and same_pose(pose(2), poses_before[2])
    duplicates = [obj.name for obj in bpy.data.objects
                  if "KLS" in obj.name and obj.name[-4:-3] == "." and obj.name[-3:].isdigit()]
    assert not duplicates, duplicates[:8]


def main():
    kileido.register()
    try:
        load(1, MAIN)
        load(2, DAUGHTER)
        file = FOLDER / "boards.usda"
        check_save(file)
        check_open(file)
        check_open_first(file)
        check_blend()
        print("arrangement checks passed")
    finally:
        kileido.unregister()


main()
