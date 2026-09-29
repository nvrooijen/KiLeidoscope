"""The Blender vendored codec must read bridge frames without importing bpy."""

import ast
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


def test_return_path_frame_decodes_in_addon():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import split_board
    from kileido_bridge.return_path import ReturnPathCheck, checked_nets
    board = split_board.board()
    issues = ReturnPathCheck().check(board, checked_nets(board))
    payload = protocol.return_path_message(checked_nets(board), issues, revision=3)
    ((header, arrays),) = _addon_client().FrameDecoder().feed(payload)
    assert header["type"] == "return_path" and len(header["issues"]) == len(issues)
    assert arrays["mark"].dtype == np.dtype("<i4") and arrays["mark"].shape[1] == 5
    assert len(arrays["mark_issue"]) == len(arrays["mark_layer"]) == len(arrays["mark"])


def test_addon_knows_every_bridge_message_type():
    assert _addon_client().MESSAGE_TYPES == protocol.MESSAGE_TYPES
    apply = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "apply.py"
    tree = ast.parse(apply.read_text(encoding="utf-8"))
    handled = {node.comparators[0].value for node in ast.walk(tree)
               if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
               and node.left.id == "message_type" and isinstance(node.comparators[0], ast.Constant)}
    assert handled == set(protocol.MESSAGE_TYPES)
