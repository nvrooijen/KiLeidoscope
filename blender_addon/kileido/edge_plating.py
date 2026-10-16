"""Plated board edge: copper over the board's wall, where KiCad's Board Setup asks for it.

KiCad stores one board-wide flag (Board Finish: Plated board edge, `(edge_plating yes)` in
the board file). A fab plates the edge where copper reaches it, so the plating covers the
stretches of the outline that copper on both outer layers reaches (cut.plated_edges). It
is a thin skin on the wall, the board's whole height, in the hole plating's material:
copper, or the board's finish, lit like all copper in Realistic mode.

Stretches that follow on from each other (a rounded corner is many short ones) make one
continuous strip whose walls are shaded smooth, so a curved edge reads as one surface
instead of a row of facets; its top, bottom and ends stay flat. At a sharp outside corner
the two strips end on the corner, and a rounded piece wraps it (plating rounds a corner);
at an inside corner they overlap.
"""

import bpy
import numpy as np

from . import cut
from .objects import OUTLINE, owned_object, set_visible
from .placement import copper_placement
from .state import board

OBJECT = "KLS edge plating"
MILLED = "KLS {} milled edge"  # per outer layer: its copper's cut face where it crosses the outline
THICKNESS_M = 25e-6  # a typical plated wall, as the panel's default Via wall
WALL_GAP_M = 0.5e-6  # off the board's own wall, so the two never share a face
JOIN_M = 1e-9  # stretches whose ends meet this closely continue one strip,
SMOOTH_TURN = np.cos(np.radians(30))  # unless the edge turns more than this there (a real corner)
CORNER_STEP = np.radians(10)  # a rounded corner piece, one face per this much turn
REFRESH_DELAY_S = 0.05  # live edits arriving together redraw it once


def chains(stretches):
    """Runs of stretches that follow on from each other: (points, outward normal per
    point, closed), each normal the mean of its two segments' at a joint."""
    def follows(run, start, outward):
        return bool(np.hypot(*(run[0][-1] - start)) < JOIN_M) and float(np.dot(run[1][-1], outward)) >= SMOOTH_TURN

    runs = []
    for start, end, outward in stretches:
        start, end, outward = (np.asarray(v, np.float64) for v in (start, end, outward))
        if runs and follows(runs[-1], start, outward):
            runs[-1][0].append(end)
            runs[-1][1].append(outward)
        else:
            runs.append(([start, end], [outward]))
    # An outline loop that starts part way along a strip lists the strip's start last: that
    # run goes on into the loop's first one (in every loop, cutouts too).
    for index in range(len(runs) - 1, -1, -1):
        run = runs[index]
        into = next((other for other in runs if other is not run and follows(run, other[0][0], other[1][0])), None)
        if into is not None:
            into[0][:0] = run[0][:-1]
            into[1][:0] = run[1]
            del runs[index]
    result = []
    for run in runs:
        closed = len(run[0]) > 2 and follows(run, run[0][0], run[1][0])  # round to its own start, smoothly
        points, normals = np.array(run[0]), np.array(run[1])
        if closed:
            points = points[:-1]
            joints = normals + np.roll(normals, 1, axis=0)
        else:
            joints = np.vstack((normals[:1], normals[:-1] + normals[1:], normals[-1:]))
        joints /= np.maximum(np.hypot(joints[:, 0], joints[:, 1]), 1e-12)[:, None]
        result.append((points, joints, closed))
    return result


def corners(stretches):
    """Sharp outside corners between plated stretches: (corner xy, outward normal before,
    outward normal after), where one stretch ends on the next's start and the edge turns
    away from the board by more than a smooth bend."""
    starts = {}  # rounded start -> outward normals of the stretches starting there
    for start, _, outward in stretches:
        starts.setdefault(tuple(np.round(np.asarray(start, np.float64) / JOIN_M)), []).append(
            np.asarray(outward, np.float64))
    found = []
    for start, end, outward in stretches:
        end, before = np.asarray(end, np.float64), np.asarray(outward, np.float64)
        along = end - np.asarray(start, np.float64)
        for after in starts.get(tuple(np.round(end / JOIN_M)), ()):
            if float(np.dot(before, after)) < SMOOTH_TURN and float(np.dot(after, along)) > 0:
                found.append((end, before, after))
    return found


def _corner(point, before, after, top):
    """(vertices, faces, smooth per face) of a rounded piece around an outside corner: a
    quarter-round (or what the turn is) of the plating's thickness, centred on the corner."""
    first, last = np.arctan2(before[1], before[0]), np.arctan2(after[1], after[0])
    turn = (last - first + np.pi) % (2 * np.pi) - np.pi  # the short way round
    steps = max(2, int(np.ceil(abs(turn) / CORNER_STEP)))
    angles = first + turn * np.linspace(0.0, 1.0, steps + 1)
    arc = point + THICKNESS_M * np.column_stack((np.cos(angles), np.sin(angles)))
    vertices = [(*point, 0.0), (*point, top)]
    vertices += [(x, y, 0.0) for x, y in arc] + [(x, y, top) for x, y in arc]
    low, high = 2, 2 + len(arc)
    faces, smooth = [], []
    for k in range(steps):
        wall = [low + k, low + k + 1, high + k + 1, high + k]
        faces += [wall if turn > 0 else wall[::-1]]  # facing out of the corner
        faces += [[0, low + k + 1, low + k] if turn > 0 else [0, low + k, low + k + 1],  # bottom, facing down
                  [1, high + k, high + k + 1] if turn > 0 else [1, high + k + 1, high + k]]  # top, facing up
        smooth += [True, False, False]
    return vertices, faces, smooth


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
        at = np.array([vertices[index] for index in quad])
        normal = np.cross(at[1] - at[0], at[3] - at[0])
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
            across = np.array([inner[k], outer[k]])
            low, high = ring(across, 0.0), ring(across, top)
            add((low[0], low[1], high[1], high[0]), (*along, 0.0), False)
    return vertices, faces, smooth


def _fill(mesh, vertices, faces, material, smooth=None):
    """Replace the mesh's geometry and give it its one material."""
    mesh.clear_geometry()
    mesh.from_pydata(vertices, [], faces)
    if smooth is not None:
        mesh.polygons.foreach_set("use_smooth", np.array(smooth, bool))
    mesh.update()
    if mesh.materials:
        mesh.materials[0] = material
    else:
        mesh.materials.append(material)


def refresh_milled():
    """Each outer layer's cut copper face on the edge: copper drawn past the outline is
    see-through there (holes.clip_to_board), and this closes its end, the copper's own
    height, facing out."""
    from . import layers  # layers imports this module
    found = cut.milled_edges()
    for layer in ("F.Cu", "B.Cu"):
        name, stretches = MILLED.format(layer), found.get(layer, ())
        obj = board.collection.all_objects.get(name)
        if not stretches:
            if obj is not None:
                obj.data.clear_geometry()
                set_visible(obj, False)
            continue
        obj = owned_object(name)
        z, thickness = copper_placement(layer, "pads")  # the highest of the layer's copper
        z0, z1 = sorted((z, z + thickness))
        vertices, faces = [], []
        for start, end, outward in stretches:
            first = len(vertices)
            vertices += [(*start, z0), (*end, z0), (*end, z1), (*start, z1)]
            run = np.asarray(end, np.float64) - np.asarray(start, np.float64)
            quad = [first, first + 1, first + 2, first + 3]
            faces.append(quad if run[1] * outward[0] - run[0] * outward[1] >= 0 else quad[::-1])  # facing out
        _fill(obj.data, vertices, faces, board.materials["copper_cut"])
        set_visible(obj, not layers.hidden(obj))
        board.touched.add(obj.name)


def refresh():
    """Draw the plating for the board as it is now, or clear it; the cut copper faces too."""
    if board.collection is None or board.in_snapshot:
        return
    refresh_milled()
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
    parts = [_shell(points, normals, closed, top) for points, normals, closed in chains(stretches)]
    parts += [_corner(point, before, after, top) for point, before, after in corners(stretches)]
    for part in parts:
        faces += [[index + len(vertices) for index in face] for face in part[1]]
        vertices += part[0]
        smooth += part[2]
    _fill(obj.data, vertices, faces, board.materials["plating_edge"], smooth)  # outside the outline: never clipped
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
