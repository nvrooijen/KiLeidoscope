"""Complete-snapshot frames: what `kileido_bridge dump x.kls` writes and Blender loads."""

import json
from dataclasses import replace
from pathlib import Path

from kileido_bridge import model
from kileido_bridge.protocol import FrameDecoder, layer_heights_nm, snapshot_frames

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"  # KiCad output


def fixture():
    return model.snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_json_roundtrip_is_lossless():
    snapshot = fixture()
    assert model.snapshot_from_jsonable(model.to_jsonable(snapshot)) == snapshot


def test_snapshot_frames_order_and_board_header():
    frames = FrameDecoder().feed(b"".join(snapshot_frames(fixture())))
    types = [header["type"] for header, _ in frames]
    assert types[0] == "snapshot_begin" and types[1] == "board" and types[-1] == "snapshot_end"
    board = frames[1][0]
    assert board["origin_nm"] == [20_000_000, 15_000_000]  # centre of the 40 x 30 mm outline
    z = {layer["name"]: layer["z_m"] for layer in board["layers"]}
    assert z["B.Cu"] == 0 and abs(z["F.Cu"] - 0.00154) < 1e-12 and abs(board["board_thickness_m"] - 0.00154) < 1e-12
    assert abs(z["In1.Cu"] - 0.001305) < 1e-12 and abs(z["In2.Cu"] - 0.00087) < 1e-12
    kinds = {(h.get("layer"), h.get("kind")) for h, _ in frames if h["type"] == "layer_data"}
    assert {("F.Cu", "tracks"), ("B.Cu", "tracks"), ("F.Cu", "pads"), ("B.Cu", "pads"),
            ("In1.Cu", "zones"), ("", "vias"), ("", "outline")} <= kinds


def test_missing_stackup_spaces_layers_evenly_and_warns():
    snapshot = replace(fixture(), stackup=model.Stackup(()))
    heights, thickness, warnings = layer_heights_nm(snapshot)
    assert thickness == 1_600_000 and heights["B.Cu"] == 0 and heights["F.Cu"] == 1_600_000
    assert heights["B.Cu"] < heights["In1.Cu"] < heights["F.Cu"]  # In2.Cu has no items without a stackup
    assert warnings


def test_pad_frames_include_through_hole_drills_on_each_copper_layer():
    frames = FrameDecoder().feed(b"".join(snapshot_frames(fixture())))
    pad_frames = [(header, arrays) for header, arrays in frames
                  if header.get("kind") == "pads"]
    assert {header["layer"] for header, _ in pad_frames} == {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
    for header, arrays in pad_frames:
        assert arrays["drill"].shape == (1, 4)
        assert arrays["drill"][0].tolist() == [12_000_000, 23_000_000, 2_000_000, 1_000_000]
        assert header["ids"][int(arrays["drill_item"][0])] == "55555555-5555-4555-8555-555555555555"
        assert arrays["drill_oval"].tolist() == [1]


def test_rotated_oval_drill_angle_and_shape_survive_binary_frame():
    original = fixture()
    pad = replace(original.pads[0], drill_angle_rad=0.7, drill_shape="oval")
    changed = replace(original, pads=(pad, *original.pads[1:]))
    frames = FrameDecoder().feed(b"".join(snapshot_frames(changed)))
    arrays = next(arrays for header, arrays in frames
                  if header.get("layer") == "F.Cu" and header.get("kind") == "pads")
    assert abs(float(arrays["drill_angle"][0]) - 0.7) < 1e-6
    assert arrays["drill_oval"].tolist() == [1]


def test_non_plated_hole_without_copper_is_drilled_on_outer_layers():
    original = fixture()
    hole = model.Pad("66666666-6666-4666-8666-666666666666", "", "", "", (5_000_000, 6_000_000),
                     (990_600, 990_600), {}, "round")  # np_thru_hole: no copper on any layer
    frames = FrameDecoder().feed(b"".join(snapshot_frames(replace(original, pads=(*original.pads, hole)))))
    drilled = {}
    for header, arrays in frames:
        if header.get("kind") == "pads":
            drilled[header["layer"]] = [header["ids"][int(i)] for i in arrays["drill_item"]]
    assert hole.id in drilled["F.Cu"] and hole.id in drilled["B.Cu"]
    assert hole.id not in drilled["In1.Cu"]
