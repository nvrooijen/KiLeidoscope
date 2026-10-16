"""Component objects' Outliner names, and finding those objects by footprint id.

A footprint's objects are named after what KiCad shows: its frame Empty "R12", its
placeholder box "R12 · R_0603_1608Metric (box)" and its model parts "R12 ·
R_0603_1608Metric", "R12 · R_0603_1608Metric 2". The names follow a changed
reference, so code finds the objects by the ids they carry, never by name.
"""

import re
from pathlib import PureWindowsPath

import bpy
from mathutils import Matrix

from .state import board

NAME_BYTES = 63  # Blender's longest object name
_SUFFIX = re.compile(r"\.\d{3,}$")  # Blender's ".001" on a taken name

FRAME = "frame"
PLACEHOLDER = "placeholder"
MODEL = "model"


def label(reference, model_paths):
    """"R12 · R_0603_1608Metric": the reference and the first 3D model's file name."""
    stem = next((PureWindowsPath(path).stem for path in model_paths if path), "")
    reference = reference or "?"
    return f"{reference} · {stem}" if stem else reference


def frame_name(reference):
    return _fit(reference or "?")


def placeholder_name(reference, model_paths):
    return _fit(label(reference, model_paths), " (box)")


def model_name(reference, model_paths, part):
    return _fit(label(reference, model_paths), "" if part == 0 else f" {part + 1}")


def _fit(name, tail=""):
    """`name` + `tail` in Blender's name length (a long model name gives way)."""
    room = NAME_BYTES - len(tail.encode())
    return name.encode()[:room].decode(errors="ignore") + tail


def key_of(obj):
    """(FRAME | PLACEHOLDER, footprint id) or (MODEL, footprint id, part), or None."""
    if obj.get("kls_footprint") == 1:
        return FRAME, obj.get("kls_id")
    if obj.get("kls_footprint_placeholder") == 1:
        return PLACEHOLDER, obj.get("kls_footprint_id")
    footprint_id = obj.get("kls_model_fp_id")
    if footprint_id is None:
        return None
    part = obj.get("kls_model_part")
    if part is None:  # saved before parts had labels: "KLS model <id> <part>"
        tail = obj.name.rpartition(" ")[2]
        part = int(tail) if tail.isdigit() else -1
    return MODEL, footprint_id, int(part)


def find(*key):
    """The board's object for `key` (see `key_of`), or None."""
    objects = board.collection.all_objects
    pointer = board.collection.as_pointer()
    if board.component_names_of != pointer:
        _index(pointer)
    obj = objects.get(board.component_names.get(key, ""))
    if obj is not None and key_of(obj) == key:
        return obj
    if key in board.component_names:  # renamed or removed since (the Outliner, a view-only board)
        _index(pointer)
        obj = objects.get(board.component_names.get(key, ""))
    return obj


def remember(obj):
    """After creating or renaming a component object."""
    key = key_of(obj)
    if key is not None:
        board.component_names[key] = obj.name


def rename(obj, name):
    """Give `obj` its label. Blender adds ".001" to a taken name (two parts on one
    reference, R12 and R13 swapped): kept until the plain name is free."""
    stripped = _SUFFIX.sub("", obj.name)
    suffixed = stripped != obj.name and name.startswith(stripped)
    if obj.name != name and not (suffixed and name in bpy.data.objects):
        obj.name = name
    remember(obj)


def attach(obj, frame, local):
    """Parent `obj` to its footprint's frame at `local` (in the frame's axes): the Outliner
    shows it inside "R12", and it follows the frame (a move, a fold) by itself."""
    if obj.parent != frame:
        obj.parent = frame
        if obj.animation_data is not None:  # a fold keyed on it before parts had frames
            obj.animation_data_clear()
        for key in ("kls_flat_matrix", "kls_folded_matrix"):
            obj.pop(key, None)
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_basis = local


def _index(pointer):
    board.component_names = {}
    board.component_names_of = pointer
    for obj in board.collection.all_objects:
        key = key_of(obj)
        if key is not None:
            board.component_names[key] = obj.name
