"""Open a board in KiCad from Blender: a board shown offline (a saved copy, a lost link) or
one an opened arrangement waits for. KiCad's PCB editor starts with the board's file; Open
in Blender there then puts the board back live in its row (join.py), so getting a board
back is a click here and one in KiCad.

The PCB editor is the one beside a kicad-cli we know of: the running KiCad's, which the
bridge sends with each board (and a saved board keeps), the last one seen (settings.json,
`remember`), or an installed one (kicad_cli.executable).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import kicad_cli, settings
from .state import boards

SETTING = "kicad_cli"  # in settings.json (join.py): the last running KiCad's kicad-cli
_CHILD_ENVIRONMENT = ("PYTHONHOME", "PYTHONPATH", "KICAD_API_SOCKET", "KICAD_API_TOKEN")  # not for KiCad


def remember(export):
    """A live board's kicad-cli (the bridge's `export`), kept for a Blender that shows only
    saved boards later."""
    cli = (export or {}).get("kicad_cli") or ""
    if not cli or _remembered() == cli:
        return
    try:
        settings.put(**{SETTING: cli})
    except OSError:
        pass


def _remembered():
    return settings.read().get(SETTING) or ""


def _editor_beside(cli):
    """The PCB editor of the KiCad that `cli` (a kicad-cli) belongs to, or None."""
    cli = Path(cli)
    if sys.platform == "win32":
        found = cli.with_name("pcbnew.exe")
    elif sys.platform == "darwin":  # .../KiCad/KiCad.app/Contents/MacOS/kicad-cli
        if len(cli.parents) < 4:  # too short a path to hold an app bundle
            return None
        found = cli.parents[3] / "pcbnew.app" / "Contents" / "MacOS" / "pcbnew"
    else:
        found = cli.with_name("pcbnew")
    return found if found.is_file() else None


def editor(export=None):
    """KiCad's PCB editor: beside `export`'s kicad-cli, another board's, the last one seen,
    else an installed one; None when there is none."""
    candidates = [(export or {}).get("kicad_cli"), *((state.export or {}).get("kicad_cli") for state in boards.values()),
                  _remembered()]
    try:
        candidates.append(kicad_cli.executable())
    except RuntimeError:
        pass
    for cli in candidates:
        if cli and Path(cli).is_file():
            found = _editor_beside(cli)
            if found is not None:
                return found
    return None


def _keep_kicad_connections_apart():
    """Windows: the empty api.sock file that makes each KiCad started from now on answer on a
    pipe of its own (KiCad issue #23994; the fix Kikakuka documents), as the launcher makes
    it (kileido_bridge.launcher.keep_kicad_connections_apart)."""
    if sys.platform != "win32":
        return
    marker = Path(tempfile.gettempdir()) / "kicad" / "api.sock"
    try:
        marker.parent.mkdir(exist_ok=True)
        if not marker.exists():
            marker.touch()
    except OSError:
        pass


class AlreadyOpen(RuntimeError):
    """The board is open in a KiCad already (its lock file says so): Open in Blender there."""


def lock_file(board_path):
    """KiCad's lock beside the board, ~<name>.lck: made when a PCB editor opens the board, with
    the user and machine; left behind by a crash."""
    path = Path(board_path)
    return path.with_name(f"~{path.name}.lck")


def open_in_kicad(board_path):
    """Who has the board open in KiCad, "nickv at DESKTOPNVR", as its lock file says; "" when
    there is no lock (or it cannot be read)."""
    try:
        data = json.loads(lock_file(board_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return " at ".join(str(data[key]) for key in ("username", "hostname") if data.get(key)) or "someone"


def open_board(board_path, export=None):
    """Start KiCad's PCB editor with `board_path`. Raises FileNotFoundError when the board
    file or KiCad cannot be found, AlreadyOpen when a KiCad has the board open already."""
    if not board_path or not Path(board_path).is_file():
        raise FileNotFoundError(f"the board file is not there: {board_path or 'never saved'}")
    holder = open_in_kicad(board_path)
    if holder:
        raise AlreadyOpen(f"{Path(board_path).name} is open in KiCad already ({holder}): use Open in Blender there. "
                          f"If no KiCad has it open, delete {lock_file(board_path).name} beside the board")
    found = editor(export)
    if found is None:
        raise FileNotFoundError("KiCad was not found (Open in Blender in KiCad once, and it is remembered)")
    _keep_kicad_connections_apart()
    environment = {key: value for key, value in os.environ.items() if key not in _CHILD_ENVIRONMENT}
    options = {"cwd": str(Path(board_path).parent), "env": environment, "stdin": subprocess.DEVNULL,
               "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True  # KiCad stays when Blender quits
    _start([str(found), str(board_path)], **options)
    return found


def _start(command, **options):
    subprocess.Popen(command, **options)
