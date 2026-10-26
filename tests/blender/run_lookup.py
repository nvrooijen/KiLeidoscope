"""Headless checks of objects.lookup, the board's remembered name -> object index: it
answers like the collection's own lookup after objects are removed, renamed or
recreated, never with an object off the board, and a view-only board lent to `board`
gets its own.

blender --background --factory-startup --python tests/blender/run_lookup.py
"""

import json
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, objects, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def same_as_collection(name):
    found = objects.lookup(name)
    assert found == state.board.collection.all_objects.get(name), (name, found)
    return found


def main():
    kileido.register()
    try:
        snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
        apply.load_frames(b"".join(snapshot_frames(snapshot)))
        board = state.board
        outline = same_as_collection(objects.OUTLINE)
        assert outline is not None and objects.lookup(objects.OUTLINE) is outline  # remembered
        assert same_as_collection("KLS no such object") is None

        made = objects.new_owned("KLS lookup probe")  # linked through the helpers: known at once
        assert board.object_index["KLS lookup probe"] == made and same_as_collection("KLS lookup probe") == made

        made.name = "KLS lookup probe renamed"  # renamed (the Outliner): the old name is gone
        assert same_as_collection("KLS lookup probe") is None
        assert same_as_collection("KLS lookup probe renamed") == made

        bpy.data.objects.remove(made)  # removed: no stale answer, no ReferenceError
        assert same_as_collection("KLS lookup probe renamed") is None
        again = bpy.data.objects.new("KLS lookup probe renamed", None)  # same name, linked by hand
        board.collection.objects.link(again)
        assert same_as_collection("KLS lookup probe renamed") == again

        stranger = bpy.data.objects.new("KLS not on the board", None)  # in the scene, not the board
        bpy.context.scene.collection.objects.link(stranger)
        assert same_as_collection("KLS not on the board") is None

        live_index = board.object_index
        other = bpy.data.collections.new("KiLeidoscope lookup other board")
        bpy.context.scene.collection.children.link(other)
        live, board.collection = board.collection, other  # as packages._as_board lends `board`
        assert objects.lookup(objects.OUTLINE) is None and board.object_index is not live_index
        board.collection = live
        assert objects.lookup(objects.OUTLINE) is outline
        print("lookup checks passed")
    finally:
        kileido.unregister()


main()
