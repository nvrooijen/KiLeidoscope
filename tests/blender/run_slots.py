"""Headless checks of two live boards in one Blender (slots 1 and 2, multi-board): each its
own collection, objects, materials and hole mask under its own names, and an edit to one
leaves the other alone. Both are the fixture board, so every name would clash.

blender --background --factory-startup --python tests/blender/run_slots.py
"""

import json
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, holes, objects, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def frames(name, drop_tracks=0):
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = name
    if drop_tracks:
        snapshot["tracks"] = snapshot["tracks"][:-drop_tracks]
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def kls_ids():
    """Every KiLeidoscope-named object, mesh, material and image: (kind, name)."""
    found = []
    for kind in ("objects", "meshes", "materials", "images", "collections"):
        found += [(kind, block.name) for block in getattr(bpy.data, kind)
                  if "KLS" in block.name or block.name.startswith("KiLeidoscope")]
    return found


def owned_by(slot):
    with state.using(slot):
        return set(state.board.collection.all_objects)


def main():
    kileido.register()
    try:
        apply.load_frames(frames("First board.kicad_pcb"))
        single = {name for _, name in kls_ids()}
        with state.using(2):
            apply.load_frames(frames("Second board.kicad_pcb"))
        first, second = state.boards[1], state.boards[2]
        assert first.collection.name == "KiLeidoscope: First board.kicad_pcb"
        assert second.collection.name == "KiLeidoscope 2: Second board.kicad_pcb"
        assert (first.collection["kls_slot"], second.collection["kls_slot"]) == (1, 2)

        duplicates = [name for _, name in kls_ids() if name[-4:-3] == "." and name[-3:].isdigit()]
        assert not duplicates, f"names clashed between the boards: {duplicates[:8]}"
        assert {name for _, name in kls_ids()} >= single, "board 1's names changed when board 2 came"

        ones, twos = owned_by(1), owned_by(2)
        assert ones and twos and not ones & twos
        assert all(not obj.name.startswith("B2 ") for obj in ones)
        assert all(obj.name.startswith("B2 KLS ") for obj in twos if objects.role(obj.name).startswith("KLS ")), \
            [obj.name for obj in twos if not obj.name.startswith("B2 ")][:8]
        assert all(material.name.startswith("B2 KLS ") for key, material in second.materials.items()
                   if objects.role(material.name).startswith("KLS ")), "board 2 shares a material with board 1"
        assert not set(first.materials.values()) & set(second.materials.values())

        with state.using(1):
            outline_1, mask_1 = objects.lookup(objects.OUTLINE), holes.mask_image()
        with state.using(2):
            outline_2, mask_2 = objects.lookup(objects.OUTLINE), holes.mask_image()
        assert outline_1 in ones and outline_2 in twos and outline_2.name == "B2 " + objects.OUTLINE
        assert mask_1.name == "KLS holes" and mask_2.name == "B2 KLS holes"
        assert first.holes is not second.holes

        # An edit in board 1's KiCad (a track gone) changes board 1 only.
        before = {obj.name: len(obj.data.vertices) for obj in twos if obj.type == "MESH"}
        tracks_1 = sum(len(obj.data.vertices) for obj in ones if obj.get("kls_copper", ("", ""))[1] == "tracks")
        apply.load_frames(frames("First board.kicad_pcb", drop_tracks=3))
        assert sum(len(obj.data.vertices) for obj in owned_by(1)
                   if obj.get("kls_copper", ("", ""))[1] == "tracks") < tracks_1
        assert {obj.name: len(obj.data.vertices) for obj in owned_by(2) if obj.type == "MESH"} == before
        assert state.current() is first, "the slot in use leaked out of `using`"
        print("slots checks passed")
    finally:
        kileido.unregister()


main()
