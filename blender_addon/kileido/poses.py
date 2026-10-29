"""Where each board stands in the scene: its root Empty and its pose.

Every object a board owns hangs from the board's root Empty ("KLS root"), directly with an
identity parent inverse or through a footprint's frame, and the add-on places them in the
root's space: KiCad nanometres around the board's `origin_nm` (transform.xy_m), z 0 at its
bottom face. So a board moves as one and nothing it draws needs to know where it stands;
only what looks at the scene as a whole (picking, the camera, lights, collisions, the cut
plane, the 3D views) works in world space.

A board's pose is kept in KiCad's frame, not around its centre (which moves with an outline
edit or IMS mode): `T_B = Translate(t) · R` places KiCad's origin (x, -y, z up, z 0 at the
bottom face, metres) in the scene, R turning X first, then Y, then Z (Blender's XYZ Euler).
One session offset, the first board's centre when it came (never re-centred while a board
is shown), keeps a board shown alone exactly where it always was:

    root = Translate(-scene_offset) · T_B · Translate(origin)

A board that joins others keeps KiCad's X and Y and sits a gap above everything shown. The
panel shows centre-relative numbers (`shown`, `set_shown`); Flip and Turn 90° turn a board
about its current centre (`turn`).
"""

import math

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import objects
from .objects import ROOT, link_owned, lookup, owned_name
from .state import SLOTS, board, boards, each_loaded, using

ROOT_TAG = "kls_root"  # on a live board's root Empty: its slot
MOVED_M = 1e-6  # the root this far (m, or rad) from where its pose puts it: moved by hand
POSE_KEY = "kls_pose"  # on it: the pose it was placed for, translation then angles
STACK_GAP_M = 0.01  # a board that joins others: this far above the highest thing shown
SPACE = "KLS board space"  # the shader group giving a point and its normal in the root's space
_session = {"offset": None}  # scene_offset: (x, y, z) metres in KiCad's frame
OFFSET_KEY = "kls_scene_offset"  # on the scene: the session offset, for a saved file (saved.restore)


def reset_session():
    """No board shown any more: the next one to come is centred again."""
    _session["offset"] = None
    bpy.context.scene.pop(OFFSET_KEY, None)


def restore_session(scene):
    """A file opened: its session offset, if it kept one (else the next board sets it)."""
    stored = list(scene.get(OFFSET_KEY, ()))
    _session["offset"] = tuple(float(v) for v in stored) if len(stored) == 3 else None


def _set_offset(offset):
    _session["offset"] = tuple(float(v) for v in offset)
    bpy.context.scene[OFFSET_KEY] = list(_session["offset"])


def centre():
    """The board's centre in KiCad's frame (metres): its outline's middle, mid-thickness."""
    x, y = board.origin_nm
    return np.array((x * 1e-9, -y * 1e-9, board.thickness_m / 2))


# --- Rotations, in float64 ---------------------------------------------------------------------

def rotation(angles):
    """XYZ Euler angles (radians) as a 3x3 matrix: Rz · Ry · Rx, X turned first."""
    turned = np.eye(3)
    for axis, angle in zip(range(3), angles):
        turned = axis_rotation(axis, angle) @ turned
    return turned


def axis_rotation(axis, angle):
    """A turn by `angle` (radians) about world axis 0, 1 or 2."""
    c, s = math.cos(angle), math.sin(angle)
    i, j = [(1, 2), (2, 0), (0, 1)][axis]
    turned = np.eye(3)
    turned[i, i], turned[i, j], turned[j, i], turned[j, j] = c, -s, s, c
    return turned


def euler(turned):
    """The XYZ Euler angles (radians) of a rotation matrix (`rotation` undone)."""
    sin_y = -max(-1.0, min(1.0, turned[2, 0]))
    y = math.asin(sin_y)
    if math.cos(y) > 1e-9:
        x = math.atan2(turned[2, 1], turned[2, 2])
        z = math.atan2(turned[1, 0], turned[0, 0])
    else:  # gimbal lock: X and Z turn about the same axis, Z takes none of it
        x, z = math.atan2(-turned[1, 2], turned[1, 1]), 0.0
    return tuple(0.0 if abs(angle) < 1e-12 else angle for angle in (x, y, z))


# --- The pose -------------------------------------------------------------------------------

def _offset():
    if _session["offset"] is None:
        _set_offset((*centre()[:2], 0.0))
    return np.array(_session["offset"])


def pose_matrix():
    """T_B as a 4x4 float64 matrix (identity before the board has a pose)."""
    matrix = np.eye(4)
    if board.pose is not None:
        translation, angles = board.pose
        matrix[:3, :3] = rotation(angles)
        matrix[:3, 3] = translation
    return matrix


def root_matrix():
    """The root Empty's world matrix (4x4 float64): Translate(-scene_offset) · T_B · Translate(origin)."""
    matrix = pose_matrix()
    origin = centre()
    origin[2] = 0.0
    matrix[:3, 3] += matrix[:3, :3] @ origin - _offset()
    return matrix


def _key():
    return board.board_path or board.board_name


def _default_pose():
    """Where a board new to its slot goes: where an opened arrangement puts it (arrangement.py);
    else alone, where it always went (the scene centred on it); beside others, at KiCad's X
    and Y, a gap above the highest thing they show."""
    from . import arrangement  # it imports this module
    others = [slot for slot in SLOTS if slot != board.slot and boards[slot].loaded]
    entry = arrangement.wanted()
    if not others:
        reset_session()
        if entry is not None:  # the scene centred on it where the arrangement puts it
            middle = centre()
            middle[2] = 0.0
            at = rotation(entry["angles"]) @ middle + np.array(entry["translation"])
            _set_offset((at[0], at[1], 0.0))
    _offset()
    if entry is not None:
        board.hidden = bool(entry["hidden"])
        return tuple(float(v) for v in entry["translation"]), tuple(float(v) for v in entry["angles"])
    top = max((_top(boards[slot].collection) for slot in others), default=None)
    lift = 0.0 if top is None else top + STACK_GAP_M
    return (0.0, 0.0, float(lift)), (0.0, 0.0, 0.0)


def _top(collection):
    """The highest world z of what a board collection shows, or None."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    tops = []
    for obj in collection.all_objects:
        if obj.type not in ("MESH", "CURVE", "FONT") or not obj.visible_get():
            continue
        matrix = obj.matrix_world
        tops += [(matrix @ Vector(corner)).z for corner in obj.evaluated_get(depsgraph).bound_box]
    return max(tops, default=None)


def follow_board():
    """A board frame (apply.apply_board): the root made if missing and placed; a board new to
    its slot (another file, another board) gets the default place first."""
    if board.pose is None or board.pose_of != _key():
        board.pose, board.pose_of = _default_pose(), _key()
    place_root(ensure_root())


def set_pose(translation, angles):
    """T_B: where KiCad's origin goes (metres) and the turn (XYZ Euler, radians)."""
    board.pose = (tuple(float(v) for v in translation), tuple(float(v) for v in angles))
    board.pose_of = _key()
    place_root(root())


# --- The panel's numbers: about the board's centre -----------------------------------------------

def shown():
    """(offset, angles) as the panel shows them: how far the board's centre moved from where
    KiCad's frame puts it (metres), and the turn (XYZ Euler, radians)."""
    matrix = pose_matrix()
    middle = centre()
    return tuple(matrix[:3, :3] @ middle + matrix[:3, 3] - middle), tuple(board.pose[1] if board.pose else (0.0,) * 3)


def set_shown(offset=None, angles=None):
    """The panel's numbers changed: new `angles` turn the board about its centre (which stays),
    a new `offset` moves the centre."""
    matrix = pose_matrix()
    middle = centre()
    translation, turn_angles = matrix[:3, 3], tuple(board.pose[1] if board.pose else (0.0,) * 3)
    if angles is not None:
        at = matrix[:3, :3] @ middle + translation
        turn_angles = tuple(float(a) for a in angles)
        translation = at - rotation(turn_angles) @ middle
    if offset is not None:
        translation = np.asarray(offset, dtype=np.float64) + middle - rotation(turn_angles) @ middle
    set_pose(translation, turn_angles)


def turn(axis, angle):
    """Turn the board by `angle` (radians) about the world axis `axis` (0, 1, 2) through its
    centre, after the turn it has: Flip is (0, pi), Turn 90° is (2, pi / 2)."""
    matrix = pose_matrix()
    middle = centre()
    at = matrix[:3, :3] @ middle + matrix[:3, 3]
    angles = euler(axis_rotation(axis, angle) @ matrix[:3, :3])
    set_pose(at - rotation(angles) @ middle, angles)


# --- The root Empty -----------------------------------------------------------------------------

def root():
    return objects.root()


def ensure_root():
    """The board's root Empty, made if missing; what hangs from nothing hangs from it."""
    obj = lookup(ROOT)
    if obj is None:
        obj = bpy.data.objects.new(owned_name(ROOT), None)
        obj["kileido_owned"] = 1
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = 1e-4  # Blender's minimum: a millimetre board's frame, unseen
        link_owned(obj, "root")  # no such group: the board collection's top, the one place not locked
    if obj.name not in board.collection.objects:  # a session before the root could be selected
        board.collection.objects.link(obj)
        for group in board.collection.children:
            if obj.name in group.objects:
                group.objects.unlink(obj)
    obj.lock_location = obj.lock_rotation = (False, False, False)  # G and R move the board (follow_root)
    obj[ROOT_TAG] = board.slot
    board.touched.add(obj.name)
    adopt_all()
    return obj


def adopt_all():
    """Objects of the board that hang from nothing (made before the root, a file saved before
    roots) hang from the root, where they are in its space."""
    obj = lookup(ROOT)
    if obj is None:
        return
    for child in tuple(board.collection.all_objects):
        if child.parent is None and child != obj:
            objects.adopt(child, obj)


def place_root(obj):
    """Put the root where the pose says; the board-space shader group follows its turn."""
    if obj is None or board.pose is None:
        return
    matrix = root_matrix()
    target = Matrix(matrix.tolist())
    stored = [*board.pose[0], *board.pose[1]]  # what an undo step puts back with the root (`_undone`)
    if list(obj.get(POSE_KEY, ())) != stored:
        obj[POSE_KEY] = stored
    if any(abs(a - b) > MOVED_M for row, other in zip(obj.matrix_world, target) for a, b in zip(row, other)):
        obj.matrix_world = target  # only when it moved (matrix_world is float32): every object of the board follows
        _cut_moved()
    group = bpy.data.node_groups.get(owned_name(SPACE))
    if group is not None:
        _bind(group, obj, matrix)
    from . import findings_markers  # it imports modules that import this one
    findings_markers.invalidate()  # drawn where the board stands now


def follow_root(obj):
    """The root moved by hand (G, R in the viewport): the pose follows it, so the panel's
    numbers, a saved arrangement and the next frame from KiCad keep the board there."""
    if board.pose is None:
        return
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    if np.abs(matrix - root_matrix()).max() <= MOVED_M:
        return
    turned = matrix[:3, :3] / np.linalg.norm(matrix[:3, :3], axis=0)  # its turn alone
    middle = centre()
    middle[2] = 0.0
    board.pose = (tuple(float(v) for v in matrix[:3, 3] + _offset() - turned @ middle), euler(turned))
    obj[POSE_KEY] = [*board.pose[0], *board.pose[1]]
    group = bpy.data.node_groups.get(owned_name(SPACE))
    if group is not None:
        _bind(group, obj, root_matrix())
    from . import findings_markers  # it imports modules that import this one
    findings_markers.invalidate()
    _cut_moved()


def _cut_moved():
    """The board moved: the cut section, drawn through the picked board, follows it."""
    from . import cut, state  # cut imports this module
    if board.slot == state.picked():
        cut.moved()


@bpy.app.handlers.persistent
def _moved(_scene, depsgraph):
    """A live board's root moved in the viewport: its pose follows (`follow_root`)."""
    for update in depsgraph.updates:
        if not update.is_updated_transform:
            continue
        obj = getattr(update.id, "original", None)
        slot = obj.get(ROOT_TAG) if isinstance(obj, bpy.types.Object) else None
        if slot not in SLOTS or not boards[slot].loaded:
            continue
        with using(slot):
            try:
                if root() == obj:
                    follow_root(obj)
            except ReferenceError:  # freed by an undo: its next snapshot starts over
                pass


def to_root():
    """World -> the board's root space."""
    obj = root()
    return obj.matrix_world.inverted() if obj is not None else Matrix.Identity(4)


def to_world():
    """The board's root space -> world."""
    obj = root()
    return obj.matrix_world.copy() if obj is not None else Matrix.Identity(4)


def in_root(obj, world_to_root=None):
    """`obj`'s matrix in its board's root space: its own matrix if it hangs from the root
    (fresh before Blender evaluates a move), else its world matrix brought there."""
    parent = obj.parent
    if parent is not None and parent.get(ROOT_TAG) is not None:
        return obj.matrix_parent_inverse @ obj.matrix_basis
    return (world_to_root if world_to_root is not None else to_root()) @ obj.matrix_world


def set_in_root(obj, matrix, root_to_world=None):
    """Place `obj` at `matrix` in its board's root space: its world matrix, which reads back
    at once (its own matrix waits for Blender to evaluate it)."""
    obj.matrix_world = (root_to_world if root_to_world is not None else to_world()) @ matrix


# --- Shaders: a point and its normal in the root's space -------------------------------------------

def space_group_name():
    return owned_name(SPACE)


def space_group():
    """The board's "KLS board space" shader group: Position (the root's TexCoord Object) and
    Normal (the world normal turned back by the root's turn), so plots stay on the board
    wherever it stands."""
    name = owned_name(SPACE)
    group = bpy.data.node_groups.get(name)
    if group is None:
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
        group.interface.new_socket(name="Position", in_out="OUTPUT", socket_type="NodeSocketVector")
        group.interface.new_socket(name="Normal", in_out="OUTPUT", socket_type="NodeSocketVector")
        nodes, links = group.nodes, group.links
        out = nodes.new("NodeGroupOutput")
        coordinates = nodes.new("ShaderNodeTexCoord")
        coordinates.name = "KLS root"
        links.new(coordinates.outputs["Object"], out.inputs["Position"])
        normal = nodes.new("ShaderNodeNewGeometry").outputs["Normal"]
        combine = nodes.new("ShaderNodeCombineXYZ")
        for axis in "XYZ":  # (R^T n)_i = column i of R . n
            dot = nodes.new("ShaderNodeVectorMath")
            dot.name, dot.operation = f"KLS root {axis}", "DOT_PRODUCT"
            links.new(normal, dot.inputs[0])
            links.new(dot.outputs["Value"], combine.inputs[axis])
        links.new(combine.outputs["Vector"], out.inputs["Normal"])
    obj = root()
    live = obj is not None and obj.get(ROOT_TAG) is not None  # a view-only board's root has no pose: no turn
    _bind(group, obj, root_matrix() if live and board.pose is not None else np.eye(4))
    return group


def unturned(group):
    """A space group that came in with a package (packages.import_board), bound with the live
    board's turn there: the view-only board's normal is the world's, as it stands wherever
    the user puts it (its Position input still follows its own root)."""
    _bind(group, None, np.eye(4))


def _bind(group, obj, matrix):
    coordinates = group.nodes["KLS root"]
    if obj is not None and coordinates.object != obj:
        coordinates.object = obj
    for index, axis in enumerate("XYZ"):
        column = tuple(float(v) for v in matrix[:3, index])
        socket = group.nodes[f"KLS root {axis}"].inputs[1]
        if tuple(socket.default_value) != column:
            socket.default_value = column


# --- Undo ----------------------------------------------------------------------------------------

@bpy.app.handlers.persistent
def _undone(*_):
    """An undo or redo step put each root back where it stood then: the board's pose goes back
    with it, so the panel's numbers and what follows the pose agree."""
    for _slot in each_loaded():
        try:
            board.collection.name
        except ReferenceError:  # freed by the step: its next snapshot starts over (apply._begin_snapshot)
            continue
        obj = root()
        stored = list(obj.get(POSE_KEY, ())) if obj is not None else []
        if len(stored) == 6:
            board.pose = (tuple(stored[:3]), tuple(stored[3:]))
            place_root(obj)


def install():
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _undone not in handlers:
            handlers.append(_undone)
    if _moved not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_moved)


def uninstall():
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _undone in handlers:
            handlers.remove(_undone)
    if _moved in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_moved)
