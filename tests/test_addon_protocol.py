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


def test_findings_request_encodes_for_bridge():
    addon = _addon_client()
    client = addon.SocketClient("127.0.0.1", 0, "token")
    client.request_findings("dismiss", "3f2a", "file")
    assert not client.outgoing  # nothing queued before the link is up
    client.state = "connected"
    client.request_findings("dismiss", "3f2a", "file")
    client.request_findings("run_drc")
    frames = protocol.FrameDecoder().feed(bytes(client.outgoing))
    assert [header for header, _ in frames] == [
        {"type": "findings", "action": "dismiss", "key": "3f2a", "source": "file", "protocol": addon.PROTOCOL},
        {"type": "findings", "action": "run_drc", "key": "", "source": "drc", "protocol": addon.PROTOCOL}]
    from kileido_bridge.server import BridgeServer
    server = BridgeServer(port=0, token="t")
    try:
        for header, _ in frames:
            server._handle(header)
        request = server.take_findings_requests()[-1]
        assert request == ("run_drc", "", "drc"), request
    finally:
        server.close()


def test_addon_applies_every_frame_type_the_bridge_sends():
    import ast
    apply = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "apply.py"
    function = next(node for node in ast.walk(ast.parse(apply.read_text(encoding="utf-8")))
                    if isinstance(node, ast.FunctionDef) and node.name == "apply_frame")
    handled = {node.comparators[0].value for node in ast.walk(function)
               if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
               and node.left.id == "message_type" and isinstance(node.ops[0], ast.Eq)}
    assert handled == set(protocol.MESSAGE_TYPES)
