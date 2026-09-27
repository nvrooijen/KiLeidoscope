"""The DC solve in its own process, so the 200 ms KiCad poll never waits for it.

`SolverProcess` (bridge side) keeps one worker process running
`python -c ... dcworker.main()` and talks to it over its stdin and stdout:
length-prefixed pickles, a job in, progress, log lines and one result or error
out. A reader thread moves the replies to a queue that `poll` drains. A job that
is superseded (the copper changed while it ran) is not waited for: `cancel` kills
the process, which also stops a direct sparse solve that never checks in, and
starts a fresh one so its imports are done before the next job.

`solve` is the worker's half: Fill Resistance's pipeline without its figures
(rasterize, contact masks, PDN solve), then the fields reduced to what the viewer
draws: per copper layer |J| (A/mm2), potential (V) and the current direction on a
grid of at most `MAX_DISPLAY_CELLS` pixels, and the current through each barrel.
"""

import json
import math
import os
import pickle
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import warnings
from pathlib import Path

import numpy as np

MAX_DISPLAY_CELLS = 1_500_000  # all layers together: 4 float32 fields stay under 24 MB
J_TOP_PERCENTILE = 99.9  # of the copper cells' |J|: the "max" the viewer shows
_LENGTH = struct.Struct(">I")
_BOOT = ("import json, os, sys; sys.path[:0] = json.loads(os.environ['KILEIDO_DC_PATH']); "
         "from kileido_bridge.dcworker import main; main()")


# --- Worker side ----------------------------------------------------------------------

def solve(built, cell_um=None, display_cells=None) -> dict:
    """Solve a dc.Built problem; everything the viewer shows, as plain data. A
    guessed load (built.optional) on copper cut off from every supply is left out
    (named in "left_out") and the net solved again, as long as a load remains."""
    from .dcsolve.errors import ConnectivityError
    left_out = []
    while True:
        try:
            return {**_solve(built, cell_um, display_cells), "left_out": left_out}
        except ConnectivityError as exc:
            cut_off = re.search(r"Load '([^']+)'", str(exc))
            loads = [terminal for terminal in built.problem.terminals if terminal.role == "load"]
            if cut_off is None or cut_off[1] not in built.optional or len(loads) < 2:
                raise
            built.problem.terminals = [terminal for terminal in built.problem.terminals
                                       if terminal.label != cut_off[1]]
            left_out.append(cut_off[1])


def _solve(built, cell_um, display_cells) -> dict:
    from .dcsolve import config, raster, solver
    started = time.perf_counter()
    config.CELL_UM_OVERRIDE = cell_um
    config.ADAPTIVE_CELLS = True
    problem = built.problem
    h = raster.choose_cell_size(problem.copper_bbox(), len(problem.layers))
    _stage(f"rasterizing {len(problem.layers)} layer(s) at {h / 1000:.0f} um")
    stack = raster.rasterize_stack(problem, h)
    masks = raster.terminal_masks(stack, problem)
    parts = raster.terminal_partition(stack, problem)
    rastered = time.perf_counter()
    draw = sum(t.i_draw_a for t in problem.terminals if t.role == "load")
    _stage(f"solving {draw:g} A over {int(stack.masks.sum()):,} cells")
    result = solver.run_solve_pdn(problem, stack, masks, parts)
    solved = time.perf_counter()
    display = display_fields(stack, result, problem, display_cells or MAX_DISPLAY_CELLS)
    finished = time.perf_counter()
    v_ref = max(s.v_oc for s in result.supplies)
    loads = [{"name": load.label, "i_a": load.i_a, "v_mean": load.v_mean, "v_min": load.v_min, "p_w": load.p_w,
              "drop_mean_v": v_ref - load.v_mean, "drop_max_v": v_ref - load.v_min} for load in result.loads]
    return {
        "net": problem.net_name, "layers": list(stack.layer_names), "v_ref": v_ref,
        "cell_nm": h, "cells": int(stack.masks.sum()), "unknowns": int(result.n_free),
        "method": result.solve_info.method, "iterations": result.solve_info.iterations,
        "supplies": [{"name": s.label, "v_oc": s.v_oc, "i_a": s.i_a, "v_contact": s.v_contact}
                     for s in result.supplies],
        "loads": loads,
        "pairs": [{"supply": p.supply, "load": p.load, "r_ohm": p.r_ohm, "i_a": p.i_share_a, "p_w": p.p_w}
                  for p in result.pairs],
        "p_copper_w": float(result.P_total), "p_layers_w": [float(p) for p in result.P_layers],
        "p_vias_w": float(result.P_vias), "p_loads_w": float(result.P_loads),
        "power_balance": float(result.power_balance_rel),
        "timings_s": {"raster": rastered - started, "solve": solved - rastered, "display": finished - solved,
                      **{key.removesuffix("_s"): value for key, value in result.timings.items()}},
        **display, "barrels": _barrels(built, result),
    }


def _stage(text):
    from .dcsolve import progress
    progress.stage(text)


def _barrels(built, result) -> dict:
    """Current and loss of each via and plated pad the solve used (the largest
    segment current where a barrel joins several layers)."""
    index = {(v.x, v.y, v.kind): n for n, v in enumerate(built.problem.vias)}
    rows = []
    for report in result.via_reports:
        n = index.get((round(report.x_mm * 1e6), round(report.y_mm * 1e6), report.kind))
        if n is not None:
            rows.append((n, report.current_a, report.power_w))
    rows.sort(key=lambda row: -row[1])
    vias = built.problem.vias
    return {"ids": [built.barrel_ids[n] for n, _, _ in rows], "spans": [built.barrel_layers[n] for n, _, _ in rows],
            "xy": np.array([(vias[n].x, vias[n].y) for n, _, _ in rows], np.int64).reshape(-1, 2),
            "size": np.array([vias[n].pad_nm or vias[n].drill_nm for n, _, _ in rows], np.int64),
            "current_a": np.array([current for _, current, _ in rows], np.float64),
            "power_w": np.array([power for _, _, power in rows], np.float64)}


def _flow(stack, V: np.ndarray, rho: float) -> tuple[np.ndarray, np.ndarray]:
    """Direction of the current per cell (x right, y down, unnormalized): the mean
    of the face currents around sheet cells; along the links for the 1D chains of
    narrow tracks (their cells have no sheet faces)."""
    masks = stack.masks
    sheet = masks & ~stack.chain if stack.chain is not None else masks
    v = np.nan_to_num(V)
    fx = np.where(sheet[:, :, :-1] & sheet[:, :, 1:], v[:, :, :-1] - v[:, :, 1:], 0.0)
    fy = np.where(sheet[:, :-1, :] & sheet[:, 1:, :], v[:, :-1, :] - v[:, 1:, :], 0.0)
    jx = np.zeros(masks.shape)
    jy = np.zeros(masks.shape)
    jx[:, :, :-1] += fx
    jx[:, :, 1:] += fx
    jy[:, :-1, :] += fy
    jy[:, 1:, :] += fy
    if stack.chain is not None and stack.chain_edges is not None and len(stack.chain_edges[0]):
        a, b, _, _, length = stack.chain_edges
        flat = masks.reshape(-1)
        alive = flat[a] & flat[b]
        a, b, length = a[alive], b[alive], length[alive]
        _, ny, nx = masks.shape
        step = np.stack([(b % nx) - (a % nx), (b // nx) % ny - (a // nx) % ny], axis=1).astype(np.float64)
        step /= np.maximum(np.hypot(*step.T), 1e-12)[:, None]
        current = (v.reshape(-1)[a] - v.reshape(-1)[b]) / (rho * length)  # A/m2 along the link, a to b
        chain = stack.chain.reshape(-1)
        for axis, out in enumerate((jx.reshape(-1), jy.reshape(-1))):
            for end in (a, b):
                keep = chain[end]
                np.add.at(out, end[keep], current[keep] * step[keep, axis])
    return jx, jy


def display_fields(stack, result, problem, max_cells=MAX_DISPLAY_CELLS) -> dict:
    """|J|, V and the current direction per layer, block-reduced to at most
    `max_cells` pixels: |J| keeps each block's maximum (hot spots stay visible),
    V and the direction their mean. NaN where there is no copper."""
    L, ny, nx = stack.masks.shape
    factor = max(1, math.ceil(math.sqrt(L * ny * nx / max_cells)))
    jx, jy = _flow(stack, result.V, problem.rho_ohm_m)
    norm = np.hypot(jx, jy)
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(norm > 0, result.Jmag / norm, 0.0)
    fields = {"j": result.Jmag * 1e-6, "v": result.V, "jx": jx * scale * 1e-6, "jy": jy * scale * 1e-6}
    for key in ("jx", "jy"):
        fields[key][~stack.masks] = np.nan
    if factor > 1:
        rows, columns = -(-ny // factor) * factor, -(-nx // factor) * factor
        for key, values in fields.items():
            padded = np.full((L, rows, columns), np.nan)
            padded[:, :ny, :nx] = values
            blocks = padded.reshape(L, rows // factor, factor, columns // factor, factor)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks stay NaN
                fields[key] = np.nanmax(blocks, axis=(2, 4)) if key == "j" else np.nanmean(blocks, axis=(2, 4))
    copper = result.Jmag[np.isfinite(result.Jmag)] * 1e-6
    return {"grid": {"x0_nm": float(stack.x0_nm), "y0_nm": float(stack.y0_nm),
                     "pitch_nm": float(stack.h_nm * factor), "factor": factor},
            "fields": {key: values.astype(np.float32) for key, values in fields.items()},
            # The top of the colour range: single cells a via drill almost fills read far
            # higher (their thin remaining copper), so the peak is kept apart.
            "j_max": float(np.percentile(copper, J_TOP_PERCENTILE)) if len(copper) else 0.0,
            "j_peak": float(copper.max()) if len(copper) else 0.0,
            "v_min": float(np.nanmin(result.V)), "v_max": float(np.nanmax(result.V))}


class _LogLines:
    """The vendored solver prints its notes and warnings: pass them on line by line."""

    def __init__(self, send):
        self.send, self.buffer = send, ""

    def write(self, text):
        self.buffer += text
        *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            if line.strip():
                self.send(("log", None, line.strip()))
        return len(text)

    def flush(self):
        pass


def main():
    """The worker process: jobs from stdin, replies on the original stdout. Anything
    else written to stdout (C libraries) goes to stderr, never into the replies."""
    from .dcsolve import progress
    from .dcsolve.errors import UserFacingError
    replies = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    lock = threading.Lock()

    def send(message):
        data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
        with lock:
            replies.write(_LENGTH.pack(len(data)) + data)
            replies.flush()

    sys.stdout = _LogLines(send)
    try:
        from .dcsolve import adaptive, solver  # noqa: F401  ~1 s of imports, done before the first job
        missing = ""
    except ImportError as exc:
        missing = (f"The DC analysis needs SciPy and PyAMG ({exc}). Reinstall KiLeidoscope's package "
                   "so KiCad installs them, or pip install scipy pyamg")
    source = sys.stdin.buffer
    send(("ready", None, os.getpid()))
    while True:
        job = _read(source)
        if job is None:
            return
        job_id, items, options = job
        for index, (net, built) in enumerate(items):
            if missing:
                send(("error", job_id, (net, missing)))
                continue
            progress.report = (lambda text, elapsed, job_id=job_id, index=index, net=net:
                               send(("progress", job_id, (text, elapsed, index, len(items), net))))
            progress.start()
            try:
                send(("result", job_id, {**solve(built, **options), "net": net}))
            except UserFacingError as exc:
                send(("error", job_id, (net, str(exc))))
            except Exception as exc:  # a solver bug: report it, go on with the next net
                traceback.print_exc()
                send(("error", job_id, (net, f"DC solve failed: {type(exc).__name__}: {exc}")))
            finally:
                progress.done()
        send(("done", job_id, None))


def _read(source):
    head = source.read(_LENGTH.size)
    if len(head) < _LENGTH.size:
        return None
    (length,) = _LENGTH.unpack(head)
    data = source.read(length)
    return pickle.loads(data) if len(data) == length else None


# --- Bridge side ----------------------------------------------------------------------

class SolverProcess:
    """One worker process at a time; replies of older processes and jobs are dropped."""

    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path
        self.process = None
        self.replies = queue.Queue()
        self.job = None  # id of the job the current process is running

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        root = str(Path(__file__).resolve().parents[1])
        paths = [root] + [path for path in sys.path if isinstance(path, str) and path and path != root]
        environment = {**os.environ, "KILEIDO_DC_PATH": json.dumps(paths)}
        log = subprocess.DEVNULL
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                log = open(self.log_path, "ab")
            except OSError:
                pass
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-c", _BOOT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            if log is not subprocess.DEVNULL:
                log.close()
        self.job = None
        threading.Thread(target=self._read, args=(self.process,), name="KiLeidoscope DC replies",
                         daemon=True).start()

    def _read(self, process):
        while True:
            try:
                message = _read(process.stdout)
            except Exception:
                message = None
            if message is None:
                self.replies.put((process, ("exit", None, process.wait())))
                return
            self.replies.put((process, message))

    def submit(self, job_id, items, **options):
        """Solve [(net, dc.Built), ...] in turn; the process must be idle (cancel a
        running job first). Each net replies with a "result" or an "error"
        (net, message), then the job with "done"."""
        self.start()
        data = pickle.dumps((job_id, items, options), protocol=pickle.HIGHEST_PROTOCOL)
        try:
            self.process.stdin.write(_LENGTH.pack(len(data)) + data)
            self.process.stdin.flush()
        except OSError:  # it died meanwhile: start over once
            self.restart()
            self.process.stdin.write(_LENGTH.pack(len(data)) + data)
            self.process.stdin.flush()
        self.job = job_id

    def cancel(self):
        """Stop the running job at once, and start a fresh process for the next."""
        if self.job is not None:
            self.restart()

    def restart(self):
        self.stop()
        self.start()

    def poll(self) -> list:
        """(kind, job id, payload) replies of the current process: "ready", "progress",
        "log", "result", "error", "done" and "exit" (it died)."""
        messages = []
        while True:
            try:
                process, message = self.replies.get_nowait()
            except queue.Empty:
                return messages
            if process is not self.process:
                continue
            if message[0] == "done" and message[1] == self.job:
                self.job = None
            elif message[0] == "exit":
                self.process = None
                self.job = None
            messages.append(message)

    def stop(self):
        process, self.process, self.job = self.process, None, None
        if process is None:
            return
        try:
            process.kill()
        except OSError:
            pass
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass

    close = stop
