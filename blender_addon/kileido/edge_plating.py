"""Plated board edge: copper over the board's wall, where KiCad's Board Setup asks for it.

KiCad stores one board-wide flag (Board Finish: Plated board edge, `(edge_plating yes)` in
the board file). A fab plates the edge where copper reaches it, so the plating covers the
stretches of the outline that copper on both outer layers reaches (cut.plated_edges). It
is a thin skin on the wall, the board's whole height, in the hole plating's material:
copper, or the board's finish, lit like all copper in Realistic mode.

Stretches that follow on from each other (a rounded corner is many short ones) make one
continuous strip whose walls are shaded smooth, so a curved edge reads as one surface
instead of a row of facets; its top, bottom and ends stay flat.
"""

import bpy
import numpy as np

from . import cut
from .objects import OUTLINE, owned_object, set_visible
from .state import board

OBJECT = "KLS edge plating"
THICKNESS_M = 25e-6  # as a via barrel's wall
WALL_GAP_M = 0.5e-6  # off the board's own wall, so the two never share a face
JOIN_M = 1e-9  # stretches whose ends meet this closely continue one strip,
SMOOTH_TURN = np.cos(np.radians(30))  # unless the edge turns more than this there (a real corner)
REFRESH_DELAY_S = 0.05  # live edits arriving together redraw it once


def chains(stretches):
    """Runs of stretches that follow on from each other: (points, outward normal per
    point, closed), each normal the mean of its two segments' at a joint."""
    def follows(run, start, outward):
        return np.hypot(*(run[0][-1] - start)) < JOIN_M and float(np.dot(run[1][-1], outward)) >= SMOOTH_TURN

    runs = []
    for start, end, outward in stretches:
        start, end, outward = (np.asarray(v, np.float64) for v in (start, end, outward))
        if runs and follows(runs[-1], start, outward):
            runs[-1][0].append(end)
            runs[-1][1].append(outward)
        else:
            runs.append(([start, end], [outward]))
    if len(runs) > 1 and follows(runs[-1], runs[0][0][0], runs[0][1][0]):  # the last runs into the first
        last = runs.pop()
        runs[0] = (last[0][:-1] + runs[0][0], last[1] + runs[0][1])
    result = []
    for points, normals in runs:
        points, normals = np.array(points), np.array(normals)
        closed = len(points) > 2 and np.hypot(*(points[-1] - points[0])) < JOIN_M
        if closed:
            points = points[:-1]
            joints = normals + np.roll(normals, 1, axis=0)
        else:
            joints = np.vstack((normals[:1], normals[:-1] + normals[1:], normals[-1:]))
        joints /= np.maximum(np.hypot(joints[:, 0], joints[:, 1]), 1e-12)[:, None]
        result.append((points, joints, closed))
    return result


def _shell(points, normals, closed, top):
    """(vertices, faces, smooth per face) of one strip: walls on shared, smooth vertices;
    top, bottom and ends on their own, flat."""
    count = len(points)
    inner, outer = points + normals * WALL_GAP_M, points + normals * THICKNESS_M
    vertices, faces, smooth = [], [], []

    def ring(xy, z):
        first = len(vertices)
        vertices.extend((x, y, z) for x, y in xy)
        return np.arange(first, first + len(xy))

    def add(quad, facing, round_wall):
        """A quad, wound so its normal points along `facing`."""
        corners = np.array([vertices[index] for index in quad])
        normal = np.cross(corners[1] - corners[0], corners[3] - corners[0])
        faces.append(list(quad) if np.dot(normal, facing) >= 0 else list(quad)[::-1])
        smooth.append(round_wall)

    steps = range(count) if closed else range(count - 1)
    for xy, side in ((outer, 1.0), (inner, -1.0)):  # the outer wall faces out, the inner one in
        low, high = ring(xy, 0.0), ring(xy, top)
        for k in steps:
            n = (k + 1) % count
            facing = (*(side * (normals[k] + normals[n])), 0.0)
            add((low[k], low[n], high[n], high[k]), facing, True)
    for z, up in ((0.0, -1.0), (top, 1.0)):
        outer_ring, inner_ring = ring(outer, z), ring(inner, z)
        for k in steps:
            n = (k + 1) % count
            add((outer_ring[k], outer_ring[n], inner_ring[n], inner_ring[k]), (0.0, 0.0, up), False)
    if not closed:
        for k, along in ((0, points[0] - points[1]), (count - 1, points[-1] - points[-2])):
            low, high = ring(np.array([inner[k], outer[k]]), 0.0), ring(np.array([inner[k], outer[k]]), top)
            add((low[0], low[1], high[1], high[0]), (*along, 0.0), False)
    return vertices, faces, smooth


def refresh():
    """Draw the plating for the board as it is now, or clear it."""
    if board.collection is None or board.in_snapshot:
        return
    stretches = cut.plated_edges()
    obj = board.collection.all_objects.get(OBJECT)
    if not stretches:
        if obj is not None:
            obj.data.clear_geometry()
            set_visible(obj, False)
        return
    obj = owned_object(OBJECT)
    top = board.thickness_m or 0.0016
    vertices, faces, smooth = [], [], []
    for points, normals, closed in chains(stretches):
        part = _shell(points, normals, closed, top)
        faces += [[index + len(vertices) for index in face] for face in part[1]]
        vertices += part[0]
        smooth += part[2]
    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(vertices, [], faces)
    mesh.polygons.foreach_set("use_smooth", np.array(smooth, bool))
    mesh.update()
    if not mesh.materials:
        mesh.materials.append(board.materials["plating"])
    mesh.materials[0] = board.materials["plating"]
    outline = board.collection.all_objects.get(OUTLINE)
    set_visible(obj, outline is None or not outline.hide_get())  # with the board solid
    board.touched.add(obj.name)


def invalidate():
    """The board or its stackup changed: draw the plating again soon (once per burst)."""
    if not bpy.app.timers.is_registered(_refresh_soon):
        bpy.app.timers.register(_refresh_soon, first_interval=REFRESH_DELAY_S)


def _refresh_soon():
    refresh()
    return None


def uninstall():
    if bpy.app.timers.is_registered(_refresh_soon):
        bpy.app.timers.unregister(_refresh_soon)
