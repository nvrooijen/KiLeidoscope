"""Footprint frames, their placeholder envelopes, and the model parts bound to them.

Each footprint is a hidden Empty at its KiCad placement. Components without a
bound 3D model show a KiCad-bounds envelope; bound model parts follow the Empty.
"""

import math

from mathutils import Matrix

from . import models, transform
from .objects import owned_object, set_modifier, set_visible, single_point
from . import state
from .state import board

HIDDEN_EMPTY_SIZE_M = 1e-4  # Blender 5.1's minimum Empty display size
PLACEHOLDER_LIFT_M = 0.0002  # envelopes start just off the board face


def footprint_name(footprint_id):
    return f"KLS footprint {footprint_id}"


def placeholder_name(footprint_id):
    return f"KLS footprint placeholder {footprint_id}"


def oriented_placeholder_size(bbox_nm, angle_rad):
    """Estimate a rotated rectangle from KiCad's world-axis footprint bounds.

    A 45-degree AABB cannot reveal its original aspect ratio. Use a square in
    that singular case, which gives a correctly rotated envelope without
    claiming the placeholder is an exact package body.
    """
    width = max(0.0, bbox_nm[2] * 1e-9)
    height = max(0.0, bbox_nm[3] * 1e-9)
    cosine = abs(math.cos(angle_rad))
    sine = abs(math.sin(angle_rad))
    square = max(0.0001, (width + height) / (2 * (cosine + sine)))
    determinant = cosine * cosine - sine * sine
    if abs(determinant) < 0.05:
        return square, square
    local_width = (width * cosine - height * sine) / determinant
    local_height = (height * cosine - width * sine) / determinant
    if local_width <= 0 or local_height <= 0:
        return square, square
    return max(0.0001, local_width), max(0.0001, local_height)


def apply(header):
    """The complete footprint list (protocol.footprints_message)."""
    records = header["footprints"]
    for record in records:
        _apply_record(record)
    active_ids = {record["id"] for record in records}
    _retire_missing(active_ids, {placeholder_name(record["id"]) for record in records
                                 if record.get("bbox_nm") is not None})


def _apply_record(record):
    footprint_id = record["id"]
    name = footprint_name(footprint_id)
    bbox = record.get("bbox_nm")
    box_name = placeholder_name(footprint_id) if bbox is not None else None
    paths = list(record.get("model_paths", ()))
    model_visible = list(record.get("model_visible", ()))
    existing = board.collection.all_objects.get(name)
    if (existing is not None and footprint_id in board.model_bound and
            list(existing.get("kls_model_paths", ())) != paths):
        _unbind_models(footprint_id)  # new model definitions: the next export rebinds
    signature = (record["x"], record["y"], record["rot"], record["side"],
                 record["ref"], tuple(paths), tuple(model_visible),
                 tuple(bbox) if bbox is not None else None,
                 tuple(board.origin_nm), board.thickness_m)
    if board.footprint_state.get(footprint_id) == signature and _unchanged(existing, box_name):
        _keep(existing, box_name, footprint_id)
        return
    footprint, world = _place_footprint(name, record, paths, model_visible)
    if bbox is not None:
        # Like KiCad's 3D viewer: no box for a footprint without a declared model
        # (fiducials, test points, logos) or with every model hidden.
        all_hidden = not paths or (bool(model_visible) and not any(model_visible))
        _place_placeholder(box_name, record, all_hidden)
    if board.in_snapshot:
        board.touched.update(board.model_objects_by_fp.get(footprint_id, ()))
    _move_models(footprint_id, footprint, world)
    board.footprint_state[footprint_id] = signature


def _unchanged(existing, box_name):
    """The objects of an unchanged record still exist."""
    return existing is not None and (box_name is None or box_name in board.collection.all_objects)


def _keep(footprint, box_name, footprint_id):
    set_visible(footprint, False)
    footprint["kls_active"] = 1
    board.touched.add(footprint.name)
    if box_name is not None:
        board.touched.add(box_name)
    if board.in_snapshot:
        board.touched.update(board.model_objects_by_fp.get(footprint_id, ()))


def _unbind_models(footprint_id):
    state.log(f"models unbound from footprint {footprint_id}: its model definitions changed")
    board.model_bound.discard(footprint_id)
    for model_name in board.model_objects_by_fp.pop(footprint_id, ()):
        model_obj = board.collection.all_objects.get(model_name)
        if model_obj is not None:
            set_visible(model_obj, False)


def _place_footprint(name, record, paths, model_visible):
    """The footprint's hidden Empty, and its world matrix (valid before a depsgraph update)."""
    obj = owned_object(name, "EMPTY")
    xy = transform.xy_m([[record["x"], record["y"]]], board.origin_nm)[0]
    bottom = record["side"] == "bottom"
    obj.location = (float(xy[0]), float(xy[1]), 0 if bottom else board.thickness_m)
    # KiCad angles are counter-clockwise on screen for both sides; with Y flipped
    # into Blender that is +angle about Z. Bottom footprints are mirrored in local
    # Y (pad 1 of a bottom 0402 stays at x = -0.51 mm) and face down: scale (1, -1, -1).
    # Both measured on a real board.
    obj.rotation_euler = (0, 0, float(record["rot"]))
    obj.scale = (1, -1, -1) if bottom else (1, 1, 1)
    world = (Matrix.Translation(obj.location) @ obj.rotation_euler.to_matrix().to_4x4() @
             Matrix.Diagonal((*obj.scale, 1.0)))
    obj["kls_id"] = record["id"]
    obj["kls_reference"] = record["ref"]
    obj["kls_side"] = record["side"]
    obj["kls_model_paths"] = paths
    obj["kls_model_visible"] = model_visible
    obj["kls_footprint"] = 1
    obj["kls_active"] = 1
    # An Empty's default 1 m axes dwarf a millimetre-scale PCB and appear
    # as black lines throughout the viewport when Blender Extras is on.
    obj.empty_display_size = HIDDEN_EMPTY_SIZE_M
    set_visible(obj, False)
    board.touched.add(obj.name)
    return obj, world


def _place_placeholder(name, record, all_hidden):
    bbox = record["bbox_nm"]
    box = owned_object(name)
    box["kls_footprint_placeholder"] = 1
    box["kls_footprint_id"] = record["id"]
    single_point(box.data)
    center = transform.xy_m([[bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2]], board.origin_nm)[0]
    bottom = record["side"] == "bottom"
    box.location = (float(center[0]), float(center[1]),
                    -PLACEHOLDER_LIFT_M if bottom else board.thickness_m + PLACEHOLDER_LIFT_M)
    box.rotation_euler = (0, 0, float(record["rot"]))
    box.scale = (1, 1, 1)
    width, height = oriented_placeholder_size(bbox, float(record["rot"]))
    box["kls_placeholder_width_m"] = width
    box["kls_placeholder_height_m"] = height
    set_modifier(box, board.groups["footprint_placeholder"], "footprint_placeholder",
                 {"Width": width, "Height": height})
    set_visible(box, not (all_hidden or record["id"] in board.model_bound))
    board.touched.add(name)


def _move_models(footprint_id, footprint, world):
    """Bound model parts keep their local frame; they follow the moved footprint."""
    for model_name in board.model_objects_by_fp.get(footprint_id, ()):
        model_obj = board.collection.all_objects.get(model_name)
        if model_obj is None:
            continue
        values = model_obj.get("kls_model_local_matrix")
        if values is not None and len(values) == 16:
            model_obj.matrix_world = world @ Matrix(tuple(values[row * 4:row * 4 + 4] for row in range(4)))
        set_visible(model_obj, models.model_is_visible(footprint, model_obj))


def _retire_missing(active_ids, active_placeholders):
    """Hide the objects of footprints that left the board, and forget their models."""
    for footprint_id in set(board.footprint_state) - active_ids:
        del board.footprint_state[footprint_id]
    gone = set(board.model_bound) - active_ids
    if gone:
        state.log(f"{len(gone)} bound footprints left the board: models forgotten")
    for footprint_id in gone:
        board.model_bound.discard(footprint_id)
        board.model_objects_by_fp.pop(footprint_id, None)
    active = {footprint_name(footprint_id) for footprint_id in active_ids}
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_footprint") == 1 and obj.name not in active:
            obj["kls_active"] = 0
            set_visible(obj, False)
        if obj.get("kls_footprint_placeholder") == 1 and obj.name not in active_placeholders:
            obj.data.clear_geometry()
            set_visible(obj, False)
        if obj.get("kls_model_fp_id") is not None and obj.get("kls_model_fp_id") not in active_ids:
            set_visible(obj, False)
