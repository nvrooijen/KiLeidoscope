"""Headless checks of where boards stand (poses.py, multi-board M3): every object a board owns
hangs from its root Empty; a board alone stands where it always stood; a second one comes in
at KiCad's X and Y, a gap above the first; and a board moved and turned keeps everything on
it in place through a full resync, a fold, a highlight, a click, its solder and its section.

Board 1 is the solder test's fixture (stencil apertures), board 2 the flex test's (a bend).

blender --background --factory-startup --python tests/blender/run_placement.py
"""

import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kileido  # noqa: E402
from kileido import (apply, boardprops, components, cut, fold, foldmath, highlight, models, objects, packages,  # noqa: E402
                     pick, poses, slots, solder, state)
from kileido.objects import read_attribute, read_coordinates  # noqa: E402
import run_flex  # noqa: E402  (its frames: the fixture with a bend)
import run_solder  # noqa: E402  (its frames and stand-in parts)

J3 = run_solder.J3
TRACK = run_flex.TRACK  # B.Cu at x = 30 mm


def owned(slot):
    with state.using(slot):
        return list(state.board.collection.all_objects)


def check_hangs_from_root(slot):
    """Every object of the board hangs from its root: directly with an identity parent
    inverse, or through a footprint's frame."""
    with state.using(slot):
        root = objects.root()
        assert root is not None and root[poses.ROOT_TAG] == slot, root
        assert root.name == objects.owned_name(objects.ROOT) and root.parent is None
        for obj in state.board.collection.all_objects:
            if obj == root:
                continue
            top = obj
            while top.parent is not None and top.parent != root:
                top = top.parent
            assert top.parent == root, (obj.name, top.name)
            if obj.parent == root:
                assert obj.matrix_parent_inverse == Matrix.Identity(4), obj.name
        return root


def in_root_all(slot):
    with state.using(slot):
        to_root = poses.to_root()
        return {obj.name: np.array(poses.in_root(obj, to_root)) for obj in state.board.collection.all_objects}


def same_places(before, after, what):
    moved = [name for name, matrix in before.items() if name in after and np.abs(after[name] - matrix).max() > 1e-6]
    assert not moved, f"{what}: moved on the board: {moved[:6]}"


def world_centre(slot):
    with state.using(slot):
        return np.array(poses.to_world() @ Vector((0.0, 0.0, state.board.thickness_m / 2)))


def check_alone():
    """Board 1 alone: its root at the world origin, so everything stands where it always did."""
    apply.load_frames(run_solder.frames())
    root = check_hangs_from_root(1)
    assert root.matrix_world == Matrix.Identity(4), root.matrix_world
    assert state.boards[1].pose == ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)), state.boards[1].pose
    bpy.context.view_layer.update()
    with state.using(1):
        outline = objects.lookup(objects.OUTLINE)
        assert outline.matrix_world == outline.matrix_basis
    return {obj.name: obj.matrix_world.copy() for obj in owned(1)}


def check_joined(first_world):
    """Board 2 comes in at KiCad's X and Y, a gap above board 1; board 1 does not move."""
    with state.using(2):
        apply.load_frames(run_flex.frames())
    check_hangs_from_root(2)
    with state.using(1):
        top = poses._top(state.board.collection)
    translation, angles = state.boards[2].pose
    assert translation[:2] == (0.0, 0.0) and angles == (0.0, 0.0, 0.0), state.boards[2].pose
    assert abs(translation[2] - (top + poses.STACK_GAP_M)) < 1e-9, (translation, top)
    bpy.context.view_layer.update()
    with state.using(2):
        outline = objects.lookup(objects.OUTLINE)
        corners = np.array([tuple(outline.matrix_world @ Vector(c)) for c in outline.bound_box])
    with state.using(1):
        own = objects.lookup(objects.OUTLINE)
        under = np.array([tuple(own.matrix_world @ Vector(c)) for c in own.bound_box])
    assert np.abs(corners[:, :2].min(axis=0) - under[:, :2].min(axis=0)).max() < 1e-7, "same KiCad X and Y"
    assert corners[:, 2].min() > under[:, 2].max(), "above board 1"
    assert {obj.name: obj.matrix_world for obj in owned(1)} == first_world, "board 1 moved"


def check_moved():
    """Board 2 flipped, turned and moved: centre-relative numbers, its centre where they say,
    nothing moved on it by a full resync, its plots and normals in its root's space."""
    with state.using(2):
        at = poses.in_root(components.find(components.FRAME, J3)).translation  # a stand-in model on J3: boxed
        body = run_solder.part("J3 body", J3, "top", (at.x - 1e-3, at.y - 0.5e-3, at.z),
                               (at.x + 1e-3, at.y + 0.5e-3, at.z + 1e-3), run_solder.material("body", run_solder.BLACK))
        bpy.context.view_layer.update()  # boxes read the parts' world matrices
        highlight.apply_selection({"selected": [], "pair": [], "footprints": [J3]})
        box = next(obj for obj in state.board.collection.all_objects if obj.get("kls_highlight_box") == 1
                   and obj.visible_get())
        before = in_root_all(2)
        centre = world_centre(2)
        poses.turn(0, math.pi)  # Flip
        poses.turn(2, math.pi / 2)  # Turn 90°
        assert np.abs(world_centre(2) - centre).max() < 1e-7, "turned about its centre"  # float32 in Blender
        offset, angles = poses.shown()
        assert np.abs(np.array(offset) - (centre - kicad_centre(2))).max() < 1e-7, (offset, centre)
        turned = poses.rotation(angles)
        assert np.abs(turned - poses.axis_rotation(2, math.pi / 2) @ poses.axis_rotation(0, math.pi)).max() < 1e-12
        poses.set_shown(offset=(0.02, -0.01, 0.03))
        assert np.abs(np.array(poses.shown()[0]) - (0.02, -0.01, 0.03)).max() < 1e-12
        assert np.abs(world_centre(2) - kicad_centre(2) - (0.02, -0.01, 0.03)).max() < 1e-7
        root = objects.root()
        placed = root.matrix_world.copy()
        same_places(before, in_root_all(2), "turning the root")
        apply.load_frames(run_flex.frames())  # a full resync
        assert objects.root() == root, "the resync made another root"
        assert np.abs(np.array(root.matrix_world) - np.array(placed)).max() < 1e-7, "the resync moved the root"
        assert state.boards[2].pose[1] == angles
        same_places(before, in_root_all(2), "a resync")
        objects.set_visible(body, True)  # as models bind again after a snapshot
        bpy.context.view_layer.update()
        highlight.apply_selection({"selected": [], "pair": [], "footprints": [J3]})
        assert np.abs(np.array(poses.in_root(box)) - before[box.name]).max() < 1e-6, "the highlight box moved"
        highlight.apply_selection({})
        # Plots and normals: the board's own shader group, bound to its root and its turn.
        group = bpy.data.node_groups["B2 KLS board space"]
        assert group.nodes["KLS root"].object == root
        for index, axis in enumerate("XYZ"):
            column = np.array(group.nodes[f"KLS root {axis}"].inputs[1].default_value)
            assert np.abs(column - np.array(root.matrix_world.to_3x3().col[index])).max() < 1e-6, axis
        users = [material for material in state.board.materials.values() if material.node_tree is not None
                 and getattr(material.node_tree.nodes.get("KLS board space"), "node_tree", None) == group]
        assert users, "no board-2 material samples its plots in the board's space"


def kicad_centre(slot):
    """Where KiCad's frame puts the board's centre in the scene."""
    with state.using(slot):
        return poses.centre() - np.array(poses._session["offset"])


def check_pick():
    """Board 2 lies flipped over board 1: a click from above meets its B.Cu track."""
    with state.using(2):
        local = foldmath.world_xy([(30_000_000, 12_500_000)], state.board.origin_nm)[0]
        bottom = Vector((float(local[0]), float(local[1]), 0.0))
        above = poses.to_world() @ bottom + Vector((0.0, 0.0, 0.05))
        bpy.context.view_layer.update()
        item = pick.item_at(bpy.context.scene, bpy.context.evaluated_depsgraph_get(), above, Vector((0, 0, -1)))
        assert item == TRACK, item


def check_fold(scene):
    """The fold on the moved board: copies in its root's space, as foldmath folds them; parts
    fold and come back to where they were on the board."""
    with state.using(2):
        flat_places = in_root_all(2)
        boardprops.put(scene, "kileido_flex", True)  # board 2's own
        boardprops.put(scene, "kileido_fold", 1.0)
        root = objects.root()
        board = state.board
        plan = foldmath.plan(board.flex, board.origin_nm, board.heights, board.layer_thickness)
        angles = foldmath.angles_at(plan, 1.0)
        folded = fold._folded_collection()
        assert folded is not None and len(folded.objects) >= 3
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for obj in folded.objects:
            assert obj.parent == root, obj.name
            mesh = obj.data
            flat = np.empty(len(mesh.vertices) * 3, np.float32)
            mesh.attributes["kls_flat"].data.foreach_get("vector", flat)
            flat = flat.reshape(-1, 3).astype(np.float64)
            mask = read_attribute(mesh, "kls_fold_mask", np.float32).astype(np.int64)
            zone = read_attribute(mesh, "kls_fold_zone", np.float32).astype(np.int64)
            expected = foldmath.fold(flat, mask, zone, angles, plan)
            moved = read_coordinates(obj.evaluated_get(depsgraph).data).astype(np.float64)
            assert np.abs(moved - expected).max() < 1e-6, obj.name
        parts = [obj for obj in fold._parts() if obj.get("kls_flat_matrix") is not None]
        assert parts, "parts folded with their regions"
        for obj in parts:
            flat_matrix = np.reshape(obj["kls_flat_matrix"], (4, 4))
            assert np.abs(flat_matrix - flat_places[obj.name]).max() < 1e-6, obj.name
        assert any(np.abs(np.array(poses.in_root(obj)) - flat_places[obj.name]).max() > 1e-4 for obj in parts)
        for grip in fold._grips():
            assert grip.parent.parent == root, grip.name
        boardprops.put(scene, "kileido_fold", 0.0)
        same_places(flat_places, in_root_all(2), "unfolding")
        boardprops.put(scene, "kileido_flex", False)


def check_models():
    """A model binds to its footprint on the moved board where KiCad's GLB puts it on the
    board, and binds there again with the board turned once more."""
    with state.using(2):
        glb = bpy.data.objects.new("GLB root", None)
        bpy.context.scene.collection.objects.link(glb)
        mesh = bpy.data.meshes.new("J3 model")
        mesh.from_pydata([(-5e-4, -5e-4, 0), (5e-4, -5e-4, 0), (5e-4, 5e-4, 0), (-5e-4, 5e-4, 0)], [], [(0, 1, 2, 3)])
        source = bpy.data.objects.new("J3", mesh)  # KiCad names a model's GLB node by its reference
        bpy.context.scene.collection.objects.link(source)
        source.parent = glb
        source.location = (0.031, -0.024, 0.0016)  # near J3 in the GLB's frame
        bpy.context.view_layer.update()
        x, y = state.board.origin_nm
        wanted = (Matrix.Translation((0.0, 0.0, models._seat_shift("top"))) @
                  Matrix.Translation((-x * 1e-9, y * 1e-9, 0.0)) @ source.matrix_world)
        placed = []
        for asset in ("a" * 64, "b" * 64):
            assert models.bind_root(glb, asset)["matched"] == 1
            bpy.context.view_layer.update()
            (name,) = state.board.model_objects_by_fp[J3]
            placed.append(np.array(poses.in_root(objects.lookup(name))))
            poses.turn(2, math.pi / 2)
        assert np.abs(placed[0] - np.array(wanted)).max() < 1e-6, (placed[0], wanted)
        assert np.abs(placed[1] - placed[0]).max() < 1e-6, "the model bound elsewhere on the turned board"
        for obj in (source, glb):
            bpy.data.objects.remove(obj)


def check_solder(scene):
    """Board 1 moved and turned: its solder is the same, and nothing is built again."""
    tin = run_solder.material("stand-in tin", run_solder.PIN_01)
    with state.using(1):
        scene.kileido_show_solder = True
        scene.kileido_solder_reflowed = True
        top = objects.lookup("KLS F.Paste solder")
        reflowed = objects.lookup(solder.NAME.format(layer="F.Paste"))
        pad = read_coordinates(top.data)[:, :2].astype(np.float64)
        low, high = pad.min(axis=0), pad.max(axis=0)
        seat = top.location.z - 3e-6
        middle = (low[0] + high[0]) / 2
        run_solder.part("J3 end", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat),
                        (middle, high[1] + 0.1e-3, seat + 0.3e-3), tin)
        bpy.context.view_layer.update()
        solder.build(top, reflowed)
        assert solder.last_build()["wetted"] == 1, solder.last_build()
        before = read_coordinates(reflowed.data).copy()
        poses.set_shown(offset=(-0.01, 0.005, 0.0), angles=(0.0, 0.0, math.pi / 4))
        bpy.context.view_layer.update()
        solder.build(top, reflowed)
        stats = solder.last_build()
        assert stats["computed"] == 0 and stats["revisited"] == 0 and stats["wetted"] == 1, stats
        assert np.array_equal(read_coordinates(reflowed.data), before)
        scene.kileido_solder_reflowed = False


def check_section(scene):
    """The cut through board 2's middle, before and after it moves: the same section."""
    with state.using(2):
        scene.kileido_cut = True
        cut.place("Y", centered=True)
        first = cut.rectangles(scene)
        assert first is not None and first[0], "no section"
        poses.set_shown(offset=(0.05, 0.02, -0.01))
        cut.place("Y", centered=True)
        second = cut.rectangles(scene)
        assert len(first[0]) == len(second[0])
        assert np.abs(np.array([r[:4] for r in first[0]]) - np.array([r[:4] for r in second[0]])).max() < 1e-6
        face = bpy.data.objects[cut.FACE]
        off = np.abs(np.array(face.matrix_world) - np.array(objects.root().matrix_world)).max()
        assert off < 1e-6, "the face drawn where board 2 stands"
    slots.pick(2)  # the section is the picked board's
    with state.using(2):
        poses.set_shown(offset=(0.05, 0.02, 0.02))  # the board moves under the plane, which stays
        cut._moved_soon()  # the timer the move started
        off = np.abs(np.array(face.matrix_world) - np.array(objects.root().matrix_world)).max()
        assert off < 1e-6, "the face stayed where board 2 was"
    slots.pick(1)
    scene.kileido_cut = False


def main():
    kileido.register()
    try:
        scene = bpy.context.scene
        first_world = check_alone()
        check_joined(first_world)
        check_moved()
        check_pick()
        check_fold(scene)
        check_section(scene)
        check_models()
        check_solder(scene)
        assert state.current() is state.boards[1], "the slot in use leaked out of `using`"
        print("placement checks passed")
    finally:
        kileido.unregister()


main()
