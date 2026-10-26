"""Where each board stands, kept in a file of its own (Save arrangement / Open arrangement):
a USD text file (.usda) any USD tool reads. Millimetres (metersPerUnit 0.001), Z up, one Xform
per board placing that board's KiCad frame (x, -y, z up, z 0 at its bottom face: poses.py)
with a translate and an XYZ rotation, and the board's .kicad_pcb as an asset path relative to
the file. KiCad stays the source of every board: the file holds no geometry. Our own values
sit under a "kileidoscope:" namespace.

Saving writes every board shown. Opening places the boards shown now that it names (by file,
else by name) and keeps the others on the scene: each is placed as it comes (poses.follow_board
asks `wanted`).
"""

import json
import math
import os
import re
from pathlib import Path

import bpy
import numpy as np

from . import poses
from .state import SLOTS, board, boards, using

KEY = "kls_arrangement"  # on the scene: the file opened and its boards (JSON), see `_entries`
VERSION = 1


def _same_file(a, b):
    return bool(a and b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# --- Saving ---------------------------------------------------------------------------------

def _prim_name(name, taken):
    stem = re.sub(r"\W", "_", Path(name).stem or "Board") or "Board"
    stem = "_" + stem if stem[0].isdigit() else stem
    unique, count = stem, 1
    while unique in taken:
        count += 1
        unique = f"{stem}_{count}"
    taken.add(unique)
    return unique


def _string(text):
    return json.dumps(text, ensure_ascii=False)  # a USD string: the same quoting and escapes


def _asset(path, folder):
    """`path` relative to `folder` where it can be (another drive keeps it whole), forward slashes."""
    if not path:
        return ""
    try:
        path = os.path.relpath(path, folder)
    except ValueError:
        pass
    return path.replace("\\", "/")


def _number(value):
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def save(filepath):
    """Write the boards shown to `filepath` (.usda); the number written."""
    folder = os.path.dirname(os.path.abspath(filepath))
    lines = ["#usda 1.0", "(", "    customLayerData = {", "        dictionary kileidoscope = {",
             f"            int version = {VERSION}", "        }", "    }", '    defaultPrim = "Boards"',
             '    doc = "KiLeidoscope arrangement: where each board stands. The boards are KiCad files."',
             "    metersPerUnit = 0.001", '    upAxis = "Z"', ")", "", 'def Xform "Boards"', "{"]
    taken, count = set(), 0
    for slot in SLOTS:
        state = boards[slot]
        if not state.loaded or state.pose is None:
            continue
        translation, angles = state.pose
        lines += ["", f'    def Xform "{_prim_name(state.board_name, taken)}"', "    {",
                  f"        asset kileidoscope:board = @{_asset(state.board_path, folder)}@",
                  f"        string kileidoscope:name = {_string(state.board_name)}",
                  f"        bool kileidoscope:hidden = {int(state.hidden)}",
                  "        double3 xformOp:translate = ({})".format(", ".join(_number(v * 1e3) for v in translation)),
                  "        double3 xformOp:rotateXYZ = ({})".format(", ".join(_number(math.degrees(a)) for a in angles)),
                  '        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]', "    }"]
        count += 1
    lines += ["}", ""]
    Path(filepath).write_text("\n".join(lines), encoding="utf-8")
    bpy.context.scene[KEY] = json.dumps({"file": os.path.abspath(filepath), "boards": _shown_entries(folder)})
    return count


def _shown_entries(folder):
    return [{"path": state.board_path, "name": state.board_name, "translation": list(state.pose[0]),
             "angles": list(state.pose[1]), "hidden": state.hidden}
            for state in boards.values() if state.loaded and state.pose is not None]


# --- Opening --------------------------------------------------------------------------------

def _matrix_pose(matrix):
    """((x, y, z) metres, XYZ angles) of a rigid 4x4 matrix in metres."""
    turned = matrix[:3, :3] / np.linalg.norm(matrix[:3, :3], axis=0)  # no scale: only its turn
    return tuple(float(v) for v in matrix[:3, 3]), poses.euler(turned)


def _read_pxr(filepath):
    from pxr import Usd, UsdGeom  # Blender's own USD
    stage = Usd.Stage.Open(str(filepath))
    if stage is None:
        raise ValueError("Not a USD file")
    metres = UsdGeom.GetStageMetersPerUnit(stage)
    found = []
    for prim in stage.Traverse():
        attribute = prim.GetAttribute("kileidoscope:board")
        if not attribute or not prim.IsA(UsdGeom.Xformable):
            continue
        asset = attribute.Get()
        name = prim.GetAttribute("kileidoscope:name")
        hidden = prim.GetAttribute("kileidoscope:hidden")
        matrix = np.array(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T
        matrix[:3, 3] *= metres
        found.append((asset.path if asset is not None else "", name.Get() if name else "",
                      bool(hidden.Get()) if hidden else False, matrix))
    return found


_PRIM = re.compile(r'def Xform "[^"]*"\s*\{(?P<body>[^{}]*)\}', re.S)


def _read_text(filepath):
    """Without Blender's USD module: the file as `save` writes it."""
    text = Path(filepath).read_text(encoding="utf-8")
    if not text.startswith("#usda"):
        raise ValueError("Not a USD text file")
    metres = float((re.search(r"metersPerUnit\s*=\s*([\d.eE+-]+)", text) or [None, 1.0])[1])
    found = []
    for match in _PRIM.finditer(text):
        body = match["body"]
        asset = re.search(r"asset kileidoscope:board\s*=\s*@([^@]*)@", body)
        if asset is None:
            continue
        name = re.search(r'string kileidoscope:name\s*=\s*("(?:[^"\\]|\\.)*")', body)
        hidden = re.search(r"bool kileidoscope:hidden\s*=\s*(\w+)", body)

        def vector(op):
            found_op = re.search(r"xformOp:" + op + r"\s*=\s*\(([^)]*)\)", body)
            return [float(v) for v in found_op[1].split(",")] if found_op else [0.0, 0.0, 0.0]
        matrix = np.eye(4)
        matrix[:3, :3] = poses.rotation([math.radians(v) for v in vector("rotateXYZ")])
        matrix[:3, 3] = np.array(vector("translate")) * metres
        found.append((asset[1], json.loads(name[1]) if name else "",
                      bool(hidden) and hidden[1] in ("1", "true"), matrix))
    return found


def read(filepath):
    """[{"path", "name", "translation", "angles", "hidden"}] of the boards a file places; paths
    made absolute from the file's folder."""
    try:
        rows = _read_pxr(filepath)
    except ImportError:
        rows = _read_text(filepath)
    folder = os.path.dirname(os.path.abspath(filepath))
    entries = []
    for path, name, hidden, matrix in rows:
        translation, angles = _matrix_pose(matrix)
        absolute = os.path.normpath(os.path.join(folder, path)) if path else ""
        entries.append({"path": absolute, "name": name, "translation": list(translation),
                        "angles": list(angles), "hidden": hidden})
    return entries


def load(filepath):
    """Open an arrangement: the boards shown that it names are placed now, the others as they
    come. (placed now, waiting)"""
    entries = read(filepath)
    bpy.context.scene[KEY] = json.dumps({"file": os.path.abspath(filepath), "boards": entries})
    placed = 0
    from . import slots  # slots imports modules that import this one
    for slot in SLOTS:
        if not boards[slot].loaded:
            continue
        with using(slot):
            entry = wanted()
            if entry is not None:
                poses.set_pose(entry["translation"], entry["angles"])
                placed += 1
        if entry is not None and entry["hidden"] != boards[slot].hidden:
            slots.set_hidden(slot, entry["hidden"])
    return placed, len(waiting())


def _entries():
    try:
        return json.loads(bpy.context.scene.get(KEY) or "{}").get("boards") or []
    except ValueError:
        return []


def wanted():
    """The current board's place in the opened arrangement (by file, else by name), or None."""
    for entry in _entries():
        if _same_file(entry["path"], board.board_path) or \
                (not (entry["path"] and board.board_path) and entry["name"] and entry["name"] == board.board_name):
            return entry
    return None


def waiting():
    """Names of the opened arrangement's boards that are not shown."""
    shown = [state for state in boards.values() if state.loaded]
    return [entry["name"] or Path(entry["path"]).name for entry in _entries()
            if not any(_same_file(entry["path"], state.board_path) or
                       (entry["name"] and entry["name"] == state.board_name) for state in shown)]


def file_name():
    """The opened (or saved) arrangement's file name, or ""."""
    try:
        return Path(json.loads(bpy.context.scene.get(KEY) or "{}").get("file") or "").name
    except ValueError:
        return ""
