"""Return-path issues from the bridge (protocol.return_path_message): where a checked
net loses its reference plane, the track glows red over the gap, and a via that
changes reference without a return via gets a red disc on each layer it joins.

Marks are one object per copper layer ("KLS <layer> highlight return path"), drawn
by the tracks group like the selection highlight, wider and a little higher, so
they show over it. The panel's Return-path check box hides them.
"""

import bpy
import numpy as np

from . import transform
from .highlight import hide_copy, show_copy
from .objects import owned_object, set_modifier, write_attribute
from .placement import copper_placement, outward
from .state import board

HALO_M = 0.1e-3  # each side beyond the track (or via land)
LIFT_M = 2e-6  # above the copper's outer surface; the selection highlight sits at 1 um
TAG = "kls_return_path"


def enabled():
    return bool(getattr(bpy.context.scene, "kileido_return_path", True))


def apply_return_path(header, arrays):
    """The bridge's latest check (it sends one whenever the issues change)."""
    board.return_path = {
        "issues": list(header.get("issues", ())), "nets": list(header.get("nets", ())),
        "layers": list(header.get("layers", ())), "error": header.get("error", ""),
        "elapsed_ms": header.get("elapsed_ms"),
        "mark": np.array(arrays.get("mark", np.empty((0, 5))), dtype=np.int64).reshape(-1, 5),
        "mark_issue": np.array(arrays.get("mark_issue", ()), dtype=np.int32),
        "mark_layer": np.array(arrays.get("mark_layer", ()), dtype=np.int32),
    }
    refresh()


def refresh():
    """Rebuild the marks from the last check; hidden while the check box is off.
    Skipped inside a snapshot: `snapshot_end` calls it once."""
    if board.collection is None or board.in_snapshot:
        return
    data = board.return_path
    wanted = {}
    if enabled() and data:
        for index, layer in enumerate(data["layers"]):
            chosen = data["mark_layer"] == index
            if chosen.any():
                wanted[layer] = chosen
    for obj in tuple(board.collection.all_objects):
        if obj.get(TAG) and obj[TAG] not in wanted:
            hide_copy(obj)
    for layer, chosen in wanted.items():
        _draw(layer, data["mark"][chosen], data["mark_issue"][chosen])


def _draw(layer, marks, issue):
    obj = owned_object(f"KLS {layer} highlight return path")
    obj[TAG] = layer
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
    widths = marks[:, 4].astype(np.float64) * 1e-9 + 2 * HALO_M
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
    show_copy(obj)


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
