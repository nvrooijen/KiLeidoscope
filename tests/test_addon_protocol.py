"""The add-on's copies of bridge code (frame codec, differential-pair rule) must match it without bpy."""

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from kileido_bridge import model, protocol, selection


def _addon_client():
    path = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "client.py"
    spec = importlib.util.spec_from_file_location("kileido_addon_client_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_frame_decodes_in_addon():
    addon = _addon_client()
    array = np.array([[1, -2], [3, 4]], dtype="<i4")
    payload = protocol.encode_frame({"type": "layer_data", "layer": "F.Cu"}, {"points": array})
    decoder = addon.FrameDecoder()
    assert decoder.feed(payload[:7]) == []
    frames = decoder.feed(payload[7:])
    assert len(frames) == 1
    header, arrays = frames[0]
    assert header["type"] == "layer_data"
    assert header["layer"] == "F.Cu"
    assert np.array_equal(arrays["points"], array)


def test_addon_frame_decodes_in_bridge():
    addon = _addon_client()
    payload = addon.encode_frame({"type": "status"}, {"flags": np.array([0, 1], dtype="|u1")})
    frames = protocol.FrameDecoder().feed(payload)
    assert len(frames) == 1
    assert frames[0][0]["type"] == "status"
    assert frames[0][1]["flags"].tolist() == [0, 1]


def _addon_function(module, name):
    """One function of an add-on module, without importing the module (and so bpy)."""
    path = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {}
    exec(compile(ast.Module([node], []), str(path), "exec"), namespace)
    return namespace[name]


def test_addon_diff_pair_rule_matches_the_bridge():
    addon = _addon_function("proximity.py", "diff_pair_partner")
    nets = {"USB_D+", "USB_D-", "ETH_TXP", "ETH_TXN", "GND", "VCAP", "LVDS_N", "A+", "+", "-"}
    for net in (*nets, "", "VCAN", "B-", "CLK_P"):
        assert addon(net, nets) == selection.diff_pair_partner(net, nets), net


def test_track_and_footprint_frames_carry_nets():
    snapshot = SimpleNamespace(
        tracks=(model.Track("t1", "F.Cu", "USB_D+", (0, 0), (1_000_000, 0), 200_000),
                model.Track("t2", "B.Cu", "GND", (0, 0), (1_000_000, 0), 200_000)),
        arcs=(model.Arc("a1", "F.Cu", "USB_D-", (0, 0), (500_000, 500_000), (1_000_000, 0), 200_000),),
        footprints=(model.Footprint("fp1", "J1", (0, 0), 0.0, "top", ()),
                    model.Footprint("fp2", "H1", (0, 0), 0.0, "top", ())),
        pads=(SimpleNamespace(footprint_id="fp1", net="USB_D-"), SimpleNamespace(footprint_id="fp1", net="USB_D+"),
              SimpleNamespace(footprint_id="fp1", net=""), SimpleNamespace(footprint_id="fp2", net="")))
    header, _ = protocol.decode_frame(protocol.tracks_message(snapshot, "F.Cu", 1)[4:])
    assert header["ids"] == ["t1", "a1"] and header["nets"] == ["USB_D+", "USB_D-"]
    header, _ = protocol.decode_frame(protocol.footprints_message(snapshot, 1)[4:])
    assert [record["nets"] for record in header["footprints"]] == [["USB_D+", "USB_D-"], []]
