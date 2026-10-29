"""Headless checks of the picked board and the Boards list with two live boards (multi-board
M4): a row per board, picking one hands the tools over (the fold), the Position numbers move
the picked board about its centre, a click meets the board above and picks it, the eye hides
a whole board through a resync, a setting all boards share reaches both, an undo puts the
pose back with the root, and the X removes a board and frees its slot.

Board 1 is the plain fixture, board 2 the flex test's (a bend).

blender --background --factory-startup --python tests/blender/run_picking.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kileido  # noqa: E402
from kileido import apply, boardprops, columns, fold, layers, objects, packages, pick, poses, slots, state  # noqa: E402
import run_flex  # noqa: E402  (its frames: the fixture with a bend)
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402


class Layout:
    """Records what a panel draws: (kind, name or text)."""

    def __init__(self, drawn=None):
        self.drawn = [] if drawn is None else drawn
        self.alignment, self.active, self.enabled, self.alert = "EXPAND", True, True, False

    def _child(self, *args, **kwargs):
        return type(self)(self.drawn)

    box = split = row = column = _child

    def label(self, text="", icon="NONE", icon_value=0):
        self.drawn.append(("label", text))

    def prop(self, data, name, **kwargs):
        self.drawn.append(("prop", name))

    def operator(self, idname, text="", **kwargs):
        self.drawn.append(("operator", idname))
        return type("Properties", (), {})()

    def template_list(self, *args, **kwargs):
        self.drawn.append(("list", args[0]))

    def separator(self, *args, **kwargs):
        pass

    def panel(self, idname, default_closed=False):
        """A fold, drawn open."""
        self.drawn.append(("panel", idname))
        return type(self)(self.drawn), type(self)(self.drawn)

    def menu(self, idname, **kwargs):
        self.drawn.append(("menu", idname))

    def props_enum(self, data, name):
        self.drawn.append(("enum", name))


def frames(name):
    """The plain fixture under `name`."""
    snapshot = json.loads(run_flex.FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = name
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def second_frames():
    """The flex test's board under the name "Second board.kicad_pcb"."""
    snapshot = json.loads(run_flex.FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = "Second board.kicad_pcb"
    fixture, run_flex.FIXTURE = run_flex.FIXTURE, Path(tempfile.mkdtemp()) / "second.kicad_dump.json"
    try:
        run_flex.FIXTURE.write_text(json.dumps(snapshot), encoding="utf-8")
        return run_flex.frames()
    finally:
        run_flex.FIXTURE = fixture


def drawn_boards():
    layout = Layout()
    kileido._Boards(layout).draw(bpy.context)
    return layout.drawn


def check_rows(scene):
    assert state.picked() == 1 and state.board.slot == 1
    rows = kileido._board_rows_wanted()
    assert rows == [("ALL", "All boards"), ("S1", "First board.kicad_pcb"), ("S2", "Second board.kicad_pcb")], rows
    drawn_boards()
    kileido._sync_board_rows()  # the timer the panel asked for
    assert [(row.value, row.label) for row in scene.kileido_board_rows] == rows
    assert scene.kileido_layers_board == "S1" and scene.kileido_board_row == 1
    drawn = drawn_boards()
    assert ("list", kileido.KILEIDO_UL_boards.bl_idname) in drawn, drawn
    assert ("operator", "kileido.export_board") in drawn and ("operator", "kileido.import_board") in drawn, drawn
    assert ("prop", "kileido_board_offset") not in drawn and ("menu", "KILEIDO_MT_boards") not in drawn, drawn
    assert columns.tab_state("BOARDS") == "on"
    column = Layout()  # the Multi-board column: the picked board and its place, the collision check, the file, other KiCads
    kileido.COLUMNS["BOARDS"](column, scene)
    assert ("label", "Multi-board") in column.drawn and ("label", "First board") in column.drawn, column.drawn
    assert any(kind == "label" and " mm · " in text and text.endswith(" layers") for kind, text in column.drawn), column.drawn
    assert [entry for entry in column.drawn if entry[0] in ("operator", "enum", "prop")] == [
        ("prop", "kileido_board_offset"), ("prop", "kileido_board_rotation"),
        ("operator", "kileido.board_turn"), ("operator", "kileido.board_turn"), ("operator", "kileido.board_turn"),
        ("prop", "kileido_collisions"),
        ("operator", "kileido.save_arrangement"), ("operator", "kileido.open_arrangement"),
        ("prop", "kileido_join_choice")], column.drawn


def check_pick_by_row(scene):
    """A click on board 2's row picks it: `board` is board 2 outside `using`."""
    scene.kileido_board_row = 2
    assert state.picked() == 2 and state.board.slot == 2 and scene.kileido_layers_board == "S2"
    with state.using(1):
        assert state.board.slot == 1
    assert state.board.slot == 2, "the pick is lost after a `using` block"


def check_position(scene):
    """The Position numbers: board 2's centre moved, then flipped about it; board 1 stays."""
    first = state.boards[1].pose
    before = poses.shown()[0]
    scene.kileido_board_offset = (10.0, 0.0, 5.0)
    assert np.abs(np.array(poses.shown()[0]) - (0.01, 0.0, 0.005)).max() < 1e-12, poses.shown()
    assert np.abs(np.array(scene.kileido_board_offset) - (10.0, 0.0, 5.0)).max() < 1e-6
    assert before != poses.shown()[0] and state.boards[1].pose == first
    assert bpy.ops.kileido.board_turn(turn="FLIP") == {"FINISHED"}
    assert abs(scene.kileido_board_rotation[0] - math.pi) < 1e-6, tuple(scene.kileido_board_rotation)
    assert np.abs(np.array(scene.kileido_board_offset) - (10.0, 0.0, 5.0)).max() < 1e-6, "flipped about its centre"
    scene.kileido_board_rotation = (0.0, 0.0, math.pi / 2)
    assert np.abs(np.array(scene.kileido_board_offset) - (10.0, 0.0, 5.0)).max() < 1e-6, "turned about its centre"
    assert state.boards[1].pose == first
    assert bpy.ops.kileido.board_turn(turn="RESET") == {"FINISHED"}  # back where its KiCad coordinates put it
    assert np.abs(np.array(scene.kileido_board_offset)).max() < 1e-6, tuple(scene.kileido_board_offset)
    assert np.abs(np.array(scene.kileido_board_rotation)).max() < 1e-9, tuple(scene.kileido_board_rotation)
    scene.kileido_board_offset = (10.0, 0.0, 5.0)  # stacked over board 1 again, as the checks below expect
    scene.kileido_board_rotation = (0.0, 0.0, math.pi / 2)
    assert state.boards[1].pose == first


def check_select_and_move(scene):
    """The arrow on board 2's row selects its root alone; moved and turned there (as G and R
    do), the board's pose follows, and a resync keeps it where it was put."""
    assert bpy.ops.kileido.select_live_board(slot=2) == {"FINISHED"}
    root = objects.root()
    assert root.select_get() and bpy.context.view_layer.objects.active == root
    assert [obj.name for obj in bpy.context.selected_objects] == [root.name]
    centre = np.array(poses.to_world() @ Vector((0.0, 0.0, 0.0)))
    turned = scene.kileido_board_rotation[2]
    root.matrix_world = Matrix.Translation((0.004, -0.003, 0.002)) @ root.matrix_world  # G
    root.rotation_euler.z += math.radians(15)  # R about the root's own place: the centre
    bpy.context.view_layer.update()  # the depsgraph handler follows the root
    moved = poses.pose_matrix()
    assert np.abs(np.array(poses.to_world() @ Vector((0.0, 0.0, 0.0))) - centre - (0.004, -0.003, 0.002)).max() \
        < 1e-6
    assert abs(scene.kileido_board_rotation[2] - turned - math.radians(15)) < 1e-6, tuple(scene.kileido_board_rotation)
    placed = np.array(root.matrix_world)
    apply.load_frames(second_frames())  # a resync
    assert np.abs(np.array(root.matrix_world) - placed).max() < 1e-6 and np.abs(poses.pose_matrix() - moved).max() \
        < 1e-12
    root.select_set(False)


def check_undo():
    """An undo step puts the root back with the pose it carries: the pose follows."""
    root = objects.root()
    kept = list(root[poses.POSE_KEY])
    root[poses.POSE_KEY] = [0.0, 0.0, 0.05, 0.0, 0.0, 0.0]  # as an undo step would bring it back
    poses._undone()
    assert state.boards[2].pose == ((0.0, 0.0, 0.05), (0.0, 0.0, 0.0))
    assert np.abs(np.array(root.matrix_world.translation) - poses.root_matrix()[:3, 3]).max() < 1e-7
    poses.set_pose(kept[:3], kept[3:])


def check_click():
    """A ray from above meets board 2 (stacked over board 1) and picks it; asked for board 1
    only, it passes board 2 and meets board 1."""
    slots.pick(1)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    with state.using(1):
        middle = poses.to_world() @ Vector((0.0, 0.0, 0.0))
    above = Vector((middle.x, middle.y, 1.0))
    found = pick.board_at(bpy.context.scene, depsgraph, above, Vector((0, 0, -1)))
    assert found is not None and found[0] == 2, found
    kileido._pick_board(bpy.context.scene, found[0])
    assert state.picked() == 2 and bpy.context.scene.kileido_layers_board == "S2"
    below = pick.board_at(bpy.context.scene, depsgraph, above, Vector((0, 0, -1)), only=1)
    assert below is not None and below[0] == 1, below


def check_fold_handover(scene):
    """The fold works on the picked board: picking board 1 leaves board 2 flat, picking board 2
    folds it again."""
    with state.using(2):
        flat = {obj.name: np.array(poses.in_root(obj)) for obj in fold._parts()}
    assert boardprops.name("kileido_flex") == "kileido_b2_flex"  # the picked board's own, as the panel shows it
    boardprops.put(scene, "kileido_flex", True)
    boardprops.put(scene, "kileido_fold", 1.0)
    assert scene.kileido_b2_flex and not scene.kileido_flex and scene.kileido_b2_fold == 1.0
    assert fold._folded_collection() is not None and fold._grips()
    slots.pick(1)
    with state.using(2):
        assert fold._folded_collection() is None and not fold._grips(), "board 2 still folded"
        moved = [name for name, matrix in flat.items()
                 if np.abs(np.array(poses.in_root(state.board.collection.all_objects[name])) - matrix).max() > 1e-6]
        assert not moved, moved
    assert fold._folded_collection() is None, "board 1 has no flex: nothing folds"
    slots.pick(2)
    fold._settled()  # the timer `pick` started
    assert fold._folded_collection() is not None, "board 2 folded again"
    boardprops.put(scene, "kileido_fold", 0.0)
    boardprops.put(scene, "kileido_flex", False)
    fold._settled()


def check_eye():
    """Board 2's eye hides the whole board, through a resync too; board 1 stays."""
    assert bpy.ops.kileido.live_board(slot=2, action="TOGGLE") == {"FINISHED"}
    second = state.boards[2].collection
    assert second.hide_viewport and second.hide_render and not state.boards[1].collection.hide_viewport
    with state.using(2):
        apply.load_frames(second_frames())
    assert second.hide_viewport, "a resync showed the hidden board"
    assert bpy.ops.kileido.live_board(slot=2, action="TOGGLE") == {"FINISHED"}
    assert not second.hide_viewport


def check_join_while_hidden():
    """A third board joins while board 2 is hidden: it comes in above board 1 (a hidden board
    shows nothing to stack on). And a slot's hidden flag does not outlive its board: the next
    board to take the slot (as after a file load that restored nothing there) starts shown."""
    slots.set_hidden(2, True)
    with state.using(3):
        apply.load_frames(frames("Third board.kicad_pcb"))
    assert state.boards[3].loaded and not state.boards[3].collection.hide_viewport
    with state.using(1):
        top = poses._top(state.board.collection)
    assert state.boards[3].pose[0][2] >= top + poses.STACK_GAP_M - 1e-9, (state.boards[3].pose, top)
    slots.set_hidden(2, False)
    slots.set_hidden(3, True)
    with state.using(3):
        gone = state.board.collection
        state.board.reset()  # as a file load does (_file_loaded), the file holding nothing for slot 3
        slots._delete(gone)
    assert state.boards[3].hidden and not state.boards[3].loaded
    with state.using(3):
        apply.load_frames(frames("Fourth board.kicad_pcb"))
    assert not state.boards[3].hidden and not state.boards[3].collection.hide_viewport, "the old flag hid the new board"
    slots.remove(3)
    assert state.picked() == 2


def top_copper_hidden(slot):
    with state.using(slot):
        copper = [obj for obj in state.board.collection.all_objects
                  if (obj.get("kls_copper") or ("",))[0] == "F.Cu" and len(obj.data.vertices)]
        assert copper, slot
        return all(obj.hide_get() for obj in copper)


def check_shared(scene):
    """A setting both boards share reaches both, whichever is picked; a layer's eye is each
    board's own, the All boards eye switches both."""
    scene.kileido_color_mode = "EDITOR"
    assert state.boards[1].color_mode == state.boards[2].color_mode == "EDITOR"
    scene.kileido_color_mode = "FAB"
    assert state.picked() == 2 and layers.property_name("F.Cu") == "kileido_b2_show_F_Cu"
    setattr(scene, layers.property_name("F.Cu"), False)
    assert top_copper_hidden(2) and not top_copper_hidden(1) and scene.kileido_show_F_Cu
    setattr(scene, layers.property_name("F.Cu"), True)
    assert not top_copper_hidden(2)
    packages.set_row_everywhere("F.Cu", False)
    assert top_copper_hidden(1) and top_copper_hidden(2) and not packages.shown_everywhere("F.Cu")
    packages.set_row_everywhere("F.Cu", True)
    assert not top_copper_hidden(1) and not top_copper_hidden(2)


def check_remove():
    """Board 2's X: its link closed, nothing of it left, board 1 picked; the slot takes a
    board again under clean names."""
    slots.remove(2)
    assert not state.boards[2].loaded and state.boards[2].pose is None and state.picked() == 1
    left = [block.name for kind in ("objects", "meshes", "materials", "images", "collections", "node_groups")
            for block in getattr(bpy.data, kind) if block.name.startswith(("B2 ", "KiLeidoscope 2"))]
    assert not left, left[:8]
    assert kileido._board_rows_wanted() == [("ALL", "All boards"), ("S1", "First board.kicad_pcb")]
    assert columns.tab_state("BOARDS") == "unavailable", "one board: nothing to place or check"
    with state.using(2):
        apply.load_frames(frames("Third board.kicad_pcb"))
    assert state.boards[2].loaded and state.picked() == 1
    duplicates = [obj.name for obj in bpy.data.objects  # parts are named by reference: J1 and J1.001 on two boards
                  if "KLS" in obj.name and obj.name[-4:-3] == "." and obj.name[-3:].isdigit()]
    assert not duplicates, duplicates[:8]


def main():
    kileido.register()
    try:
        scene = bpy.context.scene
        apply.load_frames(frames("First board.kicad_pcb"))
        with state.using(2):
            apply.load_frames(second_frames())
        check_rows(scene)
        check_pick_by_row(scene)
        check_position(scene)
        check_select_and_move(scene)
        check_undo()
        check_click()
        check_fold_handover(scene)
        check_eye()
        check_join_while_hidden()
        check_shared(scene)
        check_remove()
        print("picking checks passed")
    finally:
        kileido.unregister()


main()
