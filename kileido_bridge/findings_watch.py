"""Findings files in, `findings` frames out.

The bridge watches the open board's `.kileidoscope/` folder (`kls_folder`) for its two
findings files, `<stem>.drc.kls-findings.json` (ours, source "drc") and
`<stem>.kls-findings.json` (an LLM's or a tool's, source "file"), and keeps one payload
for Blender: both files resolved on the live board (`findings.resolve_file`), the
confirm/dismiss state of `dismissed.json`, the state of a DRC run and of a prompt, and
the check catalogue (`checks`) Blender ticks checks from.

`tick` is cheap and runs on the KiCad worker (also while KiCad is disconnected): it
stats the files and hands work to a thread of its own, newest work first (as
`hatch.Hatcher`), so neither KiCad polling nor Blender waits on a 300-finding resolve.
A DRC run (`request("run_drc")`) runs kicad-cli on a frozen run folder in another
thread and writes the DRC findings file, which then takes the same path as an LLM's.
A prompt (`request("prompt", checks=ids)`) freezes a run the same way, with
the board digest and `request.md` beside it (`prompt.make_run`), and hands Blender the
clipboard prompt that points an LLM at it, with the user's notes in it.

A file's suggestions (ghosts) are judged twice: the resolve's quick fit check, and KiCad's
own DRC on a copy of the board with every ghost written in (`ghost_drc`), run once per
(ghosts as resolved, board text) in a thread of its own, one at a time, the newest asked
for next. Its outcome is kept for that key and put on the payload (`ghost_drc.apply`):
"pending" until it is in; a file or board edit that changes the key makes it pending
again, and a result that comes back for an older key is dropped. Without
kicad-cli, or when it fails, the ghosts say so and keep the quick check's verdict.

While the LLM works it may keep `<stem>.kls-progress.json` (`kls_folder.PROGRESS_FORMAT`).
It is read like the findings files (settled, capped, never trusted) and gives the payload's
"progress": "waiting" once a prompt is ready, "working" while a progress file for that run
and stamp is there, "done" once the findings file for that run is. Only the file's content
and the run change it: Blender ages it from the unix times itself, so time passing sends nothing.

While KiCad is disconnected the last payload stays, marked not connected: without the
board, `uuid:` targets cannot be named, so a resolve would change every finding's key
(and a confirm or dismiss made then would be saved under a key that changes back). A file
that changes meanwhile is read once KiCad is back.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import drc_findings, findings, ghost_drc, kls_folder
from .board_index import BoardIndex
from .kls_folder import SOURCES

ACTIONS = frozenset({"confirm", "dismiss", "reset", "run_drc", "prompt"})
MARKS = {"confirm": "confirmed", "dismiss": "dismissed", "reset": ""}
SAVED_ONLY = "saved file, unsaved edits not checked"
SAVE_FIRST = "Save the board first"
NO_TEXT = "KiCad did not give the board text; try again once it is idle"
NO_CHECKS = "None of the chosen checks is known to this bridge: tick at least one"
DRC_BUSY = "a DRC run was already going; its results are in the DRC tab"
MAX_CHECKS = 100  # ids in one prompt request
MAX_NOTES = 500  # characters of a prompt request's notes
PROGRESS = "progress"  # the progress file, watched beside SOURCES
MAX_PROGRESS_COUNT = 10_000  # "done" and "total" are clamped to 0..this
MAX_PROGRESS_CHECK = 64
MAX_PROGRESS_NOTE = kls_folder.MAX_PROGRESS_NOTE
_UNREAD = object()  # a file not read yet (None: read, and missing)


def drc_state(state: str = "idle", error: str = "", run: str = "") -> dict:
    return {"state": state, "error": error, "run": run}


def prompt_state(state: str = "idle", run: str = "", text: str = "", error: str = "") -> dict:
    """"idle" | "making" | "ready" (`text`: the clipboard prompt of run `run`) | "failed"."""
    return {"state": state, "run": run, "text": text, "error": error}


def progress_state(state: str = "none", run: str = "", done: int = 0, total: int = 0, check: str = "",
                   note: str = "", updated_unix: float = 0.0, made_unix: float = 0.0) -> dict:
    """"none" (no prompt run) | "waiting" (a prompt is ready, no progress for it yet) |
    "working" (a progress file for that run and stamp) | "done" (its findings file is in)."""
    return {"state": state, "run": run, "done": done, "total": total, "check": check, "note": note,
            "updated_unix": updated_unix, "made_unix": made_unix}


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0
    return int(min(max(value, 0), MAX_PROGRESS_COUNT))


def _plain(value, limit: int) -> str:
    return (findings._text(value, limit, "", []) or "") if isinstance(value, str) else ""


def parse_progress(data: bytes) -> dict | None:
    """A progress file's {"run", "stamp", "done", "total", "check", "note"}: counts clamped,
    text plain and cut. None for anything that is not one (too big, not JSON, wrong format)."""
    if len(data) > kls_folder.MAX_PROGRESS_BYTES:
        return None
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not (isinstance(raw, dict) and raw.get("format") == kls_folder.PROGRESS_FORMAT
            and isinstance(raw.get("run"), str) and isinstance(raw.get("stamp"), str)):
        return None
    return {"run": raw["run"], "stamp": raw["stamp"], "done": _count(raw.get("done")),
            "total": _count(raw.get("total")), "check": _plain(raw.get("check"), MAX_PROGRESS_CHECK),
            "note": _plain(raw.get("note"), MAX_PROGRESS_NOTE)}


def _group_row(group, titles: dict) -> dict:
    """{"id", "title"} of one of `checks.GROUPS`, however it is written there."""
    if isinstance(group, str):
        return {"id": group, "title": str(titles.get(group, group))}
    if isinstance(group, (tuple, list)) and group:
        return {"id": str(group[0]), "title": str(group[1] if len(group) > 1 else group[0])}
    ident = str(getattr(group, "id", group))
    return {"id": ident, "title": str(getattr(group, "title", ident))}


def catalogue() -> tuple[list, list] | None:
    """(checks, groups) for Blender's check list: {"id", "group", "title", "v1"} per check and
    {"id", "title"} per group, so Blender needs no bridge import; None without `checks`
    (or with a broken one: the findings never wait on the catalogue)."""
    try:
        from . import checks
        rows = [{"id": check.id, "group": check.group, "title": check.title, "v1": bool(check.v1)}
                for check in checks.CATALOGUE]
        groups = checks.GROUPS.items() if isinstance(checks.GROUPS, dict) else checks.GROUPS
        titles = getattr(checks, "GROUP_TITLES", None) or {}
        return rows, [_group_row(group, titles) for group in groups]
    except Exception:
        return None


def make_prompt(folder, text: str, snapshot, ids, notes: str = "", drc=None, dismissed=()) -> tuple[str, str, str]:
    """(run id, stamp, clipboard prompt) of a new prompt run for the checks `ids` (unknown
    ones dropped), with the user's `notes`, what KiCad's DRC found (`prompt._drc_text`) and
    what the user dismissed on this board (`kls_folder.State.dismissed`)."""
    from . import checks, prompt
    chosen = checks.by_id(ids)
    if not chosen:
        raise ValueError(NO_CHECKS)
    run, clipboard = prompt.make_run(folder, text, snapshot, chosen, notes=notes, drc=drc, dismissed=dismissed)
    return run.id, run.stamp, clipboard


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
    reload: set = field(default_factory=set)  # sources whose file changed, and PROGRESS
    resolve: set = field(default_factory=set)  # sources to resolve again (board change)
    save: bool = False  # dismissed.json

    def __bool__(self) -> bool:
        return bool(self.reload or self.resolve or self.save)


@dataclass(frozen=True)
class _Board:
    revision: int | None  # None: KiCad not connected
    snapshot: object
    live_stamp: str
    text: str = ""  # KiCad's raw board text ("": not given; the saved file stands in)
    hint: str = ""  # the running KiCad's kicad-cli


class FindingsWatch:
    """Watches one board's findings files; `result()` is `(version, payload)`.

    Payload (the `findings` frame's "findings"): {"status": "ok" | "none" | "unsaved",
    "folder": "<project>/.kileidoscope" or "", "drc": {"state": "idle" | "running" |
    "failed", "error", "run"}, "prompt": {"state": "idle" | "making" | "ready" |
    "failed", "run", "text", "error"}, "progress": `progress_state`, "catalogue": [{"id",
    "group", "title", "v1"}], "groups": [{"id", "title"}], "sources": {"drc": <source
    payload>, "file": <source payload>}}. A source payload is what `findings.resolve_file`
    or `findings.status_payload` gives: {"status", "error", "source", "path", "run",
    "stamp", "live_stamp", "handshake", "changed", "connected", "tool", "problems", "drc",
    "skipped", "findings"}, each finding {"key", "check", "group", "severity",
    "confidence", "title", "message", "fix", "values", "values_text", "state", "faded",
    "problems", "bbox_nm", "disagree", "passes", "mentions", "draws"}; a ghost draw's
    "kicad_drc" says what KiCad's DRC made of it (`ghost_drc.apply`). "none": neither
    file exists yet."""

    CHECK_S = 1.0  # between file checks
    SETTLE_S = 0.5  # a changed file is read once it stays unchanged this long
    RESOLVE_DEBOUNCE_S = 1.0  # at most one resolve for board edits per this long

    def __init__(self, run_drc=drc_findings.run_drc, find_cli=drc_findings.find_kicad_cli,
                 make_prompt=make_prompt, catalogue=catalogue, run_ghost_drc=ghost_drc.run_drc):
        self._run_drc = run_drc
        self._run_ghost_drc = run_ghost_drc
        self._find_cli = find_cli
        self._make_prompt = make_prompt
        self._catalogue_of = catalogue
        self._catalogue = None  # (checks, groups) once `checks` gives them
        self._lock = threading.Lock()
        self._runs_lock = threading.Lock()  # a DRC run and a prompt never number a run at once
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
        self._prompt = prompt_state()
        self._prompt_wanted = None  # (check ids, notes) of a prompt the next tick starts
        self._prompt_running = False
        self._prompt_run = None  # (run, stamp, made at, unix) of the last prompt made
        self._progress = (None, 0.0)  # (parse_progress of the progress file or None, its mtime)
        self._job = _Job()
        self._board = _Board(None, None, "")
        self._resolved = None  # board key the payload was resolved for
        self._resolved_at = -float("inf")
        self._index = (None, None)  # (revision, BoardIndex)
        self._ghost_key = {}  # source -> key of the ghosts its payload shows (`_with_ghosts`)
        self._ghost_done = {}  # source -> (key, ghost_drc.Outcome) of its last ghost DRC run
        self._ghost_want = {}  # source -> (key, ghosts, text, hint, folder) of the run asked for next
        self._ghost_running = None  # (source, key) of the ghost DRC run going

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
        paths = {**{source: folder.path_for(source) for source in SOURCES}, PROGRESS: folder.progress_path}
        signatures = {name: _signature(path) for name, path in paths.items()} if folder.available else {}
        connected = snapshot is not None
        with self._lock:
            if folder is not self.folder:
                return
            self._board = _Board(revision if connected else None, snapshot, live_stamp, copy_text,
                                 tools.get("kicad_cli", ""))
            if self._drc_wanted:
                self._drc_wanted = False
                self._start_drc(folder, snapshot, tools.get("kicad_cli", ""), copy_text)
            if self._prompt_wanted is not None:
                (ids, notes), self._prompt_wanted = self._prompt_wanted, None
                self._start_prompt(folder, snapshot, tools.get("kicad_cli", ""), copy_text, ids, notes)
            if not folder.available:
                context = self._context(folder, "drc", {})
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
                if settled and (connected or source == PROGRESS) and self._read.get(source, _UNREAD) != signature:
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

    def request(self, action: str, key: str = "", source: str = "drc", checks=(), notes: str = "") -> None:
        """confirm | dismiss | reset a finding of `source` by its key, run_drc, or make a
        prompt for the check ids `checks` with the user's `notes`."""
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
            if action == "prompt":
                if self._prompt_running or self._prompt_wanted is not None:
                    return
                if not folder.available:
                    self._prompt = prompt_state("failed", error=SAVE_FIRST)
                else:
                    self._prompt = prompt_state("making")
                    notes = notes[:MAX_NOTES] if isinstance(notes, str) else ""
                    self._prompt_wanted = (tuple(checks)[:MAX_CHECKS], notes)  # the next tick has the board text
                self._publish()
                return
            if not folder.available:
                return
            payload = self._sources.get(source)
            row = next((row for row in payload["findings"] if row["key"] == key), None) if payload else None
            if row is None and action != "reset":
                return  # not a finding Blender was sent
            self._state.mark(state_key(source, key), MARKS[action], row["check"] if row else "",
                             row["title"] if row else "", folder.stem)
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
        return {key[len(prefix):]: entry["state"] for key, entry in self._state.findings.items()
                if (key.startswith(prefix) if prefix else ":" not in key)}

    def _context(self, folder, source: str, runs: dict) -> findings.Context:
        return findings.Context(path=str(folder.path_for(source)) if folder.available else "", source=source,
                                runs=runs, live_stamp=self._board.live_stamp,
                                states=self._states(source))

    def _progress_payload(self) -> dict:
        """The "progress" of the last prompt run, while the prompt is ready."""
        if self._prompt["state"] != "ready" or self._prompt_run is None:
            return progress_state()
        run, run_stamp, made = self._prompt_run
        progress, updated = self._progress
        if progress is None or progress["run"] != run or progress["stamp"] != run_stamp:
            progress, updated = None, 0.0  # none yet, or another run's
        answer = self._parsed.get("file")
        if isinstance(answer, findings.FindingsFile) and answer.run == run:
            signature = self._read.get("file")
            updated = signature[0] / 1e9 if isinstance(signature, tuple) else updated
            state = "done"
        else:
            state = "working" if progress is not None else "waiting"
        fields = {key: progress[key] for key in ("done", "total", "check", "note")} if progress else {}
        return progress_state(state, run, updated_unix=updated, made_unix=made, **fields)

    def _publish(self):
        folder = self.folder
        sources = {source: self._sources.get(source) or findings.status_payload("none") for source in SOURCES}
        if not folder.available:
            status = "unsaved"
        elif all(payload["status"] == "none" for payload in sources.values()):
            status = "none"
        else:
            status = "ok"
        if self._catalogue is None:
            self._catalogue = self._catalogue_of()
        rows, groups = self._catalogue or ([], [])
        payload = {"status": status, "folder": str(folder.root) if folder.available else "",
                   "drc": dict(self._drc), "prompt": dict(self._prompt), "progress": self._progress_payload(),
                   "catalogue": rows, "groups": groups, "sources": sources}
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
        """The findings thread: one job at a time, for the whole session. A bug in one job
        is published on both sources and the thread goes on to the next."""
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                job, self._job = self._job, _Job()
                generation, folder, board = self._generation, self.folder, self._board
                state = kls_folder.State(dict(self._state.findings))
            if not job:
                continue
            try:
                self._work(job, generation, folder, board, state)
            except Exception as exc:  # a bug costs this job's files, never the thread
                self._failed(generation, folder, f"could not read the findings ({type(exc).__name__}: {exc}); "
                                                  "please report it")

    def _failed(self, generation: int, folder, error: str):
        """A job's error on both sources: a listed file keeps its findings, with the error."""
        with self._lock:
            if generation != self._generation:
                return
            for source in SOURCES:
                shown = self._sources.get(source) or findings.status_payload(
                    "error", self._context(folder, source, {}), connected=self._board.revision is not None)
                self._sources[source] = {**shown, "status": "error", "error": error[:findings.MAX_TEXT]}
            self._publish()

    def _work(self, job: _Job, generation: int, folder, board: _Board, state):
        if job.save:
            try:
                folder.save_state(state)
            except (OSError, ValueError, kls_folder.NotAvailable):
                pass  # kept in memory; the next confirm or dismiss tries again
        files = {source: self._read_file(folder, source) for source in job.reload if source in SOURCES}
        progress = self._read_progress(folder) if PROGRESS in job.reload else None
        index = self._index_of(board)
        runs = folder.runs()
        with self._lock:
            if generation != self._generation:
                return
            for source, found in files.items():
                if found is _UNREAD:
                    self._read.pop(source, None)  # could not read it (locked): the next tick tries again
                else:
                    self._parsed[source] = found
            if progress is _UNREAD:
                self._read.pop(PROGRESS, None)  # locked: the next tick tries again
            elif progress is not None:
                self._progress = progress
            todo = [source for source in SOURCES if source in job.resolve or source in files]
            work = [(source, self._parsed.get(source), self._context(folder, source, runs)) for source in todo]
            good = dict(self._good)
        resolved = {source: self._resolve(found, index, context, good.get(source)) for source, found, context in work}
        with self._lock:
            if generation != self._generation:
                return
            for source, (payload, parsed) in resolved.items():
                payload = _with_states(payload, self._states(source))  # a click while this ran
                payload = self._with_ghosts(source, payload, board, folder)
                if parsed:
                    self._good[source] = payload
                elif payload["status"] == "none":
                    self._good.pop(source, None)
                self._sources[source] = payload
            self._publish()

    def _read_file(self, folder, source: str):
        """FindingsFile, FindingsError, None (no file) or _UNREAD (try again)."""
        try:
            with folder.open_read(folder.path_for(source)) as handle:
                data = handle.read(findings.MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            return _UNREAD
        except ValueError:
            return findings.FindingsError("the findings file is a link out of the .kileidoscope folder: not read")
        try:
            return findings.parse(data)
        except findings.FindingsError as error:
            return error

    def _read_progress(self, folder):
        """(parse_progress or None, mtime) of the progress file, or _UNREAD (try again)."""
        try:
            with folder.open_read(folder.progress_path) as handle:
                mtime = os.fstat(handle.fileno()).st_mtime
                data = handle.read(kls_folder.MAX_PROGRESS_BYTES + 1)
        except (FileNotFoundError, ValueError):  # none, or a link out of the folder: not read
            return None, 0.0
        except OSError:
            return _UNREAD
        return parse_progress(data), mtime

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

    # --- KiCad's DRC on the ghosts ----------------------------------------------------------

    def _with_ghosts(self, source: str, payload: dict, board: _Board, folder) -> dict:
        """In the lock: `payload` with KiCad's DRC on its ghosts. The outcome kept for these
        ghosts on this board text when there is one; else "pending", and a run asked for
        (the newest ask replaces an older one; the run going is never asked again, and an
        older ask for this source is dropped when the payload is back at the run going)."""
        ghosts = ghost_drc.collect(payload)
        if not ghosts:
            self._ghost_key.pop(source, None)
            self._ghost_want.pop(source, None)
            return payload
        on = board.live_stamp if board.text else f"saved@{board.revision}"
        key = (on, ghost_drc.digest(ghosts))
        self._ghost_key[source] = key
        done = self._ghost_done.get(source)
        if done is not None and done[0] == key:
            self._ghost_want.pop(source, None)
            return ghost_drc.apply(payload, done[1])
        if self._ghost_running == (source, key):
            self._ghost_want.pop(source, None)  # back at the run going (an edit undone): no other run wanted
        else:
            self._ghost_want[source] = (key, ghosts, board.text, board.hint, folder)
            self._start_ghosts()
        return ghost_drc.apply(payload, None)

    def _start_ghosts(self):
        """In the lock: the next ghost DRC run asked for, unless one is going."""
        if self._ghost_running is not None or not self._ghost_want:
            return
        source = next(iter(self._ghost_want))
        key, ghosts, text, hint, folder = self._ghost_want.pop(source)
        self._ghost_running = (source, key)
        threading.Thread(target=self._ghost_run, args=(self._generation, source, key, folder, ghosts, text, hint),
                         name="kileido-ghost-drc", daemon=True).start()

    def _ghost_run(self, generation: int, source: str, key, folder, ghosts, text: str, hint: str):
        """KiCad's DRC on a copy of `text` (else the saved file) with `ghosts` written in;
        the outcome is kept under `key` and shown while the payload still shows that key,
        and dropped when it no longer does (it would only push out a newer key's)."""
        try:
            if not text:
                text = Path(folder.board_path).read_text(encoding="utf-8")
            cli = self._find_cli(hint)
            if not cli:
                raise drc_findings.DrcFailed(ghost_drc.NO_CLI)
            outcome = ghost_drc.check(folder, text, ghosts, cli, self._run_ghost_drc, self._runs_lock)
        except Exception as exc:  # kicad-cli failed, the run could not be written, a bug: the quick check stands
            message = str(exc) if isinstance(exc, (drc_findings.DrcFailed, kls_folder.NotAvailable)) \
                else f"{type(exc).__name__}: {exc}"
            outcome = ghost_drc.Outcome(error=message[-300:] or "KiCad's DRC failed")
        with self._lock:
            if generation != self._generation:
                return
            self._ghost_running = None
            if self._ghost_key.get(source) == key:
                self._ghost_done[source] = (key, outcome)
                for store in (self._sources, self._good):
                    if source in store:
                        store[source] = ghost_drc.apply(store[source], outcome)
                self._publish()
            self._start_ghosts()

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
        """Run KiCad's DRC on `text` and publish it; {check: entries} of the run, or why it
        failed (a prompt made right after names them, `_prompt_make`)."""
        run_id, found = "", None
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
            found = {}
            for item in data.get("findings", ()):
                found[item.get("check") or "drc"] = found.get(item.get("check") or "drc", 0) + 1
        except Exception as exc:  # kicad-cli failed, a file could not be written, or a converter bug
            message = str(exc) if isinstance(exc, (drc_findings.DrcFailed, kls_folder.NotAvailable)) \
                else f"{type(exc).__name__}: {exc}"
            outcome, path = drc_state("failed", message[-300:], run_id), None
        with self._lock:
            if generation != self._generation:
                return found if found is not None else outcome["error"]
            self._drc_running = False
            self._drc = outcome
            if path is not None:  # read it now, not after the next settle
                self._read["drc"] = _signature(path)
                self._job.reload.add("drc")
            self._publish()
            self._kick()
        return found if found is not None else outcome["error"]

    # --- Prompt -------------------------------------------------------------------------

    def _start_prompt(self, folder, snapshot, hint: str, text: str, ids: tuple, notes: str):
        """In the lock, on the tick that follows a prompt request: the run is made from
        KiCad's raw board text, so it hashes to the stamp the findings echo. KiCad's DRC
        runs on the same text first (unless a DRC run is already going), so the request
        tells the LLM what DRC already found, and what the user dismissed on this board."""
        error = findings.NOT_CONNECTED if snapshot is None else ("" if text else NO_TEXT)
        if error:
            self._prompt = prompt_state("failed", error=error)
            self._publish()
            return
        self._prompt_running = True
        with_drc = not self._drc_running and not self._drc_wanted
        if with_drc:
            self._drc_running = True
            self._drc = drc_state("running", run=self._drc["run"])
            self._publish()
        dismissed = self._state.dismissed(folder.stem)
        threading.Thread(target=self._prompt_make,
                         args=(self._generation, folder, snapshot, hint if with_drc else None, text, ids, notes,
                               dismissed), name="kileido-prompt", daemon=True).start()

    def _prompt_make(self, generation: int, folder, snapshot, hint, text: str, ids: tuple, notes: str, dismissed):
        """`hint` None: no DRC first (one was already running)."""
        made = None
        drc = DRC_BUSY if hint is None else self._drc_run(generation, folder, snapshot, hint, text)
        try:
            with self._runs_lock:
                run_id, run_stamp, clipboard = self._make_prompt(folder, text, snapshot, ids, notes, drc,
                                                                 dismissed=dismissed)
            outcome = prompt_state("ready", str(run_id), clipboard)
            made = (str(run_id), str(run_stamp), time.time())
        except Exception as exc:  # a file could not be written, no checks, or a digest bug
            message = str(exc) if isinstance(exc, (ValueError, kls_folder.NotAvailable)) \
                else f"{type(exc).__name__}: {exc}"
            outcome = prompt_state("failed", error=message[-300:])
        with self._lock:
            if generation != self._generation:
                return
            self._prompt_running = False
            self._prompt = outcome
            self._prompt_run = made or self._prompt_run
            if outcome["state"] == "ready":  # a known run now: files echoing it (or a pruned one) resolve again
                self._job.resolve.update(SOURCES)
            self._publish()
            self._kick()
