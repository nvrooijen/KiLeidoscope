"""EEVEE final renders: fit the camera's clip range to the scene, for depth precision.

EEVEE rasterizes against a depth buffer spread over the camera's whole clip range.
An orthographic camera at Blender's default 0.1..1000 m resolves ~60 um, coarser
than 35 um copper, the 10 um mask sheet and the 1-3 um lifts over it: the board
face hid the copper, or not, depending on the camera height (measured on the
synthetic fixture). Cycles traces rays and has no depth buffer; the viewport
has its own clip range.

For the render only, the scene camera's clip_start/clip_end are narrowed to the
render-visible objects' depth span plus a generous margin, never widened, and
restored when the frame is done. Nothing that was inside the range is clipped:
the margin covers stale bounding boxes and instances outside an object's own box.
"""

import math

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector


MARGIN_M = 1.0  # a 2 m range still resolves ~0.1 um
GEOMETRY = {"MESH", "CURVE", "CURVES", "SURFACE", "META", "FONT", "POINTCLOUD", "VOLUME", "GREASEPENCIL"}
_saved = {}  # camera datablock name -> (clip_start, clip_end) set by the user


def depth_range(scene, camera):
    """Nearest and farthest depth (m, along the view axis) of render-visible geometry."""
    to_camera = camera.matrix_world.inverted()
    low, high = math.inf, -math.inf

    def visit(obj, matrix, level):
        nonlocal low, high
        if obj.instance_type == "COLLECTION" and obj.instance_collection is not None and level < 8:
            offset = matrix @ Matrix.Translation(-Vector(obj.instance_collection.instance_offset))
            for child in obj.instance_collection.all_objects:
                visit(child, offset @ child.matrix_world, level + 1)
        if obj.type not in GEOMETRY:
            return
        for corner in obj.bound_box:
            depth = -(to_camera @ (matrix @ Vector(corner))).z
            low, high = min(low, depth), max(high, depth)

    for obj in scene.objects:
        if not obj.hide_render and obj != camera:
            visit(obj, obj.matrix_world, 0)
    return low, high


def fit(scene):
    """Narrow the scene camera's clip range for this frame; returns the range set, or None."""
    camera = scene.camera
    if scene.render.engine != "BLENDER_EEVEE" or camera is None or camera.type != "CAMERA":
        return None
    data = camera.data
    low, high = depth_range(scene, camera)
    if not math.isfinite(low):
        return None
    near = max(data.clip_start, low - MARGIN_M)
    far = min(data.clip_end, high + MARGIN_M)
    if far <= near or (near, far) == (data.clip_start, data.clip_end):
        return None
    _saved.setdefault(data.name, (data.clip_start, data.clip_end))
    data.clip_start, data.clip_end = near, far
    return near, far


def restore():
    for name, (near, far) in _saved.items():
        data = bpy.data.cameras.get(name)
        if data is not None:
            data.clip_start, data.clip_end = near, far
    _saved.clear()


@persistent
def _render_pre(scene, *_unused):
    restore()  # a cancelled frame may not have reached render_post
    fit(scene)


@persistent
def _render_done(_scene, *_unused):
    restore()


HANDLERS = (("render_pre", _render_pre), ("render_post", _render_done),
            ("render_cancel", _render_done), ("render_complete", _render_done))


def install():
    """Idempotent, and replaces handlers left by an earlier copy of this module."""
    uninstall()
    for event, handler in HANDLERS:
        getattr(bpy.app.handlers, event).append(handler)


def uninstall():
    for event, _ in HANDLERS:
        handlers = getattr(bpy.app.handlers, event)
        for handler in [h for h in handlers if getattr(h, "__module__", None) == __name__]:
            handlers.remove(handler)
