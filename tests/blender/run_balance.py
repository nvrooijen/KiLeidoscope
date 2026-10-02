"""Headless checks of the copper balance panel's analysis and heatmap on the synthetic fixture.

blender --background --factory-startup --python tests/blender/run_balance.py
"""

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, balance, copper_balance, state  # noqa: E402
from kileido.client import FrameDecoder  # noqa: E402
from kileido_bridge import model  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import messages_for, snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def mm(value):
    return int(round(value * 1e6))


def heatmap_pixels():
    """The heatmap image as (rows, columns, 4), row 0 on top like the analysis."""
    image = bpy.data.images[balance.OBJECT]
    columns, rows = image.size
    pixels = np.empty(rows * columns * 4, np.float32)
    image.pixels.foreach_get(pixels)
    return pixels.reshape(rows, columns, 4)[::-1]


def main():
    kileido.register()
    scene = bpy.context.scene
    try:
        snapshot = model.snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
        apply.load_frames(b"".join(snapshot_frames(snapshot)))
        assert balance.result() is None and not bpy.app.timers.is_registered(balance._start), "ran while off"

        scene.kileido_balance = True
        assert bpy.app.timers.is_registered(balance._start), "switching on schedules the analysis"
        balance.wait()
        found = balance.result()
        assert found is not None, balance.status()
        assert balance.status() is None
        assert found.order == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), found.order
        assert abs(found.board_mm2 - (40 * 30 - 4 * 4)) < 0.05, found.board_mm2  # outline less its cutout
        percent = {name: layer.percent for name, layer in found.layers.items()}
        assert all(0 < value < 100 for value in percent.values()), percent
        assert percent["In1.Cu"] > 70 and percent["In2.Cu"] < 5, percent  # a zone against one pad

        # Same numbers as the analysis on the frames themselves (the viewer adds nothing).
        frames = copper_balance.CopperFrames()
        for header, arrays in FrameDecoder().feed(b"".join(snapshot_frames(snapshot))):
            frames.observe(header, arrays)
        direct = copper_balance.analyze(*frames.inputs())
        assert all(abs(direct.layers[n].percent - percent[n]) < 1e-9 for n in percent)

        outer, inner = balance.pairs()
        assert (outer.top, outer.bottom, inner.top, inner.bottom) == ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
        assert inner.flagged and inner.difference > 60 and len(inner.worst) == copper_balance.WORST_TILES
        assert outer.flagged == (outer.difference > 15)
        scene.kileido_balance_threshold = 100.0
        assert not any(pair.flagged for pair in balance.pairs())
        scene.kileido_balance_threshold = 15.0

        # The heatmap: its own collection (clicks and board exports pass it by), over the top copper.
        obj = bpy.data.objects[balance.OBJECT]
        assert obj.name not in state.board.collection.all_objects
        assert obj.users_collection[0].name == balance.COLLECTION
        assert not obj.hide_get() and obj.show_in_front and obj.hide_select
        assert obj["kls_balance_layer"] == "F.Cu" and obj.location.z > state.board.thickness_m
        pixels = heatmap_pixels()
        assert pixels.shape[:2] == found.mask.shape
        rows, columns = found.grid.rows, found.grid.columns
        scale = pixels.shape[0] // rows
        # Tile colours follow the densities; alpha is the board (zero in the cutout).
        for row, column in ((0, 0), (rows // 2, columns // 2), (rows - 1, columns - 1)):
            centre = pixels[row * scale + scale // 2, column * scale + scale // 2]
            expected = copper_balance.ramp(found.layers["F.Cu"].density[row, column])
            assert np.allclose(centre[:3], expected, atol=1 / 255), (row, column, centre, expected)
        cutout = pixels[int(25e6 / found.grid.pixel_nm / (found.grid.per_tile // scale)),
                        int(20e6 / found.grid.pixel_nm / (found.grid.per_tile // scale))]
        assert cutout[3] == 0, cutout
        vertices = np.empty(12, np.float32)
        obj.data.vertices.foreach_get("co", vertices)
        width = np.ptp(vertices.reshape(4, 3)[:, 0])
        assert abs(width - columns * found.grid.tile_nm * 1e-9) < 1e-7, width

        scene.kileido_balance_layer = "In1.Cu"
        assert obj["kls_balance_layer"] == "In1.Cu"
        assert abs(obj.location.z - state.board.heights["In1.Cu"]) < 1e-9
        assert heatmap_pixels()[..., :3].mean() < pixels[..., :3].mean()  # In1's zone: darker
        scene.kileido_balance_layer = "B.Cu"
        assert obj.location.z < 0

        # A live edit: a 6 x 7 mm zone on bare B.Cu, as the bridge sends it.
        before = found.layers["B.Cu"].copper_mm2
        ring = ((mm(32), mm(21)), (mm(38), mm(21)), (mm(38), mm(28)), (mm(32), mm(28)))
        edited = replace(snapshot, zones=(*snapshot.zones, model.ZoneFill("new", "", "B.Cu", ((ring,),))))
        started = time.perf_counter()
        apply.load_frames(b"".join(messages_for(edited, frozenset({("B.Cu", "zones")}), 2)))
        apply_ms = (time.perf_counter() - started) * 1000
        assert bpy.app.timers.is_registered(balance._start), "a copper edit schedules the analysis"
        assert balance.result() is found, "the analysis ran inside the apply"
        balance.wait()
        after = balance.result().layers["B.Cu"].copper_mm2
        assert abs(after - before - 42) < 0.01, (before, after)

        started = time.perf_counter()
        balance.wait()
        analysis_ms = (time.perf_counter() - started) * 1000

        # A status frame is not copper: nothing is scheduled.
        apply.apply_frame({"type": "status", "kicad": "connected"}, {})
        assert not bpy.app.timers.is_registered(balance._start)

        scene.kileido_balance_tile_mm = 10.0
        balance.wait()
        assert balance.result().layers["F.Cu"].density.shape == (3, 4)

        scene.kileido_balance_heatmap = False
        assert obj.hide_get()
        scene.kileido_balance_heatmap = True
        assert not obj.hide_get()
        scene.kileido_balance = False
        assert obj.hide_get()
        assert hasattr(bpy.types, "KILEIDO_PT_balance")
    finally:
        kileido.unregister()
    assert not hasattr(bpy.types.Scene, "kileido_balance")
    print("KLS_BALANCE_OK=" + json.dumps({"percent": {k: round(v, 2) for k, v in percent.items()},
                                          "pairs": [round(p.difference, 2) for p in (outer, inner)],
                                          "edit_apply_ms": round(apply_ms, 2),
                                          "analysis_ms": round(analysis_ms, 1)}))


if __name__ == "__main__":
    main()
