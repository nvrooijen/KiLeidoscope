"""What names the user, taken out of an LLM run's copies of the board and project.

Basic privacy only, for a cloud LLM: the board copy loses its title block (company, author,
revision, comments), its board-level text variables and each footprint's schematic sheet
(file name and sheet name), and every 3D model path is cut to its file name (absolute
paths carry the user's name and folders). The project copy keeps only the sections a board
review reads (design settings, net classes, component classes, tuning profiles). Nets,
parts, values and geometry stay: the review needs them. KiCad DRC runs keep the raw files; the stamp is
always taken from KiCad's raw board text, so a scrubbed copy still matches its run.
"""

from __future__ import annotations

import json
import re

_NODE = re.compile(r'"(?:[^"\\]|\\.)*"|\(([A-Za-z_][A-Za-z0-9_]*)?|\)')
_MODEL = re.compile(r'\(model\s+"((?:[^"\\]|\\.)*)"')
_DROPPED = {"title_block": None, "property": 1,  # node -> only at this depth (None: any)
            "sheetfile": 2, "sheetname": 2}  # a footprint's schematic sheet
_SHEET_PROPERTY = re.compile(r'\(property\s+"(?:Sheetfile|Sheetname)"')  # the same, KiCad 7's form
PROJECT_KEPT = ("meta", "board", "net_settings", "component_class_settings", "tuning_profiles")


def _nodes(text: str):
    """(start, end, name, depth) of every list node: `start` at its "(", `end` past its
    ")", depth 0 for the file's own (kicad_pcb ...). Quoted strings are skipped whole."""
    stack = []
    for match in _NODE.finditer(text):
        token = match.group(0)
        if token[0] == "(":
            stack.append((match.start(), match.group(1) or ""))
        elif token == ")" and stack:
            start, name = stack.pop()
            yield start, match.end(), name, len(stack)


def _line_start(text: str, start: int) -> int:
    """`start` moved back over the indent and the line break before it, so a removed node
    leaves no blank line."""
    while start > 0 and text[start - 1] in " \t":
        start -= 1
    if start > 0 and text[start - 1] == "\n":
        start -= 1
        if start > 0 and text[start - 1] == "\r":
            start -= 1
    return start


def _file_name(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def board(text: str) -> str:
    """KiCad's board text without its title block and board-level text variables, its 3D
    model paths cut to the file name."""
    edits = []  # (start, end, replacement), never overlapping
    for start, end, name, depth in _nodes(text):
        if (name in _DROPPED and _DROPPED[name] in (None, depth)) or (
                name == "property" and depth == 2 and _SHEET_PROPERTY.match(text, start)):
            edits.append((_line_start(text, start), end, ""))
        elif name == "model":
            match = _MODEL.match(text, start)
            if match and _file_name(match.group(1)) != match.group(1):
                edits.append((match.start(1), match.end(1), _file_name(match.group(1))))
    edits.sort()
    kept, last = [], 0
    for start, end, replacement in edits:
        if start < last:
            continue  # inside a node already removed
        kept += [text[last:start], replacement]
        last = end
    kept.append(text[last:])
    return "".join(kept)


def project(data: bytes) -> bytes | None:
    """The .kicad_pro with only the sections a board review reads (`PROJECT_KEPT`): no text
    variables, sheet names, saved paths or file name. None when it is not a JSON object
    (then it is left out, not copied raw)."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    kept = {key: raw[key] for key in PROJECT_KEPT if key in raw}
    if isinstance(kept.get("meta"), dict):
        kept["meta"] = {**kept["meta"], "filename": "board.kicad_pro"}
    return (json.dumps(kept, indent=2) + "\n").encode("utf-8")


def clean(suffix: str, data: bytes) -> bytes | None:
    """An LLM run's copy of one project file (`KlsFolder.new_run`'s `clean`): the board and
    project scrubbed, the rules file as it is."""
    if suffix == ".kicad_pcb":
        return board(data.decode("utf-8")).encode("utf-8")
    if suffix == ".kicad_pro":
        return project(data)
    return data
