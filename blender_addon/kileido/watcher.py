"""Background watchers of the board file that kicad-cli exports read.

`models` (3D models) and `cosmetics` (mask, silkscreen, drawings) both watch the
bridge's live board copy (else the saved board) on a worker thread, export with
kicad-cli there, and hand finished results to Blender's main thread through a
queue drained by a timer. The worker never touches bpy.
"""

import atexit
import queue
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import bpy

from .state import board

POLL_S = 0.5  # one stat per poll: the bridge rewrites its copy ~0.5 s after edits settle
TIMER_S = 0.2
SCRATCH_PREFIXES = ("kileido_models_", "kileido_overlays_")  # the exports' scratch_directory folders
STALE_S = 3600  # a scratch folder this old is a crashed Blender's; an export's lives seconds
_scratch = set()  # scratch folders not yet removed, emptied again when Python exits
_scratch_lock = threading.Lock()


@dataclass
class Result:
    """What a worker hands to the main thread. `directory` is removed once handled."""
    generation: int
    board_path: str
    directory: Path | None = None
    error: str | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Job:
    """One watched board file for one worker thread."""
    source: Path  # the file kicad-cli reads
    board_path: str  # identifies the board in Blender
    export: dict
    generation: int
    stop: threading.Event
    results: queue.SimpleQueue

    def emit(self, directory=None, error=None, **data):
        self.results.put(Result(self.generation, self.board_path, directory, error, data))

    @contextmanager
    def scratch_directory(self, prefix):
        """A temporary folder for one export. It goes out with the result (and is
        removed once that is handled), or is removed here once the job was stopped
        (a stopped job's results are dropped). A failure is reported, not raised."""
        directory = Path(tempfile.mkdtemp(prefix=prefix))
        with _scratch_lock:
            _scratch.add(directory)
        try:
            yield directory
        except Exception as exc:
            if not self.stop.is_set():
                self.emit(directory=directory, error=str(exc))
                return
        if self.stop.is_set():
            _remove(directory)


def _remove(directory):
    shutil.rmtree(directory, ignore_errors=True)
    with _scratch_lock:
        _scratch.discard(Path(directory))


def remove_later(directory):
    """Delete a folder off the main thread (large exports take a while)."""
    if directory is not None:
        threading.Thread(target=_remove, args=(directory,), daemon=True).start()


@atexit.register
def _remove_unhandled():
    """Blender quitting stops the daemon threads, maybe mid-removal, and drops queued
    results: remove what is left of this session's scratch folders."""
    with _scratch_lock:
        left = list(_scratch)
    for directory in left:
        _remove(directory)


def sweep_stale(now=None):
    """Remove scratch folders a crashed Blender left in the temp folder (off the main thread)."""
    now = time.time() if now is None else now
    stale = []
    for prefix in SCRATCH_PREFIXES:
        for path in Path(tempfile.gettempdir()).glob(prefix + "*"):
            try:
                if path.is_dir() and now - path.stat().st_mtime > STALE_S:
                    stale.append(path)
            except OSError:
                pass
    if stale:
        threading.Thread(target=lambda: [_remove(path) for path in stale], daemon=True).start()
    return stale


class BoardWatcher:
    """Calls `on_change(job, memory)` on a worker whenever the watched file changes and
    `on_result(result)` on the main thread for every successful result.

    `memory` is a dict kept for the life of one watch; it is cleared after an
    unexpected error so the next file change retries from scratch. `on_result`
    returns the panel status text.
    """

    def __init__(self, name, on_change, on_result, *, starting, unavailable, failed, import_failed,
                 first_interval=TIMER_S):
        self.name = name
        self.on_change = on_change
        self.on_result = on_result
        self.starting = starting  # callable(export) -> status while the first export runs
        self.unavailable = unavailable  # status when the snapshot has no board file
        self.failed = failed  # status prefix for worker errors
        self.import_failed = import_failed  # status prefix for errors while applying a result
        self.first_interval = first_interval
        self.status = ""
        self._results = queue.SimpleQueue()
        self._generation = 0
        self._board_path = ""
        self._source = ""
        self._stop = None
        self._timer = self._drain  # one bound method: Blender timers compare by identity

    def follow(self, board_path, export=None):
        """Watch the board of a newly shown snapshot (no-op when it is unchanged)."""
        export = dict(export or {})
        source = export.get("path") or board_path
        if board_path == self._board_path and source == self._source:
            if not board_path and board.board_name:
                self.status = self.unavailable
            return
        if self._stop is not None:
            self._stop.set()
        self._generation += 1
        self._board_path, self._source = board_path, source
        if not board_path or not source:
            self._stop = None
            self.status = self.unavailable
            return
        self._stop = threading.Event()
        self.status = self.starting(export)
        job = Job(Path(source), board_path, export, self._generation, self._stop, self._results)
        threading.Thread(target=self._watch, args=(job,), name=f"KiLeidoscope {self.name}", daemon=True).start()
        if not bpy.app.timers.is_registered(self._timer):
            bpy.app.timers.register(self._timer, first_interval=self.first_interval)

    def stop(self):
        if self._stop is not None:
            self._stop.set()
        self._stop = None
        self._board_path = self._source = ""
        self._generation += 1
        if bpy.app.timers.is_registered(self._timer):
            bpy.app.timers.unregister(self._timer)
        while True:
            try:
                remove_later(self._results.get_nowait().directory)
            except queue.Empty:
                break

    def _watch(self, job):
        """Worker thread: a stat every POLL_S; exports only when the file changed."""
        last = None
        memory = {}
        while not job.stop.is_set():
            try:
                info = job.source.stat()
                stamp = (info.st_mtime_ns, info.st_size)
                if stamp != last:
                    last = stamp
                    self.on_change(job, memory)
            except OSError as exc:
                # The live copy appears a moment after the snapshot; only a missing
                # saved board is an error.
                if last != "missing" and not job.export.get("live"):
                    job.emit(error=str(exc))
                last = "missing"
            except Exception as exc:  # e.g. kicad-cli not found; retried on the next file change
                job.emit(error=str(exc))
                memory.clear()
            job.stop.wait(POLL_S)

    def drain(self):
        """Apply at most one finished result (main thread); the timer callback."""
        return self._drain()

    def _drain(self):
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            return TIMER_S if self._stop is not None else None
        try:
            if result.generation != self._generation or result.board_path != board.board_path:
                pass  # a board switch superseded this worker result
            elif result.error:
                self.status = f"{self.failed}: {result.error}"
            else:
                self.status = self.on_result(result)
        except Exception as exc:
            self.status = f"{self.import_failed}: {exc}"
        finally:
            remove_later(result.directory)
        return TIMER_S if self._stop is not None else None
