"""A private file copy of the open board, unsaved edits included, for kicad-cli.

Solder mask, silkscreen, drawings and 3D models come from `kicad-cli` exports,
and kicad-cli reads files, not the running editor. The bridge asks KiCad for the
board text (`kicad_reader.board_text`, read-only) and writes it to its own
temporary folder; Blender's export workers watch that file. The user's board
and project files are only read, never written.
"""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path


class LiveBoardCopy:
    """Rewrites `<temp>/<board stem>.kicad_pcb` when the open board's text changes.

    IPC polling sees tracks, pads, zones, footprints and shapes change, so those
    trigger a refresh `debounce_s` after the edits stop. Board text, dimensions,
    colours and finish are invisible to that polling, so the text is also
    re-checked every `recheck_s` while idle (~75 ms of KiCad time per check on the
    reference board).
    """

    def __init__(self, directory: Path | None = None, debounce_s: float = 0.5,
                 recheck_s: float = 4.0):
        self.directory = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="kileido_live_"))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.debounce_s = debounce_s
        self.recheck_s = recheck_s
        self.path: Path | None = None
        self.project_dir = ""
        self._digest = None
        self._project_stamp = None
        self._changed_at: float | None = None
        self._checked_at: float | None = None

    def target(self, board_name: str, saved_path: str) -> Path:
        """The copy's path. It keeps the board's file stem: kicad-cli names
        `--mode-multi` outputs after it, and it finds `<stem>.kicad_pro` next to it."""
        stem = Path(saved_path).stem if saved_path else Path(board_name).stem or "board"
        path = self.directory / f"{stem}.kicad_pcb"
        if path != self.path:
            self.path = path
            self._digest = None
            self._project_stamp = None
            self._changed_at = None
            self._checked_at = None
        self.project_dir = str(Path(saved_path).parent) if saved_path else ""
        return path

    def changed(self, now: float) -> None:
        """IPC saw an edit; refresh once the edits settle."""
        self._changed_at = now

    def due(self, now: float) -> bool:
        if self._checked_at is None:
            return True
        if self._changed_at is not None and self._changed_at >= self._checked_at:
            return now - self._changed_at >= self.debounce_s
        return now - self._checked_at >= self.recheck_s

    def write(self, text: str, now: float, saved_path: str = "") -> bool:
        """Store the board text; True when the file content changed. A failed write
        (disk full; on Windows, a reader holding the file) keeps the last good copy
        and is retried on the next check."""
        self._checked_at = now
        data = text.encode("utf-8")
        digest = hashlib.blake2b(data, digest_size=16).digest()
        try:
            self._copy_project(saved_path)
            if digest == self._digest and self.path.is_file():
                return False
            partial = self.path.with_name(self.path.name + ".partial")
            partial.write_bytes(data)
            os.replace(partial, self.path)  # watchers never see a half-written board
        except OSError:
            return False
        self._digest = digest
        return True

    def _copy_project(self, saved_path: str):
        """Project text variables and settings live in `<stem>.kicad_pro`."""
        if not saved_path:
            return
        source = Path(saved_path).with_suffix(".kicad_pro")
        try:
            info = source.stat()
        except OSError:
            return
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp != self._project_stamp:
            shutil.copyfile(source, self.path.with_suffix(".kicad_pro"))
            self._project_stamp = stamp

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
