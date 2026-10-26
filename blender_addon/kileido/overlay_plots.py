"""The overlay plots drawn from kicad-cli's Gerbers, in a Python process of its own.

cosmetics' worker runs this file with Blender's Python. Parsing Gerbers is pure Python:
in a worker thread it held Python's lock (the GIL) from Blender's main thread for the
second or so after every edit, and the board's own updates waited (measured on a
1,500-object board: its zone updates 0.13 s alone, 1.84 s while Gerbers were parsed).
`plot` also runs in place when no separate Python can be started.

    python -I overlay_plots.py <job.json>

The job names each layer's Gerber file, the layers to draw and the folder to draw into;
the answer, plots.json in that folder, holds the images' rectangle and, per drawn layer,
its PNG and (walled layers) its outline .npz.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

if __package__:
    from . import gerber
else:  # run as a script: no bpy, no add-on package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gerber  # noqa: E402

ANSWER = "plots.json"


def plot(files, layers, directory, relief=(), relief_blur_m=0.0, walled=()):
    """Draw `layers` that have a file in `files` (layer -> Gerber path; "Edge.Cuts" gives
    the images' extent) into `directory`: {"rect": [x0, y0, x1, y1] or None, "plots":
    [[layer, png, outline or None], ...]}. No rectangle and no plots: nothing to draw."""
    directory = Path(directory)
    walled = tuple(walled)
    with ThreadPoolExecutor(max_workers=4) as pool:  # numpy releases the GIL while drawing
        parsed = dict(zip(files, pool.map(lambda path: gerber.parse(Path(path).read_text(encoding="utf-8")),
                                          files.values())))
        edge = parsed.pop("Edge.Cuts", None)
        # The images cover the board outline's box, as KiCad's board-area SVG plots did.
        bounds = edge.bounds(centre_line=True) if edge is not None else None
        if bounds is None:
            boxes = [box for plot in parsed.values() if (box := plot.bounds()) is not None]
            bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                      max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None
        if bounds is None:
            return {"rect": None, "plots": []}
        pixel, _, _, rect = gerber.grid(bounds)

        def draw(layer):
            plot = parsed[layer]
            if not plot.groups and not layer.endswith(".Mask"):
                return None  # an empty layer gets no overlay (a mask without openings does)
            png = directory / f"{layer}.png"
            coverage = gerber.rasterize(plot, bounds)
            if layer in relief:
                coverage = gerber.blur(coverage, max(0.7, relief_blur_m * 1e9 / pixel))
            gerber.write_png(png, coverage)
            outline = None
            if walled and layer.endswith(walled):
                points, sizes = gerber.contours(plot, spacing=pixel)
                outline = directory / f"{layer}.npz"
                # float32 relative to the image corner: a few nm at board scale
                np.savez(outline, points=(points - rect[:2]).astype(np.float32), sizes=sizes)
            return [layer, str(png), str(outline) if outline else None]

        plots = [row for row in pool.map(draw, [layer for layer in layers if layer in parsed]) if row]
    return {"rect": [float(value) for value in rect], "plots": plots}


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    answer = plot(job["files"], job["layers"], job["directory"], tuple(job["relief"]), job["relief_blur_m"],
                  tuple(job["walled"]))
    (Path(job["directory"]) / ANSWER).write_text(json.dumps(answer), encoding="utf-8")


if __name__ == "__main__":
    main()
