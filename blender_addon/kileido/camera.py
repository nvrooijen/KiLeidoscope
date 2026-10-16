"""The KiLeidoscope camera ("KLS Camera"): put where it sees every board, and the parts on
it, from the direction the 3D view looks from, and made the scene camera, so F12 renders
what the view showed. Placed on request only (the Studio column's Frame camera, or
studio.fit_camera): once placed it is the user's to move, until it is framed again."""

import itertools
import math

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from . import studio
from .objects import view3d_spaces

CAMERA = "KLS Camera"
MARGIN = 1.08  # the board spans 1/MARGIN of the frame's limiting side
# Looking down at the board from the front right, as a new 3D view does: for scripts
# without a 3D view (background Blender).
DEFAULT_ROTATION = Euler((math.radians(60.0), 0.0, math.radians(45.0))).to_quaternion()


def view_rotation(view=None):
    """The direction a 3D view looks from, as the rotation a camera needs to look the same
    way: `view` (a RegionView3D), else the first 3D view's; DEFAULT_ROTATION without one.
    Looking through a camera already, that camera's own."""
    if view is None:
        view = next((space.region_3d for space in view3d_spaces()), None)
    if view is None:
        return DEFAULT_ROTATION.copy()
    scene_camera = bpy.context.scene.camera
    if view.view_perspective == "CAMERA" and scene_camera is not None:
        return scene_camera.matrix_world.to_quaternion()
    return view.view_rotation.copy()


def frame(rotation=None, parts=True, margin=MARGIN, camera=None):
    """Put `camera` (default: KiLeidoscope's own, made when missing) where every board, and
    with `parts` the component models on it, fills its frame with `margin` around, looking
    along `rotation` (default: the 3D view's), and make it the scene camera. The camera's
    lens, sensor and type are kept; its lens shift centres the board. None before any board."""
    bounds = studio.board_bounds(parts)
    if bounds is None:
        return None
    scene = bpy.context.scene
    if camera is None:
        camera = _camera(scene)
    if rotation is None:
        rotation = view_rotation()
    elif not isinstance(rotation, Quaternion):
        rotation = rotation.to_quaternion()
    low, high = bounds
    centre = (low + high) / 2
    to_camera = rotation.to_matrix().transposed()  # world -> the camera's axes (it looks along -Z)
    corners = [to_camera @ (Vector(corner) - centre)
               for corner in itertools.product((low.x, high.x), (low.y, high.y), (low.z, high.z))]
    data = camera.data
    render = scene.render
    aspect = (render.resolution_x * render.pixel_aspect_x) / (render.resolution_y * render.pixel_aspect_y)
    if data.type == "ORTHO":
        width = 2 * margin * max(abs(p.x) for p in corners)
        height = 2 * margin * max(abs(p.y) for p in corners)
        horizontal = data.sensor_fit == "HORIZONTAL" or (data.sensor_fit == "AUTO" and aspect >= 1)
        data.ortho_scale = max(width, height * aspect) if horizontal else max(height, width / aspect)
        data.shift_x = data.shift_y = 0.0
        distance = max(p.z for p in corners) + (high - low).length
    else:
        distance, data.shift_x, data.shift_y = _perspective_fit(corners, *_half_tangents(data, aspect), margin)
    camera.matrix_world = (Matrix.Translation(centre + rotation @ Vector((0.0, 0.0, distance)))
                           @ rotation.to_matrix().to_4x4())
    data.clip_start = min(data.clip_start, distance / 100)
    data.clip_end = max(data.clip_end, distance * 100)
    scene.camera = camera
    return camera


def _half_tangents(data, aspect):
    """tan of the half angles of view across and down the frame, and of the side the
    sensor is fitted to: its width spans the frame's larger side, unless the camera's
    sensor fit says which. Blender's lens shift is a fraction of that side's width."""
    if data.sensor_fit == "VERTICAL":
        tan_y = data.sensor_height / (2 * data.lens)
        return tan_y * aspect, tan_y, tan_y
    if data.sensor_fit == "HORIZONTAL" or aspect >= 1:
        tan_x = data.sensor_width / (2 * data.lens)
        return tan_x, tan_x / aspect, tan_x
    tan_y = data.sensor_width / (2 * data.lens)
    return tan_y * aspect, tan_y, tan_y


def _perspective_fit(corners, tan_x, tan_y, tan_fit, margin):
    """How far back along its axis the camera stands from the bounds' centre so every
    corner is inside the frame with `margin`, and the lens shift that centres the corners
    in it (a fraction of the fitted side's width, `tan_fit`). Two passes: the shift that
    centres the corners depends on the distance, and the distance on the shift."""
    mid_x = mid_y = 0.0
    for _ in range(2):
        distance = 0.0
        for p in corners:
            # At depth (distance - p.z) a corner sits p.x / ((distance - p.z) tan_x) across the
            # frame, -1 to 1 edge to edge; the frame centred on mid_x has 1 + mid_x or 1 - mid_x
            # of room on the corner's side, less the margin.
            room_x = (1 + mid_x if p.x >= 0 else 1 - mid_x) / margin
            room_y = (1 + mid_y if p.y >= 0 else 1 - mid_y) / margin
            distance = max(distance, p.z + 1e-6, p.z + abs(p.x) / (tan_x * room_x), p.z + abs(p.y) / (tan_y * room_y))
        xs = [p.x / ((distance - p.z) * tan_x) for p in corners]
        ys = [p.y / ((distance - p.z) * tan_y) for p in corners]
        mid_x, mid_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    # mid_x is in half-widths across; a shift of 1 is the fitted side's whole width.
    return distance, mid_x * tan_x / (2 * tan_fit), mid_y * tan_y / (2 * tan_fit)


def _camera(scene):
    """KiLeidoscope's camera in this scene, made when missing. Not tagged as the board's:
    it is the user's object, kept through resyncs and exports."""
    obj = bpy.data.objects.get(CAMERA)
    if obj is None or obj.type != "CAMERA":
        obj = bpy.data.objects.new(CAMERA, bpy.data.cameras.get(CAMERA) or bpy.data.cameras.new(CAMERA))
    if obj.name not in scene.collection.all_objects:
        scene.collection.objects.link(obj)
    return obj
