"""Return-path issues from the bridge (protocol.return_path_message).

Where a checked net loses its reference plane, the broken part of the plane glows
red on the reference layer: the void, antipad or split channel, whole. The track
over it gets a thin red stripe, so it is clear which one it concerns; X-ray mode
shows inner planes through the board. A track with no plane at all glows red along
its length, and a via that changes reference without a return via gets a red disc
on each layer it joins.

Marks are one object per copper layer ("KLS <layer> highlight return path"), drawn
by the tracks group like the selection highlight, higher, so they show over it; the
plane patches another ("... return path area"). The panel's check box hides them.
"""

import bpy
import numpy as np

from . import transform
from .highlight import hide_copy, show_copy
from .objects import owned_object, set_modifier, write_attribute
from .placement import copper_placement, outward
from .state import board

HALO_M = 0.1e-3  # each side beyond the track (or via land)
STRIPE = 0.4  # a gap's or split's track mark, as a fraction of the track width
LIFT_M = 2e-6  # above the copper's outer surface; the selection highlight sits at 1 um
AREA_LIFT_M = 4e-6  # plane patches, out from the plane's copper surface
PLANE_KINDS = ("gap", "split")  # issues whose plane is marked, not the track
TAG = "kls_return_path"


def enabled():
    return bool(getattr(bpy.context.scene, "kileido_return_path", True))


def _array(arrays, name, columns=None, dtype=np.int32):
    values = np.array(arrays.get(name, ()), dtype=dtype)
    return values.reshape(-1, columns) if columns else values


def apply_return_path(header, arrays):
    """The bridge's latest check (it sends one whenever the issues change)."""
    board.return_path = {
        "issues": list(header.get("issues", ())), "nets": list(header.get("nets", ())),
        "layers": list(header.get("layers", ())), "error": header.get("error", ""),
        "elapsed_ms": header.get("elapsed_ms"),
        "mark": _array(arrays, "mark", 5, np.int64), "mark_issue": _array(arrays, "mark_issue"),
        "mark_layer": _array(arrays, "mark_layer"),
        "area": _array(arrays, "area", 4, np.int64), "area_issue": _array(arrays, "area_issue"),
        "area_layer": _array(arrays, "area_layer"),
    }
    refresh()


def refresh():
    """Rebuild the marks from the last check; hidden while the check box is off.
    Skipped inside a snapshot: `snapshot_end` calls it once."""
    if board.collection is None or board.in_snapshot:
        return
    data = board.return_path
    wanted = {}  # object name -> (draw, layer, rows, issues)
    if enabled() and data:
        for index, layer in enumerate(data["layers"]):
            for kind, draw in (("mark", _draw_marks), ("area", _draw_area)):
                chosen = data[f"{kind}_layer"] == index
                if chosen.any():
                    name = f"KLS {layer} highlight return path" + (" area" if kind == "area" else "")
                    wanted[name] = (draw, layer, data[kind][chosen], data[f"{kind}_issue"][chosen])
    for obj in tuple(board.collection.all_objects):
        if obj.get(TAG) and obj.name not in wanted:
            hide_copy(obj)
    for name, (draw, layer, rows, issues) in wanted.items():
        obj = owned_object(name)
        obj[TAG] = layer
        draw(obj, layer, rows, issues)
        show_copy(obj)


def _draw_marks(obj, layer, marks, issue):
    """Segments (x1, y1, x2, y2, width) as a glowing ribbon over the track."""
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(len(marks) * 2)
    mesh.edges.add(len(marks))
    ends = np.empty((len(marks) * 2, 2), dtype=np.int64)
    ends[0::2], ends[1::2] = marks[:, :2], marks[:, 2:4]
    coordinates = np.zeros((len(ends), 3), dtype=np.float32)
    coordinates[:, :2] = transform.xy_m(ends, board.origin_nm)
    mesh.vertices.foreach_set("co", coordinates.ravel())
    mesh.edges.foreach_set("vertices", np.arange(len(ends), dtype=np.int32))
    kinds = [entry["kind"] for entry in board.return_path["issues"]]
    stripe = np.array([kinds[number] in PLANE_KINDS for number in issue.tolist()], bool)
    track = marks[:, 4].astype(np.float64) * 1e-9
    widths = np.where(stripe, track * STRIPE, track + 2 * HALO_M)
    write_attribute(mesh, "width", "FLOAT", np.repeat(widths, 2).astype(np.float32))
    write_attribute(mesh, "item", "INT", np.repeat(issue, 2))
    mesh.update()
    z, thickness = copper_placement(layer, "tracks")
    up = outward(layer)
    if thickness:
        obj.location.z, lift = z, thickness + up * LIFT_M
    else:
        obj.location.z, lift = z + up * LIFT_M, 0.0
    set_modifier(obj, board.groups["tracks"], "highlight_return_path", {"Thickness": lift, "Up": up})


def _draw_area(obj, layer, rects, issue):
    """Rects (left, top, right, bottom) of a broken plane as flat quads just out from its copper."""
    mesh = obj.data
    mesh.clear_geometry()
    corners = np.stack([rects[:, [0, 1]], rects[:, [2, 1]], rects[:, [2, 3]], rects[:, [0, 3]]], axis=1)
    coordinates = np.zeros((len(rects) * 4, 3), dtype=np.float32)
    coordinates[:, :2] = transform.xy_m(corners.reshape(-1, 2), board.origin_nm)
    mesh.vertices.add(len(coordinates))
    mesh.vertices.foreach_set("co", coordinates.ravel())
    mesh.loops.add(len(coordinates))
    mesh.loops.foreach_set("vertex_index", np.arange(len(coordinates), dtype=np.int32))
    mesh.polygons.add(len(rects))
    mesh.polygons.foreach_set("loop_start", np.arange(0, len(coordinates), 4, dtype=np.int32))
    mesh.polygons.foreach_set("loop_total", np.full(len(rects), 4, dtype=np.int32))
    write_attribute(mesh, "item", "INT", np.repeat(issue, 4))
    mesh.update()
    if mesh.materials:
        mesh.materials[0] = board.materials["highlight_return_path"]
    else:
        mesh.materials.append(board.materials["highlight_return_path"])
    obj.location.z = transform.copper_z(layer, "zones", board.heights) + outward(layer) * AREA_LIFT_M


def summary():
    """One line for the panel: what was checked and found."""
    data = board.return_path
    if not data:
        return "No check yet"
    if data["error"]:
        return data["error"]
    nets, found = len(data["nets"]), len(data["issues"])
    if not nets:
        return "No differential pairs or selected nets"
    text = f"{found} issue{'s' * (found != 1)} on {nets} net{'s' * (nets != 1)}"
    return text if data["elapsed_ms"] is None else f"{text} ({data['elapsed_ms']:.0f} ms)"
