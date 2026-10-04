"""Hatched fills of board shapes, as KiCad itself draws them.

KiCad 10 fills a shape (rectangle, polygon, circle) solid, or with a hatch: lines at 45°
one way (`hatch`), the other (`reverse_hatch`) or both (`cross_hatch`). The board file
keeps only that word: KiCad works the hatch out from the shape when it draws it
(`EDA_SHAPE::UpdateHatching`). Two things never do (KiCad 10.0.3, measured):
- the IPC API, which reports a hatched shape as unfilled (its enum has no hatch), so
  hatched copper arrived as its outline alone;
- kicad-cli's plots, which load the file and plot the shape's outline only, so the
  hatch was missing from the mask, silkscreen and copper overlays (and from Gerbers).

So when the board text has a hatched fill, KiCad's own Python (`pcbnew`) loads the live
board copy in a background process, asks each hatched shape for its hatch, and returns
them as polygons. The bridge adds them to the copper graphics (`apply`) and bakes them
into its private live board copy as plain filled polygons (`bake`), which kicad-cli then
plots. The user's files are only read. Without hatched fills nothing runs; without a
KiCad Python that imports pcbnew, hatches stay outlines, as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from . import model

HATCHED = re.compile(r"\(fill (?:hatch|reverse_hatch|cross_hatch)\)")
TIMEOUT_S = 120

# Run by KiCad's Python: every hatched shape on the board or in a footprint, its hatch
# fractured into outlines (no holes), in nm, by its id: {id: [layer number, [rings]]}.
SCRIPT = r'''
import json, sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
items = list(board.GetDrawings())
for footprint in board.GetFootprints():
    items += list(footprint.GraphicalItems())
found = {}
for item in items:
    item = item.Cast()
    if not isinstance(item, pcbnew.PCB_SHAPE) or not item.IsHatchedFill():
        continue
    item.UpdateHatching()
    hatch = pcbnew.SHAPE_POLY_SET(item.GetHatching())
    hatch.Fracture()
    rings = []
    for index in range(hatch.OutlineCount()):
        outline = hatch.Outline(index)
        rings.append([[outline.CPoint(k).x, outline.CPoint(k).y] for k in range(outline.PointCount())])
    found[item.m_Uuid.AsString()] = [item.GetLayer(), rings]
json.dump(found, sys.stdout)
'''


def has_hatch(text: str) -> bool:
    return HATCHED.search(text) is not None


def digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


def layer_names(text: str) -> dict[int, str]:
    """The board's layer numbers and canonical names, from its (layers ...) table."""
    start = text.find("\n\t(layers")
    end = text.find("\n\t)", start)
    return {int(number): name for number, name in re.findall(r'\((\d+) "([^"]+)"', text[start:end])}


def shapes_from(raw: dict, text: str) -> dict[str, tuple[str, tuple[model.Ring, ...]]]:
    """{id: (canonical layer, rings)} from the script's output and the board's layer table."""
    names = layer_names(text)
    return {uid: (names.get(number, ""), tuple(tuple((int(x), int(y)) for x, y in ring) for ring in rings))
            for uid, (number, rings) in raw.items() if number in names}


def apply(graphics, shapes) -> tuple:
    """Copper graphics with each hatched one's hatch added to its polygons."""
    out = []
    for graphic in graphics:
        found = shapes.get(graphic.id)
        if found is not None and found[0] == graphic.layer and found[1]:
            graphic = model.CopperGraphic(graphic.id, graphic.net, graphic.layer,
                                          graphic.polygons + tuple((ring,) for ring in found[1]))
        out.append(graphic)
    return tuple(out)


def bake(text: str, shapes) -> str:
    """The board text with every hatch (of a shape still in it) as a filled polygon on its
    layer, for kicad-cli to plot. Deterministic: the same text and hatches, the same copy."""
    added = []
    for uid in sorted(shapes):
        layer, rings = shapes[uid]
        if not layer or f'"{uid}"' not in text:
            continue
        for index, ring in enumerate(rings):
            points = " ".join(f"(xy {x / 1e6:.6f} {y / 1e6:.6f})" for x, y in ring)
            mark = uuid.uuid5(uuid.NAMESPACE_URL, f"kileidoscope-hatch-{uid}-{index}")
            added.append(f'\t(gr_poly\n\t\t(pts {points})\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type solid)\n\t\t)\n'
                         f'\t\t(fill yes)\n\t\t(layer "{layer}")\n\t\t(uuid "{mark}")\n\t)\n')
    if not added:
        return text
    end = text.rstrip().rfind(")")
    return text[:end] + "".join(added) + text[end:]


def python_candidates(kicad_cli: str) -> list[str]:
    """Where KiCad's Python (the one with pcbnew) may be, from kicad-cli's path."""
    found = [os.environ.get("KILEIDO_KICAD_PYTHON", "")]
    if kicad_cli:
        cli = Path(kicad_cli)
        found.append(str(cli.with_name("python.exe")))  # Windows: beside kicad-cli.exe
        contents = next((parent for parent in cli.parents if parent.name == "Contents"), None)
        if contents is not None:  # macOS: inside KiCad.app
            found.append(str(contents / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3"))
    found += ["/usr/bin/python3", shutil.which("python3") or "", sys.executable]  # Linux: the system's
    return [path for path in dict.fromkeys(found) if path and Path(path).is_file()]


def find_python(kicad_cli: str) -> str:
    """The first candidate that imports pcbnew, or ""."""
    for path in python_candidates(kicad_cli):
        try:
            result = subprocess.run([path, "-c", "import pcbnew"], capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return path
    return ""


def compute(python: str, text: str) -> dict:
    """{id: (layer, rings)} for the board `text`, worked out by KiCad's Python."""
    with tempfile.TemporaryDirectory(prefix="kileido_hatch_") as directory:
        board = Path(directory) / "board.kicad_pcb"
        board.write_text(text, encoding="utf-8")
        result = subprocess.run([python, "-c", SCRIPT, str(board)], capture_output=True, timeout=TIMEOUT_S,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-300:])
    return shapes_from(json.loads(result.stdout), text)


class Hatcher:
    """Works hatches out in the background, the newest board text first.

    `want(text)` hands it the board text (cheap: a regex, nothing for unhatched boards);
    `shapes` is the latest result, `version` counts results. Until a new text's hatches
    are ready the last ones stand (a moved hatched shape catches up a moment later)."""

    def __init__(self, kicad_cli: str = "", compute_fn=None):
        self.kicad_cli = kicad_cli
        self._compute = compute_fn
        self.shapes: dict = {}
        self.version = 0
        self.error = ""
        self._done = None  # digest of the text `shapes` are for
        self._wanted = None  # (digest, text)
        self._cleared = 0  # counts boards without hatches: a job started before one is dropped
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._python = None

    def want(self, text: str) -> None:
        if not has_hatch(text):
            with self._lock:
                self._wanted = None
                self._cleared += 1
                if self.shapes:
                    self.shapes, self._done = {}, None
                    self.version += 1
            return
        key = digest(text)
        with self._lock:
            if key == self._done or (self._wanted and self._wanted[0] == key):
                return
            self._wanted = (key, text)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kileido-hatch", daemon=True)
            self._thread.start()
        self._wake.set()

    def ready_for(self, text: str) -> bool:
        return self._done == digest(text)

    def result(self) -> tuple[int, dict]:
        """(version, shapes), read together: the version is the one those shapes have."""
        with self._lock:
            return self.version, self.shapes

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                job, self._wanted = self._wanted, None
                cleared = self._cleared
            if job is None:
                continue
            key, text = job
            try:
                shapes = self._work(text)
            except Exception as exc:  # no KiCad Python, or it failed: hatches stay outlines
                self.error = f"{type(exc).__name__}: {exc}"
                shapes = None
            with self._lock:
                if cleared != self._cleared:
                    continue  # the board lost its hatches meanwhile: these are stale
                if shapes is not None:
                    self.error = ""
                    if shapes != self.shapes:
                        self.shapes = shapes
                        self.version += 1
                self._done = key  # tried: not again for this text

    def _work(self, text: str) -> dict:
        if self._compute is not None:
            return self._compute(text)
        if self._python is None:
            self._python = find_python(self.kicad_cli)
        if not self._python:
            raise RuntimeError("no KiCad Python with pcbnew found")
        return compute(self._python, text)
