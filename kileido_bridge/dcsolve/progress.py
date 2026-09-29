# Adapted from Fill Resistance 1.4.2, fill_resistance/progress.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Rewritten without its Qt busy window: the same stage/tick/Cancelled interface,
# reporting to a callback the KiLeidoscope solve worker (kileido_bridge.dcworker) sets.
"""Progress of the running solve, for the process that runs it.

The solver calls stage() when a phase starts and tick() from inside the
linear-solver iterations. Both are no-ops until `report` is set; tick()
is throttled, so it is safe to call every iteration. A cancelled solve
(`cancel()`) raises Cancelled at its next tick.
"""
from __future__ import annotations

import time

TICK_INTERVAL_S = 0.25          # progress messages at most this often

report = None                   # callable(stage_text, elapsed_s) or None
_text = ""
_t0 = 0.0
_last = 0.0
_cancelled = False


class Cancelled(Exception):
    """The solve was superseded or stopped. Not a failure."""


def start(title: str = "") -> bool:
    global _t0, _last, _cancelled, _text
    _t0, _last, _cancelled, _text = time.monotonic(), 0.0, False, ""
    return report is not None


def cancel() -> None:
    global _cancelled
    _cancelled = True


def stage(text: str, echo: bool = True) -> None:
    """Name the phase now running (always reported: stages are rare)."""
    global _text, _last
    _text = text
    if report is not None:
        _last = time.monotonic()
        report(text, _last - _t0)


def tick() -> None:
    global _last
    if _cancelled:
        raise Cancelled()
    if report is None:
        return
    now = time.monotonic()
    if now - _last < TICK_INTERVAL_S:
        return
    _last = now
    report(_text, now - _t0)


def done() -> None:
    global _text, _cancelled
    _text, _cancelled = "", False
