"""Binary frame format and message builders."""

import ast
from pathlib import Path

import numpy as np
import pytest
from kipy.proto.board.board_types_pb2 import BoardLayer

from kileido_bridge.kicad_reader import BoardReader
from kileido_bridge.protocol import (FrameDecoder, MAX_FRAME_BYTES, MESSAGE_TYPES, encode_frame, findings_message,
                                     messages_for)
from test_phase1 import FakeBoard, item_id, point, polygon, track, via
from types import SimpleNamespace as NS


def decode_all(frames):
    decoder = FrameDecoder()
    return decoder.feed(b"".join(frames))


def test_roundtrip_keeps_integer_arrays_exact():
    seg = np.array([[-2_000_000_000, 5, 6, 7, 160_000]], dtype="<i4")
    [(header, arrays)] = decode_all([encode_frame({"type": "x"}, {"seg": seg})])
    assert header["type"] == "x" and header["protocol"] == 1
    assert arrays["seg"].dtype == np.dtype("<i4") and np.array_equal(arrays["seg"], seg)


def test_decoder_handles_split_and_merged_reads():
    frames = [encode_frame({"n": i}, {"a": np.arange(i + 1, dtype="<i4")}) for i in range(3)]
    stream = b"".join(frames)
    decoder = FrameDecoder()
    received = []
    for i in range(0, len(stream), 7):  # odd-sized chunks cut through lengths and headers
        received += decoder.feed(stream[i:i + 7])
    assert [h["n"] for h, _ in received] == [0, 1, 2]
    assert np.array_equal(received[2][1]["a"], [0, 1, 2])


def test_oversized_frame_is_rejected():
    with pytest.raises(ValueError):
        FrameDecoder().feed((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))


def test_float64_is_refused_to_keep_the_wire_integer_or_float32():
    with pytest.raises(ValueError):
        encode_frame({}, {"a": np.zeros(2, dtype=np.float64)})


def test_messages_only_for_dirty_groups_with_arc_sampled_into_tracks():
    board = FakeBoard()
    curved = track("a1")
    curved.mid = point(500_000, 500_000)
    curved.end = point(1_000_000, 0)
    board.tracks = [track("t1"), curved, track("t2", layer=BoardLayer.BL_B_Cu)]
    board.vias = [via()]
    outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(3, 3), (7, 3), (7, 7)]
    board.zones = [NS(id=item_id("z1"), is_rule_area=lambda: False, net=NS(name="GND"),
                      filled_polygons={BoardLayer.BL_F_Cu: [polygon(outer, hole)]})]
    reader = BoardReader(board)
    first = reader.poll()
    frames = {(h.get("layer"), h.get("kind", h["type"])): (h, a)
              for h, a in decode_all(messages_for(first.snapshot, first.dirty, revision=1))}
    header, arrays = frames[("F.Cu", "tracks")]
    assert header["ids"] == ["t1", "a1"]
    assert arrays["seg"].shape[1] == 5 and len(arrays["seg"]) > 2  # arc became several segments
    assert set(arrays["item"][1:]) == {1}
    header, arrays = frames[("F.Cu", "zones")]
    assert list(arrays["ring_start"]) == [0, 4, 7] and list(arrays["ring_hole"]) == [0, 1]
    assert ("", "vias") in frames and ("B.Cu", "tracks") in frames

    board.tracks[2] = track("t2", x=5, layer=BoardLayer.BL_B_Cu)
    second = reader.poll()
    only = decode_all(messages_for(second.snapshot, second.dirty, revision=2))
    assert [(h["layer"], h["kind"]) for h, _ in only] == [("B.Cu", "tracks")]


def test_findings_frame_round_trip():
    payload = {"status": "ok", "folder": "C:/p/.kileidoscope", "drc": {"state": "idle", "error": "", "run": "4"},
               "sources": {"drc": {"status": "none", "findings": []},
                           "file": {"status": "ok", "findings": [{"key": "3f2a", "title": "Stub µ"}]}}}
    [(header, arrays)] = decode_all([findings_message(payload, 7)])
    assert header["type"] == "findings" and header["revision"] == 7 and header["findings"] == payload
    assert arrays == {}


def _sent_types(path: Path) -> set[str]:
    """Every literal "type" of a dict built in `path`: the frames it sends."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "type" and isinstance(value, ast.Constant):
                    found.add(value.value)
    return found


def test_message_types_lists_every_frame_the_bridge_sends():
    bridge = Path(__file__).resolve().parents[1] / "kileido_bridge"
    sent = _sent_types(bridge / "protocol.py") | _sent_types(bridge / "loop.py")
    assert sent == set(MESSAGE_TYPES) and len(MESSAGE_TYPES) == len(set(MESSAGE_TYPES))
