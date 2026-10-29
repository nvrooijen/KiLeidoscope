"""Where each board stands, kept in a file of its own (Save arrangement / Open arrangement):
a USD text file (.usda) any USD tool reads. Millimetres (metersPerUnit 0.001), Z up, one Xform
per board placing that board's KiCad frame (x, -y, z up, z 0 at its bottom face: poses.py)
with a translate and an XYZ rotation, and the board's .kicad_pcb as an asset path relative to
the file. KiCad stays the source of every board: the file holds no geometry. Our own values
sit under a "kileidoscope:" namespace. Parts marked Mated (collisions.py) are a Scope per
pair under "Boards/Mates": a relationship to each board's prim and the part's reference.

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
from .state import SLOTS, board, boards, same_file, using

KEY = "kls_arrangement"  # on the scene: the file opened and its boards (JSON), see `_entries`
VERSION = 1


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
    """Write the boards shown to `filepath` (.usda), and the boards an opened arrangement still
    waits for, where it puts them (a file saved over keeps them); the number written."""
    folder = os.path.dirname(os.path.abspath(filepath))
    lines = ["#usda 1.0", "(", "    customLayerData = {", "        dictionary kileidoscope = {",
             f"            int version = {VERSION}", "        }", "    }", '    defaultPrim = "Boards"',
             '    doc = "KiLeidoscope arrangement: where each board stands. The boards are KiCad files."',
             "    metersPerUnit = 0.001", '    upAxis = "Z"', ")", "", 'def Xform "Boards"', "{"]
    from . import collisions  # it imports modules that import this one
    entries = [*_shown_entries(), *waiting_entries()]
    taken, prims = set(), {}
    for entry in entries:
        prim = _prim_name(entry["name"] or entry["path"], taken)
        prims[collisions.board_key(entry["path"], entry["name"])] = prim
        lines += ["", f'    def Xform "{prim}"', "    {",
                  f"        asset kileidoscope:board = @{_asset(entry['path'], folder)}@",
                  f"        string kileidoscope:name = {_string(entry['name'])}",
                  f"        bool kileidoscope:hidden = {int(entry['hidden'])}",
                  "        double3 xformOp:translate = ({})".format(
                      ", ".join(_number(v * 1e3) for v in entry["translation"])),
                  "        double3 xformOp:rotateXYZ = ({})".format(
                      ", ".join(_number(math.degrees(a)) for a in entry["angles"])),
                  '        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]', "    }"]
    mates = [pair for pair in sorted(collisions.mated()) if all(key in prims for key, _ in pair)]
    if mates:
        lines += ["", '    def Scope "Mates"', "    {"]
        for number, ((key_a, ref_a), (key_b, ref_b)) in enumerate(mates, start=1):
            lines += [f'        def Scope "Mate_{number}"', "        {",
                      f"            rel kileidoscope:first = </Boards/{prims[key_a]}>",
                      f"            string kileidoscope:firstReference = {_string(ref_a)}",
                      f"            rel kileidoscope:second = </Boards/{prims[key_b]}>",
                      f"            string kileidoscope:secondReference = {_string(ref_b)}", "        }"]
        lines += ["    }"]
    lines += ["}", ""]
    Path(filepath).write_text("\n".join(lines), encoding="utf-8")
    bpy.context.scene[KEY] = json.dumps({"file": os.path.abspath(filepath), "boards": entries})
    return len(prims)


def _shown_entries():
    """The boards shown, as `read` gives a file's: in slot order."""
    return [{"path": state.board_path, "name": state.board_name, "translation": list(state.pose[0]),
             "angles": list(state.pose[1]), "hidden": state.hidden}
            for slot in SLOTS if (state := boards[slot]).loaded and state.pose is not None]


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
    found, mates = [], []
    for prim in stage.Traverse():
        first, second = prim.GetRelationship("kileidoscope:first"), prim.GetRelationship("kileidoscope:second")
        if first and second and first.GetTargets() and second.GetTargets():
            mates.append((str(first.GetTargets()[0]), prim.GetAttribute("kileidoscope:firstReference").Get() or "",
                          str(second.GetTargets()[0]), prim.GetAttribute("kileidoscope:secondReference").Get() or ""))
            continue
        attribute = prim.GetAttribute("kileidoscope:board")
        if not attribute or not prim.IsA(UsdGeom.Xformable):
            continue
        asset = attribute.Get()
        name = prim.GetAttribute("kileidoscope:name")
        hidden = prim.GetAttribute("kileidoscope:hidden")
        matrix = np.array(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T
        matrix[:3, 3] *= metres
        found.append((str(prim.GetPath()), asset.path if asset is not None else "", name.Get() if name else "",
                      bool(hidden.Get()) if hidden else False, matrix))
    return found, mates


_PRIM = re.compile(r'def Xform "(?P<name>[^"]*)"\s*\{(?P<body>[^{}]*)\}', re.S)
_MATE = re.compile(r'def Scope "[^"]*"\s*\{(?P<body>[^{}]*)\}', re.S)


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
        found.append((f"/Boards/{match['name']}", asset[1], json.loads(name[1]) if name else "",
                      bool(hidden) and hidden[1] in ("1", "true"), matrix))
    mates = []
    for match in _MATE.finditer(text):
        body = match["body"]
        targets = re.findall(r"rel kileidoscope:(first|second)\s*=\s*<([^>]*)>", body)
        refs = dict(re.findall(r'string kileidoscope:(first|second)Reference\s*=\s*("(?:[^"\\]|\\.)*")', body))
        targets = dict(targets)
        if "first" in targets and "second" in targets:
            mates.append((targets["first"], json.loads(refs.get("first", '""')),
                          targets["second"], json.loads(refs.get("second", '""'))))
    return found, mates


def read(filepath):
    """[{"path", "name", "translation", "angles", "hidden"}] of the boards a file places; paths
    made absolute from the file's folder."""
    return read_all(filepath)[0]


def read_all(filepath):
    """(the boards a file places as `read` gives them, the parts it marks Mated:
    [[board, reference, board, reference]] by collisions.board_key)."""
    from . import collisions  # it imports modules that import this one
    try:
        rows, mates = _read_pxr(filepath)
    except ImportError:
        rows, mates = _read_text(filepath)
    folder = os.path.dirname(os.path.abspath(filepath))
    entries, keys = [], {}
    for prim, path, name, hidden, matrix in rows:
        translation, angles = _matrix_pose(matrix)
        absolute = os.path.normpath(os.path.join(folder, path)) if path else ""
        entries.append({"path": absolute, "name": name, "translation": list(translation),
                        "angles": list(angles), "hidden": hidden})
        keys[prim] = collisions.board_key(absolute, name)
    pairs = [[keys[a], ref_a, keys[b], ref_b] for a, ref_a, b, ref_b in mates if a in keys and b in keys]
    return entries, pairs


def load(filepath):
    """Open an arrangement: the boards shown that it names are placed now, the others as they
    come. (placed now, waiting)"""
    entries, pairs = read_all(filepath)
    bpy.context.scene[KEY] = json.dumps({"file": os.path.abspath(filepath), "boards": entries})
    from . import collisions  # it imports modules that import this one
    for pair in pairs:  # the file's marks join those already made
        collisions.set_mated(*pair)
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


_opened = {"text": None, "data": {}}  # the scene's KEY as last parsed: the panel asks several times per draw


def _read():
    """The opened arrangement as the scene keeps it ({"file", "boards"}; {} when none), parsed
    again only when the scene's text changed. Callers read it; they never change it."""
    text = bpy.context.scene.get(KEY) or "{}"
    if text != _opened["text"]:
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        _opened.update(text=text, data=data if isinstance(data, dict) else {})
    return _opened["data"]


def _entries():
    return _read().get("boards") or []


def wanted():
    """The current board's place in the opened arrangement (by file, else by name), or None."""
    for entry in _entries():
        if same_file(entry["path"], board.board_path) or \
                (not (entry["path"] and board.board_path) and entry["name"] and entry["name"] == board.board_name):
            return entry
    return None


def _matches(entry, path, name):
    return same_file(entry["path"], path) or (not (entry["path"] and path) and bool(entry["name"])
                                               and entry["name"] == name)


def waiting_entries():
    """The opened arrangement's boards that are not shown: each waits for Open in Blender."""
    shown = [state for state in boards.values() if state.loaded]
    return [entry for entry in _entries()
            if not any(_matches(entry, state.board_path, state.board_name) for state in shown)]


def waiting():
    """Names of the opened arrangement's boards that are not shown."""
    return [entry["name"] or Path(entry["path"]).name for entry in waiting_entries()]


def expects(path, name):
    """A board the opened arrangement waits for (Open in Blender in its KiCad: no question)."""
    return any(_matches(entry, path, name) for entry in waiting_entries())


def forget(path, name):
    """Stop waiting for a board: it leaves the opened arrangement's list (not the file)."""
    opened = dict(_read())
    opened["boards"] = [entry for entry in opened.get("boards") or [] if not _matches(entry, path, name)]
    bpy.context.scene[KEY] = json.dumps(opened)


def file_name():
    """The opened (or saved) arrangement's file name, or ""."""
    return Path(_read().get("file") or "").name
