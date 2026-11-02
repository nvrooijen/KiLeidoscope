"""The bridge connection: a vendored copy of the bridge's frame codec
(kileido_bridge/protocol.py; tests/test_addon_protocol.py keeps them compatible)
and one non-blocking socket."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import secrets
import select
import socket
import struct

import numpy as np

PROTOCOL = 1
MAX_FRAME_BYTES = 64 * 1024 * 1024
DTYPES = {"<i4", "|u1", "<f4"}
# Every frame type the bridge sends (the bridge's protocol.MESSAGE_TYPES; a test keeps them equal).
MESSAGE_TYPES = ("snapshot_begin", "board", "layer_data", "footprints", "stackup", "flex", "snapshot_end",
                 "appearance", "selection", "status", "export", "findings", "phase_list", "phase_pair")
_LENGTH = struct.Struct(">I")
_CONNECT_PENDING = {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                    *(getattr(errno, name) for name in ("WSAEWOULDBLOCK", "WSAEINPROGRESS", "WSAEALREADY")
                      if hasattr(errno, name))}  # Windows reports its own codes
_NO_SIGNAL = getattr(socket, "MSG_NOSIGNAL", 0)


def encode_frame(header: dict, arrays: dict[str, np.ndarray] | None = None) -> bytes:
    arrays = arrays or {}
    specs, blobs = [], []
    for name, array in arrays.items():
        array = np.ascontiguousarray(array)
        dtype = array.dtype.newbyteorder("<").str
        if dtype not in DTYPES:
            raise ValueError(f"unsupported dtype {array.dtype}")
        specs.append({"name": name, "dtype": dtype, "shape": list(array.shape)})
        blobs.append(array.astype(dtype, copy=False).tobytes())
    head = json.dumps({**header, "protocol": PROTOCOL, "arrays": specs},
                      separators=(",", ":")).encode("utf-8")
    body = _LENGTH.pack(len(head)) + head + b"".join(blobs)
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError("frame too large")
    return _LENGTH.pack(len(body)) + body


def proof(token: str, side: str, nonce: str) -> str:
    """HMAC-SHA256 of "<side>:<nonce>" under the token (the bridge's protocol.proof)."""
    return hmac.new(token.encode("utf-8"), f"{side}:{nonce}".encode("utf-8"), hashlib.sha256).hexdigest()


def _shape(shape) -> bool:
    return (isinstance(shape, list) and len(shape) <= 8 and
            all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in shape))


def decode_frame(body: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    """One frame body; anything malformed raises ValueError (the bridge's decode_frame)."""
    try:
        (head_length,) = _LENGTH.unpack_from(body, 0)
        header = json.loads(body[4:4 + head_length].decode("utf-8"))
    except (struct.error, RecursionError) as exc:
        raise ValueError(f"malformed frame header: {exc}") from None
    specs = header.pop("arrays", None) if isinstance(header, dict) else None
    if not isinstance(specs, list):
        raise ValueError("frame header is not an object with an array table")
    offset = 4 + head_length
    arrays = {}
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not _shape(spec.get("shape")):
            raise ValueError("malformed array table entry")
        if not isinstance(spec.get("dtype"), str) or spec["dtype"] not in DTYPES:
            raise ValueError(f"unsupported dtype {spec.get('dtype')}")
        dtype = np.dtype(spec["dtype"])
        count = math.prod(spec["shape"])
        size = count * dtype.itemsize
        if offset + size > len(body):
            raise ValueError("frame shorter than its array table")
        arrays[spec["name"]] = np.frombuffer(body, dtype, count, offset).reshape(spec["shape"])
        offset += size
    if offset != len(body):
        raise ValueError("trailing bytes after the last array")
    return header, arrays


class FrameDecoder:
    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[dict, dict[str, np.ndarray]]]:
        self._buffer += data
        frames = []
        while len(self._buffer) >= 4:
            (length,) = _LENGTH.unpack_from(self._buffer, 0)
            if length > MAX_FRAME_BYTES:
                raise ValueError("incoming frame too large")
            if len(self._buffer) < 4 + length:
                break
            body = bytes(self._buffer[4:4 + length])
            del self._buffer[:4 + length]
            frames.append(decode_frame(body))
        return frames


class SocketClient:
    """One non-blocking TCP connection and one decoder for its entire lifetime."""

    def __init__(self, host: str, port: int, token: str):
        self.host, self.port, self.token = host, port, token
        self.socket = None
        self.decoder = FrameDecoder()
        self.outgoing = bytearray()
        self.state = "disconnected"
        self.last_error = ""
        self.nonce = ""
        self.verified = False  # the bridge's welcome proved the token; nothing is applied before

    def connect(self):
        self.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        error = sock.connect_ex((self.host, self.port))
        if error not in _CONNECT_PENDING:
            self.last_error = f"connect error {error}"
            sock.close()
            return
        self.socket = sock
        self.state = "connecting"
        if error == 0:
            self._connected()

    def _connected(self):
        self.state = "connected"
        self.nonce, self.verified = secrets.token_hex(16), False
        self.outgoing.extend(encode_frame({"type": "hello", "nonce": self.nonce,
                                           "proof": proof(self.token, "viewer", self.nonce)}))

    def _welcomed(self, frames):
        """The frames after the bridge's welcome, which comes first and proves the token:
        another program on this port is refused before anything of it is applied."""
        header = frames[0][0]
        given = header.get("proof")
        expected = proof(self.token, "bridge", self.nonce)
        if (header.get("type") != "welcome" or not isinstance(given, str)
                or not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))):
            raise ConnectionError("the program on the bridge's port is not this board's bridge")
        self.verified = True
        return frames[1:]

    def request_resync(self, adopt=False):
        """Ask for the whole board again; `adopt` (the user's Resync button) also lets the
        bridge follow a KiCad other than the one it was let into."""
        if self.state == "connected":
            self.outgoing.extend(encode_frame({"type": "resync", "adopt": True} if adopt else {"type": "resync"}))

    def request_select(self, ids, extend=False, center=False, exact=False):
        """Ask the bridge to select these KiCad items (a click in Blender), and with
        `center` to pan KiCad's PCB editor to them; `exact` (a finding) highlights the
        items alone, not their whole nets."""
        if self.state == "connected":
            self.outgoing.extend(encode_frame({"type": "select", "ids": list(ids), "extend": bool(extend),
                                               "center": bool(center), "exact": bool(exact)}))

    def request_findings(self, action, key="", source="drc", checks=None, notes=None):
        """Ask the bridge to act on its findings: "confirm", "dismiss" or "reset" the finding
        `key`, "run_drc", or make a "prompt" for the check ids
        `checks` with the user's `notes` (plain text, a prompt's only); `source` is the file
        ("drc" or "file")."""
        if self.state == "connected":
            header = {"type": "findings", "action": str(action), "key": str(key), "source": str(source)}
            if checks is not None:
                header["checks"] = [str(check) for check in checks]
            if notes is not None and action == "prompt":
                header["notes"] = str(notes)
            self.outgoing.extend(encode_frame(header))

    def request_phase_settings(self, settings: dict):
        """The dynamic-phase settings (kileido_bridge/phase.py Settings.from_request)."""
        if self.state == "connected":
            self.outgoing.extend(encode_frame({"type": "phase_settings", **settings}))

    def poll_io(self) -> list[tuple[dict, dict[str, np.ndarray]]]:
        sock = self.socket
        if sock is None:
            return []
        if self.state == "connecting":
            _, writable, failed = select.select([], [sock], [sock], 0)
            if not writable and not failed:
                return []
            error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                self.last_error = f"connect error {error}"
                self.close()
                return []
            self._connected()
        received = []
        try:
            if self.outgoing:
                try:
                    # MSG_NOSIGNAL (Linux): a reset peer must not raise SIGPIPE, which
                    # Blender's embedded Python leaves at its default: exit the process.
                    count = sock.send(self.outgoing, _NO_SIGNAL)
                    if count == 0:
                        raise ConnectionError("socket closed while sending")
                    del self.outgoing[:count]
                except (BlockingIOError, InterruptedError):
                    pass
            for _ in range(16):  # at most 1 MiB per timer tick
                try:
                    chunk = sock.recv(65536)
                except (BlockingIOError, InterruptedError):
                    break
                if not chunk:
                    raise ConnectionError("bridge closed the connection")
                received.extend(self.decoder.feed(chunk))
            if received and not self.verified:
                received = self._welcomed(received)
        except Exception as exc:  # socket errors and malformed frames alike end this connection
            self.last_error = str(exc)
            if not self.verified:
                received = []  # nothing from a peer that never proved the token
            self.close()
        return received

    def leave(self):
        """Tell the bridge this Blender is done with it (the board's X), then close: the
        bridge stops at once, not when it finds the viewer away for half a minute."""
        if self.socket is not None and self.state == "connected" and self.verified:
            self.outgoing.extend(encode_frame({"type": "bye"}))
            try:
                self.socket.send(self.outgoing, _NO_SIGNAL)  # one try: unsent, the bridge stops later
            except OSError:
                pass
        self.close()

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.state = "disconnected"
        self.verified = False
        self.decoder = FrameDecoder()
        self.outgoing.clear()
