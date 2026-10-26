"""The overlay plots drawn in a Python process of their own (`overlay_plots.py`, run by
cosmetics' worker), without Blender: the same answer as drawing in place."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "overlay_plots.py"
_SPEC = importlib.util.spec_from_file_location("kileido_overlay_plots", _PATH)
overlay_plots = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(overlay_plots)

HEADER = "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\nG01*\n"
EDGE = "%ADD10C,0.100000*%\nD10*\nX0Y0D02*\nX10000000Y0D01*\nX10000000Y8000000D01*\nX0Y8000000D01*\nX0Y0D01*\n"
SILK = "%ADD11C,0.150000*%\nD11*\nX2000000Y2000000D02*\nX8000000Y2000000D01*\n"
MASK = "%ADD12R,1.000000X1.000000*%\nD12*\nX5000000Y5000000D03*\n"


def _job(folder):
    files = {}
    for layer, body in (("Edge.Cuts", EDGE), ("F.SilkS", SILK), ("F.Mask", MASK), ("F.Fab", "")):
        path = folder / f"{layer}.gbr"
        path.write_text(HEADER + body + "M02*\n", encoding="utf-8")
        files[layer] = str(path)
    return {"files": files, "layers": ["F.Mask", "F.SilkS", "F.Fab"], "directory": str(folder),
            "relief": [], "relief_blur_m": 50e-6, "walled": [".SilkS"]}


def test_a_separate_process_draws_what_drawing_in_place_draws(tmp_path):
    apart, here = tmp_path / "process", tmp_path / "in_place"
    apart.mkdir()
    here.mkdir()
    job = _job(apart)
    (apart / "job.json").write_text(json.dumps(job), encoding="utf-8")
    finished = subprocess.run([sys.executable, "-I", str(_PATH), str(apart / "job.json")],
                              capture_output=True, text=True, timeout=120)
    assert finished.returncode == 0, finished.stderr
    answer = json.loads((apart / overlay_plots.ANSWER).read_text(encoding="utf-8"))

    in_place = overlay_plots.plot({**_job(here)["files"]}, job["layers"], here, (), 50e-6, (".SilkS",))
    assert answer["rect"] == in_place["rect"] and answer["rect"][2] > answer["rect"][0]
    assert [row[0] for row in answer["plots"]] == ["F.Mask", "F.SilkS"]  # an empty F.Fab is left out
    for (layer, png, outline), (_, png_here, outline_here) in zip(answer["plots"], in_place["plots"]):
        assert Path(png).read_bytes() == Path(png_here).read_bytes()
        assert (outline is None) == (layer != "F.SilkS")
    silk = np.load(answer["plots"][1][2])
    assert len(silk["sizes"]) and silk["points"].shape[1] == 2
