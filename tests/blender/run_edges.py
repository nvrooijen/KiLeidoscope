"""Headless Blender checks of the plated board edge on the synthetic fixture board, with
F.Cu and B.Cu pours along its left edge: plated there and nowhere else, only when the
board asks for it, and shown in the cut plane's section."""

import json
import sys
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, cut, edge_plating, section, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

MM = 1e-3


def frames(plated):
    """The fixture (40 x 30 mm) with 3 mm wide pours on F.Cu and B.Cu along its left edge."""
    snapshot = json.loads((ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json")
                          .read_text(encoding="utf-8"))
    strip = [[[0, 0], [3_000_000, 0], [3_000_000, 30_000_000], [0, 30_000_000]]]
    template = snapshot["zones"][0]
    snapshot["zones"] += [{**template, "id": f"99999999-9999-4999-8999-99999999999{index}", "layer": layer,
                           "polygons": [strip]} for index, layer in enumerate(("F.Cu", "B.Cu"))]
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot), appearance={"edge_plating": plated}))


def main():
    assert bpy.app.background, "run with Blender --background"
    kileido.register()
    scene = bpy.context.scene
    try:
        # Not asked for in Board Setup: no plating, whatever copper reaches the edge.
        apply.load_frames(frames(False))
        board = state.board
        plating = board.collection.all_objects.get(edge_plating.OBJECT)
        assert plating is None or not len(plating.data.polygons)

        # Asked for: where both outer layers' copper reaches the edge: the whole left edge, and
        # the pours' 3 mm along the top and bottom edges.
        apply.load_frames(frames(True))
        stretches = cut.plated_edges()
        lengths = sorted(round(float(np.hypot(*(end - start))) / MM, 3) for start, end, _ in stretches)
        assert lengths == [3.0, 3.0, 30.0], lengths
        start, end, outward = next(s for s in stretches if np.isclose(abs(s[1][1] - s[0][1]), 30 * MM, atol=1e-6))
        assert np.allclose([start[0], end[0]], -20 * MM, atol=1e-7)  # Blender x of KiCad x = 0
        assert np.isclose(abs(end[1] - start[1]), 30 * MM, atol=1e-6) and np.allclose(outward, (-1, 0))
        plating = board.collection.all_objects[edge_plating.OBJECT]
        assert plating.data.materials[0] == board.materials["plating"] and not plating.hide_get()
        coordinates = np.array([v.co[:] for v in plating.data.vertices])
        assert np.isclose(coordinates[:, 0].min(), -20 * MM - edge_plating.THICKNESS_M, atol=1e-8)
        assert np.isclose(coordinates[:, 2].max(), board.thickness_m, atol=1e-9)
        normals = np.array([p.normal[:] for p in plating.data.polygons])
        assert normals[:, 0].min() < -0.99  # the outer face looks out of the board

        # With the board solid hidden, the plating goes too.
        scene.kileido_show_board = False
        assert plating.hide_get()
        scene.kileido_show_board = True

        # The cut plane's section: a copper strip outside the plated left edge, none on the right.
        scene.kileido_cut = True
        rects, (line, _) = cut.rectangles(scene)
        left = [r for r in rects if r[4] == section.COPPER and r[1] <= line.s([(-20 * MM, 0)])[0] + 1e-9
                and r[0] >= line.s([(-20 * MM, 0)])[0] - edge_plating.THICKNESS_M - 1e-9]
        assert left and np.isclose(max(r[3] for r in left) - min(r[2] for r in left), board.thickness_m, atol=1e-9)
        right_edge = line.s([(20 * MM, 0)])[0]
        assert not [r for r in rects if r[0] >= right_edge - 1e-9]
        scene.kileido_cut = False
        print("KLS_EDGES_OK")
    finally:
        kileido.unregister()


main()
