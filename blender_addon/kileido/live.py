"""Blender timer for the live bridge. Socket operations are always non-blocking."""

import time
from collections import deque

import bpy

from . import apply, dump, studio
from .client import PROTOCOL, SocketClient
from .state import board

HOST = "127.0.0.1"
RETRY_S = 2.0
TICK_S = 0.05
APPLY_BUDGET_S = 0.008  # per tick; one frame is the smallest unit
UPDATE_FRAMES = ("layer_data", "footprints", "snapshot_end")  # "update n s ago" in the panel


class LiveLink:
    """One connection to a bridge, reconnecting until `disconnect`.

    Frames are queued as they arrive and applied within a time budget per tick. A
    newer frame for the same (layer, kind) replaces a queued older one, and a
    snapshot supersedes everything queued before it.
    """

    def __init__(self):
        self.enabled = False
        self.port = 0
        self.token = ""
        self.client = None
        self.next_retry = 0.0
        self.pending = deque()
        self.receiving_snapshot = False
        self.bridge_status = "disconnected"  # KiCad side: "connected", "editing" or "disconnected"
        self.bridge_error = ""
        self.new_kicad = False  # the bridge waits for the user to follow another KiCad
        self.outdated_pads = 0  # pads KiCad only had an outdated shape for (after an undo)
        self.last_update_at = None
        self.last_apply_ms = None

    def connect(self, port, token):
        self.disconnect()
        self.enabled = True
        dump.last_path = ""  # the board is KiCad's from here on, not the dump's
        self.port, self.token = port, token
        self.next_retry = 0.0
        self.last_update_at = None
        if not bpy.app.timers.is_registered(tick):
            bpy.app.timers.register(tick, first_interval=TICK_S, persistent=True)

    def disconnect(self):
        self.enabled = False
        if self.client is not None:
            self.client.close()
            self.client = None
        self._drop_pending()
        self.bridge_status = "disconnected"
        self.bridge_error = ""
        self.new_kicad = False
        self.outdated_pads = 0
        if bpy.app.timers.is_registered(tick):
            bpy.app.timers.unregister(tick)
        if board.in_snapshot:
            board.fail("Stale: bridge disconnected")

    def status_text(self):
        if not self.enabled:
            return f"{board.status} — static snapshot; not connected" if board.board_name else board.status
        if self.client is None or self.client.state != "connected":
            return "Stale: bridge disconnected"
        if self.bridge_error:  # the panel shows the whole message (error_text)
            return "No connection to KiCad" if self.bridge_status == "disconnected" else "KiCad read failed"
        if self.bridge_status != "connected":
            return "Editing in KiCad…" if self.bridge_status == "editing" else "Stale: KiCad disconnected"
        if self.last_update_at is None:
            return "Connected; waiting for board"
        return f"Connected; update {time.monotonic() - self.last_update_at:.1f} s ago"

    def health(self):
        """The panel LED: "ok" live and current, "busy" waiting or KiCad editing,
        "down" link or KiCad lost, "off" never connected (a static dump)."""
        if not self.enabled:
            return "off"
        if self.client is None or self.client.state != "connected" or self.bridge_error:
            return "down"
        if self.bridge_status == "connected" and self.last_update_at is not None:
            return "ok"
        return "busy" if self.bridge_status in ("connected", "editing") else "down"

    def tick(self):
        """Timer callback: socket I/O, then apply queued frames within the budget."""
        if not self.enabled:
            return None
        self._receive()
        if studio.updates_held():  # a render or a studio add-on: received, applied afterwards
            return TICK_S
        deadline = time.perf_counter() + APPLY_BUDGET_S
        while self.pending and time.perf_counter() < deadline:
            header, arrays = self.pending.popleft()
            if not self._apply(header, arrays):
                break
        return 0.0 if self.pending else TICK_S  # queued frames continue on the next event-loop pass

    def _drop_pending(self):
        self.pending.clear()
        self.receiving_snapshot = False

    def _receive(self):
        now = time.monotonic()
        if (self.client is None or self.client.state == "disconnected") and now >= self.next_retry:
            self.client = SocketClient(HOST, self.port, self.token)
            self.client.connect()
            self.next_retry = now + RETRY_S
        if self.client is None:
            return
        frames = self.client.poll_io()
        if self.client.state == "disconnected":
            # Discard a partial final TCP read; the reconnect requests a full snapshot.
            self._drop_pending()
            board.fail("Stale: bridge disconnected")
            self.bridge_status = "disconnected"
            return
        for header, arrays in frames:
            if header.get("protocol") != PROTOCOL:
                self.client.close()
                board.fail("Stale: protocol version mismatch")
                self.pending.clear()
                return
            self._queue(header, arrays)

    def _queue(self, header, arrays):
        kind = header.get("type")
        if kind == "snapshot_begin":
            self.pending.clear()  # full snapshot supersedes queued incremental updates
            self.receiving_snapshot = True
        elif (kind == "layer_data" and not self.receiving_snapshot and
              not any(queued.get("type") == "snapshot_end" for queued, _ in self.pending)):
            key = (header.get("layer"), header.get("kind"))
            kept = [(queued, queued_arrays) for queued, queued_arrays in self.pending
                    if queued.get("type") != "layer_data" or (queued.get("layer"), queued.get("kind")) != key]
            self.pending.clear()
            self.pending.extend(kept)
        self.pending.append((header, arrays))
        if kind == "snapshot_end":
            self.receiving_snapshot = False

    def _apply(self, header, arrays):
        """Apply one frame; False stops this tick (a failure asks for a full resync)."""
        started = time.perf_counter()
        try:
            apply.apply_frame(header, arrays)
        except Exception as exc:
            board.fail(f"Stale: apply failed: {exc}")
            self.pending.clear()
            request_resync()
            return False
        self.last_apply_ms = (time.perf_counter() - started) * 1000
        kind = header["type"]
        if kind == "status":
            self.bridge_status = header.get("kicad", "disconnected")
            self.bridge_error = header.get("error", "")
            self.new_kicad = bool(header.get("new_kicad", False))
            self.outdated_pads = int(header.get("outdated_pads", 0))
        elif kind in UPDATE_FRAMES:
            self.last_update_at = time.monotonic()
        return True


link = LiveLink()


def tick():
    try:
        return link.tick()
    except Exception as exc:  # Blender drops a timer that raises: keep the link alive, start over
        link.pending.clear()
        board.fail(f"Stale: {exc}")
        request_resync()
        return RETRY_S


def connected() -> bool:
    """A live session (it may be reconnecting); False for a static dump."""
    return link.enabled


def linked() -> bool:
    """The bridge is reachable now, so requests to it arrive."""
    return link.client is not None and link.client.state == "connected"


def connect(port: int, token: str):
    link.connect(port, token)


def disconnect():
    link.disconnect()


def status_text() -> str:
    return link.status_text()


def health() -> str:
    return link.health()


def error_text() -> str:
    """The bridge's last error in full (why the board cannot be read), or ""."""
    return link.bridge_error if link.enabled else ""


def last_apply_text() -> str:
    return "" if link.last_apply_ms is None else f"Last apply: {link.last_apply_ms:.2f} ms"


def outdated_pads_text() -> str:
    """KiCad answered for some pads with an outdated copy that only placement could not fix."""
    count = link.outdated_pads if link.enabled else 0
    return f"{count} pad shape(s) outdated in KiCad (undo): reopen the board" if count else ""


def new_kicad() -> bool:
    """Another KiCad now serves the plugin connection; a Resync follows it."""
    return link.enabled and link.new_kicad


def request_resync(adopt=False):
    if link.client is not None:
        link.client.request_resync(adopt)


def request_select(ids, extend=False, center=False):
    """A click in Blender: select these items in KiCad, and with `center` pan KiCad to them."""
    if link.client is not None:
        link.client.request_select(ids, extend, center)
