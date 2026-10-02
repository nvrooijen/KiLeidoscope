"""Plated board edge: copper over the board's wall, where KiCad's Board Setup asks for it.

KiCad stores one board-wide flag (Board Finish: Plated board edge, `(edge_plating yes)` in
the board file). A fab plates the edge where copper reaches it, so the plating covers the
stretches of the outline that copper on both outer layers reaches (cut.plated_edges). It
is a thin closed skin on the wall, the board's whole height, in the hole plating's
material: copper, or the board's finish, lit like all copper in Realistic mode.
"""

import bmesh
import bpy
import numpy as np

from . import cut
from .objects import OUTLINE, owned_object, set_visible
from .state import board

OBJECT = "KLS edge plating"
THICKNESS_M = 25e-6  # as a via barrel's wall
WALL_GAP_M = 0.5e-6  # off the board's own wall, so the two never share a face
REFRESH_DELAY_S = 0.05  # live edits arriving together redraw it once


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
    mesh = obj.data
    top = board.thickness_m or 0.0016
    shell = bmesh.new()
    for start, end, outward in stretches:
        start, end, outward = (np.asarray(v, np.float64) for v in (start, end, outward))
        footprint = [start + outward * WALL_GAP_M, end + outward * WALL_GAP_M,
                     end + outward * THICKNESS_M, start + outward * THICKNESS_M]
        low = [shell.verts.new((*point, 0.0)) for point in footprint]
        high = [shell.verts.new((*point, top)) for point in footprint]
        shell.faces.new(low[::-1])
        shell.faces.new(high)
        for index in range(4):
            following = (index + 1) % 4
            shell.faces.new((low[index], low[following], high[following], high[index]))
    bmesh.ops.recalc_face_normals(shell, faces=shell.faces)
    shell.to_mesh(mesh)
    shell.free()
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
