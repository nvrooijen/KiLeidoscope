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


_read = {"key": None, "data": {}}  # the file as last parsed, by (path, inode, mtime, size): the panel asks on every draw


def read():
    """The settings, {} when there are none (or the file cannot be read); parsed again only
    when the file changed (a stat per call)."""
    file = path()
    try:
        stat = file.stat()
    except OSError:
        _read.update(key=None, data={})
        return {}
    key = (str(file), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if _read["key"] != key:
        try:
            found = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            found = {}
        _read.update(key=key, data=found if isinstance(found, dict) else {})
    return dict(_read["data"])


def put(**values):
    """Set these settings, keeping the others. Raises OSError when the file cannot be written."""
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    write(file, {**read(), **values})
    _read["key"] = None  # read again: a write within the file time's grain would read as unchanged


def write(file, data):
    """`data` as JSON to `file`, whole or nothing: another process never reads half a file,
    and each process writes a temporary of its own (two Blenders may write at once)."""
    temporary = file.with_name(f"{file.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(temporary, file)
