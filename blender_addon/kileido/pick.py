"""Click picking: which KiCad item is under the mouse (for selecting it in KiCad).

A ray from the view passes overlays and highlights (mask, silkscreen, glow boxes,
solder, drill walls) and stops at the first KiCad item. The hit object says what
kind of item it is; its source mesh (the frames KiLeidoscope received) says which one:
point-to-segment distance for tracks, point in polygon for pads, nearest via.
Only the live board picks: view-only boards are not in KiCad. With the board cut open
(cut.py), the ray only counts where it runs through the side that is shown.
"""

import numpy as np

from . import cut
from .objects import OUTLINE, read_attribute, read_coordinates, read_edges
from .state import board

SKIP_PREFIXES = ("KLS overlay", "KLS footprint highlight")
TOLERANCE_M = 5e-6
STEP_M = 1e-7  # past a hit that is not an item, to the next one
FAR_M = 1e6  # a ray without an end


def _local_xy(obj, location):
    return np.array(tuple(obj.matrix_world.inverted() @ location)[:2])


def _track_at(obj, point):
    mesh = obj.data
    count = len(mesh.vertices)
    if count < 2:
        return None
    coords = read_coordinates(mesh)[:, :2].astype(np.float64)
    widths = read_attribute(mesh, "width", np.float32)
    items = read_attribute(mesh, "item", np.int32)
    start, end = coords[0::2], coords[1::2]
    span = end - start
    length = np.maximum((span * span).sum(axis=1), 1e-30)
    t = np.clip(((point - start) * span).sum(axis=1) / length, 0.0, 1.0)
    distance = np.hypot(*(start + t[:, None] * span - point).T)
    margin = distance - widths[0::2] / 2
    best = int(np.argmin(margin))
    return int(items[2 * best]) if margin[best] <= TOLERANCE_M else None


def _pad_at(obj, point):
    """Even-odd point in polygon over each pad's rings (its hole rings included)."""
    mesh = obj.data
    count = len(mesh.vertices)
    if not count or not len(mesh.edges):
        return None
    coords = read_coordinates(mesh)[:, :2].astype(np.float64)
    items = read_attribute(mesh, "item", np.int32)
    edges = read_edges(mesh)
    a, b = coords[edges[:, 0]], coords[edges[:, 1]]
    straddles = (a[:, 1] > point[1]) != (b[:, 1] > point[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        cross_x = a[:, 0] + (point[1] - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1])
    crossing = straddles & (point[0] < cross_x)
    counts = np.bincount(items[edges[:, 0]][crossing], minlength=items.max() + 1)
    inside = np.flatnonzero(counts % 2 == 1)
    return int(inside[0]) if len(inside) else None


def _via_at(obj, point):
    mesh = obj.data
    count = len(mesh.vertices)
    if not count:
        return None
    diameters = read_attribute(mesh, "diameter", np.float32)
    distance = np.hypot(*(read_coordinates(mesh)[:, :2] - point).T)
    best = int(np.argmin(distance - diameters / 2))
    return best if distance[best] <= diameters[best] / 2 + TOLERANCE_M else None


def _item(obj, location):
    """(KiCad id or None, stop): stop=True ends the ray even without an item."""
    ids = list(obj.get("kls_ids", ()))
    placement = obj.get("kls_copper")
    if placement:
        kind = placement[1]
        if kind == "zones":  # the ray hit the fill itself (holes are open), so it is this pour
            return (ids[0] if ids else None), True
        finder = _track_at if kind == "tracks" else _pad_at
        index = finder(obj, _local_xy(obj, location))
        return (ids[index] if index is not None and index < len(ids) else None), index is not None
    if obj.name == "KLS vias":
        index = _via_at(obj, _local_xy(obj, location))
        return (ids[index] if index is not None and index < len(ids) else None), index is not None
    if obj.get("kls_model_fp_id") is not None:
        return obj["kls_model_fp_id"], True
    if obj.get("kls_footprint_placeholder") == 1:
        return obj.get("kls_footprint_id"), True
    if obj.name == OUTLINE:
        return None, True
    return None, False  # overlays, highlights, solder, drill walls: look further


def describe(item_id):
    """"track", "via", "pad", "component" or "item", for the status bar."""
    collection = board.collection
    if collection is not None:
        for obj in tuple(collection.all_objects):
            if item_id in list(obj.get("kls_ids", ())):
                placement = obj.get("kls_copper")
                if placement:
                    return {"tracks": "a track", "pads": "a pad's component", "zones": "a pour"}.get(placement[1], "an item")
                if obj.name == "KLS vias":
                    return "a via"
    return "a component"


def item_at(scene, depsgraph, origin, direction, max_hits=32):
    """The KiCad id under a view ray, or None."""
    collection = board.collection
    if collection is None:
        return None
    direction = direction.normalized()
    span = cut.shown_span(scene, origin, direction)
    if span is None:
        return None
    start, end = span
    origin = origin + direction * start
    reach = FAR_M if end is None else end - start
    for _ in range(max_hits):
        if reach <= 0:
            return None
        hit, location, _normal, _index, obj, _matrix = scene.ray_cast(depsgraph, origin, direction, distance=reach)
        if not hit:
            return None
        if obj.name in collection.all_objects and not obj.name.startswith(SKIP_PREFIXES) and "highlight" not in obj.name:
            item, stop = _item(obj, location)
            if item is not None or stop:
                return item
        reach -= (location - origin).length + STEP_M
        origin = location + direction * STEP_M
    return None
