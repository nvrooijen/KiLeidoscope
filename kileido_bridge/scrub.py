"""What names the user, taken out of an LLM run's copies of the board and project.

Basic privacy only, for a cloud LLM: the board copy loses its title block (company, author,
revision, comments), its board-level text variables and each footprint's schematic sheet
(file name and sheet name), every 3D model path is cut to its file name (absolute paths
carry the user's name and folders), and a footprint's Datasheet that is a local file (a
path, a `file:` URL) is emptied; a web link (http, https) stays. The project copy keeps
only the sections a board review reads (design settings, net classes, component classes,
tuning profiles). Nets, parts, values and geometry stay: the review needs them. KiCad DRC
runs keep the raw files; the stamp is always taken from KiCad's raw board text, so a
scrubbed copy still matches its run.

Not scrubbed: the clipboard prompt and `request.md` name the run folder and the answer
file by their absolute paths (the LLM tool needs them to find the files), so they carry
the project's folder and, on most systems, the user's name.
"""

from __future__ import annotations

import json
import re

_NODE = re.compile(r'"(?:[^"\\]|\\.)*"|\(([A-Za-z_][A-Za-z0-9_]*)?|\)')
_MODEL = re.compile(r'\(model\s+"((?:[^"\\]|\\.)*)"')
_DROPPED = {"title_block": None, "property": 1,  # node -> only at this depth (None: any)
            "sheetfile": 2, "sheetname": 2}  # a footprint's schematic sheet
_SHEET_PROPERTY = re.compile(r'\(property\s+"(?:Sheetfile|Sheetname)"')  # the same, KiCad 7's form
_DATASHEET = re.compile(r'\(property\s+"Datasheet"\s+"((?:[^"\\]|\\.)*)"')  # a footprint's
# a local file: a file: URL, a drive, a UNC or absolute path, ~/ or ./ ../, a ${VARIABLE}, any backslash
_LOCAL = re.compile(r'(?i)^\s*(?:file:|[a-z]:[\\/]|[\\/]|~[\\/]|\.\.?[\\/]|\$\{)|\\')
PROJECT_KEPT = ("meta", "board", "net_settings", "component_class_settings", "tuning_profiles")


def nodes(text: str, start: int = 0):
    """(start, end, name, depth) of every list node from `start` on, in the order they
    close: `start` at its "(", `end` past its ")", depth 0 for the outermost (the file's own
    (kicad_pcb ...)). Quoted strings are skipped whole. `ghost_drc` walks footprints so."""
    stack = []
    for match in _NODE.finditer(text, start):
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


def local_file(value: str) -> bool:
    """Whether a Datasheet value names a local file (`_LOCAL`) rather than a web link."""
    return not re.match(r"(?i)^\s*https?://", value) and bool(_LOCAL.search(value))


def board(text: str) -> str:
    """KiCad's board text without its title block and board-level text variables, its 3D
    model paths cut to the file name, a footprint Datasheet that is a local file emptied."""
    edits = []  # (start, end, replacement), never overlapping
    for start, end, name, depth in nodes(text):
        if (name in _DROPPED and _DROPPED[name] in (None, depth)) or (
                name == "property" and depth == 2 and _SHEET_PROPERTY.match(text, start)):
            edits.append((_line_start(text, start), end, ""))
        elif name == "property" and depth == 2:
            match = _DATASHEET.match(text, start)
            if match and local_file(match.group(1)):
                edits.append((match.start(1), match.end(1), ""))
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
