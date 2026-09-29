"""Headless Blender check of the dynamic-phase p_traces, terminal markers and pair list."""

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

        # Both traces get a see-through copy over their copper: the lead over the partner and
        # the delay from the start at each point. N's detour at x = 47 comes first from J1,
        # so P is ahead (red) and N n_trace (blue) over the middle of the pair.
        result = tracker.results["D_P"]
        p_trace, n_trace = (state.board.collection.all_objects[f"KLS phase trace {side} {slots['D_P']}"]
                           for side in "pn")
        plateau = -result.dt_ps[len(result.dt_ps) // 2]
        for trace, sign, delay in ((p_trace, 1, result.delay_p_ps), (n_trace, -1, result.delay_n_ps)):
            lead = read_attribute(trace.data, phase.LEAD, np.float32)
            reached = read_attribute(trace.data, phase.DELAY, np.float32)
            assert abs(np.median(lead) - sign * plateau) < 0.01 and abs(reached.max() - delay) < 1e-3
            assert trace.show_in_front and not trace.hide_get() and len(trace.data.polygons) > 10
        assert plateau > 2.5
        assert p_trace.users_collection[0].get("kls_group") == "phase"
        first = read_coordinates(p_trace.data)
        data = state.board.phase["D_P"]
        strip_vertices, strip_faces, _ = phase._strip(data["p_xyz"], data["p_width"])  # first, then the pads
        count = len(strip_vertices) // 2  # left edge, then right edge
        # Both end pads join the copy: one face per pad outline (this pair has no vias).
        p_pads = [pad for pad in data["pads"] if pad["side"] == "p"]
        assert len(p_pads) == 2 and len(p_trace.data.polygons) == len(strip_faces) + sum(len(pad["rings"]) for pad in p_pads)
        width = np.hypot(*(first[0, :2] - first[count, :2]))
        assert abs(width - (phase_boards.WIDTH * 1e-9 + phase.GROW_M)) < 1e-7  # just over the track
        top = state.board.thickness_m + state.board.layer_thickness.get("F.Cu", 0.0)
        assert np.all(first[:, 2] > top)  # over the copper's outer face
        material = p_trace.data.materials[0]
        names = {node.attribute_name for node in material.node_tree.nodes if node.type == "ATTRIBUTE"}
        assert names == {phase.LEAD, phase.DELAY} and material.surface_render_method == "BLENDED"
        # P red and N blue, each flashing in its own colour; open over drilled holes like the copper.
        assert (material.name, n_trace.data.materials[0].name) == (f"{phase.TRACE} P", f"{phase.TRACE} N")
        for trace, side in ((p_trace, "p"), (n_trace, "n")):
            nodes = trace.data.materials[0].node_tree.nodes
            mix = next(node for node in nodes if node.type == "MIX" and node.data_type == "RGBA")
            assert np.allclose(tuple(mix.inputs["A"].default_value)[:3], phase.COLORS[side])
            assert nodes.get("KLS holes plot") is not None
        scene.kileido_phase_scale_ps = 2.0
        assert material.node_tree.nodes[phase.SCALE_NODE].outputs[0].default_value == 2.0

        # Pulses: the clock runs on a timer (ticked by hand here) while they are on.
        scene.kileido_phase_pulses = True
        assert phase._pulse_tick() == phase.TICK_S
        nodes = material.node_tree.nodes
        assert nodes[phase.PULSE_NODE].outputs[0].default_value == 1.0
        clock = nodes[phase.CLOCK_NODES[0]].outputs[0].default_value
        assert -3 * phase._pulse_width() <= clock <= max(result.delay_p_ps, result.delay_n_ps) + 3 * phase._pulse_width()
        # The energy field: a tube around each trace, lit by the same clock, only while pulsing;
        # it stands out of the board and is not clickable (a click reaches the copper).
        glow = state.board.collection.all_objects[f"KLS phase glow p {slots['D_P']}"]
        assert not glow.hide_get() and "kls_phase_ids" not in glow and len(glow.data.polygons) > 100
        assert read_coordinates(glow.data)[:, 2].max() > top + phase.GLOW_RADIUS_M
        assert glow.data.materials[0].name == f"{phase.GLOW} P"
        assert glow.data.materials[0].node_tree.nodes[phase.CLOCK_NODES[0]].outputs[0].default_value == clock
        # Brighter pulses: one value in the tints and the glows.
        scene.kileido_phase_pulse_glow = 4.0
        assert all(m.node_tree.nodes[phase.BRIGHT_NODE].outputs[0].default_value == 4.0
                   for m in (material, glow.data.materials[0]))
        # A comet tail behind each pulse, its length the panel's.
        scene.kileido_phase_pulse_tail_ps = 5.0
        assert glow.data.materials[0].node_tree.nodes[phase.TAIL_NODE].outputs[0].default_value == 5.0
        # The second pulse runs half a loop behind the first.
        width = phase._pulse_width()
        longest = max(max(data['delay_p_ps'], data['delay_n_ps']) for data in state.board.phase.values())
        period = longest + 6 * width + 4 * phase._pulse_tail()  # the slowest pair sets the loop
        leading, trailing = (nodes[name].outputs[0].default_value for name in phase.CLOCK_NODES)
        assert abs((trailing - leading) % period - period / 2) < 1e-3
        scene.kileido_phase_pulses = False
        assert phase._pulse_tick() is None and nodes[phase.PULSE_NODE].outputs[0].default_value == 0.0
        assert glow.hide_get()
        phase.stop_pulses()

        # A pair through vias: its copy runs on F.Cu, then on B.Cu (the barrel is left open).
        dive = state.board.collection.all_objects[f"KLS phase trace p {slots['E_P']}"]
        coordinates = read_coordinates(dive.data)
        z = coordinates[:, 2]
        assert z.min() < 0 and z.max() > state.board.thickness_m
        # Its via land is tinted on both layers too: a disc centred on the via (x 20, y 10 mm in KiCad).
        centres = np.flatnonzero(np.hypot(coordinates[:, 0] + 0.005, coordinates[:, 1] + 0.005) < 1e-7)
        assert {bool(z[index] > 0) for index in centres} == {True, False}
        # And its barrel is a light column: just outside the 0.3 mm drill, from B.Cu up to F.Cu.
        field = read_coordinates(state.board.collection.all_objects[f"KLS phase glow p {slots['E_P']}"].data)
        ring = np.abs(np.hypot(field[:, 0] + 0.005, field[:, 1] + 0.005) - (0.15e-3 + phase.COLUMN_GROW_M)) < 1e-7
        assert ring.sum() >= 2 * phase.GLOW_SIDES
        assert field[ring, 2].min() == 0 and abs(field[ring, 2].max() - state.board.heights["F.Cu"]) < 1e-9

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

        # A click on a trace copy from above picks the pair's copper; on a terminal marker, its pads.
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        down = Vector((0, 0, -1))
        middle = p_trace.matrix_world @ Vector(tuple((first[count // 2] + first[count + count // 2]) / 2))
        ids = pick.item_at(scene, depsgraph, middle + Vector((0, 0, 0.05)), down)
        assert set(ids) == set(result.p.item_ids + result.n.item_ids)
        assert pick.item_at(scene, depsgraph, Vector(tuple(read_coordinates(start.data)[-1])) + Vector((0, 0, 0.05)),
                            down) == ["J1.1", "J1.2"]

        # Showing, labels only, flipping.
        scene.kileido_phase_labels = False
        assert labels["D_P start"].hide_get() and not p_trace.hide_get()
        scene.kileido_phase_show = False
        assert p_trace.hide_get() and start.hide_get()
        # Colours off, pulses on: the trace copies stay for the flash, their tint faded out.
        scene.kileido_phase_pulses = True
        assert not p_trace.hide_get() and start.hide_get()
        assert p_trace.data.materials[0].node_tree.nodes[phase.COLOUR_NODE].outputs[0].default_value == 0.0
        scene.kileido_phase_pulses = False
        phase.stop_pulses()
        assert p_trace.hide_get()
        scene.kileido_phase_show = scene.kileido_phase_labels = True
        assert not p_trace.hide_get() and not labels["D_P start"].hide_get()
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
        assert p_trace.users_collection[0].get("kls_group") == "phase" and not p_trace.hide_get()

        # A resync snapshot keeps the pairs; a pair list without E removes E's objects.
        apply.load_frames(b"".join(snapshot_frames(snapshot, revision=3)))
        assert len(p_trace.data.vertices) and not p_trace.hide_get()
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
