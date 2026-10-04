"""Read-only KiCad poll loop and complete/incremental Blender frame delivery."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import replace

from . import flex_checks, hatch, protocol
from .board_specs import appearance_signature, read_appearance, set_kicad_version, settings_dir
from .board_text import copper_items
from .kicad_reader import (KiCadBusy, NewKiCad, PollResult, board_text, connect_reader, explain_connection_error,
                           follow_new_kicad, kicad_tools, saved_board_path, select_in_kicad, selected_ids)
from .live_copy import LiveBoardCopy
from .selection import components, highlight_nets, selected_nets, unconnected


def _is_timeout(exc: Exception) -> bool:
    """kipy reports a reply timeout as ConnectionError("Error receiving reply from KiCad:
    Timed out"), raised from None: only the message tells it from a lost KiCad."""
    return (isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()
            or "timed out" in str(exc).lower())


def _copper_changes(old, tracks, arcs, vias) -> set:
    """(layer, kind) groups whose tracks, arcs or vias differ from `old`'s."""
    dirty = set()
    for kind, before, after in (("tracks", old.tracks, tracks), ("arcs", old.arcs, arcs)):
        dirty |= {(item.layer, kind) for item in set(before) ^ set(after)}
    if set(old.vias) != set(vias):
        dirty.add(("", "vias"))
    return dirty


class BridgeRuntime:
    TEXT_POLL_INTERVAL_S = 0.5  # each read pauses KiCad ~75 ms, also mid-route
    MAX_TIMEOUTS = 3  # consecutive read timeouts before the reader is dropped
    REQUEST_GRACE_S = 10.0  # how long KiCad may take to show a Blender click (measured up to ~9 s)

    def __init__(self, server, connector=None, poll_interval_s: float = 0.2,
                 reconnect_interval_s: float = 2.0, clock=time.monotonic,
                 live_copy: LiveBoardCopy | None = None, follower=follow_new_kicad):
        self.server = server
        self.follower = follower
        self.connector = connector or (lambda: connect_reader(connections=4))
        self.poll_interval_s = poll_interval_s
        self.reconnect_interval_s = reconnect_interval_s
        self.clock = clock
        # Connection and schedule
        self.reader = None
        self.status = "disconnected"
        self.error = ""
        self.new_kicad = False  # another KiCad serves the socket; waits for the user's Resync
        self.outdated_pads = 0  # pads KiCad only answers for with an outdated copy (after an undo)
        self.next_connect_at = 0.0
        self.next_poll_at = 0.0
        self.next_text_poll_at = 0.0
        self.timeout_count = 0
        self.sent_from_text = False  # Blender has copper the reader has not seen (board text)
        # The board as Blender last received it
        self.snapshot = None
        self.origin_nm = None
        self.revision = 0
        self.board_path = ""
        self.tools = {}
        self.appearance = {}
        self.appearance_sig = None
        self.flex = None  # flex mode's panel content as Blender last received it
        self.flex_error = ""  # why the flex model last failed, so it is said once
        self.copy = live_copy or LiveBoardCopy()
        self.copy_enabled = True
        self.copy_text = ""  # the board text last written to the copy (before its hatches are baked in)
        self.hatcher = hatch.Hatcher()  # KiCad's own hatches of hatched shapes, worked out in the background
        self.hatch_version = 0  # the hatcher's result the snapshot and copy carry
        self.hatch_error = ""  # the hatcher's error as the status frame last said it
        # Selection highlight
        self.highlight = ((), (), (), ())
        self.sticky_nets = frozenset()  # highlighted nets, kept through routing
        self.had_selection = False
        self.busy_seen = False
        self.selected = frozenset()  # KiCad's selection as last read or requested
        self.requested = None  # (ids, deadline) of a Blender click KiCad has not shown yet
        # Threads (start): KiCad calls run on a worker, so a stalled KiCad delays no click.
        self.lock = threading.RLock()  # the server and the highlight; never held during a KiCad call
        self.kicad_selects = deque()  # clicks the worker still has to select in KiCad
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.worker = None

    # --- Connection state ---------------------------------------------------------------

    def _status_frame(self, **extra) -> bytes:
        self.hatch_error = self.hatcher.error
        return protocol.encode_frame({"type": "status", "kicad": self.status, "revision": self.revision,
                                      **extra, "error": self.error, "new_kicad": self.new_kicad,
                                      "outdated_pads": self.outdated_pads, "hatch_error": self.hatch_error})

    def _status(self, value, last_read_ms=None, error=""):
        if value == self.status and error == self.error:
            return
        self.status = value
        self.error = error
        self._send([self._status_frame(last_read_ms=last_read_ms)])

    def _send(self, frames, snapshot: bool = False):
        with self.lock:
            self.server.send_frames(frames, snapshot=snapshot)

    def _disconnect_reader(self, error=""):
        if self.reader is not None:
            self.reader.close()
        with self.lock:  # a resync on the main thread reads these
            self.reader = None
            self.snapshot = None
            self.origin_nm = None
            self.board_path = ""
            self.appearance = {}
            self.appearance_sig = None
            self.flex = None
            self.tools = {}
            self.copy_enabled = True
            self.timeout_count = 0
            self.next_connect_at = self.clock() + self.reconnect_interval_s
            self.sent_from_text = False
            self.outdated_pads = 0
            self._status("disconnected", error=error)

    def _connect(self):
        try:
            self.reader = self.connector()
            self.next_poll_at = self.clock()
            self.timeout_count = 0
            self.new_kicad = False
        except Exception as exc:
            self.reader = None
            self.next_connect_at = self.clock() + self.reconnect_interval_s
            self.new_kicad = isinstance(exc, NewKiCad)
            self._status("disconnected", error=str(exc) if self.new_kicad else explain_connection_error(exc))

    # --- Polling ------------------------------------------------------------------------

    def _poll(self):
        try:
            result = self.reader.poll(full=self.snapshot is None)
        except KiCadBusy:
            self.busy_seen = True  # e.g. the interactive router: item reads answer busy
            self._status("editing")
            self._poll_board_text()
            return
        except Exception as exc:
            if _is_timeout(exc):
                self.timeout_count += 1
                if self.timeout_count < self.MAX_TIMEOUTS:
                    return
            self._disconnect_reader(error=f"{type(exc).__name__}: {exc}")
            return
        self.timeout_count = 0
        result = self._with_hatches(result)
        snapshot = result.snapshot
        board_changed = self.snapshot is None or snapshot.board_name != self.snapshot.board_name
        if board_changed:
            self._on_board_changed(snapshot)
        elif self.sent_from_text:
            # The reader diffs against what it last read, from before the board-text
            # updates; an edit undone since then would otherwise leave their copper behind.
            changes = _copper_changes(self.snapshot, snapshot.tracks, snapshot.arcs, snapshot.vias)
            result = replace(result, dirty=result.dirty | changes)
        self.sent_from_text = False
        full_snapshot = board_changed or ("", "stackup") in result.dirty
        if full_snapshot:
            self._refresh_copy(force=True)
            source = self._appearance_source()
            self.appearance_sig = appearance_signature(source)  # stamp before reading
            self.appearance = read_appearance(source)
            self.flex = self._flex_report(snapshot)
        self.snapshot = snapshot
        frames = self._geometry_frames(result, full_snapshot)
        if not full_snapshot:
            if result.dirty:
                self.copy.changed(self.clock())
            self._refresh_copy()
            appearance = self._appearance_frames()
            frames += appearance + self._flex_frames(snapshot, bool(result.dirty or appearance))
        frames += self._selection_frames(snapshot, force=full_snapshot)
        if result.outdated_pads != self.outdated_pads or self.hatcher.error != self.hatch_error:
            self.outdated_pads = result.outdated_pads
            frames.append(self._status_frame())
        self._send(frames, snapshot=full_snapshot)
        self._status("connected", snapshot.read_timings_ms.get("total"))

    def _on_board_changed(self, snapshot):
        """A new board (or the first): fix its origin and find its file, tools and copy."""
        self.origin_nm = protocol.board_origin_nm(snapshot)
        board = getattr(self.reader, "board", None)
        self.board_path = saved_board_path(board)
        self.tools = kicad_tools(board) if board is not None else {}
        set_kicad_version(self.tools.get("kicad_version"))
        self.hatcher.kicad_cli = self.tools.get("kicad_cli", "")
        self.copy.target(snapshot.board_name, self.board_path)
        self.copy_enabled = True

    def _geometry_frames(self, result: PollResult, full_snapshot: bool) -> list[bytes]:
        """A complete snapshot after a board or stackup change, else one frame per dirty group."""
        if full_snapshot:
            self.revision += 1
            return self._snapshot_frames()
        if result.dirty:
            self.revision += 1
            return protocol.messages_for(result.snapshot, result.dirty, self.revision)
        return []

    def _snapshot_frames(self) -> list[bytes]:
        return protocol.snapshot_frames(self.snapshot, self.revision, self.origin_nm,
                                        board_path=self.board_path,
                                        appearance=self.appearance,
                                        export=self._export(), flex=self.flex)

    def _flex_report(self, snapshot) -> dict | None:
        """Flex mode's report; one that says so (an error in its problems) when the model
        fails on this board, so a flex bug never disconnects KiCad."""
        stack = self.appearance.get("flex_stack", {})
        try:
            report = flex_checks.report(snapshot, stack)
        except Exception as exc:
            error = f"Flex mode could not read this board ({type(exc).__name__}: {exc}); please report it"
            if error != self.flex_error:
                self.flex_error = error
                self._status(self.status, error=error)
            return flex_checks.error_report(error, stack)
        self.flex_error = ""
        return report

    def _flex_frames(self, snapshot, changed: bool) -> list[bytes]:
        """Flex mode's checks again after an edit (or a stackup change in the board file)."""
        if not changed:
            return []
        report = self._flex_report(snapshot)
        if report == self.flex:
            return []
        self.flex = report
        self.revision += 1
        return [protocol.flex_message(report, self.revision)]

    def _appearance_frames(self) -> list[bytes]:
        """IPC has no colours or finish. They come from the live board copy (so they
        follow unsaved Board Setup edits) and from KiCad's theme files."""
        source = self._appearance_source()
        signature = appearance_signature(source)
        if signature == self.appearance_sig:
            return []
        self.appearance_sig = signature
        self.appearance = read_appearance(source)
        self.revision += 1
        return [protocol.encode_frame({"type": "appearance", "revision": self.revision,
                                       "appearance": self.appearance})]

    def _poll_board_text(self):
        """KiCad busy (its route tool stays active between routes): item reads fail but
        the board text still comes (measured), with every committed route in it. Read
        tracks, arcs and vias from it so routes appear without leaving the tool."""
        now = self.clock()
        if self.snapshot is None or now < self.next_text_poll_at:
            return
        self.next_text_poll_at = now + self.TEXT_POLL_INTERVAL_S
        try:
            text = board_text(self.reader.board)
        except Exception:
            return  # also busy for text (a modal dialog), or no board
        if self.copy_enabled and self.copy.path is not None:
            self._write_copy(text, now)  # mask/silkscreen overlays follow too
        tracks, arcs, vias = copper_items(text)
        dirty = _copper_changes(self.snapshot, tracks, arcs, vias)
        frames = []
        if dirty:
            snapshot = replace(self.snapshot, tracks=tracks, arcs=arcs, vias=vias)
            self.snapshot = snapshot
            self.sent_from_text = True
            self.revision += 1
            frames = protocol.messages_for(snapshot, frozenset(dirty), self.revision)
            frames += self._flex_frames(snapshot, True)
        frames += self._selection_frames(self.snapshot)  # the selection read works while busy
        self._send(frames)

    def _selection_frames(self, snapshot, force: bool = False, selected=None) -> list[bytes]:
        """Highlighted nets (and components), sent when they change. `selected` is
        KiCad's selection, read from KiCad unless given.

        Sticky through routing: KiCad answers busy while its router runs and clears
        the selection when routing starts. A selection that empties after a busy
        period keeps its nets highlighted (new tracks on them join in); one that
        empties without a busy period is a deselect and clears them.
        """
        read = selected is None
        if read:
            try:
                selected = selected_ids(self.reader.board)
            except KiCadBusy:
                self.busy_seen = True
                return []
            except Exception as exc:
                if _is_timeout(exc):
                    return []  # KiCad is slow to answer, not deselected: keep the highlight
                selected = frozenset()  # no selection API (tests' fake reader)
        with self.lock:  # a click on the main thread changes the highlight too
            if read and self._before_requested(selected):
                return []
            self.selected = selected
            nets = selected_nets(snapshot, selected)
            if nets:
                self.sticky_nets = nets
            elif self.had_selection and not self.busy_seen:
                self.sticky_nets = frozenset()
            self.had_selection = bool(selected)
            self.busy_seen = False
            on_nets, pair = highlight_nets(snapshot, self.sticky_nets)
            loose = unconnected(snapshot, selected)  # net-less items: only while selected
            current = (tuple(sorted({*on_nets, *loose})), pair, *components(snapshot, selected))
            if current == self.highlight and not force:
                return []
            self.highlight = current
            return [protocol.selection_message(*current, self.revision)]

    def _before_requested(self, selected) -> bool:
        """A read from before KiCad applied a Blender click: the bridge reads over
        several connections, so a stalled KiCad can answer it first (the old or, between
        clear and add, an empty selection). Ignored until KiCad shows the click or
        REQUEST_GRACE_S pass (a selection made in KiCad meanwhile then takes over)."""
        if self.requested is None:
            return False
        ids, deadline = self.requested
        if (ids <= selected if ids else not selected) or self.clock() >= deadline:
            self.requested = None
            return False
        return True

    # --- Live board copy (kicad-cli exports) --------------------------------------------

    def _refresh_copy(self, force: bool = False):
        """Keep the live board copy current; kicad-cli exports in Blender read it."""
        now = self.clock()
        if not self.copy_enabled or self.copy.path is None or not (force or self.copy.due(now)):
            return
        try:
            text = board_text(self.reader.board)
        except KiCadBusy:
            return  # an interactive tool is running; retry on the next poll
        except Exception:
            if not self.copy.path.is_file():
                self.copy_enabled = False  # this KiCad cannot serialize boards; use the saved file
            return  # otherwise keep the last good copy and retry later
        self._write_copy(text, now)

    def _write_copy(self, text: str, now: float):
        """The copy as KiCad gives it, with the hatches of its hatched shapes baked in
        (kicad-cli plots them as their outlines alone; `hatch`)."""
        self.copy_text = text
        self.hatcher.want(text)
        self.copy.write(hatch.bake(text, self.hatcher.shapes), now, self.board_path)

    def _with_hatches(self, result: PollResult) -> PollResult:
        """The reader's snapshot with the hatches in its copper graphics (IPC reports a
        hatched shape as unfilled). New hatches from the background mark those layers
        changed, and the copy takes them too."""
        version, shapes = self.hatcher.result()
        snapshot = result.snapshot
        if shapes:
            snapshot = replace(snapshot, graphics=hatch.apply(snapshot.graphics, shapes))
        dirty = result.dirty
        if version != self.hatch_version:
            self.hatch_version = version
            before = self.snapshot.graphics if self.snapshot is not None else ()
            dirty = dirty | {(graphic.layer, "graphics") for graphic in (*before, *snapshot.graphics)}
            if self.copy_text and self.copy_enabled and self.copy.path is not None:
                self.copy.write(hatch.bake(self.copy_text, shapes), self.clock(), self.board_path)
        return replace(result, snapshot=snapshot, dirty=dirty)

    def _appearance_source(self) -> str:
        if self.copy_enabled and self.copy.path is not None and self.copy.path.is_file():
            return str(self.copy.path)
        return self.board_path

    def _export(self) -> dict:
        live = self.copy_enabled and self.copy.path is not None
        return {"path": str(self.copy.path) if live else self.board_path,
                "live": live,
                "project_dir": self.copy.project_dir if live else "",
                "kicad_cli": self.tools.get("kicad_cli", ""),
                "kicad_settings": str(settings_dir())}  # its kicad_common.json: Configure Paths

    # --- Blender requests and the main loop ---------------------------------------------

    def _take_select_requests(self):
        """A click in Blender (main thread, under the lock): highlight it now, and queue
        selecting it in KiCad for the worker. A pad selects its footprint. KiCad can take
        seconds to apply a selection and answer (measured 1-9 s with its window hidden
        behind Blender); later polls confirm it."""
        for ids, extend, center in self.server.take_select_requests():
            snapshot = self.snapshot
            if self.reader is None or snapshot is None:
                continue
            owner = {pad.id: pad.footprint_id for pad in snapshot.pads}
            wanted = list(dict.fromkeys(owner.get(item_id) or item_id for item_id in ids))
            if not wanted and not extend:
                self.sticky_nets = frozenset()  # a click on bare board: clear the highlight
            selected = frozenset(wanted) | (self.selected if extend else frozenset())
            self.requested = (selected, self.clock() + self.REQUEST_GRACE_S)
            self.server.send_frames(self._selection_frames(snapshot, selected=selected))
            self.server.pump()
            self.kicad_selects.append((wanted, extend, center))
            self.wake.set()

    def _run_kicad_selects(self):
        """The worker's half of a click. Clicks queued behind a stalled KiCad collapse
        to the last plain click and the Shift+clicks after it."""
        with self.lock:
            pending = list(self.kicad_selects)
            self.kicad_selects.clear()
        plain = [index for index, (_, extend, _) in enumerate(pending) if not extend]
        for wanted, extend, center in pending[plain[-1] if plain else 0:]:
            if self.reader is None:
                return
            try:
                select_in_kicad(self.reader.board, wanted, extend, center)
            except Exception:
                pass  # KiCad busy (a tool is running) or gone: the click is dropped
            self.next_poll_at = self.clock()  # read the new selection back promptly

    def _send_resync(self):
        """The viewer asked for everything again (new connection or its own request)."""
        if self.snapshot is not None:
            self.server.send_frames(self._snapshot_frames(), snapshot=True)
            self.server.send_frames([protocol.selection_message(*self.highlight, self.revision)])
        self.server.send_frames([self._status_frame()])

    def step(self) -> None:
        """Serve Blender; without a started worker (tests), also talk to KiCad."""
        with self.lock:
            self.server.pump()
            self._take_select_requests()
            if self.server.take_adopt() and self.new_kicad:
                self.follower()
                self.next_connect_at = 0.0  # connect now, not at the next retry
            if self.server.take_resync():
                self._send_resync()
        if self.worker is None:
            self._kicad_step()
        with self.lock:
            self.server.pump()

    def wait(self, timeout: float) -> None:
        """Serve Blender for up to `timeout` s, returning early when it sends something."""
        with self.lock:
            self.server.pump(timeout)

    def _kicad_step(self):
        self._run_kicad_selects()
        now = self.clock()
        if self.reader is None and now >= self.next_connect_at:
            self._connect()
        if self.reader is not None and now >= self.next_poll_at:
            self.next_poll_at = now + self.poll_interval_s
            self._poll()

    def start(self) -> None:
        """Talk to KiCad on a worker thread from now on; `step` then only serves Blender."""
        self.worker = threading.Thread(target=self._kicad_loop, name="KiLeidoscope KiCad", daemon=True)
        self.worker.start()

    def _kicad_loop(self):
        while not self.stopping.is_set():
            try:
                self._kicad_step()
            except Exception as exc:  # keep serving: a dead worker would freeze the viewer
                self._disconnect_reader(error=f"{type(exc).__name__}: {exc}")
            next_time = self.next_poll_at if self.reader is not None else self.next_connect_at
            self.wake.wait(max(0.0, min(0.05, next_time - self.clock())))  # a click wakes it
            self.wake.clear()

    def run_forever(self) -> None:
        try:
            self.start()
            while True:
                self.step()
                self.wait(0.05)
        finally:
            self.close()

    def close(self) -> None:
        self.stopping.set()
        self.wake.set()
        if self.worker is not None:
            self.worker.join(timeout=5.0)  # a KiCad call can take up to its 3 s timeout
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        with self.lock:
            self.server.close()
        self.copy.close()
