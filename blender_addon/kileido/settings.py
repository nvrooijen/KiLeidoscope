"""The add-on's own settings file: settings.json beside the cache (kicad_cli.cache_root), kept
between sessions and read by every KiLeidoscope Blender. It holds the remembered answer to
a board from another KiCad joining (join.py) and the last running KiCad's kicad-cli
(kicad_app.py). Read whole; written whole or not at all, as the join files are.
"""

import json
import os
from pathlib import Path

from . import kicad_cli


def path():
    return Path(kicad_cli.cache_root()).parent / "settings.json"


def read():
    """The settings, {} when there are none (or the file cannot be read)."""
    try:
        found = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def put(**values):
    """Set these settings, keeping the others. Raises OSError when the file cannot be written."""
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    write(file, {**read(), **values})


def write(file, data):
    """`data` as JSON to `file`, whole or nothing: another process never reads half a file."""
    temporary = file.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(temporary, file)
