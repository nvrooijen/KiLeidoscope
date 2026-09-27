"""Headless Blender check of the DC analysis display on a synthetic two-layer board
(tests/dc_board.py): maps, arrows, via currents, markers and the panel's table.

Blender's Python has no SciPy, so the result is made up here in the shape the
solver's (kileido_bridge.dcworker.solve) has; tests/test_dc_solve.py checks the solver."""

import sys
import types
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dc_board  # noqa: E402
import kileido  # noqa: E402
from dc_board import MM  # noqa: E402
from kileido import apply, dc, state  # noqa: E402
from kileido_bridge import dc as bridge_dc  # noqa: E402
from kileido_bridge import model, protocol  # noqa: E402

COLLECTION = "KiLeidoscope: dc_board.kicad_pcb"


def board():
    """F.Cu pour 40 x 20 mm fed at its left end (J1.1), B.Cu pour drawn at its right end
    (J2.1, underneath), four vias between them at x = 20 mm."""
    zones = (model.ZoneFill("zone-f", dc_board.NET, "F.Cu", ((dc_board.rect(0, 0, 40 * MM, 20 * MM),),)),
             model.ZoneFill("zone-b", dc_board.NET, "B.Cu", ((dc_board.rect(0, 0, 40 * MM, 20 * MM),),)))
    pads = (dc_board.smd_pad("pad-j1", "fp-j1", "1", "F.Cu", 0, 8 * MM, 3 * MM, 12 * MM),
            dc_board.smd_pad("pad-j2", "fp-j2", "1", "B.Cu", 37 * MM, 8 * MM, 40 * MM, 12 * MM))
    footprints = (dc_board.footprint("fp-j1", "J1", 1_500_000, 10 * MM),
                  dc_board.footprint("fp-j2", "J2", 38_500_000, 10 * MM, side="bottom"))
    vias = tuple(model.Via(f"via-{n}", dc_board.NET, (20 * MM, (4 + 4 * n) * MM), 600_000, 300_000, "F.Cu", "B.Cu")
                 for n in range(4))
    return dc_board.snapshot(zones, pads, footprints, vias)


def made_up_result():
    """Current along +x: from J1 over F.Cu (0..20 mm) to the vias, on over B.Cu
    (20..40 mm) to J2; |J| rises toward the vias; 5 A shared by four vias."""
    pitch, margin = 200_000, 2
    columns, rows = 200 + 2 * margin, 100 + 2 * margin
    x = (np.arange(columns) - margin + 0.5) * pitch
    inside = (x > 0) & (x < 40 * MM)
    copper = np.zeros((rows, columns), bool)
    copper[margin:-margin, :] = inside
    fields = {key: np.full((2, rows, columns), np.nan, np.float32) for key in ("j", "v", "jx", "jy")}
    for layer, side in enumerate((x < 20 * MM, x >= 20 * MM)):
        on = copper & side
        j = np.broadcast_to(1.0 + 4.0 * (1 - np.abs(x - 20 * MM) / (20 * MM)), (rows, columns))
        fields["j"][layer][on] = j[on]
        fields["jx"][layer][on] = j[on]
        fields["jy"][layer][on] = 0.0
        fields["v"][layer][on] = np.broadcast_to(3.3 - 0.02 * x / (40 * MM), (rows, columns))[on]
    return {
        "net": dc_board.NET, "layers": ["F.Cu", "B.Cu"], "v_ref": 3.3, "cell_nm": 200_000.0, "cells": 40_000,
        "unknowns": 16_000, "method": "spsolve", "iterations": None,
        "supplies": [{"name": "S1", "v_oc": 3.3, "i_a": 5.0, "v_contact": 3.3}],
        "loads": [{"name": "L1", "i_a": 5.0, "v_mean": 3.281, "v_min": 3.28, "p_w": 16.4,
                   "drop_mean_v": 0.019, "drop_max_v": 0.02}],
        "pairs": [{"supply": "S1", "load": "L1", "r_ohm": 0.0038, "i_a": 5.0, "p_w": 0.095}],
        "p_copper_w": 0.095, "p_layers_w": [0.05, 0.04], "p_vias_w": 0.005, "p_loads_w": 16.4,
        "power_balance": 1e-9, "timings_s": {"raster": 0.1, "solve": 0.9, "display": 0.05, "build": 0.01,
                                             "total": 1.23},
        "grid": {"x0_nm": -margin * pitch, "y0_nm": -margin * pitch, "pitch_nm": pitch, "factor": 1},
        "fields": fields, "j_max": float(np.nanmax(fields["j"])), "v_min": 3.28, "v_max": 3.3,
        "barrels": {"ids": [f"via-{n}" for n in range(4)], "spans": [("F.Cu", "B.Cu")] * 4,
                    "xy": np.array([(20 * MM, (4 + 4 * n) * MM) for n in range(4)], np.int64),
                    "size": np.full(4, 600_000, np.int64), "current_a": np.array([1.4, 1.3, 1.2, 1.1]),
                    "power_w": np.full(4, 0.00125)},
    }


class Layout:
    """Stands in for a panel's UILayout (background Blender draws no UI): records labels."""

    def __init__(self, lines):
        self.lines = lines

    def __getattr__(self, name):
        if name in ("row", "column", "box", "split"):
            return lambda *args, **kwargs: Layout(self.lines)
        if name == "label":
            return lambda text="", **kwargs: self.lines.append(text)
        if name == "operator":
            return lambda *args, text="", **kwargs: self.lines.append(text) or types.SimpleNamespace()
        return lambda *args, **kwargs: None

    def __setattr__(self, name, value):
        if name == "lines":
            object.__setattr__(self, name, value)


def panel_text(panel):
    lines = []
    context = types.SimpleNamespace(scene=bpy.context.scene, screen=None, region=None,
                                    preferences=bpy.context.preferences)
    panel.draw(types.SimpleNamespace(layout=Layout(lines)), context)
    return lines


def objects():
    return bpy.data.collections[COLLECTION].all_objects


def evaluated(obj):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), depsgraph=depsgraph)
    try:
        points = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", points)
        return points.reshape(-1, 3) + np.asarray(obj.location, np.float32), len(mesh.polygons)
    finally:
        bpy.data.meshes.remove(mesh)


def main():
    assert bpy.app.background
    kileido.register()
    try:
        snapshot = board()
        session = bridge_dc.DcAnalysis(clock=lambda: 0.0, solver_factory=lambda: None)
        session.setup = bridge_dc.DcSetup(dc_board.NET, [bridge_dc.DcTerminal("S1", "supply", ["J1.1"], 3.3),
                                                         bridge_dc.DcTerminal("L1", "load", ["J2.1"], 5.0)],
                                          cell_um=200)
        session.active = 1
        setup = session.resync_frames(snapshot)[0]
        result = made_up_result()
        frames = b"".join(protocol.snapshot_frames(snapshot)) + setup + protocol.dc_result_message(result, 1)
        apply.load_frames(frames + protocol.dc_status_message({"state": "done", "message": "Solved in 1.2 s",
                                                               "elapsed_s": 1.2, "notes": []}, 1))
        scene = bpy.context.scene
        heights = state.board.heights

        # The panel's table mirrors the setup, without echoing edits back.
        items = scene.kileido_dc_terminals
        assert [(item.name, item.role, round(item.value, 3), item.parts) for item in items] == [
            ("S1", "supply", 3.3, "J1.1"), ("L1", "load", 5.0, "J2.1")]
        assert scene.kileido_dc_active == 1 and not dc.syncing()

        # A colour map per layer, just out from its copper, textured with the field.
        front, back = objects()["KLS F.Cu dc map"], objects()["KLS B.Cu dc map"]
        assert not front.hide_get() and not back.hide_get()
        # Two quads each: out from the copper's outer face and in from its inner face.
        for obj, layer, up in ((front, "F.Cu", 1), (back, "B.Cu", -1)):
            zs = sorted({round(v.co.z, 9) for v in obj.data.vertices}, key=lambda z: -up * z)
            copper = state.board.layer_thickness.get(layer, 0.0)
            assert len(obj.data.polygons) == 2 and len(zs) == 2
            assert up * (zs[0] - heights[layer]) > 3e-6  # outside the via lands
            assert up * (heights[layer] - copper - zs[1]) > 0  # below the copper, on the laminate side
            normals = [obj.data.polygons[n].normal.z for n in range(2)]
            assert normals[0] * up > 0.99 and normals[1] * up < -0.99
        image = bpy.data.images["KLS DC F.Cu"]
        rows, columns = np.asarray(result["fields"]["j"][0]).shape
        assert tuple(image.size) == (columns, rows)
        pixels = np.empty(columns * rows * 4, np.float32)
        image.pixels.foreach_get(pixels)
        pixels = pixels.reshape(rows, columns, 4)
        assert pixels[0, 0, 3] == 0.0  # the margin: no copper, see-through
        middle = pixels[rows // 2, columns // 4]  # F.Cu carries the current left of the vias
        assert middle[3] == 1.0 and middle[:3].max() > 0.0
        xs = [v.co.x for v in front.data.vertices]
        assert np.isclose(max(xs) - min(xs), columns * result["grid"]["pitch_nm"] * 1e-9, rtol=1e-6)
        # The legend runs from the largest |J| down three decades.
        legend = dc.legend()
        assert len(legend) == 5 and float(legend[0][1]) == float(f"{result['j_max']:.3g}")
        assert float(legend[-1][1]) == float(f"{result['j_max'] / 1000:.2e}")
        # The legend: a colour bar right of the board with the decades of |J| and the top value.
        bar = objects()["KLS dc legend"]
        from kileido.objects import outline_bounds
        assert min(v.co.x for v in bar.data.vertices) > outline_bounds()[2] and not bar.hide_get()
        texts = {obj.data.body for obj in objects() if obj.type == "FONT" and not obj.hide_get()}
        assert {"Current density |J|", "A/mm², log scale", "– 0.01", "– 0.1", "– 1"} <= texts, texts
        assert f"– {dc._format(result['j_max'])}" in texts and "Arrows: current direction" in texts
        assert any(text.startswith("DC, steady current: +3V3, 5 A") for text in texts)
        before = pixels.copy()
        scene.kileido_dc_field = "DROP"
        image.pixels.foreach_get(pixels.ravel())
        assert not np.array_equal(pixels, before) and dc.unit() == "mV"
        texts = {obj.data.body for obj in objects() if obj.type == "FONT" and not obj.hide_get()}
        assert {"Voltage drop", "mV below 3.3 V", "– 5", "– 10", "– 15", f"– {dc._format(dc.value_range()[1])}",
                f"– {dc._format(dc.value_range()[0])}"} <= texts, texts
        assert dc.legend()[-1][1] == "0" or float(dc.legend()[-1][1]) >= 0
        scene.kileido_dc_auto_range = False
        low, high, _ = dc.value_range()
        assert np.isclose(scene.kileido_dc_max, high) and scene.kileido_dc_min == low  # starts from auto
        scene.kileido_dc_field = "CURRENT"
        assert scene.kileido_dc_auto_range  # a range in mV means nothing for A/mm2

        # Particles along streamlines of the current: on F.Cu from J1 (left) toward the vias.
        shown = state.board.dc["result"]
        paths = dc.flow_paths(shown, 0, scene.kileido_dc_arrow_mm * 1e6)
        assert len(paths) > 50 and all(len(path) >= dc.FLOW_MIN_POINTS for path in paths)
        assert np.median([path[-1][0] - path[0][0] for path in paths]) > 1 * MM
        arrows = objects()["KLS F.Cu dc arrows"]
        assert arrows.modifiers[0].node_group.name == dc.FLOW_GROUP
        from kileido.nodes import modifier_value
        material = arrows.modifiers[0].node_group.interface.items_tree["Material"].identifier
        assert modifier_value(arrows.modifiers[0], material) == state.board.materials["highlight_selected"]
        heads = np.zeros(len(arrows.data.vertices), bool)
        arrows.data.attributes["kls_head"].data.foreach_get("value", heads)
        assert heads.sum() == len(paths)
        from kileido.nodes import modifier_input

        def particles(clock):
            modifier = arrows.modifiers[0]
            modifier_input(modifier, modifier.node_group, "Clock", clock)
            arrows.update_tag()
            return evaluated(arrows)

        scene.frame_set(1)
        first, faces = particles(0.0)
        assert faces > 0 and np.isfinite(first).all() and first[:, 2].min() > heights["F.Cu"]
        later, _ = particles(0.3)  # the viewport clock moves them without the timeline
        assert len(later) == len(first) and np.mean(later[:, 0] - first[:, 0]) > 0  # downstream, +x
        scene.frame_set(9)
        assert not np.allclose(particles(0.3)[0], later)  # and so does the timeline
        scene.kileido_dc_arrow_mm = 4.0
        assert len(dc.flow_paths(shown, 0, 4e6)) < len(paths)

        # Via currents: one column per via, the four sharing the 5 A.
        vias = objects()["KLS vias dc"]
        assert len(vias.data.polygons) == 4 * 18 and not vias.hide_get()
        assert np.isclose(sum(result["barrels"]["current_a"]), 5.0, rtol=1e-6)
        points, _ = evaluated(vias)
        assert points[:, 2].max() > heights["F.Cu"] and points[:, 2].min() < heights["B.Cu"]
        assert len(dc.top_vias()) == 4

        # Markers: red supply cone on top, blue load cone under the board (J2 is on B.Cu).
        supply, load = objects()["KLS dc markers supply"], objects()["KLS dc markers load"]
        top, _ = evaluated(supply)
        bottom, _ = evaluated(load)
        assert top[:, 2].min() > heights["F.Cu"] and bottom[:, 2].max() < heights["B.Cu"]

        # Panel text.
        lines = dc.summary_lines()
        assert lines[0].startswith("S1: 3.3 V source, delivers 5 A")
        assert any(line.startswith("R S1 → L1:") for line in lines)
        assert dc.status_line() == ("CHECKMARK", "Solved in 1.2 s")
        assert "cells of 200 µm" in dc.timing_line()
        text = panel_text(kileido.KILEIDO_PT_dc)
        assert dc_board.NET in text and "Solved in 1.2 s" in text and "J2.1" in text
        text = panel_text(kileido.KILEIDO_PT_dc_display)
        assert "Current density (A/mm²)" in text and "Arrow speed is illustrative, not the electron drift" in text
        assert any(line.startswith("(20.00, 4.00) mm: 1.4 A") for line in text)
        text = " ".join(panel_text(kileido.KILEIDO_PT_dc_settings))
        assert "Fill Resistance by Janik Oltmanns / B4L" in text and "GPL-3.0-or-later" in text

        # Layer eyes: the F.Cu eye hides its map and arrows; Vias hides the via currents.
        scene.kileido_show_F_Cu = False
        assert front.hide_get() and objects()["KLS F.Cu dc arrows"].hide_get() and not back.hide_get()
        scene.kileido_show_F_Cu = True
        assert not front.hide_get()
        scene.kileido_show_vias = False
        assert vias.hide_get()
        scene.kileido_show_vias = True

        # Layer: B.Cu alone, seen from above (board, mask, F.Cu off); All puts the eyes back.
        scene.kileido_show_F_SilkS = False
        scene.kileido_dc_layer = "B.Cu"
        assert front.hide_get() and not back.hide_get() and not scene.kileido_show_board
        assert not scene.kileido_show_F_Cu and scene.kileido_show_vias  # via currents stay
        scene.kileido_dc_layer = "ALL"
        assert not front.hide_get() and scene.kileido_show_board and scene.kileido_show_F_Cu
        assert not scene.kileido_show_F_SilkS  # off before, off again

        # The panel's check box hides everything; a resync snapshot keeps the result.
        scene.kileido_dc = False
        assert front.hide_get() and vias.hide_get() and supply.hide_get()
        assert objects()["KLS dc legend title"].hide_get()
        scene.kileido_dc = True
        assert not front.hide_get() and not supply.hide_get()
        apply.load_frames(b"".join(protocol.snapshot_frames(snapshot, revision=2)))
        assert not front.hide_get() and len(front.data.vertices) == 8 and not vias.hide_get()
        # A cleared result (setup incomplete) hides the maps; the markers stay.
        apply.load_frames(protocol.dc_result_message(None, 3))
        assert front.hide_get() and vias.hide_get() and not supply.hide_get()
        assert dc.summary_lines() == [] and dc.legend() == []
    finally:
        kileido.unregister()
    print("KLS_DC_OK")


if __name__ == "__main__":
    main()
