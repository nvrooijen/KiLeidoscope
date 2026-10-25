"""Item types newer than kipy: KiCad 10.0.7's `ReferencePoint` reached kipy 0.8.0 unmapped,
and one inside a footprint failed every footprint read (user report, 2026-10-20)."""

from google.protobuf.any_pb2 import Any
from kipy.board import Board
from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.types import DocumentSpecifier

from kileido_bridge.kicad_reader import selected_ids


def _point() -> Any:
    """A packed item of a type kipy has no mapping for, as KiCad 10.0.7 sends it."""
    body = bt.Track()
    body.id.value = "point-1"  # ReferencePoint's field 1 is its id, like Track's
    return Any(type_url="type.googleapis.com/kiapi.board.types.ReferencePoint", value=body.SerializeToString())


def _pad() -> Any:
    pad = bt.Pad(type=bt.PT_SMD, number="1")
    pad.id.value = "pad-1"
    packed = Any()
    packed.Pack(pad)
    return packed


class _KiCad:
    """Answers every request with `items`, as KiCad's API server would."""

    def __init__(self, items):
        self.items = items

    def send(self, command, response_type):
        response = response_type()
        response.items.extend(self.items)
        return response


def test_a_footprint_holding_a_point_is_read_with_its_pads():
    footprint = bt.FootprintInstance()
    footprint.id.value = "fp-1"
    footprint.definition.items.extend([_pad(), _point()])
    packed = Any()
    packed.Pack(footprint)

    footprints = Board(_KiCad([packed]), DocumentSpecifier()).get_footprints()

    assert [f.id.value for f in footprints] == ["fp-1"]
    assert [pad.id.value for pad in footprints[0].definition.pads] == ["pad-1"]
    first = footprints[0].proto.SerializeToString(deterministic=True)  # the reader fingerprints this
    assert footprints[0].proto.SerializeToString(deterministic=True) == first


def test_a_selected_point_is_skipped():
    track = bt.Track()
    track.id.value = "track-1"
    packed = Any()
    packed.Pack(track)

    ids = selected_ids(Board(_KiCad([_point(), packed]), DocumentSpecifier()))

    assert "track-1" in ids and "point-1" not in ids
