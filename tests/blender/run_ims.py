"""Headless checks of IMS mode: the metal base in 3D and in the cut, and an IMS board
exported, then imported view-only beside a 4-layer live board without IMS.

blender --background --factory-startup --python tests/blender/run_ims.py
"""

import json
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, cut, holes, materials, packages, section, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
UM = 1e-6


def frames(name, ims=False, pour=False):
    """The fixture (four copper layers) under another name, or cut down to two layers as an
    IMS board is drawn in KiCad: one dielectric, the board's whole 1.54 mm thickness. `pour`:
    B.Cu's tracks replaced by one pour over the whole outline, as KiCad users draw the base."""
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = name
    if pour:
        snapshot["tracks"] = [track for track in snapshot["tracks"] if track["layer"] != "B.Cu"]
        snapshot["pads"] = [pad for pad in snapshot["pads"] if pad.get("drill") or "B.Cu" not in pad["polygons"]]
        snapshot["zones"].append(dict(snapshot["zones"][0], id="ims-pour", layer="B.Cu",
                                      polygons=snapshot["outline"]["polygons"]))
    if ims:
        stack = snapshot["stackup"]["layers"]
        core = dict(stack[1], thickness_nm=sum(layer["thickness_nm"] for layer in stack[1:-1]))
        snapshot["stackup"]["layers"] = [stack[0], core, stack[-1]]
        snapshot["zones"] = [zone for zone in snapshot["zones"] if zone["layer"] in ("F.Cu", "B.Cu")]
        for pad in snapshot["pads"]:  # through-hole pads list every copper layer
            pad["polygons"] = {layer: shape for layer, shape in pad["polygons"].items() if not layer.startswith("In")}
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def z_range_um(obj):
    """The evaluated object's world z extent in um, rounded."""
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    zs = [(evaluated.matrix_world @ Vector(corner)).z for corner in evaluated.bound_box]
    return round(min(zs) / UM), round(max(zs) / UM)


def bottom_objects(collection):
    return [obj for obj in collection.all_objects
            if obj.get("kls_cosmetic_layer") in ("B.Mask", "B.SilkS") or " B.Cu " in f"{obj.name} "]


def fresh_session():
    """A new Blender session: empty file, add-on state as after a restart."""
    kileido.unregister()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    state.board.__init__()
    holes._sources = {key: value[:0] for key, value in holes._sources.items()}
    holes._bounds = holes._digest = holes._drawn = None
    kileido.register()


def check_live(scene):
    board = state.board
    assert board.ims is not None, "IMS mode on a 2-layer board"
    assert round(board.thickness_m / UM) == 1540, board.thickness_m  # KiCad's: base 1405 + epoxy 100 + F.Cu 35
    objects = board.collection.all_objects
    assert z_range_um(objects["KLS IMS base"]) == (0, 1403), z_range_um(objects["KLS IMS base"])
    assert z_range_um(objects["KLS outline"]) == (1407, 1503), z_range_um(objects["KLS outline"])
    bottom = bottom_objects(board.collection)
    assert bottom and all(obj.hide_get() for obj in bottom), [(o.name, o.hide_get()) for o in bottom]
    assert materials.core_color() == section.IMS_DIELECTRIC  # what a see-through mask shows: epoxy, not FR4
    vias = len(objects["KLS vias"].data.vertices)
    plated = sum(1 for obj in objects if obj.get("kls_drill") and obj.get("kls_plated") and len(obj.data.vertices))
    shorts = [line for line in board.ims_warnings if "would short" in line]
    assert len(shorts) == bool(vias) + bool(plated) and (vias or plated), (vias, plated, board.ims_warnings)
    scene.kileido_cut = True
    colors = {rect[4] for rect in cut.rectangles()[0]}
    assert section.ALUMINUM in colors and section.IMS_DIELECTRIC in colors, colors
    assert not colors & section.WOVEN, colors  # no FR4 left in the cut
    scene.kileido_ims_metal = "CU"
    colors = {rect[4] for rect in cut.rectangles()[0]}
    assert section.ALUMINUM not in colors and section.COPPER in colors, colors
    scene.kileido_ims_metal = "AL"
    scene.kileido_cut = False


def main():
    kileido.register()
    directory = Path(tempfile.mkdtemp())
    package = directory / "ims.blend"
    try:
        # Session 1: an IMS board live, exported; then IMS mode off brings B.Cu back.
        scene = bpy.context.scene
        scene.kileido_ims = True
        if bpy.app.timers.is_registered(kileido._ims_rebuild):  # no bridge or dump to reload here
            bpy.app.timers.unregister(kileido._ims_rebuild)
        apply.load_frames(frames("ims", ims=True))
        check_live(scene)
        assert any(line.startswith("B.Cu") for line in state.board.ims_warnings), state.board.ims_warnings  # its tracks
        apply.load_frames(frames("ims", ims=True, pour=True))  # one full pour stands for the base: no B.Cu line
        assert state.board.ims is not None and not any("B.Cu" in line for line in state.board.ims_warnings), \
            state.board.ims_warnings
        assert any("would short" in line for line in state.board.ims_warnings), state.board.ims_warnings
        packages.export_board(str(package))
        scene.kileido_ims = False
        bpy.app.timers.unregister(kileido._ims_rebuild)
        apply.load_frames(frames("ims", ims=True))
        board = state.board
        assert board.ims is None and "KLS IMS base" not in board.collection.all_objects
        assert board.ims_warnings == []
        assert materials.core_color() != section.IMS_DIELECTRIC
        assert round(board.thickness_m / UM) == 1540, board.thickness_m
        assert not any(obj.hide_get() for obj in bottom_objects(board.collection))

        # Session 2: a 4-layer live board, IMS off; the IMS board imported beside it keeps its base.
        fresh_session()
        scene = bpy.context.scene
        apply.load_frames(frames("four"))
        assert state.board.ims is None
        root = packages.import_board(str(package))
        index = root[packages.ROOT_TAG]
        collection = packages.collection_of(index)
        assert packages.ims_of(index)["metal"] == "AL"
        base = next(obj for obj in collection.all_objects if obj.name.endswith("KLS IMS base"))
        outline = packages._outline(collection)
        assert z_range_um(base) == (0, 1403) and z_range_um(outline) == (1407, 1503)
        assert all(obj.hide_get() for obj in bottom_objects(collection))
        for copper_3d in (False, True):  # the view-only board's own base, not the live board's stackup
            scene.kileido_copper_3d = copper_3d
            assert z_range_um(base) == (0, 1403), z_range_um(base)
            assert z_range_um(outline)[0] == 1407, (copper_3d, z_range_um(outline))
        assert state.board.ims is None and "KLS IMS base" not in state.board.collection.all_objects  # as it was
        print("KLS_IMS_OK", flush=True)
    finally:
        kileido.unregister()


main()
