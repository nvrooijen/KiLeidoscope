"""Findings files in, `findings` frames out.

The bridge watches the open board's `.kileidoscope/` folder (`kls_folder`) for its two
findings files, `<stem>.drc.kls-findings.json` (ours, source "drc") and
`<stem>.kls-findings.json` (a findings file written by a tool or script into the
project's .kileidoscope folder, source "file"), and keeps one payload for Blender: both
files resolved on the live board (`findings.resolve_file`), the confirm/dismiss state of
`dismissed.json` and the state of a DRC run.

`tick` is cheap and runs on the KiCad worker (also while KiCad is disconnected): it
stats the files and hands work to a thread of its own, newest work first (as
`hatch.Hatcher`), so neither KiCad polling nor Blender waits on a 300-finding resolve.
A DRC run (`request("run_drc")`) runs kicad-cli on a frozen run folder in another
thread and writes the DRC findings file, which then takes the same path as a tool's.

While KiCad is disconnected the last payload stays, marked not connected: without the
board, `uuid:` targets cannot be named, so a resolve would change every finding's key
(and a confirm or dismiss made then would be saved under a key that changes back). A file
that changes meanwhile is read once KiCad is back.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import drc_findings, findings, kls_folder
from .board_index import BoardIndex
from .kls_folder import SOURCES

ACTIONS = frozenset({"confirm", "dismiss", "reset", "run_drc"})
MARKS = {"confirm": "confirmed", "dismiss": "dismissed", "reset": ""}
SAVED_ONLY = "saved file, unsaved edits not checked"
SAVE_FIRST = "Save the board first"
_UNREAD = object()  # a file not read yet (None: read, and missing)


def drc_state(state: str = "idle", error: str = "", run: str = "") -> dict:
    return {"state": state, "error": error, "run": run}


def state_key(source: str, key: str) -> str:
    """dismissed.json's key of a finding: DRC keys as they are, others `source:key`, so
    a key both files share keeps its own state in each."""
    return key if source == "drc" else f"{source}:{key}"


def _signature(path: Path):
    """(mtime_ns, size) of a file, None when there is none."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _with_states(payload: dict, states: dict) -> dict:
    """The payload with each finding's state from `states`; the same dict when nothing
    changes (published payloads are never changed in place: they are compared and sent)."""
    rows = [row if row["state"] == states.get(row["key"], "") else {**row, "state": states.get(row["key"], "")}
            for row in payload["findings"]]
    if all(new is old for new, old in zip(rows, payload["findings"])):
        return payload
    return {**payload, "findings": rows}


@dataclass
class _Job:
    """Work for the thread; merged until it takes it."""
    reload: set = field(default_factory=set)  # sources whose file changed
    resolve: set = field(default_factory=set)  # sources to resolve again (board change)
    save: bool = False  # dismissed.json

    def __bool__(self) -> bool:
        return bool(self.reload or self.resolve or self.save)


@dataclass(frozen=True)
class _Board:
    revision: int | None  # None: KiCad not connected
    snapshot: object
    live_stamp: str


class FindingsWatch:
    """Watches one board's findings files; `result()` is `(version, payload)`.

    Payload (the `findings` frame's "findings"): {"status": "ok" | "none" | "unsaved",
    "folder": "<project>/.kileidoscope" or "", "drc": {"state": "idle" | "running" |
    "failed", "error", "run"}, "sources": {"drc": <source payload>, "file": <source
    payload>}}. A source payload is what `findings.resolve_file` or
    `findings.status_payload` gives: {"status", "error", "source", "path", "run",
    "stamp", "live_stamp", "changed", "connected", "tool", "problems", "findings"}, each
    finding {"key", "check", "group", "severity", "title", "message", "values", "state",
    "faded", "problems", "bbox_nm", "passes", "draws"}. "none": neither file exists yet."""

    CHECK_S = 1.0  # between file checks
    SETTLE_S = 0.5  # a changed file is read once it stays unchanged this long
    RESOLVE_DEBOUNCE_S = 1.0  # at most one resolve for board edits per this long

    def __init__(self, run_drc=drc_findings.run_drc, find_cli=drc_findings.find_kicad_cli):
        self._run_drc = run_drc
        self._find_cli = find_cli
        self._lock = threading.Lock()
        self._runs_lock = threading.Lock()  # runs are numbered one at a time
        self._wake = threading.Event()
        self._thread = None
        self._generation = 0  # counts boards: work for an earlier one is dropped
        self._version = 0
        self._payload = None
        self.folder = None
        self._reset(None, kls_folder.State())

    def _reset(self, folder, state):
        self.folder = folder
        self._state = state  # dismissed.json
        self._seen = {}  # source -> (signature, first seen at)
        self._read = {}  # source -> signature last handed to the thread
        self._parsed = {}  # source -> FindingsFile, FindingsError or None (no file)
        self._sources = {}  # source -> source payload
        self._good = {}  # source -> last payload of a file that parsed
        self._drc = drc_state()
        self._drc_wanted = False
        self._drc_running = False
        self._job = _Job()
        self._board = _Board(None, None, "")
        self._resolved = None  # board key the payload was resolved for
        self._resolved_at = -float("inf")
        self._index = (None, None)  # (revision, BoardIndex)

    # --- the KiCad worker's side --------------------------------------------------------

    def target(self, board_path: str) -> None:
        """The board KiCad has open ("" unsaved). The same board again (a reconnect)
        keeps everything; another starts over. An earlier session's DRC file is discarded:
        DRC is rerun by hand, so what is drawn always comes from this bridge's converter."""
        with self._lock:
            if self.folder is not None and self.folder.board_path == board_path:
                return
        folder = kls_folder.KlsFolder(board_path)
        folder.discard("drc")
        state = folder.load_state()
        with self._lock:
            self._generation += 1
            self._reset(folder, state)
            self._payload = None  # nothing is sent for the new board until its files are read

    def tick(self, now: float, snapshot, revision: int, live_stamp: str, tools: dict, copy_text: str) -> None:
        """Stat the files and queue what changed. `copy_text`: KiCad's raw board text
        ("" when KiCad cannot give it: DRC then checks the saved file)."""
        folder = self.folder
        if folder is None:
            return
        paths = {source: folder.path_for(source) for source in SOURCES}
        signatures = {name: _signature(path) for name, path in paths.items()} if folder.available else {}
        connected = snapshot is not None
        with self._lock:
            if folder is not self.folder:
                return
            self._board = _Board(revision if connected else None, snapshot, live_stamp)
            if self._drc_wanted:
                self._drc_wanted = False
                self._start_drc(folder, snapshot, tools.get("kicad_cli", ""), copy_text)
            if not folder.available:
                context = self._context(folder, "drc")
                unsaved = {source: findings.status_payload("unsaved", replace(context, source=source),
                                                           connected=connected) for source in SOURCES}
                if unsaved != self._sources:
                    self._sources = unsaved
                    self._publish()
                return
            for source, signature in signatures.items():
                seen = self._seen.get(source)
                if seen is None or seen[0] != signature:
                    self._seen[source] = (signature, now)
                    settled = signature is None or time.time() - signature[0] / 1e9 >= self.SETTLE_S
                else:
                    settled = now - seen[1] >= self.SETTLE_S
                if settled and connected and self._read.get(source, _UNREAD) != signature:
                    self._read[source] = signature
                    self._job.reload.add(source)
            key = (revision, live_stamp) if connected else None
            if not connected:
                self._resolved = None
                if any(payload.get("connected") for payload in self._sources.values()):
                    self._sources = {source: {**payload, "connected": False}
                                     for source, payload in self._sources.items()}
                    self._good = {source: {**payload, "connected": False} for source, payload in self._good.items()}
                    self._publish()
            elif key != self._resolved and now >= self._resolved_at + self.RESOLVE_DEBOUNCE_S:
                self._resolved, self._resolved_at = key, now
                self._job.resolve.update(SOURCES)
            self._kick()

    def result(self) -> tuple[int, dict | None]:
        """(version, payload), read together; payload None until the board's files are read."""
        with self._lock:
            return self._version, self._payload

    # --- Blender's requests -------------------------------------------------------------

    def request(self, action: str, key: str = "", source: str = "drc") -> None:
        """confirm | dismiss | reset a finding of `source` by its key, or run_drc."""
        if action not in ACTIONS:
            return
        source = source if source in SOURCES else "drc"
        with self._lock:
            folder = self.folder
            if folder is None:
                return
            if action == "run_drc":
                if self._drc_running or self._drc_wanted:
                    return
                if not folder.available:
                    self._drc = drc_state("failed", SAVE_FIRST, self._drc["run"])
                else:
                    self._drc = drc_state("running", run=self._drc["run"])
                    self._drc_wanted = True  # the next tick has the board text
                self._publish()
                return
            if not folder.available:
                return
            payload = self._sources.get(source)
            row = next((row for row in payload["findings"] if row["key"] == key), None) if payload else None
            if row is None and action != "reset":
                return  # not a finding Blender was sent
            self._state.mark(state_key(source, key), MARKS[action])
            states = self._states(source)
            if payload is not None:
                self._sources[source] = _with_states(payload, states)
            if source in self._good:
                self._good[source] = _with_states(self._good[source], states)
            self._publish()
            self._job.save = True
            self._kick()

    # --- inside the lock ----------------------------------------------------------------

    def _states(self, source: str) -> dict:
        prefix = state_key(source, "")
        return {key[len(prefix):]: state for key, state in self._state.findings.items()
                if (key.startswith(prefix) if prefix else ":" not in key)}

    def _context(self, folder, source: str) -> findings.Context:
        return findings.Context(path=str(folder.path_for(source)) if folder.available else "", source=source,
                                live_stamp=self._board.live_stamp, states=self._states(source))

    def _publish(self):
        folder = self.folder
        sources = {source: self._sources.get(source) or findings.status_payload("none") for source in SOURCES}
        if not folder.available:
            status = "unsaved"
        elif all(payload["status"] == "none" for payload in sources.values()):
            status = "none"
        else:
            status = "ok"
        payload = {"status": status, "folder": str(folder.root) if folder.available else "",
                   "drc": dict(self._drc), "sources": sources}
        if payload != self._payload:
            self._payload = payload
            self._version += 1

    def _kick(self):
        if not self._job:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="kileido-findings", daemon=True)
            self._thread.start()
        self._wake.set()

    # --- the findings thread ------------------------------------------------------------

    def _loop(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                job, self._job = self._job, _Job()
                generation, folder, board = self._generation, self.folder, self._board
                state = kls_folder.State(dict(self._state.findings))
            if job:
                self._work(job, generation, folder, board, state)

    def _work(self, job: _Job, generation: int, folder, board: _Board, state):
        if job.save:
            try:
                folder.save_state(state)
            except (OSError, ValueError, kls_folder.NotAvailable):
                pass  # kept in memory; the next confirm or dismiss tries again
        files = {source: self._read_file(folder, source) for source in job.reload if source in SOURCES}
        index = self._index_of(board)
        with self._lock:
            if generation != self._generation:
                return
            for source, found in files.items():
                if found is _UNREAD:
                    self._read.pop(source, None)  # could not read it (locked): the next tick tries again
                else:
                    self._parsed[source] = found
            todo = [source for source in SOURCES if source in job.resolve or source in files]
            work = [(source, self._parsed.get(source), self._context(folder, source)) for source in todo]
            good = dict(self._good)
        resolved = {source: self._resolve(found, index, context, good.get(source)) for source, found, context in work}
        with self._lock:
            if generation != self._generation:
                return
            for source, (payload, parsed) in resolved.items():
                payload = _with_states(payload, self._states(source))  # a click while this ran
                if parsed:
                    self._good[source] = payload
                elif payload["status"] == "none":
                    self._good.pop(source, None)
                self._sources[source] = payload
            self._publish()

    def _read_file(self, folder, source: str):
        """FindingsFile, FindingsError, None (no file) or _UNREAD (try again)."""
        try:
            with folder.path_for(source).open("rb") as handle:
                data = handle.read(findings.MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            return _UNREAD
        try:
            return findings.parse(data)
        except findings.FindingsError as error:
            return error

    def _index_of(self, board: _Board):
        if board.snapshot is None:
            return None
        revision, index = self._index
        if revision != board.revision or index is None:
            index = BoardIndex(board.snapshot)
            self._index = (board.revision, index)
        return index

    def _resolve(self, found, index, context, good) -> tuple[dict, bool]:
        """(source payload, whether it is a parsed file's). A file with an error keeps
        the last good list, with the error."""
        connected = index is not None
        if found is None:
            return findings.status_payload("none", context, connected=connected), False
        if isinstance(found, findings.FindingsError):
            if good is not None:
                return {**good, "status": found.status, "error": str(found)}, False
            return findings.status_payload(found.status, context, error=str(found), connected=connected), False
        try:
            return findings.resolve_file(found, index, context), True
        except Exception as exc:  # a resolver bug costs this file, never the bridge
            error = f"could not resolve this file ({type(exc).__name__}: {exc}); please report it"
            return findings.status_payload("error", context, error=error, connected=connected), False

    # --- DRC ----------------------------------------------------------------------------

    def _start_drc(self, folder, snapshot, hint: str, text: str):
        """In the lock, on the tick that follows a run_drc request."""
        if snapshot is None:
            self._drc = drc_state("failed", findings.NOT_CONNECTED, self._drc["run"])
            self._publish()
            return
        self._drc_running = True
        threading.Thread(target=self._drc_run, args=(self._generation, folder, snapshot, hint, text),
                         name="kileido-drc", daemon=True).start()

    def _drc_run(self, generation: int, folder, snapshot, hint: str, text: str):
        run_id = ""
        try:
            note = ""
            if not text:
                text, note = Path(folder.board_path).read_text(encoding="utf-8"), SAVED_ONLY
            cli = self._find_cli(hint)
            if not cli:
                raise drc_findings.DrcFailed("kicad-cli not found; set KILEIDO_KICAD_CLI to it")
            with self._runs_lock:
                run = folder.new_run(text, "drc")
            run_id = run.id
            report = self._run_drc(cli, run.directory)
            data = drc_findings.convert(report, BoardIndex(snapshot), run.id, run.stamp)
            if note:
                data["tool"] = f"{data['tool']}; {note}"
            path = folder.write_findings(data, "drc")
            outcome = drc_state(run=run.id)
        except Exception as exc:  # kicad-cli failed, a file could not be written, or a converter bug
            message = str(exc) if isinstance(exc, (drc_findings.DrcFailed, kls_folder.NotAvailable)) \
                else f"{type(exc).__name__}: {exc}"
            outcome, path = drc_state("failed", message[-300:], run_id), None
        with self._lock:
            if generation != self._generation:
                return
            self._drc_running = False
            self._drc = outcome
            if path is not None:  # read it now, not after the next settle
                self._read["drc"] = _signature(path)
                self._job.reload.add("drc")
            self._publish()
            self._kick()
