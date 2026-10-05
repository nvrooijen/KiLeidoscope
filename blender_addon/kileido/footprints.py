"""Footprint frames, their placeholder envelopes, and the model parts bound to them.

Each footprint is a tiny Empty at its KiCad placement. Components without a
bound 3D model show a KiCad-bounds envelope; it and the bound model parts are
the Empty's children, so they follow it and the Outliner shows them inside it.
"""

import math

from mathutils import Matrix

from . import components, models, transform
from .objects import new_owned, set_modifier, set_visible, single_point
from . import state
from .state import board

HIDDEN_EMPTY_SIZE_M = 1e-4  # Blender 5.1's minimum Empty display size
PLACEHOLDER_LIFT_M = 0.0002  # envelopes start just off the board face


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
    _retire_missing(active_ids, {record["id"] for record in records if record.get("bbox_nm") is not None})


def _apply_record(record):
    footprint_id = record["id"]
    bbox = record.get("bbox_nm")
    paths = list(record.get("model_paths", ()))
    model_visible = list(record.get("model_visible", ()))
    dnp = bool(record.get("dnp", False))
    existing = components.find(components.FRAME, footprint_id)
    box = components.find(components.PLACEHOLDER, footprint_id) if bbox is not None else None
    if (existing is not None and footprint_id in board.model_bound and
            list(existing.get("kls_model_paths", ())) != paths):
        _unbind_models(footprint_id)  # new model definitions: the next export rebinds
    signature = (record["x"], record["y"], record["rot"], record["side"],
                 record["ref"], tuple(paths), tuple(model_visible), dnp,
                 tuple(bbox) if bbox is not None else None,
                 tuple(board.origin_nm), board.thickness_m)
    if board.footprint_state.get(footprint_id) == signature and _unchanged(existing, bbox, box):
        _keep(existing, box, footprint_id)
        return
    footprint, world = _place_footprint(existing, record, paths, model_visible, dnp)
    if bbox is not None:
        _place_placeholder(box, record, paths, footprint, world)
    _move_models(footprint_id, footprint, world)  # renames them: touched after
    if board.in_snapshot:
        board.touched.update(board.model_objects_by_fp.get(footprint_id, ()))
    board.footprint_state[footprint_id] = signature


def _unchanged(existing, bbox, box):
    """The objects of an unchanged record still exist."""
    return existing is not None and (bbox is None or box is not None)


def _keep(footprint, box, footprint_id):
    set_visible(footprint, True)
    footprint["kls_active"] = 1
    board.touched.add(footprint.name)
    if box is not None:
        board.touched.add(box.name)
    if board.in_snapshot:
        board.touched.update(board.model_objects_by_fp.get(footprint_id, ()))


def _unbind_models(footprint_id):
    state.log(f"models unbound from footprint {footprint_id}: its model definitions changed")
    board.model_bound.discard(footprint_id)
    for model_name in board.model_objects_by_fp.pop(footprint_id, ()):
        model_obj = board.collection.all_objects.get(model_name)
        if model_obj is not None:
            set_visible(model_obj, False)


def _place_footprint(obj, record, paths, model_visible, dnp):
    """The footprint's Empty (`obj`, made if None), and its world matrix (valid before
    a depsgraph update)."""
    name = components.frame_name(record["ref"])
    obj = obj or new_owned(name, "EMPTY", "components")
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
    obj["kls_dnp"] = int(dnp)
    obj["kls_footprint"] = 1
    obj["kls_active"] = 1
    # An Empty's default 1 m axes dwarf a millimetre-scale PCB and appear
    # as black lines throughout the viewport when Blender Extras is on. Shown,
    # so its Outliner row's eye is open like its parts' (at this size it is unseen).
    obj.empty_display_size = HIDDEN_EMPTY_SIZE_M
    set_visible(obj, True)
    components.rename(obj, name)
    board.touched.add(obj.name)
    return obj, world


def _place_placeholder(box, record, paths, footprint, world):
    """The footprint's KiCad-bounds envelope (`box`, made if None)."""
    bbox = record["bbox_nm"]
    name = components.placeholder_name(record["ref"], paths)
    box = box or new_owned(name, "MESH", "components")
    box["kls_footprint_placeholder"] = 1
    box["kls_footprint_id"] = record["id"]
    components.rename(box, name)
    box["kls_side"] = record["side"]
    single_point(box.data)
    center = transform.xy_m([[bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2]], board.origin_nm)[0]
    bottom = record["side"] == "bottom"
    placed = (Matrix.Translation((float(center[0]), float(center[1]),
                                  -PLACEHOLDER_LIFT_M if bottom else board.thickness_m + PLACEHOLDER_LIFT_M)) @
              Matrix.Rotation(float(record["rot"]), 4, "Z"))
    components.attach(box, footprint, world.inverted() @ placed)
    width, height = oriented_placeholder_size(bbox, float(record["rot"]))
    box["kls_placeholder_width_m"] = width
    box["kls_placeholder_height_m"] = height
    set_modifier(box, board.groups["footprint_placeholder"], "footprint_placeholder",
                 {"Width": width, "Height": height})
    # Like KiCad's 3D viewer: no box for a footprint without a declared model
    # (fiducials, test points, logos) or with every model hidden.
    set_visible(box, models.placeholder_visible(footprint, record["id"]))
    board.touched.add(box.name)


def _move_models(footprint_id, footprint, world):
    """Bound model parts keep their local frame; they follow the moved footprint (and
    its reference, in their names)."""
    names = board.model_objects_by_fp.get(footprint_id, [])
    for index, model_name in enumerate(names):
        model_obj = board.collection.all_objects.get(model_name)
        if model_obj is None:
            continue
        components.rename(model_obj, components.model_name(
            footprint["kls_reference"], footprint["kls_model_paths"], model_obj.get("kls_model_part", index)))
        names[index] = model_obj.name
        values = model_obj.get("kls_model_local_matrix")
        if values is not None and len(values) == 16:
            components.attach(model_obj, footprint, Matrix(tuple(values[row * 4:row * 4 + 4] for row in range(4))))
        set_visible(model_obj, models.model_is_visible(footprint, model_obj))


def refresh_dnp():
    """The "DNP components" eye changed: show or hide the models and
    missing-model boxes of the footprints KiCad marks "Do not populate"."""
    if board.collection is None:
        return
    objects = board.collection.all_objects
    for footprint in tuple(objects):
        if footprint.get("kls_footprint") != 1 or footprint.get("kls_dnp") != 1:
            continue
        footprint_id = footprint.get("kls_id")
        for model_name in board.model_objects_by_fp.get(footprint_id, ()):
            model_obj = objects.get(model_name)
            if model_obj is not None:
                set_visible(model_obj, models.model_is_visible(footprint, model_obj))
        box = components.find(components.PLACEHOLDER, footprint_id)
        if box is not None:
            set_visible(box, models.placeholder_visible(footprint, footprint_id))


def _retire_missing(active_ids, placeholder_ids):
    """Hide the objects of footprints that left the board, and forget their models."""
    for footprint_id in set(board.footprint_state) - active_ids:
        del board.footprint_state[footprint_id]
    gone = set(board.model_bound) - active_ids
    if gone:
        state.log(f"{len(gone)} bound footprints left the board: models forgotten")
    for footprint_id in gone:
        board.model_bound.discard(footprint_id)
        board.model_objects_by_fp.pop(footprint_id, None)
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_footprint") == 1 and obj.get("kls_id") not in active_ids:
            obj["kls_active"] = 0
            set_visible(obj, False)
        if obj.get("kls_footprint_placeholder") == 1 and obj.get("kls_footprint_id") not in placeholder_ids:
            obj.data.clear_geometry()
            set_visible(obj, False)
        if obj.get("kls_model_fp_id") is not None and obj.get("kls_model_fp_id") not in active_ids:
            set_visible(obj, False)
