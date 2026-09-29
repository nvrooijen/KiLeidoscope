"""One-viewer, loopback-only, non-blocking binary frame server.

Any local process can open a connection, so a new connection waits in its own
slot until its first frame proves the token. Only then does it replace the
current viewer. An unauthenticated connection can therefore never disconnect
the working viewer, and its frames are capped at a few kilobytes.
"""

from __future__ import annotations

import hmac
import secrets
import select
import socket
import sys
import time

from .protocol import FrameDecoder

DEFAULT_PORT = 47811
MAX_PENDING_BYTES = 16 * 1024 * 1024
HELLO_MAX_BYTES = 64 * 1024  # a hello frame is a few hundred bytes
VIEWER_MAX_BYTES = 4 * 1024 * 1024  # viewer frames are small; a selection holds at most MAX_SELECT_IDS ids
HELLO_TIMEOUT_S = 5.0
MAX_SELECT_IDS = 10_000
RECV_BYTES = 1 << 20


class BridgeServer:
    def __init__(self, port: int = DEFAULT_PORT, token: str | None = None):
        self.token = token or secrets.token_urlsafe(18)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # Windows SO_REUSEADDR would let another process bind the same port.
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(1)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]
        self.client = None  # the authenticated viewer
        self.decoder = None
        self.candidate = None  # a new connection that has not sent its hello yet
        self.candidate_decoder = None
        self.candidate_deadline = 0.0
        self.needs_snapshot = False
        self.select_requests = []
        self.outgoing = bytearray()
        self.snapshot_bytes = 0  # of `outgoing`: the latest snapshot, exempt from the backlog cap

    @property
    def authenticated(self) -> bool:
        return self.client is not None

    def _drop_client(self):
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None
        self.decoder = None
        self.needs_snapshot = False
        self.snapshot_bytes = 0
        self.outgoing.clear()

    def _drop_candidate(self):
        if self.candidate is not None:
            try:
                self.candidate.close()
            finally:
                self.candidate = None
        self.candidate_decoder = None

    def _accept(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                return
            self._drop_candidate()  # the newest unproven connection wins the waiting slot
            connection.setblocking(False)
            self.candidate = connection
            self.candidate_decoder = FrameDecoder(HELLO_MAX_BYTES)  # decoder state belongs to this TCP connection
            self.candidate_deadline = time.monotonic() + HELLO_TIMEOUT_S

    def _valid_hello(self, header: dict) -> bool:
        token = header.get("token")
        return (header.get("type") == "hello" and isinstance(token, str) and
                hmac.compare_digest(token.encode(), self.token.encode()))

    def _read_candidate(self):
        """A valid hello promotes the candidate to viewer, replacing the previous one."""
        try:
            data = self.candidate.recv(RECV_BYTES)
            frames = self.candidate_decoder.feed(data) if data else None
        except (OSError, ValueError, KeyError):
            frames = None
        if frames is None or (frames and not self._valid_hello(frames[0][0])):
            self._drop_candidate()
            return
        if not frames:
            return  # hello not complete yet
        decoder = self.candidate_decoder
        decoder.max_frame_bytes = VIEWER_MAX_BYTES
        self._drop_client()
        self.client, self.decoder = self.candidate, decoder
        self.candidate, self.candidate_decoder = None, None
        self.needs_snapshot = True
        for header, _ in frames[1:]:
            self._handle(header)

    def _handle(self, header: dict):
        if header.get("type") == "resync":
            self.needs_snapshot = True
        elif header.get("type") == "select":  # a click in Blender
            ids = [str(item) for item in header.get("ids", ())][:MAX_SELECT_IDS]
            extend, center = bool(header.get("extend", False)), bool(header.get("center", False))
            self.select_requests.append((ids, extend, center))

    def pump(self, timeout: float = 0.0) -> None:
        if self.candidate is not None and time.monotonic() > self.candidate_deadline:
            self._drop_candidate()
        reads = [self.listener]
        writes = []
        for connection in (self.candidate, self.client):
            if connection is not None:
                reads.append(connection)
        if self.client is not None and self.outgoing:
            writes.append(self.client)
        try:
            ready_read, ready_write, errors = select.select(reads, writes, reads, timeout)
        except OSError:
            self._drop_candidate()
            self._drop_client()
            return
        candidate = self.candidate
        if candidate is not None:
            if candidate in errors:
                self._drop_candidate()
            elif candidate in ready_read:
                self._read_candidate()
        if self.listener in ready_read:
            self._accept()
        client = self.client
        if client is None or client is candidate:
            return  # no viewer, or it was only just promoted and has not been polled yet
        if client in errors:
            self._drop_client()
            return
        if client in ready_read:
            try:
                data = client.recv(RECV_BYTES)
                if not data:
                    self._drop_client()
                    return
                for header, _ in self.decoder.feed(data):
                    self._handle(header)
            except (OSError, ValueError, KeyError):
                self._drop_client()
                return
        if client is self.client and client in ready_write:
            try:
                count = client.send(self.outgoing)
                del self.outgoing[:count]
                self.snapshot_bytes = max(0, self.snapshot_bytes - count)
            except (BlockingIOError, InterruptedError):
                pass  # send buffer full; the rest goes on the next pump
            except OSError:
                self._drop_client()

    def take_select_requests(self) -> list[tuple[list[str], bool, bool]]:
        requests, self.select_requests = self.select_requests, []
        return requests

    def take_resync(self) -> bool:
        if not self.authenticated or not self.needs_snapshot:
            return False
        self.needs_snapshot = False
        self.outgoing.clear()  # a full snapshot replaces any queued older updates
        self.snapshot_bytes = 0
        return True

    def send_frames(self, frames, snapshot: bool = False) -> None:
        """Queue frames for the viewer. A snapshot may be any size; a viewer that falls
        more than MAX_PENDING_BYTES behind beyond it is dropped (and gets a new snapshot
        when it reconnects)."""
        if not self.authenticated:
            return
        for frame in frames:
            self.outgoing.extend(frame)
            if snapshot:
                self.snapshot_bytes += len(frame)
            elif len(self.outgoing) > MAX_PENDING_BYTES + self.snapshot_bytes:
                self._drop_client()
                return

    def close(self) -> None:
        self._drop_candidate()
        self._drop_client()
        self.listener.close()
