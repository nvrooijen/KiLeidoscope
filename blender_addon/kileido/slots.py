"""The live boards side by side (multi-board): which one the panel and its tools work on
(the picked board), and showing, hiding or removing one.

Every board follows its own KiCad, shows its own KiCad selection and stands where its pose
puts it (poses.py). What draws on one board at a time works on the picked board: the fold,
a finding's drawing and markers, the cut plane's section, the High speed column's pair
list, the Layers list and the Status section, Resync and Reset cache. Picking another
board hands them over: the board left lies flat again without a drawing (its shown finding
is kept, and drawn again once it is picked again).
"""

import bpy

from . import cosmetics, cut, findings_draw, findings_markers, fold, lighting, live, models, packages, phase
from . import state
from .objects import redraw_views
from .state import SLOTS, board, boards, using


def loaded():
    """Slots with a board, in order."""
    return [slot for slot in SLOTS if boards[slot].loaded]


def pick(slot):
    """Make board `slot` the picked one (the panel shows it, its buttons and tools work on it)."""
    old = state.picked()
    if slot == old:
        return
    if boards[old].loaded:
        with using(old):
            _release()
    state.pick(slot)
    if board.loaded:
        fold.invalidate()  # folds if flex mode is on for it
        findings_draw.refresh()  # its shown finding drawn again
        findings_markers.invalidate()
        phase.sync_list()
        if cut.enabled():
            cut.refresh()  # the section through it
    redraw_views()


def follow():
    """A board came or went: with the picked slot empty, the first board there is is picked."""
    if not boards[state.picked()].loaded:
        found = loaded()
        if found:
            pick(found[0])


def _release():
    """The tools leave this board: no fold, no finding drawn (the one shown is kept)."""
    fold.release()
    shown = board.findings_shown
    findings_draw.clear()
    board.findings_shown = shown


def hidden(slot):
    return boards[slot].hidden


def set_hidden(slot, hide):
    """A board's eye in the Boards list: the whole board hidden or shown (its link stays)."""
    with using(slot):
        board.hidden = bool(hide)
        if board.collection is not None and not board.in_snapshot:
            board.collection.hide_viewport = board.collection.hide_render = board.hidden
    lighting.fit_to_boards()
    redraw_views()


def remove(slot):
    """A board's X: its link closed (its bridge leaves soon after) and everything it showed
    deleted; the slot is free for a board to join."""
    with using(slot):
        if slot == state.picked():
            _release()
        live.links[slot].disconnect()
        models.stop_following()
        cosmetics.stop_following()
        collection = board.collection
        if collection is not None:
            try:
                _delete(collection)
            except ReferenceError:  # freed already (undo, file load)
                pass
        board.reset()
        board.pose, board.pose_of, board.hidden = None, "", False
    follow()
    lighting.fit_to_boards()
    redraw_views()


def _delete(collection):
    """The board collection, its objects and what only they used (materials, images, its
    board-space shader group)."""
    materials, images = packages._owned_data(collection)
    materials |= set(board.materials.values())
    data = {obj.data for obj in collection.all_objects if obj.data is not None}
    for obj in tuple(collection.all_objects):
        bpy.data.objects.remove(obj)
    for child in (*collection.children_recursive, collection):  # the groups, then the board
        bpy.data.collections.remove(child)
    for block in data:
        kind = bpy.data.meshes if isinstance(block, bpy.types.Mesh) else \
            bpy.data.curves if isinstance(block, bpy.types.Curve) else None
        if kind is not None and block.users == 0:
            kind.remove(block)
    for blocks, used in ((bpy.data.materials, materials), (bpy.data.images, images)):
        for block in used:
            try:
                if block.users == 0 or (block.users == 1 and block.use_fake_user):
                    blocks.remove(block)
            except ReferenceError:
                pass
    from . import poses  # poses imports objects, which imports this module's neighbours
    group = bpy.data.node_groups.get(poses.space_group_name())
    if group is not None and group.users == 0:
        bpy.data.node_groups.remove(group)
    if board.name_prefix:  # what else carries its prefix and nothing uses now (a fold's flex colours, its hole mask)
        for blocks in (bpy.data.materials, bpy.data.node_groups, bpy.data.images, bpy.data.meshes, bpy.data.curves):
            for block in [block for block in blocks if block.name.startswith(board.name_prefix + "KLS")]:
                if block.users == 0 or (block.users == 1 and block.use_fake_user):
                    blocks.remove(block)
