"""The bridge connection: a vendored copy of the bridge's frame codec
(kileido_bridge/protocol.py; tests/test_addon_protocol.py keeps them compatible)
and one non-blocking socket."""

import errno
import json
import select
import socket
import struct

import numpy as np

PROTOCOL = 1
MAX_FRAME_BYTES = 64 * 1024 * 1024
DTYPES = {"<i4", "|u1", "<f4"}
MESSAGE_TYPES = ("snapshot_begin", "board", "layer_data", "footprints", "stackup", "snapshot_end",
                 "appearance", "selection", "return_path", "status")  # apply.apply_frame handles each
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


def decode_frame(body: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    (head_length,) = _LENGTH.unpack_from(body, 0)
    header = json.loads(body[4:4 + head_length].decode("utf-8"))
    offset = 4 + head_length
    arrays = {}
    for spec in header.pop("arrays"):
        if spec["dtype"] not in DTYPES:
            raise ValueError(f"unsupported dtype {spec['dtype']}")
        dtype = np.dtype(spec["dtype"])
        count = int(np.prod(spec["shape"], dtype=np.int64))
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
        self.outgoing.extend(encode_frame({"type": "hello", "token": self.token}))

    def request_resync(self):
        if self.state == "connected":
            self.outgoing.extend(encode_frame({"type": "resync"}))

    def request_select(self, ids, extend=False):
        """Ask the bridge to select these KiCad items (a click in Blender)."""
        if self.state == "connected":
            self.outgoing.extend(encode_frame({"type": "select", "ids": list(ids), "extend": bool(extend)}))

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
        except Exception as exc:  # socket errors and malformed frames alike end this connection
            self.last_error = str(exc)
            self.close()
        return received

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.state = "disconnected"
        self.decoder = FrameDecoder()
        self.outgoing.clear()
