"""Read-only appearance metadata from a saved KiCad board and its local theme.

Geometry comes only from KiCad IPC. The board file (the bridge's live copy, else
the saved board) supplies the colours and finish that KiCad 10's IPC stackup
response omits; it is never modified here.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_RGB = re.compile(r"rgba?\(([^)]+)\)")
_LAYER = re.compile(r'\(layer\s+"([^"]+)"')
_COLOR = re.compile(r'\(color\s+"([^"]+)"\)')
_TYPE = re.compile(r'\(type\s+"([^"]*)"\)')
_MATERIAL = re.compile(r'\(material\s+"([^"]*)"\)')
_STACKUP = re.compile(r"\(stackup\b")
_COPPER_FINISH = re.compile(r'\(copper_finish\s+"([^"]*)"\)')
_EDGE_PLATING = re.compile(r"\(edge_plating\s+yes\)")  # Board Setup > Board Finish: Plated board edge

# KiCad 10 3d-viewer/3d_canvas/board_adapter.cpp: named stackup colours and their
# 3D-viewer values (sRGB bytes, alpha).  Stackup names outside these lists render
# as KiCad's COLOR4D() default, i.e. they are not overrides.
_SILK_COLORS = {
    "Not specified": (245, 245, 245, 1.0), "Green": (20, 51, 36, 1.0), "Red": (181, 19, 21, 1.0),
    "Blue": (2, 59, 162, 1.0), "Black": (11, 11, 11, 1.0), "White": (245, 245, 245, 1.0),
    "Purple": (32, 2, 53, 1.0), "Yellow": (194, 195, 0, 1.0),
}
_MASK_COLORS = {
    "Not specified": (20, 51, 36, 0.83), "Green": (20, 51, 36, 0.83),
    "Light Green": (91, 168, 12, 0.83), "Saturated Green": (13, 104, 11, 0.83),
    "Red": (181, 19, 21, 0.83), "Light Red": (210, 40, 14, 0.83), "Red/Orange": (239, 53, 41, 0.83),
    "Blue": (2, 59, 162, 0.83), "Light Blue 1": (54, 79, 116, 0.83),
    "Light Blue 2": (61, 85, 130, 0.83), "Green/Blue": (21, 70, 80, 0.83),
    "Black": (11, 11, 11, 0.83), "White": (245, 245, 245, 0.83), "Purple": (32, 2, 53, 0.83),
    "Light Purple": (119, 31, 91, 0.83), "Yellow": (194, 195, 0, 0.83),
}
_BOARD_COLORS = {
    "FR4 natural, dark": (51, 43, 22, 0.83), "FR4 natural": (109, 116, 75, 0.83),
    "PTFE natural": (252, 252, 250, 0.90), "Polyimide": (205, 130, 0, 0.68),
    "Phenolic natural": (92, 17, 6, 0.90), "Brown 1": (146, 99, 47, 0.83),
    "Brown 2": (160, 123, 54, 0.83),
}
_FINISH_COLORS = {"Copper": (184, 115, 50), "Gold": (178, 156, 0),
                  "Silver": (213, 213, 213), "Tin": (160, 160, 160)}
# board_adapter.cpp g_Default* (already 0..1), used when the theme lacks a key.
_VIEWER_DEFAULTS = {
    "board": [0.43, 0.45, 0.30, 0.90], "copper": [0.75, 0.61, 0.23, 1.0],
    "silkscreen_top": [0.94, 0.94, 0.94, 1.0], "silkscreen_bottom": [0.94, 0.94, 0.94, 1.0],
    "soldermask_top": [0.08, 0.20, 0.14, 0.83], "soldermask_bottom": [0.08, 0.20, 0.14, 0.83],
    "solderpaste": [0.50, 0.50, 0.50, 1.0],
}

# PCB Editor colour theme keys ("board" section) per layer name.
_EDITOR_COPPER_KEYS = {"F.Cu": "f", "B.Cu": "b", **{f"In{n}.Cu": f"in{n}" for n in range(1, 31)}}
_EDITOR_DRAWING_KEYS = {
    "F.SilkS": "f_silks", "B.SilkS": "b_silks",
    "F.Fab": "f_fab", "B.Fab": "b_fab",
    "Dwgs.User": "dwgs_user", "Cmts.User": "cmts_user",
    "Eco1.User": "eco1_user", "Eco2.User": "eco2_user",
    **{f"User.{number}": f"user_{number}" for number in range(1, 46)},
}

# Major.minor of the running KiCad; its settings live in a folder named after it.
_kicad_version = "10.0"


def set_kicad_version(version: str | None) -> None:
    """Set once the running KiCad reports its version; None keeps the current one."""
    global _kicad_version
    if version:
        _kicad_version = version


def _block(text: str, start: int) -> str:
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("Unclosed KiCad board section")


def _rgba(value: str | None):
    if not value:
        return None
    if value.startswith("#") and len(value) in (7, 9):
        octets = [int(value[index:index + 2], 16) for index in range(1, len(value), 2)]
        if len(octets) == 3:
            octets.append(255)
        return [channel / 255 for channel in octets]
    match = _RGB.fullmatch(value.strip())
    if match:
        fields = [float(field.strip()) for field in match.group(1).split(",")]
        if len(fields) in (3, 4):
            return [channel / 255 for channel in fields[:3]] + [fields[3] if len(fields) == 4 else 1.0]
    return None


def _saved_board_text(board_path: str) -> str:
    """The saved board file, or "" when there is none."""
    if not board_path:
        return ""
    try:
        return Path(board_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _saved_stackup_colors(text: str) -> dict:
    marker = _STACKUP.search(text)
    if marker is None:
        return {}
    colors = {}
    try:
        stackup = _block(text, marker.start())
        for layer in _LAYER.finditer(stackup):
            color = _COLOR.search(_block(stackup, layer.start()))
            if color is not None:
                colors[layer.group(1)] = color.group(1)
    except ValueError:  # a truncated file (read while KiCad saves it): no colours this time
        pass
    return colors


def _saved_dielectrics(text: str) -> list[dict]:
    """The saved stackup's dielectric layers, top first: {"type": "core" or "prepreg",
    "material": its material name or ""}. The IPC stackup leaves both out."""
    marker = _STACKUP.search(text)
    if marker is None:
        return []
    found = []
    try:
        stackup = _block(text, marker.start())
        for layer in _LAYER.finditer(stackup):
            block = _block(stackup, layer.start())
            kind = _TYPE.search(block)
            if kind is None or kind.group(1).casefold() not in ("core", "prepreg"):
                continue
            material = _MATERIAL.search(block)
            found.append({"type": kind.group(1).casefold(), "material": material.group(1) if material else ""})
    except ValueError:  # a truncated file (read while KiCad saves it): nothing this time
        return []
    return found


def _named(value: str | None, table: dict):
    if not value:
        return None
    if value.startswith("#"):
        return _rgba(value)
    entry = table.get(value)
    return None if entry is None else [channel / 255 for channel in entry[:3]] + [entry[3]]


def _finish_rgb(finish: str) -> list | None:
    """KiCad's finish -> 3D copper colour mapping (same string tests as board_adapter.cpp)."""
    if finish.endswith("OSP"):
        name = "Copper"
    elif finish.endswith("IG") or finish.endswith("gold"):
        name = "Gold"
    elif finish.startswith(("HAL", "HASL")) or finish.endswith(("tin", "nickel")):
        name = "Tin"
    elif finish.endswith("silver"):
        name = "Silver"
    else:
        return None
    return [channel / 255 for channel in _FINISH_COLORS[name]] + [1.0]


def settings_dir() -> Path:
    """KiCad's user settings folder (KiCad's `PATHS::GetUserSettingsPath`)."""
    base = os.environ.get("KICAD_CONFIG_HOME")
    if base:
        root = Path(base)
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "kicad"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Preferences" / "kicad"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kicad"
    return root / _kicad_version


def viewer_colors(stackup: dict, finish: str | None) -> dict:
    """The colours KiCad 10's 3D viewer shows (BOARD_ADAPTER::GetLayerColors).

    Base: the active 3D layer preset, else the "user" colour theme (the 3D viewer
    always reads that theme, not the PCB Editor's).  Then, with "Use stackup
    colours" on (KiCad's default), the saved stackup and finish override them.
    """
    root = settings_dir()
    try:
        settings = json.loads((root / "3d_viewer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    base = {}
    preset_name = settings.get("current_layer_preset") or ""
    preset = next((entry for entry in settings.get("layer_presets", ())
                   if preset_name and entry.get("name") == preset_name), None)
    if preset is not None:
        base = {entry.get("layer"): entry.get("color") for entry in preset.get("colors", ())}
    else:
        try:
            base = json.loads((root / "colors" / "user.json").read_text(encoding="utf-8")).get("3d_viewer", {})
        except (OSError, ValueError):
            pass  # no user theme: KiCad's built-in 3D defaults below
    colors = {key: _rgba(base.get(key)) or list(default) for key, default in _VIEWER_DEFAULTS.items()}
    if settings.get("use_stackup_colors", True):
        for layer, table, key in (("F.SilkS", _SILK_COLORS, "silkscreen_top"),
                                  ("B.SilkS", _SILK_COLORS, "silkscreen_bottom"),
                                  ("F.Mask", _MASK_COLORS, "soldermask_top"),
                                  ("B.Mask", _MASK_COLORS, "soldermask_bottom")):
            if layer in stackup:
                colors[key] = _named(stackup[layer], table) or [0.0, 0.0, 0.0, 1.0]
        body = None
        for name, value in stackup.items():
            if not name.startswith("dielectric"):
                continue
            layer = _named(value, _BOARD_COLORS) or [0.0, 0.0, 0.0, 1.0]
            if body is None:
                body = list(layer)
            else:  # COLOR4D::Mix keeps the running alpha
                factor = 1.0 - layer[3]
                body = [body[i] * (1 - factor) + layer[i] * factor for i in range(3)] + [body[3]]
            body[3] += (1.0 - body[3]) * layer[3] / 2
        if body is not None:
            colors["board"] = body
        copper = _finish_rgb(finish or "")
        if copper is not None:
            colors["copper"] = copper
    colors["core"] = colors.pop("board")
    return colors


def _theme() -> dict:
    """The PCB Editor's active colour theme, else the user theme, else nothing."""
    root = settings_dir()
    try:
        settings = json.loads((root / "pcbnew.json").read_text(encoding="utf-8"))
        theme_id = settings.get("appearance", {}).get("color_theme") or "_builtin_default"
    except (OSError, ValueError):
        theme_id = "_builtin_default"
    choices = ([root / "colors" / f"{theme_id}.json"] if theme_id != "_builtin_default" else [])
    choices.append(root / "colors" / "user.json")
    for path in choices:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # missing or unreadable: try the next theme file
    return {}


def appearance_signature(board_path: str = "") -> tuple:
    """Modification stamps of every file colours come from (cheap: a few stat calls).

    KiCad 10.0.3's IPC stackup returns no colours and no finish (measured), so colour
    changes reach KiLeidoscope only through these files: the saved board (stackup colours,
    finish) and the PCB Editor settings/theme.
    """
    root = settings_dir()
    paths = [Path(board_path)] if board_path else []
    paths += [root / "pcbnew.json", root / "3d_viewer.json", *sorted((root / "colors").glob("*.json"))]
    stamps = []
    for path in paths:
        try:
            info = path.stat()
            stamps.append((str(path), info.st_mtime_ns, info.st_size))
        except OSError:
            stamps.append((str(path), None, None))
    return tuple(stamps)


def read_appearance(board_path: str = "") -> dict:
    """Serializable colours; no guessed dielectric or material properties."""
    text = _saved_board_text(board_path)  # read once: stackup colours and finish
    saved = _saved_stackup_colors(text)
    finish_match = _COPPER_FINISH.search(text)
    finish = finish_match[1] if finish_match else None
    editor = _theme().get("board", {})
    copper = editor.get("copper", {})
    editor_copper = {name: color for name, key in _EDITOR_COPPER_KEYS.items()
                     if (color := _rgba(copper.get(key))) is not None}
    return {
        "copper_finish": finish,
        "edge_plating": bool(_EDGE_PLATING.search(text)),
        "dielectrics": _saved_dielectrics(text),
        "saved_colors": {name: color for name, value in saved.items()
                         if (color := _rgba(value)) is not None},
        # Final 3D-viewer colours, stackup names ("White", "FR4 natural") resolved.
        "viewer": viewer_colors(saved, finish),
        "editor_copper": editor_copper,
        "editor_layers": {name: color for name, key in _EDITOR_DRAWING_KEYS.items()
                          if (color := _rgba(editor.get(key))) is not None},
        "editor_mask_top": _rgba(editor.get("f_mask")),
        "editor_mask_bottom": _rgba(editor.get("b_mask")),
        "editor_via": _rgba(editor.get("pad_plated_hole")),
    }
