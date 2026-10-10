"""`<project>/.kileidoscope/`: frozen runs, findings files and confirm/dismiss state.

The one place the bridge writes into the user's project, and only inside this folder:
every write goes through `KlsFolder._write`, which refuses any path outside it, and
lands atomically (a partial file, then `os.replace`) so a watcher never reads half a
file. A `.gitignore` holding `*` keeps the folder out of the user's repository.

- `runs/<N>/board.kicad_pcb`: KiCad's raw board text at the moment of a run (unsaved
  edits included), so it hashes to the run's stamp; `run.json` beside it, and copies of
  `<stem>.kicad_pro` / `<stem>.kicad_dru` as `board.*` (net classes, rule severities,
  exclusions, custom rules) for kicad-cli. The last `KEEP_RUNS` runs are kept.
- `<stem>.kls-findings.json`: a findings file written by a tool or script; only read here.
- `<stem>.drc.kls-findings.json`: the bridge's DRC findings.
- `dismissed.json`: confirmed and dismissed findings, by key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FOLDER = ".kileidoscope"
KEEP_RUNS = 5
STATE_FILE = "dismissed.json"
STATE_VERSION = 1
MAX_STATE_ENTRIES = 10_000
SOURCES = ("drc", "file")
STATES = ("confirmed", "dismissed")
_RUN_ID = re.compile(r"[0-9]{1,9}")


def stamp(text: str) -> str:
    """First 8 hex of the SHA-256 of KiCad's raw board text: what a run and its findings echo."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


class NotAvailable(RuntimeError):
    """The board is not saved: there is no project folder to write into."""


@dataclass(frozen=True)
class Run:
    id: str
    stamp: str
    directory: Path


@dataclass
class State:
    """dismissed.json: finding key -> "confirmed" | "dismissed"."""
    findings: dict = field(default_factory=dict)

    def states(self) -> dict[str, str]:
        return dict(self.findings)

    def mark(self, key: str, state: str) -> None:
        """Confirm or dismiss a finding; "" (or "reset") forgets it."""
        if state not in STATES:
            self.findings.pop(key, None)
            return
        self.findings[key] = state


def _state_from(raw) -> State:
    """A State from dismissed.json's JSON, keeping only well-formed entries."""
    state = State()
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return state
    entries = raw.get("findings")
    for key, entry in list(entries.items())[:MAX_STATE_ENTRIES] if isinstance(entries, dict) else ():
        if isinstance(key, str) and len(key) <= 64 and entry in STATES:
            state.findings[key] = entry
    return state


class KlsFolder:
    """The `.kileidoscope` folder of one saved board; `available` False for an unsaved one."""

    def __init__(self, board_path: str):
        self.board_path = board_path
        self.available = bool(board_path)
        board = Path(board_path) if board_path else Path("unsaved.kicad_pcb")
        self.stem = board.stem
        self.project = board.parent
        self.root = self.project / FOLDER
        self.findings_path = self.root / f"{self.stem}.kls-findings.json"
        self.drc_findings_path = self.root / f"{self.stem}.drc.kls-findings.json"
        self.state_path = self.root / STATE_FILE
        self.runs_dir = self.root / "runs"

    def path_for(self, source: str) -> Path:
        """The findings file of a source: "drc" (ours) or "file" (a tool's or a script's)."""
        if source not in SOURCES:
            raise ValueError(f"unknown findings source {source!r}")
        return self.drc_findings_path if source == "drc" else self.findings_path

    def _check(self) -> None:
        if not self.available:
            raise NotAvailable("save the board first")

    def _inside(self, relative) -> Path:
        """`root / relative`, refused unless it stays strictly inside the root (no `..`,
        no absolute path, no link leading out)."""
        self._check()
        root = self.root.resolve()
        path = (self.root / relative).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError(f"{relative} is outside {FOLDER}")
        return path

    def _write(self, relative, data: bytes) -> Path:
        """The only writer: atomically, and only inside the root."""
        path = self._inside(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        partial.write_bytes(data)
        os.replace(partial, path)
        return path

    def _remove(self, relative) -> None:
        shutil.rmtree(self._inside(relative), ignore_errors=True)

    def ensure(self) -> None:
        """Make the folder, with a `.gitignore` that keeps all of it out of git."""
        self._check()
        self.root.mkdir(exist_ok=True)
        ignore = self.root / ".gitignore"
        try:
            current = ignore.read_bytes()
        except OSError:
            current = b""
        if current != b"*\n":
            self._write(".gitignore", b"*\n")

    def _run_numbers(self) -> list[int]:
        try:
            names = [entry.name for entry in self.runs_dir.iterdir() if entry.is_dir()]
        except OSError:
            return []
        return sorted(int(name) for name in names if _RUN_ID.fullmatch(name))

    def new_run(self, text: str, kind: str) -> Run:
        """Freeze KiCad's raw board text as the next run; older runs past KEEP_RUNS go."""
        self.ensure()
        numbers = self._run_numbers()
        number = (numbers[-1] if numbers else 0) + 1
        run_id, run_stamp = str(number), stamp(text)
        directory = f"runs/{run_id}"
        self._write(f"{directory}/board.kicad_pcb", text.encode("utf-8"))
        for suffix in (".kicad_pro", ".kicad_dru"):
            source = self.project / f"{self.stem}{suffix}"
            try:
                data = source.read_bytes()
            except OSError:
                continue  # none in this project
            self._write(f"{directory}/board{suffix}", data)
        info = {"run": run_id, "stamp": run_stamp, "board": f"{self.stem}.kicad_pcb", "kind": kind,
                "made": datetime.now().astimezone().isoformat(timespec="seconds")}
        self._write(f"{directory}/run.json", json.dumps(info, indent=1).encode("utf-8"))
        for old in [*numbers, number][:-KEEP_RUNS]:
            self._remove(f"runs/{old}")
        return Run(run_id, run_stamp, self.runs_dir / run_id)

    def load_state(self) -> State:
        """dismissed.json; a missing or broken file is an empty state."""
        if not self.available:
            return State()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return State()
        return _state_from(raw)

    def save_state(self, state: State) -> None:
        self.ensure()
        data = {"version": STATE_VERSION, "findings": state.findings}
        self._write(STATE_FILE, json.dumps(data, indent=1, ensure_ascii=False).encode("utf-8"))

    def discard(self, source: str = "drc") -> None:
        """Our DRC findings file removed: a session starts without the last one's, so a DRC
        run is always this bridge's own. A tool's file is never touched."""
        if source != "drc":
            raise ValueError("only the bridge's own findings file is discarded")
        if not self.available:
            return
        try:
            self._inside(self.drc_findings_path.name).unlink()
        except OSError:  # missing, or locked by an editor: the next run overwrites it anyway
            pass

    def write_findings(self, data: dict, source: str = "drc") -> Path:
        """A findings file the bridge made (DRC), written whole or not at all."""
        path = self.path_for(source)
        self.ensure()
        text = json.dumps(data, indent=1, ensure_ascii=False, allow_nan=False)
        return self._write(path.name, text.encode("utf-8"))
