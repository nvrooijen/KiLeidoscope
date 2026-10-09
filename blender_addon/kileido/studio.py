"""A stable hook for studio add-ons: turntables, camera rigs, lighting setups.

Another add-on reaches it without knowing how KiLeidoscope was loaded:

    import sys
    studio = sys.modules.get("kileidoscope_studio")  # None: KiLeidoscope isn't running
    if studio is not None and studio.API_VERSION >= 1:
        low, high = studio.board_bounds()

Everything in this module is public and kept stable across KiLeidoscope versions;
a change that would break a caller raises API_VERSION. Nothing else in the add-on
is meant for other add-ons. See docs/studio-hook.md.
"""

import sys
import time
from contextlib import contextmanager

import bpy
from mathutils import Vector

API_VERSION = 1
MODULE_ALIAS = "kileidoscope_studio"  # the name other add-ons look this module up by
SETTLE_S = 0.6  # no further board update for this long: an edit has fully arrived
PUMP_STEP_S = 0.01
LIGHTING_OWNER = "kls_lighting_owner"  # scene property: who looks after this scene's lights

_updates = 0  # board updates applied since the add-on started
_last_update_at = None  # time.monotonic() of the latest
_holds = 0
_render_hold = False
_listeners = []
_UPDATE_FRAMES = ("layer_data", "footprints", "snapshot_end")  # what counts as a board update


# --- The board ------------------------------------------------------------------------------

def has_board():
    """A board is shown: the live one, a loaded dump or a view-only board."""
    return board_bounds(parts=False) is not None


def is_live():
    """A live link to KiCad is on (it may be reconnecting)."""
    from . import live
    return live.connected()


def board_bounds(parts=True):
    """World-space (low, high) corners, in metres, around every board shown and, with
    `parts`, the component models on it; None before any board. Hidden parts are left
    out, as they are from a render."""
    from . import lighting
    bounds = lighting._board_bounds()
    if bounds is None:
        return None
    low, high = bounds
    if parts:
        for obj in bpy.context.scene.objects:
            if obj.get("kls_model_fp_id") is None or obj.hide_render or obj.type != "MESH":
                continue
            for corner in obj.bound_box:
                point = obj.matrix_world @ Vector(corner)
                low = Vector(map(min, low, point))
                high = Vector(map(max, high, point))
    return low.copy(), high.copy()


def board_size(parts=True):
    """The (x, y, z) extent in metres of `board_bounds`, or None before any board."""
    bounds = board_bounds(parts)
    return None if bounds is None else bounds[1] - bounds[0]


def board_center(parts=True):
    """The middle of `board_bounds` in world metres, or None before any board."""
    bounds = board_bounds(parts)
    return None if bounds is None else (bounds[0] + bounds[1]) / 2


def selected_parts():
    """The objects of the footprints selected in KiCad, to frame or focus on: each one's
    component model, or the placeholder box standing in for a model not loaded. A
    footprint with neither (no 3D model at all) is left out."""
    from .state import board
    chosen = board.highlight_components.get("footprints") or set()
    if not chosen:
        return []
    models, boxes = {}, {}
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.hide_render or obj.hide_get():
            continue
        if obj.get("kls_model_fp_id") in chosen:
            models.setdefault(obj["kls_model_fp_id"], []).append(obj)
        elif obj.get("kls_footprint_id") in chosen and len(obj.data.vertices):
            boxes.setdefault(obj["kls_footprint_id"], []).append(obj)
    return [obj for fp_id in sorted(chosen) for obj in models.get(fp_id) or boxes.get(fp_id) or ()]


# --- Camera ------------------------------------------------------------------------------------

def fit_camera(camera=None, rotation=None, parts=True, margin=None):
    """Put `camera` (default: KiLeidoscope's own, "KLS Camera", made when missing) where
    every board fills its frame, with `margin` around (1.08: the board takes 1/1.08 of the
    frame's limiting side), looking along `rotation` (a Quaternion, or anything with
    `to_quaternion()`; default: the direction the 3D view looks from), and make it the
    scene camera. The camera's lens, sensor and type (perspective or orthographic) are
    kept; its lens shift centres the board. Returns the camera, or None before any board."""
    from . import camera as framing
    return framing.frame(rotation=rotation, parts=parts, margin=framing.MARGIN if margin is None else margin,
                         camera=camera)


# --- Updates from KiCad ------------------------------------------------------------------------

def update_count():
    """Board updates applied so far. Compare two readings to see that one arrived."""
    return _updates


def seconds_since_update():
    """Since the latest board update, or None before the first."""
    return None if _last_update_at is None else time.monotonic() - _last_update_at


def is_settled(quiet_s=SETTLE_S):
    """Nothing is queued to apply and no board update came in the last `quiet_s`."""
    from . import dump, live
    if live.connected() and (live.link.pending or live.link.receiving_snapshot):
        return False
    if (dump._worker is not None and dump._worker.is_alive()) or not dump._queue.empty():
        return False
    since = seconds_since_update()
    return since is None or since >= quiet_s


def pump(seconds=0.05):
    """Run the live link (and a dump being loaded) by hand for `seconds`. Blender's own
    timers wait while a script runs, so a script that renders frame by frame calls this
    between frames to keep KiLeidoscope current. Within `held_updates`, frames are
    received but not applied."""
    from . import dump, live
    until = time.monotonic() + seconds
    while True:
        if live.connected():
            live.link.tick()
        if dump._worker is not None:
            dump.drain()
        if time.monotonic() >= until:
            return
        time.sleep(PUMP_STEP_S)


def wait_for_update(since, timeout=60.0, quiet_s=SETTLE_S):
    """Pump until a board update newer than the `update_count()` reading `since` has
    arrived and settled. For a script that changed the board in KiCad and blocks
    Blender. Returns the new update count; raises TimeoutError."""
    deadline = time.monotonic() + timeout
    while _updates == since:
        if time.monotonic() > deadline:
            raise TimeoutError("No board update from KiCad arrived")
        pump(0.05)
    return wait_until_settled(max(0.0, deadline - time.monotonic()), quiet_s)


def wait_until_settled(timeout=60.0, quiet_s=SETTLE_S):
    """Pump until `is_settled`. Returns the update count; raises TimeoutError."""
    deadline = time.monotonic() + timeout
    while not is_settled(quiet_s):
        if time.monotonic() > deadline:
            raise TimeoutError("Board updates from KiCad did not settle")
        pump(0.05)
    return _updates


def add_settled_listener(callback):
    """Call `callback()` once after each burst of board updates has settled (a KiCad
    edit, a full resync, a dump loaded), from Blender's main thread."""
    if callback not in _listeners:
        _listeners.append(callback)


def remove_settled_listener(callback):
    if callback in _listeners:
        _listeners.remove(callback)


# --- Rendering while the link is live -----------------------------------------------------------

@contextmanager
def held_updates():
    """KiLeidoscope keeps receiving from KiCad but applies nothing until the block ends,
    so nothing changes the scene under a render. Renders started any other way (F12,
    Render Animation, bpy.ops.render.render) are held the same way automatically."""
    global _holds
    _holds += 1
    try:
        yield
    finally:
        _holds -= 1


def updates_held():
    return _holds > 0 or _render_hold


# --- Lighting --------------------------------------------------------------------------------

def claim_lighting(owner, scene=None):
    """Ask KiLeidoscope to leave this scene's lights and world alone: its softboxes are
    hidden and it stops replacing the world, its background and film transparency.
    `owner` is a short name the panel shows. Saved with the scene."""
    scene = scene or bpy.context.scene
    scene[LIGHTING_OWNER] = str(owner)
    for collection in scene.collection.children:
        if collection.get("kls_studio_lights") == 1:
            collection.hide_viewport = True
            collection.hide_render = True


def release_lighting(scene=None):
    """Give the lights back: KiLeidoscope sets up its softboxes and black world again."""
    from . import lighting
    scene = scene or bpy.context.scene
    if LIGHTING_OWNER in scene:
        del scene[LIGHTING_OWNER]
    for collection in scene.collection.children:
        if collection.get("kls_studio_lights") == 1:
            collection.hide_viewport = False
            collection.hide_render = False
    if scene == bpy.context.scene and has_board():
        lighting.ensure_studio_lights()


def lighting_owner(scene=None):
    """Who claimed this scene's lighting, or ""."""
    return str((scene or bpy.context.scene).get(LIGHTING_OWNER, ""))


# --- Called by KiLeidoscope itself -------------------------------------------------------------

def note_frame(message_type):
    """apply.apply_frame applied a frame."""
    global _updates, _last_update_at
    if message_type not in _UPDATE_FRAMES:
        return
    _updates += 1
    _last_update_at = time.monotonic()
    if _listeners and not bpy.app.timers.is_registered(_settle_check):
        bpy.app.timers.register(_settle_check, first_interval=SETTLE_S)


def _settle_check():
    if not is_settled():
        return 0.1
    for callback in list(_listeners):
        try:
            callback()
        except Exception as exc:  # a listener's bug must not stop the others or the timer
            print(f"KiLeidoscope: studio listener {callback!r} failed: {exc}", flush=True)
    return None


def _render_started(*_args):
    global _render_hold
    _render_hold = True


def _render_ended(*_args):
    global _render_hold
    _render_hold = False


_RENDER_HANDLERS = (("render_init", _render_started), ("render_complete", _render_ended),
                    ("render_cancel", _render_ended))


def install():
    """At register: other add-ons find this module by MODULE_ALIAS, and renders hold updates."""
    sys.modules[MODULE_ALIAS] = sys.modules[__name__]
    for name, handler in _RENDER_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)


def uninstall():
    global _render_hold
    if sys.modules.get(MODULE_ALIAS) is sys.modules[__name__]:
        del sys.modules[MODULE_ALIAS]
    for name, handler in _RENDER_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)
    if bpy.app.timers.is_registered(_settle_check):
        bpy.app.timers.unregister(_settle_check)
    _render_hold = False
    _listeners.clear()
