"""Headless checks of the proximity warnings: synthetic box models and a view-only board
over a differential pair on the synthetic fixture board.

blender --background --factory-startup --python tests/blender/run_proximity.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, packages, proximity, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
J1, J2, J3 = "66666666-6666-4666-8666-666666666666", "77777777-7777-4777-8777-777777777777", \
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MM = 1e-3
# The fixture's outline is centred on (20, 15) mm, so the pair runs x = -6 .. 6 mm, y = 0 and -0.35 mm.
PAIR = (("USB_D+", 15_000_000), ("USB_D-", 15_350_000))


def frames(name):
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = name
    for n, (net, y) in enumerate(PAIR):
        snapshot["tracks"].append({"id": f"33333333-3333-4333-8333-33333333333{n}", "layer": "F.Cu", "net": net,
                                   "start": [14_000_000, y], "end": [26_000_000, y], "width": 200_000})
    pad = dict(next(pad for pad in snapshot["pads"] if pad["footprint_id"] == J3))
    pad.update(id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", number="2", net="USB_D+")  # J3 is on the pair
    snapshot["pads"].append(pad)
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def add_part(footprint_id, name, low, high):
    """A box-shaped model part of footprint `footprint_id`, world corners in mm."""
    low, high = np.array(low) * MM, np.array(high) * MM
    mesh = bpy.data.meshes.new(name)
    corners = [(x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]
    mesh.from_pydata(corners, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4),
                                   (1, 5, 7, 3)])
    mesh.update()
    obj = bpy.data.objects.new(f"KLS model {footprint_id} {name}", mesh)
    obj["kls_model_fp_id"] = footprint_id
    state.board.collection.objects.link(obj)
    return obj


def check():
    bpy.context.view_layer.update()
    proximity.run()
    return proximity.rows()


def span_length(warning):
    return sum(np.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2, _ in warning["spans"])


def schedules_after(change):
    """Does `change`, then the next depsgraph update, schedule a check?"""
    if bpy.app.timers.is_registered(proximity._timer):
        bpy.app.timers.unregister(proximity._timer)
    change()
    bpy.context.view_layer.update()
    return bpy.app.timers.is_registered(proximity._timer)


def main():
    kileido.register()
    scene = bpy.context.scene
    try:
        apply.load_frames(frames("alpha"))
        package = Path(tempfile.mkdtemp()) / "alpha.blend"
        packages.export_board(str(package))  # before any part: the view-only copy brings only its solid
        apply.load_frames(frames("beta"))

        tracks = state.board.collection.all_objects["KLS F.Cu tracks"]
        assert "USB_D+" in list(tracks["kls_nets"]), list(tracks["kls_nets"])
        assert set(state.board.collection.all_objects[f"KLS footprint {J3}"]["kls_nets"]) == {"RF_A", "USB_D+"}
        assert proximity.checked_nets(scene) == {"USB_D+", "USB_D-"}  # RF_A and RF_B are no pair
        heights = proximity.dielectric_heights()
        assert abs(heights["F.Cu"] - 0.2 * MM) < 1e-9 and abs(heights["B.Cu"] - 0.8 * MM) < 1e-9, heights
        surface = state.board.thickness_m / MM
        assert not check() and proximity.status() == "Nothing within the limit (2 nets checked)"

        # A 4 x 2 mm box 0.5 mm over the pair (limit 5 h = 1 mm): both nets, one box.
        box = add_part(J1, "can", (-2, -1.175, surface + 0.5), (2, 0.825, surface + 1.5))
        found = check()
        assert [(w["label"], w["net"]) for w in found] == [("J1", "USB_D+"), ("J1", "USB_D-")], found
        assert all(abs(w["distance_m"] - 0.5 * MM) < 1e-6 for w in found), [w["distance_m"] for w in found]
        assert found[0]["ids"] == ["33333333-3333-4333-8333-333333333330"] and found[0]["footprint_id"] == J1
        # Flagged where within 1 mm: 4 mm under the box and sqrt(1 - 0.5^2) = 0.87 mm past each end.
        spans = [span_length(w) / MM for w in found]
        assert all(5.4 < length < 6.1 for length in spans), spans
        markers = bpy.data.collections[proximity.COLLECTION].objects
        assert len(markers) == 3 and not any(obj.hide_get() for obj in markers), [obj.name for obj in markers]
        assert found[0]["box"] == found[1]["box"]

        assert bpy.ops.kileido.proximity_select(index=1) == {"FINISHED"}
        assert {obj.name for obj in bpy.context.selected_objects} == {found[1]["span"], found[1]["box"]}

        assert schedules_after(lambda: setattr(box.location, "z", 1.0 * MM)), "a moved part is not rechecked"
        assert not check(), "a box 1.5 mm over the pair is within 1 mm"
        assert len(markers) == 0
        assert not schedules_after(lambda: None), "an idle check schedules the next"
        scene.kileido_proximity_factor = 10.0  # limit 2 mm
        assert [round(w["distance_m"] / MM, 4) for w in check()] == [1.5, 1.5]
        scene.kileido_proximity_factor = proximity.DEFAULT_FACTOR
        box.location.z = 0.0

        box["kls_model_fp_id"] = J3  # J3 has a pad on USB_D+: its own part, not an obstacle over it
        assert [w["net"] for w in check()] == ["USB_D-"]
        box["kls_model_fp_id"] = J1

        # B.Cu (limit 5 x 0.8 = 4 mm): a part 0.5 mm under RF_B counts, one over the board
        # (1.84 mm away through it) does not: only the trace's outward side.
        add_part(J2, "under", (8, -2, -1.5), (12, 2, -0.5))
        add_part(J3, "over", (8, -2, surface + 0.3), (12, 2, surface + 1.3))
        state.board.highlight["selected"] = {"22222222-2222-4222-8222-222222222221"}  # an RF_B track
        assert bpy.ops.kileido.proximity_add_selection() == {"FINISHED"}
        assert scene.kileido_proximity_nets == "RF_B"
        found = check()
        assert [(w["label"], w["net"], w["layer"]) for w in found if w["net"] == "RF_B"] == [("J2", "RF_B", "B.Cu")]
        assert abs(next(w for w in found if w["net"] == "RF_B")["distance_m"] - 0.5 * MM) < 1e-6
        scene.kileido_proximity_nets = ""

        # A view-only board stacked 0.6 mm over the live one: its solid over the whole pair.
        root = packages.import_board(str(package))
        root.location = (0.0, 0.0, state.board.thickness_m + 0.6 * MM)
        found = check()
        stacked = [w for w in found if w["label"] == "alpha (board)"]
        assert [w["net"] for w in stacked] == ["USB_D+", "USB_D-"], [(w["label"], w["net"]) for w in found]
        assert all(0.6 * MM < w["distance_m"] < 0.65 * MM for w in stacked), [w["distance_m"] for w in stacked]
        assert all(abs(span_length(w) - 12 * MM) < 0.3 * MM for w in stacked), [span_length(w) for w in stacked]
        assert schedules_after(lambda: setattr(root.location, "z", 0.01)), "a moved board is not rechecked"
        assert not [w for w in check() if w["label"] == "alpha (board)"]

        started = time.perf_counter()
        for _ in range(10):
            proximity.find(scene)
        check_ms = (time.perf_counter() - started) * 100

        scene.kileido_proximity = False
        proximity.run()
        assert proximity.status() is None and not proximity.rows() and not len(markers)
    finally:
        kileido.unregister()
    print("KLS_PROXIMITY_OK=" + json.dumps({"span_mm": round(spans[0], 2), "check_ms": round(check_ms, 2)}))


if __name__ == "__main__":
    main()
