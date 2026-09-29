"""Headless Blender check of the dynamic-phase ribbons, terminal markers and pair list."""

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import kileido  # noqa: E402
import phase_boards  # noqa: E402
from kileido import apply, packages, phase, pick, state  # noqa: E402
from kileido.objects import read_attribute, read_coordinates  # noqa: E402
from kileido_bridge.phase import PhaseTracker  # noqa: E402
from kileido_bridge.protocol import snapshot_frames  # noqa: E402


def objects(prefix):
    return {obj.name: obj for obj in state.board.collection.all_objects if obj.name.startswith(prefix)}


def main():
    assert bpy.app.background
    kileido.register()  # the panel's scene properties and the pair list exist
    try:
        scene = bpy.context.scene
        snapshot = phase_boards.two_channels()
        apply.load_frames(b"".join(snapshot_frames(snapshot)))
        tracker = PhaseTracker()
        apply.load_frames(b"".join(tracker.update(snapshot, revision=1)))
        slots = {key: phase._slot(key) for key in ("D_P", "E_P")}
        assert set(state.board.phase) == {"D_P", "E_P"}

        # The ribbon: a strip of two vertices per centreline point, Δt on each, drawn in front.
        ribbon = state.board.collection.all_objects[f"KLS phase ribbon {slots['D_P']}"]
        result = tracker.results["D_P"]
        assert len(ribbon.data.vertices) == 2 * len(result.centre_nm) and len(ribbon.data.polygons) > 0
        dt = read_attribute(ribbon.data, phase.ATTRIBUTE, np.float32)
        assert np.allclose(dt[0::2], result.dt_ps, atol=1e-4) and dt.min() < -2.5  # N ahead from J1
        assert ribbon.show_in_front and not ribbon.hide_get()
        assert ribbon.users_collection[0].get("kls_group") == "phase"
        material = ribbon.data.materials[0]
        attribute = next(node for node in material.node_tree.nodes if node.type == "ATTRIBUTE")
        assert attribute.attribute_name == phase.ATTRIBUTE
        width = np.ptp(read_coordinates(ribbon.data)[:2, 1])  # the first cross-section, across y
        assert abs(width - (1e-3 + phase_boards.WIDTH * 1e-9)) < 1e-6
        scene.kileido_phase_scale_ps = 2.0
        assert material.node_tree.nodes[phase.SCALE_NODE].outputs[0].default_value == 2.0

        # A pair through vias: its ribbon climbs from B.Cu to F.Cu.
        dive = state.board.collection.all_objects[f"KLS phase ribbon {slots['E_P']}"]
        z = read_coordinates(dive.data)[:, 2]
        assert z.min() < 0 and z.max() > state.board.thickness_m - 1e-4

        # Terminal markers and labels: the start (J1, sorts first) green, the end violet;
        # J2 sits on the bottom, so its marker hangs below the board and its label reads from below.
        labels = {obj["kls_phase_key"] + " " + obj.name.split()[3]: obj
                  for obj in objects("KLS phase label ").values()}
        assert labels["D_P start"].data.body == "J1.1/2" and labels["D_P end"].data.body == "U1.1/2"
        assert labels["E_P start"].data.body == "J2.1/2" and labels["E_P end"].data.body == "U2.1/2"
        assert labels["E_P start"].location.z < 0 and labels["E_P start"].rotation_euler.x > 3
        start = state.board.collection.all_objects[f"KLS phase start {slots['D_P']}"]
        assert list(start["kls_phase_ids"]) == ["J1.1", "J1.2"] and start.data.materials[0].name == "KLS Phase start"
        assert read_coordinates(start.data)[:, 2].min() > state.board.thickness_m  # above the top copper

        # One excursion on D (where N's detour at x = 47 starts it), none on E.
        excursions = objects("KLS phase excursion ")
        assert [obj["kls_phase_key"] for obj in excursions.values()] == ["D_P"]
        marker = next(iter(excursions.values()))
        tip = read_coordinates(marker.data)[0]
        assert 0.0 < tip[0] < 0.1 and list(marker["kls_phase_ids"]) == [result.excursions[0].item_id]

        # The pair list in the panel.
        rows = scene.kileido_phase_pairs
        assert [row.key for row in rows] == ["D_P", "E_P"] and rows[0].excursions == 1
        assert rows[0].route == "J1.1/2 → U1.1/2" and abs(rows[0].skew_ps) < 1e-6

        # A click on the ribbon from above picks the pair's copper; on a terminal marker, its pads.
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        down = Vector((0, 0, -1))
        middle = ribbon.matrix_world @ Vector(tuple(read_coordinates(ribbon.data)[len(dt) // 2]))
        ids = pick.item_at(scene, depsgraph, middle + Vector((0, 0, 0.05)), down)
        assert set(ids) == set(result.p.item_ids + result.n.item_ids)
        assert pick.item_at(scene, depsgraph, Vector(tuple(read_coordinates(start.data)[-1])) + Vector((0, 0, 0.05)),
                            down) == ["J1.1", "J1.2"]

        # Showing, labels only, flipping.
        scene.kileido_phase_labels = False
        assert labels["D_P start"].hide_get() and not ribbon.hide_get()
        scene.kileido_phase_show = False
        assert ribbon.hide_get() and start.hide_get()
        scene.kileido_phase_show = scene.kileido_phase_labels = True
        assert not ribbon.hide_get() and not labels["D_P start"].hide_get()
        phase.flip("D_P")
        assert phase.settings()["flipped"] == ["D_P"]
        tracker.configure(phase.settings())
        apply.load_frames(b"".join(tracker.update(snapshot, revision=2)))
        assert labels["D_P start"].data.body == "U1.1/2" and state.board.phase["D_P"]["flipped"]

        # An exported board leaves the measurement out, and keeps it in the live scene.
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "board.blend")
            packages.export_board(path)
            with bpy.data.libraries.load(path) as (data_from, _):
                exported = list(data_from.objects)
        assert exported and not any(name.startswith("KLS phase") for name in exported)
        assert ribbon.users_collection[0].get("kls_group") == "phase" and not ribbon.hide_get()

        # A resync snapshot keeps the pairs; a pair list without E removes E's objects.
        apply.load_frames(b"".join(snapshot_frames(snapshot, revision=3)))
        assert len(ribbon.data.vertices) and not ribbon.hide_get()
        fewer = replace(snapshot, tracks=tuple(t for t in snapshot.tracks if not t.net.startswith("E_")),
                        vias=())
        apply.load_frames(b"".join(tracker.update(fewer, revision=4)))
        assert set(state.board.phase) == {"D_P"} and [row.key for row in rows] == ["D_P"]
        assert not len(dive.data.vertices) and dive.hide_get()
        assert labels["E_P start"].hide_get()
    finally:
        kileido.unregister()
    print("KLS_PHASE_OK")


if __name__ == "__main__":
    main()
