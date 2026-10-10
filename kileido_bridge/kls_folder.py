"""`<project>/.kileidoscope/`: frozen runs, findings files and confirm/dismiss state.

The one place the bridge writes into the user's project, and only inside this folder:
every write goes through `KlsFolder._write`, which refuses any path outside it, and
lands atomically (a partial file, then `os.replace`) so a watcher never reads half a
file. A `.gitignore` holding `*` keeps the folder out of the user's repository.

- `runs/<N>/board.kicad_pcb`: KiCad's raw board text at the moment of a run (unsaved
  edits included), so it hashes to the run's stamp; `run.json` beside it, and copies of
  `<stem>.kicad_pro` / `<stem>.kicad_dru` as `board.*` (net classes, rule severities,
  exclusions, custom rules) for kicad-cli. An LLM run's copies are scrubbed of what names
  the user (`scrub.py`) and it adds `board.kls-board.json` (the digest) and `request.md`
  (`prompt.make_run`). The last `KEEP_RUNS` runs are kept.
- `<stem>.kls-findings.json`: an LLM's (or any tool's) answer; only read here.
- `<stem>.drc.kls-findings.json`: the bridge's DRC findings.
- `<stem>.kls-progress.json`: how far an LLM is with a prompt run (optional, `PROGRESS_FORMAT`,
  rewritten whole after each check); only read here.
- `dismissed.json`: confirmed and dismissed findings, and unknown runs the user accepted.
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
MAX_ACCEPTED = 1_000
SOURCES = ("drc", "file")
STATES = ("confirmed", "dismissed")
PROGRESS_FORMAT = "kls-progress 1"
MAX_PROGRESS_BYTES = 4096
MAX_PROGRESS_NOTE = 120  # characters of a progress file's "note"
_STAMP = re.compile(r"[0-9a-f]{8}")
_RUN_ID = re.compile(r"[0-9]{1,9}")
_RUN_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_FROZEN = frozenset({"board.kicad_pcb", "board.kicad_pro", "board.kicad_dru", "run.json"})


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
    """dismissed.json: finding key -> {"state", "check", "targets", "title", "at"}, plus
    `accept_key`s of unknown runs the user accepted."""
    findings: dict = field(default_factory=dict)
    accepted_runs: list = field(default_factory=list)

    def states(self) -> dict[str, str]:
        return {key: entry["state"] for key, entry in self.findings.items()}

    def accepted(self) -> frozenset[str]:
        return frozenset(self.accepted_runs)

    def mark(self, key: str, state: str, check: str = "", targets=(), title: str = "") -> None:
        """Confirm or dismiss a finding; "" (or "reset") forgets it."""
        if state not in STATES:
            self.findings.pop(key, None)
            return
        self.findings[key] = {"state": state, "check": check, "targets": list(targets), "title": title,
                              "at": datetime.now().astimezone().isoformat(timespec="seconds")}

    def accept(self, run_key: str) -> None:
        if run_key not in self.accepted_runs:
            self.accepted_runs.append(run_key)
            del self.accepted_runs[:-MAX_ACCEPTED]


def _state_from(raw) -> State:
    """A State from dismissed.json's JSON, keeping only well-formed entries."""
    state = State()
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return state
    entries = raw.get("findings")
    for key, entry in list(entries.items())[:MAX_STATE_ENTRIES] if isinstance(entries, dict) else ():
        if not (isinstance(key, str) and len(key) <= 64 and isinstance(entry, dict)
                and entry.get("state") in STATES):
            continue
        targets = entry.get("targets")
        state.findings[key] = {
            "state": entry["state"],
            "check": entry["check"] if isinstance(entry.get("check"), str) else "",
            "targets": [t for t in targets if isinstance(t, str)] if isinstance(targets, list) else [],
            "title": entry["title"] if isinstance(entry.get("title"), str) else "",
            "at": entry["at"] if isinstance(entry.get("at"), str) else "",
        }
    accepted = raw.get("accepted_runs")
    if isinstance(accepted, list):
        state.accepted_runs = [item for item in accepted if isinstance(item, str) and len(item) <= 64][-MAX_ACCEPTED:]
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
        self.progress_path = self.root / f"{self.stem}.kls-progress.json"  # an LLM's, beside its answer
        self.state_path = self.root / STATE_FILE
        self.runs_dir = self.root / "runs"

    def path_for(self, source: str) -> Path:
        """The findings file of a source: "drc" (ours) or "file" (an LLM's or a tool's)."""
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

    def new_run(self, text: str, kind: str, clean=None) -> Run:
        """Freeze KiCad's raw board text as the next run; older runs past KEEP_RUNS go.
        `clean(suffix, data)` gives each copy as written (None: left out), for an LLM run
        (`scrub.clean`); the stamp is always the raw text's."""
        self.ensure()
        numbers = self._run_numbers()
        number = (numbers[-1] if numbers else 0) + 1
        run_id, run_stamp = str(number), stamp(text)
        directory = f"runs/{run_id}"
        copies = [(".kicad_pcb", text.encode("utf-8"))]
        for suffix in (".kicad_pro", ".kicad_dru"):
            try:
                copies.append((suffix, (self.project / f"{self.stem}{suffix}").read_bytes()))
            except OSError:
                continue  # none in this project
        for suffix, data in copies:
            data = clean(suffix, data) if clean is not None else data
            if data is not None:
                self._write(f"{directory}/board{suffix}", data)
        info = {"run": run_id, "stamp": run_stamp, "board": f"{self.stem}.kicad_pcb", "kind": kind,
                "made": datetime.now().astimezone().isoformat(timespec="seconds")}
        self._write(f"{directory}/run.json", json.dumps(info, indent=1).encode("utf-8"))
        for old in [*numbers, number][:-KEEP_RUNS]:
            self._remove(f"runs/{old}")
        return Run(run_id, run_stamp, self.runs_dir / run_id)

    def write_run_file(self, run: Run, name: str, text: str) -> Path:
        """A text file the bridge made for a run (`request.md`, the board digest), in that
        run's folder. A plain file name only; the frozen board and run.json stay as made."""
        if not _RUN_ID.fullmatch(run.id) or not _RUN_FILE.fullmatch(name) or name in _FROZEN:
            raise ValueError(f"{name!r} is not a file a run may get")
        if not (self.runs_dir / run.id).is_dir():
            raise ValueError(f"run {run.id} is gone")
        return self._write(f"runs/{run.id}/{name}", text.encode("utf-8"))

    def runs(self) -> dict[str, str]:
        """Run id -> stamp of every run on disk with a readable run.json, oldest first."""
        if not self.available:
            return {}
        known = {}
        for number in self._run_numbers():
            try:
                info = json.loads((self.runs_dir / str(number) / "run.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            run_stamp = info.get("stamp") if isinstance(info, dict) else None
            if isinstance(run_stamp, str) and _STAMP.fullmatch(run_stamp):
                known[str(number)] = run_stamp
        return known

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
        data = {"version": STATE_VERSION, "findings": state.findings, "accepted_runs": state.accepted_runs}
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

    def open_read(self, path: Path):
        """A file of this folder opened for reading in binary, refused (ValueError) when a
        link leads it out of the folder: an LLM's answer or progress file is read only so."""
        return self._inside(path.name).open("rb")

    def write_findings(self, data: dict, source: str = "drc") -> Path:
        """A findings file the bridge made (DRC), written whole or not at all."""
        path = self.path_for(source)
        self.ensure()
        text = json.dumps(data, indent=1, ensure_ascii=False, allow_nan=False)
        return self._write(path.name, text.encode("utf-8"))
