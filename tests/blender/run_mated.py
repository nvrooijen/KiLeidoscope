"""Headless checks of collisions between live boards (multi-board): two live boards are
checked against each other, each overlap is named by its parts, a pair marked Mated (a
connector in its mate) is left out with each part's reach into the other board, the mark is
kept in the scene and in a saved arrangement, and one board alone checks nothing.

blender --background --factory-startup --python tests/blender/run_mated.py
"""

import json
import sys
import tempfile
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, arrangement, collisions, poses, slots, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
FOLDER = Path(tempfile.mkdtemp())
MAIN, DAUGHTER = FOLDER / "Main.kicad_pcb", FOLDER / "Daughter.kicad_pcb"


def frames(path):
    """The fixture under `path`, every part with a 3 x 2 mm envelope (no model loaded)."""
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = path.name
    for footprint in snapshot["footprints"]:
        x, y = footprint["pos"]
        footprint["bbox_nm"] = [x - 1_500_000, y - 1_000_000, 3_000_000, 2_000_000]
        footprint["model_paths"] = ["part.step"]
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot), board_path=str(path)))


def place_daughter(z):
    with state.using(2):
        poses.set_pose((0.0, 0.0, z), (0.0, 0.0, 0.0))
    bpy.context.view_layer.update()
    return collisions.run()


def texts():
    return [hit["text"] for hit in collisions.hits()]


def main():
    kileido.register()
    try:
        with state.using(1):
            apply.load_frames(frames(MAIN))
        with state.using(2):
            apply.load_frames(frames(DAUGHTER))
        main_key, daughter_key = (collisions.board_key(str(path), path.name) for path in (MAIN, DAUGHTER))
        assert [key for key, _, _ in collisions.boards()] == [main_key, daughter_key]

        assert place_daughter(0.02) == 0, "boards 20 mm apart collide"
        on_top = place_daughter(0.0)  # the same place: the boards and every part
        assert on_top > 0 and "Main board ↔ Daughter board" in texts(), texts()
        refs = sorted({hit["pair"][1] for hit in collisions.hits() if hit["pair"]})
        assert refs and "J3 (Main) ↔ J3 (Daughter)" in texts(), texts()

        # J3 on each board marked Mated: the pair and each J3 in the other board go.
        collisions.set_mated(main_key, "J3", daughter_key, "J3")
        mated = collisions.run()
        assert mated < on_top, (mated, on_top)
        excused = {"J3 (Main) ↔ J3 (Daughter)", "J3 (Main) ↔ Daughter board", "J3 (Daughter) ↔ Main board"}
        assert not excused & set(texts()), texts()
        assert collisions.mated_shown() == [(((daughter_key, "J3"), (main_key, "J3")) if daughter_key < main_key
                                             else ((main_key, "J3"), (daughter_key, "J3")),
                                             collisions.mated_shown()[0][1])]
        assert "J3 (Main)" in collisions.mated_shown()[0][1] and "J3 (Daughter)" in collisions.mated_shown()[0][1]

        # Kept in a saved arrangement, as relationships between the boards' prims.
        file = FOLDER / "boards.usda"
        arrangement.save(file)
        from pxr import Usd
        stage = Usd.Stage.Open(str(file))
        mate = stage.GetPrimAtPath("/Boards/Mates/Mate_1")
        assert mate and {str(mate.GetRelationship(name).GetTargets()[0]) for name in
                         ("kileidoscope:first", "kileidoscope:second")} == {"/Boards/Main", "/Boards/Daughter"}
        assert {mate.GetAttribute(name).Get() for name in
                ("kileidoscope:firstReference", "kileidoscope:secondReference")} == {"J3"}
        assert arrangement._read_text(file)[1] == arrangement._read_pxr(file)[1], "the reader without USD differs"
        bpy.context.scene.pop(collisions.MATED_KEY)
        assert collisions.run() == on_top
        arrangement.load(file)
        assert collisions.run() == mated, "the arrangement's mark is not back"

        # The mark taken off: the pair collides again.
        collisions.set_mated(main_key, "J3", daughter_key, "J3", False)
        assert collisions.run() == on_top and collisions.mated_shown() == []

        slots.remove(2)
        assert collisions.run() == 0 and collisions.status() is None  # one board: nothing to check
        print("mated checks passed")
    finally:
        kileido.unregister()


main()
