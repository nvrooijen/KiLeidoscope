"""The Blender vendored codec must read bridge frames without importing bpy."""

import importlib.util
from pathlib import Path

import numpy as np

from kileido_bridge import protocol


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


def test_phase_settings_request_reaches_the_bridge_settings():
    from kileido_bridge.phase import Settings
    addon = _addon_client()
    client = addon.SocketClient("127.0.0.1", 1, "token")
    client.state = "connected"  # queue only; nothing is sent
    client.request_phase_settings({"tolerance_ps": 0.5, "min_length_mm": 2.0, "follow_series": False,
                                   "flipped": ["USB_D+"]})
    [(header, _)] = protocol.FrameDecoder().feed(bytes(client.outgoing))
    assert header["type"] == "phase_settings"
    assert Settings.from_request(header) == Settings(0.5, 2_000_000, False, frozenset({"USB_D+"}))


def test_phase_frames_decode_in_addon():
    addon = _addon_client()
    centre = np.array([[0, 0, 1_600_000], [1_000_000, 0, 1_600_000]])
    payload = protocol.phase_pair_message({"key": "D_P"}, centre, np.array([0.0, -1.5]), 7)
    [(header, arrays)] = addon.FrameDecoder().feed(payload)
    assert header["type"] == "phase_pair" and header["key"] == "D_P"
    assert arrays["centre"].tolist() == centre.tolist() and arrays["dt"].tolist() == [0.0, -1.5]
