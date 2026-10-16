"""A hole finding cut open: showing a hole-clearance finding from the DRC column puts
the cut plane (cut.py) upright through both hole centres and turns the 3D views onto
the cut face, so both barrels, their plating and the gap read through every layer. A
via size finding (diameter, annular ring, drill) cuts through its one via, squarely
towards the view, so the ring and the drill read in section.

The user's own cut (on or off, plane, Flip, Section face, Cut light) and view
directions are kept in the scene (`KEY`, JSON) before the first automatic cut and put
back when the finding goes: hidden, another finding shown, the list cleared, a file
loaded. A hole finding after another keeps the first one's saved state. A cut or view
the user changed while it was shown is theirs: it stays as they left it.
"""

import json
import math

import bpy
from mathutils import Quaternion, Vector

from . import cut, transform
from .objects import shown_views, view3d_spaces
from .state import board

HOLE_CHECKS = {"drc.hole_clearance", "drc.hole_to_hole"}  # two holes: cut through both
ONE_HOLE_CHECKS = {"drc.via_diameter", "drc.annular_width", "drc.drill_out_of_range",
                   "drc.microvia_drill_out_of_range"}  # one via: cut through it, facing the view
KEY = "kls_finding_cut"
TILT = math.radians(12)  # the views look at the cut face this far from above
SAME = 1e-6
_PROPERTIES = (("on", "kileido_cut"), ("flip", "kileido_cut_flip"), ("face", "kileido_cut_face"),
               ("light", "kileido_cut_light"))


def hole_ends(finding):
    """The two hole centres of a hole finding (Blender XY, its first arrow, distance or clearance),
    the one centre twice for a via size finding (its label's anchor), or None for any other."""
    check = (finding or {}).get("check")
    if check not in HOLE_CHECKS and check not in ONE_HOLE_CHECKS:
        return None
    draws = finding.get("draws", ())
    draw = next((draw for draw in draws
                 if draw.get("tool") in ("arrow", "distance", "clearance") and len(draw.get("points") or ()) >= 2), None)
    if draw is not None:  # two holes, or a dimension across one via (KiCad's size checks): the cut holds both ends
        points = draw["points"][:2]
    elif check in ONE_HOLE_CHECKS:  # only a label at the via (a hand-written finding): through its centre
        draw = next((draw for draw in draws if draw.get("tool") == "label" and len(draw.get("points") or ()) >= 1), None)
        if draw is None:
            return None
        points = draw["points"][:1] * 2
    else:
        return None
    a, b = (Vector((float(x), float(y))) for x, y in transform.xy_m([point[:2] for point in points], board.origin_nm))
    return a, b


# --- State ----------------------------------------------------------------------------------

def _cut_state(scene):
    plane = bpy.data.objects.get(cut.PLANE)
    state = {name: bool(getattr(scene, prop, False)) for name, prop in _PROPERTIES}
    state["plane"] = None if plane is None else {
        "location": list(plane.location), "rotation": list(plane.rotation_euler),
        "quaternion": list(plane.rotation_quaternion), "mode": plane.rotation_mode}
    return state


def _views():
    return [space for space in view3d_spaces() if getattr(space, "region_3d", None) is not None]


def _view_state_of(space):
    return {"rotation": list(space.region_3d.view_rotation), "perspective": space.region_3d.view_perspective}


def _view_state():
    return [_view_state_of(space) for space in _views()]


def _same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= SAME
    return a == b


def _set_cut(scene, state):
    """Put the cut as `state` says: plane first, then the switches (each redraws what it must)."""
    plane = bpy.data.objects.get(cut.PLANE)
    want = state["plane"]
    if want is not None:
        plane = plane or cut.ensure_plane()
        plane.rotation_mode = want["mode"]
        plane.location = want["location"]
        plane.rotation_euler = want["rotation"]
        plane.rotation_quaternion = want["quaternion"]
        if board.board_name:  # else ensure_plane would put it back across the middle
            plane["kls_board"] = board.board_name
        cut.fit(plane)
        bpy.context.view_layer.update()
    for name, prop in _PROPERTIES:
        if bool(getattr(scene, prop)) != state[name]:
            setattr(scene, prop, state[name])
    if want is None and plane is not None:  # there was no plane: the next Cut plane starts across the middle
        bpy.data.objects.remove(plane)
    cut.push(scene)


def _saved(scene):
    try:
        return json.loads(scene.get(KEY) or "")
    except ValueError:
        return None


# --- Showing and putting back ---------------------------------------------------------------

def _facing():
    """Horizontal unit vector from the board towards the user's eye in the current view
    (the view's bottom edge when it looks straight down), or None."""
    view = next((view for _, view in shown_views()), None)
    if view is None:
        view = next((space.region_3d for space in _views()), None)
    if view is None:
        return None
    rotation = view.view_rotation
    for towards in (rotation @ Vector((0.0, 0.0, 1.0)), rotation @ Vector((0.0, -1.0, 0.0))):
        flat = Vector((towards.x, towards.y))
        if flat.length > 1e-3:
            return flat.normalized()
    return None


def show(finding):
    """A finding was shown: cut through its holes, or put the user's cut back for any other."""
    scene = bpy.context.scene
    ends = hole_ends(finding) if board.collection is not None else None
    if ends is None:
        restore(scene)
        return
    a, b = ends
    along = b - a
    facing = _facing()
    if along.length > 1e-9:
        normal = Vector((-along.y, along.x)).normalized()
        if facing is not None and normal.dot(facing) < 0:
            normal.negate()  # the half facing the view is the one removed
    else:  # one hole: the face squarely towards the view
        normal = facing.copy() if facing is not None else Vector((0.0, -1.0))
    saved = _saved(scene)
    current_cut, current_views = _cut_state(scene), _view_state()
    if saved is None:
        saved = {"cut": current_cut, "views": current_views}
    else:  # one hole after another: what the user changed since the last cut is theirs
        if not _same(current_cut, saved["applied_cut"]):
            saved["cut"] = current_cut
        if not _same(current_views, saved["applied_views"]):
            saved["views"] = current_views
    middle = (a + b) / 2
    plane = {"location": [middle.x, middle.y, (board.thickness_m or 0.0016) / 2],
             "rotation": [math.pi / 2, 0.0, math.atan2(normal.x, -normal.y)],  # local z (the arrow) along `normal`
             "quaternion": [1.0, 0.0, 0.0, 0.0], "mode": "XYZ"}
    _set_cut(scene, {"on": True, "flip": False, "face": True, "light": current_cut["light"], "plane": plane})
    _frame(a, b, normal)
    saved["applied_cut"], saved["applied_views"] = _cut_state(scene), _view_state()
    saved["normal"] = [normal.x, normal.y]
    scene[KEY] = json.dumps(saved)


def _frame(a, b, normal):
    """Every 3D view on the gap, looking at the cut face from the removed side, a little from above."""
    from . import findings_draw  # it imports this module
    towards_eye = Vector((normal.x * math.cos(TILT), normal.y * math.cos(TILT), math.sin(TILT)))
    rotation = towards_eye.to_track_quat("Z", "Y")  # a view's z points back at the eye, its y up
    for space in _views():
        view = space.region_3d
        if view.view_perspective == "CAMERA":
            view.view_perspective = "PERSP"
        view.view_rotation = rotation
    # framed after turning: the eye is kept out of part models along the new direction
    findings_draw.frame_bounds(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))


def section(finding):
    """(a, b, normal) while this hole finding's cut is open: its hole centres and the cut
    face's normal out towards the viewer (Blender XY), its labels written on the face
    (label_place.on_cut); None otherwise."""
    saved = _saved(bpy.context.scene)
    ends = hole_ends(finding)
    if saved is None or "normal" not in saved or ends is None:
        return None
    return (*ends, Vector(saved["normal"]))


def follow(finding):
    """The findings changed: the user's cut back unless a hole finding is still shown."""
    if hole_ends(finding) is None:
        restore()


def restore(scene=None):
    """Put back the cut and view directions saved before the first automatic cut, where the
    user left what was set alone: switch by switch, the plane on its own (a plane the user
    deleted meanwhile comes back as theirs was)."""
    scene = scene or bpy.context.scene
    saved = _saved(scene)
    if KEY in scene:
        del scene[KEY]
    if saved is None:
        return
    current, applied, wanted = _cut_state(scene), saved["applied_cut"], saved["cut"]
    state = dict(current)
    for name, _ in _PROPERTIES:  # each switch the user left as the cut set it goes back
        if current[name] == applied[name]:
            state[name] = wanted[name]
    if current["plane"] is None or _same(current["plane"], applied["plane"]):
        state["plane"] = wanted["plane"]  # the plane as the cut placed it, or deleted meanwhile: the user's back
    if not _same(state, current):
        _set_cut(scene, state)
    for space, applied, wanted in zip(_views(), saved["applied_views"], saved["views"]):
        if _same(_view_state_of(space), applied):
            space.region_3d.view_perspective = wanted["perspective"]
            space.region_3d.view_rotation = Quaternion(wanted["rotation"])
