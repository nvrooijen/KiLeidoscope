"""Load a .kls dump without blocking Blender: decode on a worker, apply on a timer."""

import queue
import threading
import time

import bpy

from . import apply
from .client import FrameDecoder
from .state import board

APPLY_BUDGET_S = 0.008  # per timer tick; one frame is the smallest unit
READ_BYTES = 1 << 20

_queue = queue.SimpleQueue()
_worker = None
_generation = 0
last_path = ""  # the dump shown ("" once a live link takes over): IMS mode reloads it (kileido._ims_rebuild)


def load_async(filepath):
    """Read and decode a .kls frame file on a worker; apply frames on Blender's timer.

    The worker never touches bpy. One FrameDecoder per stream: frames may span reads.
    A newer load supersedes frames still queued from an older one.
    """
    global _worker, _generation, last_path
    _generation += 1
    last_path = filepath
    generation = _generation
    board.status = "Loading dump…"

    def read():
        decoder = FrameDecoder()
        try:
            with open(filepath, "rb") as stream:
                while chunk := stream.read(READ_BYTES):
                    for frame in decoder.feed(chunk):
                        _queue.put((generation, frame))
        except Exception as exc:
            _queue.put((generation, exc))

    _worker = threading.Thread(target=read, name="KiLeidoscope dump reader", daemon=True)
    _worker.start()
    if not bpy.app.timers.is_registered(drain):
        bpy.app.timers.register(drain, first_interval=0.05)


def drain():
    """Timer callback: apply decoded frames for at most APPLY_BUDGET_S."""
    deadline = time.perf_counter() + APPLY_BUDGET_S
    while time.perf_counter() < deadline:
        reading = _worker is not None and _worker.is_alive()  # before get: it may finish in between
        try:
            generation, payload = _queue.get_nowait()
        except queue.Empty:
            return 0.05 if reading else None
        if generation != _generation:
            continue
        try:
            if isinstance(payload, Exception):
                raise payload
            apply.apply_frame(*payload)
        except Exception as exc:
            board.fail(f"Load failed: {exc}")
            return None
    return 0.0


def wait(timeout=5.0):
    """Finish a load synchronously (headless tests and tools)."""
    if _worker is not None:
        _worker.join(timeout=timeout)
    while drain() is not None:
        pass
