"""Incremental reading: raw fingerprints, tiered polling, busy handling, proto polygons."""

from types import SimpleNamespace as NS

import pytest
from kipy.errors import ApiError
from kipy.board import BoardStackup
from kipy.proto.board import board_pb2
from kipy.proto.board.board_pb2 import BoardStackupLayerType
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common import ApiStatusCode
from kipy.proto.common.types import base_types_pb2 as pb

from kileido_bridge.kicad_reader import BoardReader, KiCadBusy, _polygon, read_snapshot
from test_phase1 import FakeBoard, point, track, via


class CountingBoard(FakeBoard):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.busy = False

    def __getattribute__(self, name):
        attribute = super().__getattribute__(name)
        if name.startswith("get_") and callable(attribute):
            def wrapped(*args):
                self.calls.append(name)
                if self.busy:
                    raise ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)
                return attribute(*args)
            return wrapped
        return attribute


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_idle_poll_reads_only_fast_sources_and_reports_nothing_dirty():
    board, clock = CountingBoard(), Clock()
    board.tracks = [track()]
    reader = BoardReader(board, slow_interval_s=1.0, clock=clock)
    assert reader.poll().full_read
    board.calls.clear()
    clock.now = 0.2
    result = reader.poll()
    assert not result.full_read and result.dirty == frozenset()
    assert board.calls == ["get_tracks", "get_vias"]
    assert result.snapshot.tracks[0].id == "t1"  # unchanged records are reused


def test_slow_tier_runs_after_interval():
    board, clock = CountingBoard(), Clock()
    reader = BoardReader(board, slow_interval_s=1.0, clock=clock)
    reader.poll()
    clock.now = 1.5
    assert reader.poll().full_read


def test_fast_change_triggers_full_read_in_same_poll():
    board, clock = CountingBoard(), Clock()
    reader = BoardReader(board, clock=clock)
    reader.poll()
    board.vias = [via()]
    clock.now = 0.2
    result = reader.poll()
    assert result.full_read and result.dirty == {("", "vias")}


def test_busy_raises_and_forces_full_read_afterwards():
    board, clock = CountingBoard(), Clock()
    reader = BoardReader(board, clock=clock)
    reader.poll()
    board.busy = True
    clock.now = 0.2
    with pytest.raises(KiCadBusy):
        reader.poll()
    board.busy = False
    clock.now = 0.3
    assert reader.poll().full_read


def test_removed_track_layer_is_dirty():
    board = FakeBoard()
    board.tracks = [track(layer=BoardLayer.BL_B_Cu)]
    reader = BoardReader(board)
    reader.poll()
    board.tracks = []
    assert reader.poll().dirty == {("B.Cu", "tracks")}


def _stackup(dielectric_red, dielectric_nm=100_000):
    """A real kipy stackup: F.Cu over a dielectric whose colour is `dielectric_red`."""
    proto = board_pb2.BoardStackup()
    copper = proto.layers.add(layer=BoardLayer.BL_F_Cu, enabled=True, type=BoardStackupLayerType.BSLT_COPPER)
    copper.thickness.value_nm = 35_000
    dielectric = proto.layers.add(layer=BoardLayer.BL_UNDEFINED, enabled=True,
                                  type=BoardStackupLayerType.BSLT_DIELECTRIC)
    dielectric.thickness.value_nm = dielectric_nm
    dielectric.color.r = dielectric_red
    return BoardStackup(proto)


def test_dielectric_colour_noise_does_not_dirty_the_stackup():
    """KiCad 10.0.6 sends a dielectric's colour uninitialised (1.27e-311, different on every
    read): that alone must not resend the board; a real stackup change still does."""
    board, clock = CountingBoard(), Clock()
    board.stackup = _stackup(1.2667e-311)
    reader = BoardReader(board, slow_interval_s=1.0, clock=clock)
    reader.poll()
    board.stackup = _stackup(9.3460e-307)
    clock.now = 1.5
    result = reader.poll()
    assert result.full_read and ("", "stackup") not in result.dirty
    board.stackup = _stackup(1.4241e-306, dielectric_nm=200_000)
    clock.now = 3.0
    assert ("", "stackup") in reader.poll().dirty


def test_proto_polygon_path_with_arc_node():
    """Production reads polygon points from kipy protos, not from wrappers."""
    outline = pb.PolyLine(closed=True)
    for x, y in [(0, 0), (10_000_000, 0)]:
        outline.nodes.add().point.CopyFrom(pb.Vector2(x_nm=x, y_nm=y))
    arc = outline.nodes.add().arc
    arc.start.CopyFrom(pb.Vector2(x_nm=10_000_000, y_nm=0))
    arc.mid.CopyFrom(pb.Vector2(x_nm=7_071_068, y_nm=7_071_068))
    arc.end.CopyFrom(pb.Vector2(x_nm=0, y_nm=10_000_000))
    hole = pb.PolyLine(closed=True)
    for x, y in [(1_000_000, 1_000_000), (2_000_000, 1_000_000), (2_000_000, 2_000_000)]:
        hole.nodes.add().point.CopyFrom(pb.Vector2(x_nm=x, y_nm=y))
    wrapper = NS(outline=NS(proto=outline), holes=[NS(proto=hole)])
    outer_ring, hole_ring = _polygon(wrapper)
    assert outer_ring[0] == (0, 0) and outer_ring[1] == (10_000_000, 0)
    assert outer_ring[-1] == (0, 10_000_000) and len(outer_ring) > 4
    assert len(set(outer_ring)) == len(outer_ring)  # arc start not duplicated
    assert hole_ring == ((1_000_000, 1_000_000), (2_000_000, 1_000_000), (2_000_000, 2_000_000))


def test_missing_dielectric_properties_warn():
    board = FakeBoard()
    board.stackup.layers = [NS(
        type=BoardStackupLayerType.BSLT_DIELECTRIC, layer=BoardLayer.BL_UNDEFINED,
        user_name="", thickness=121_120, material_name="", dielectric=NS(layers=[]),
    )]
    snapshot = read_snapshot(board)
    assert snapshot.stackup.layers[0].epsilon_r is None
    assert any("no dielectric properties" in warning for warning in snapshot.warnings)


def test_only_new_or_changed_items_are_converted(monkeypatch):
    import kileido_bridge.kicad_reader as reader_module
    converted = []
    original = reader_module._convert_track
    monkeypatch.setattr(reader_module, "_convert_track", lambda item: converted.append(item.id.value) or original(item))
    board = FakeBoard()
    board.tracks = [track("t1"), track("t2", x=5), track("t3", x=9)]
    reader = BoardReader(board)
    reader.poll()
    assert sorted(converted) == ["t1", "t2", "t3"]
    converted.clear()
    board.tracks[1] = track("t2", x=7)
    snapshot = reader.poll().snapshot
    assert converted == ["t2"]
    assert [t.id for t in snapshot.tracks] == ["t1", "t2", "t3"] and snapshot.tracks[1].start == (7, 0)


def test_changed_pad_fetches_shapes_for_that_pad_only_and_dirties_its_layer():
    from test_phase1 import footprint, pad, polygon
    board = CountingBoard()
    first, second = pad("p1"), pad("p2")
    board.pads = [first, second]
    board.footprints = [footprint(pads=[first, second])]
    square = polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    board.pad_polygons[("p1", BoardLayer.BL_F_Cu)] = square
    board.pad_polygons[("p2", BoardLayer.BL_B_Cu)] = square
    reader = BoardReader(board)
    reader.poll()
    requested = []
    original = board.check_padstack_presence_on_layers
    board.check_padstack_presence_on_layers = lambda pads, layers: requested.append(
        [p.id.value for p in pads]) or original(pads, layers)
    moved = pad("p2")
    moved.position = point(5, 5)
    board.pads = [first, moved]
    board.footprints = [footprint(pads=[first, moved])]
    result = reader.poll(full=True)
    assert requested == [["p2"]]
    # The footprint contains its pads, so it changes too. The pad is drilled, and every
    # pad drill rides on the F.Cu frame, so F.Cu is redrawn for its hole.
    assert result.dirty == {("B.Cu", "pads"), ("F.Cu", "pads"), ("", "footprints")}
    assert [p.footprint_id for p in result.snapshot.pads] == ["f1", "f1"]


def kicad_pad(x=0, y=0, degrees=0.0, bottom=False, size=(2_000_000, 1_000_000)):
    """A real kipy pad: the outdated-copy checks compare protos."""
    from kipy.board_types import Pad
    from kipy.proto.board import board_types_pb2 as bt
    proto = bt.Pad(type=bt.PT_SMD)
    proto.id.value = "p1"
    proto.number = "1"
    proto.position.x_nm, proto.position.y_nm = x, y
    stack = proto.pad_stack
    stack.layers.extend([BoardLayer.BL_B_Cu, BoardLayer.BL_B_Paste] if bottom else
                        [BoardLayer.BL_F_Cu, BoardLayer.BL_F_Paste])
    stack.drill.start_layer, stack.drill.end_layer = ((BoardLayer.BL_B_Cu, BoardLayer.BL_F_Cu) if bottom else
                                                      (BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu))
    stack.angle.value_degrees = degrees
    copper = stack.copper_layers.add(layer=BoardLayer.BL_F_Cu, shape=bt.PSS_RECTANGLE)
    copper.size.x_nm, copper.size.y_nm = size
    return Pad(proto)


def read_outdated(fresh, copy, copy_polygon):
    """KiCad answers for `fresh` with `copy`, whose shape is `copy_polygon` on F.Cu."""
    from test_phase1 import footprint, polygon
    board = CountingBoard()
    board.pads = [fresh]
    board.footprints = [footprint(pads=[fresh])]
    board.outdated = {"p1": copy}
    board.pad_polygons[("p1", BoardLayer.BL_F_Cu)] = polygon(copy_polygon)
    return BoardReader(board).poll(full=True)


TRIANGLE = [(0, 0), (1000, 0), (0, 500)]  # no symmetry hides a wrong rotation or mirror


def test_outdated_copy_is_moved_and_rotated_onto_the_pad():
    result = read_outdated(kicad_pad(10_000_000, 5_000_000, 90.0), kicad_pad(), TRIANGLE)
    # +90 degrees in KiCad turns +x up the screen (-y).
    assert result.snapshot.pads[0].polygons == {"F.Cu": (((
        (10_000_000, 5_000_000), (10_000_000, 4_999_000), (10_000_500, 5_000_000)),),)}
    assert result.outdated_pads == 0


def test_outdated_copy_of_a_flipped_pad_is_mirrored_onto_the_bottom():
    result = read_outdated(kicad_pad(bottom=True), kicad_pad(), TRIANGLE)
    # Mirrored across the pad's x axis, ring reversed to keep its winding.
    assert result.snapshot.pads[0].polygons == {"B.Cu": ((((0, -500), (1000, 0), (0, 0)),),)}
    assert result.outdated_pads == 0


def test_outdated_copy_with_another_shape_is_built_from_the_fresh_padstack():
    result = read_outdated(kicad_pad(size=(3_000_000, 1_000_000)), kicad_pad(), TRIANGLE)
    assert result.snapshot.pads[0].polygons == {"F.Cu": ((((1_500_000, 500_000), (-1_500_000, 500_000),
                                                          (-1_500_000, -500_000), (1_500_000, -500_000)),),)}
    assert result.outdated_pads == 0


def test_smd_pad_turned_through_hole_gets_copper_on_both_sides():
    """Measured in KiCad 10: after the change KiCad still answers for the pad as the
    B.Cu-only SMD pad it was; the fresh padstack says *.Cu."""
    from kipy.proto.board import board_types_pb2 as bt
    fresh = kicad_pad(size=(2_500_000, 2_000_000))
    fresh.proto.type = bt.PT_PTH
    stack = fresh.proto.pad_stack
    del stack.layers[:]
    stack.layers.extend([BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu])
    stack.type = bt.PST_NORMAL
    stack.copper_layers[0].shape = bt.PSS_ROUNDRECT
    stack.copper_layers[0].corner_rounding_ratio = 0.05
    stack.drill.diameter.x_nm = stack.drill.diameter.y_nm = 1_000_000
    result = read_outdated(fresh, kicad_pad(bottom=True, size=(2_500_000, 2_000_000)), TRIANGLE)
    polygons = result.snapshot.pads[0].polygons
    assert set(polygons) == {"F.Cu", "B.Cu"} and result.outdated_pads == 0
    ring = polygons["F.Cu"][0][0]
    assert (min(x for x, _ in ring), max(x for x, _ in ring)) == (-1_250_000, 1_250_000)
    assert (min(y for _, y in ring), max(y for _, y in ring)) == (-1_000_000, 1_000_000)


def test_outdated_copy_with_a_custom_shape_is_counted():
    from kipy.proto.board import board_types_pb2 as bt
    fresh = kicad_pad(size=(3_000_000, 1_000_000))
    fresh.proto.pad_stack.copper_layers[0].shape = bt.PSS_CUSTOM
    result = read_outdated(fresh, kicad_pad(), TRIANGLE)
    assert result.outdated_pads == 1


def test_zone_change_dirties_only_its_layer():
    from test_phase1 import item_id, polygon
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]

    def zone(uid, layer, size=10):
        ring = [(x * size // 10, y * size // 10) for x, y in square]
        return NS(id=item_id(uid), is_rule_area=lambda: False, net=NS(name="GND"),
                  filled_polygons={layer: [polygon(ring)]})

    board = FakeBoard()
    top = zone("z1", BoardLayer.BL_F_Cu)
    board.zones = [top, zone("z2", BoardLayer.BL_B_Cu)]
    reader = BoardReader(board)
    reader.poll()
    board.zones = [top, zone("z2", BoardLayer.BL_B_Cu, size=20)]
    assert reader.poll(full=True).dirty == {("B.Cu", "zones")}


def test_parallel_connections_give_the_same_snapshot_and_report_busy():
    from test_phase1 import footprint, pad, polygon
    board = CountingBoard()
    board.tracks = [track("t1"), track("t2", layer=BoardLayer.BL_B_Cu)]
    board.vias = [via()]
    first = pad("p1")
    board.pads = [first]
    board.footprints = [footprint(pads=[first])]
    board.pad_polygons[("p1", BoardLayer.BL_F_Cu)] = polygon([(0, 0), (1, 0), (1, 1)])
    sequential = BoardReader(board).poll().snapshot
    parallel = BoardReader(board, pool_boards=[board, board, board])
    try:
        snapshot = parallel.poll().snapshot
        assert (snapshot.tracks, snapshot.vias, snapshot.pads) == (sequential.tracks, sequential.vias, sequential.pads)
        board.busy = True
        with pytest.raises(KiCadBusy):
            parallel.poll()
    finally:
        parallel.close()


def test_restarted_kicad_is_only_followed_when_the_user_asks(monkeypatch):
    import os
    import kileido_bridge.kicad_reader as reader_module
    calls = []

    class FakeKiCad:
        def __init__(self, timeout_ms=3000, kicad_token=None):
            self.token = os.environ.get("KICAD_API_TOKEN", "") if kicad_token is None else kicad_token
            self._client = NS(_kicad_token="new-instance")
            calls.append(self.token)

        def get_board(self):
            if self.token == "old-instance":  # the KiCad that launched this bridge is gone
                raise ApiError("token mismatch", code=ApiStatusCode.AS_TOKEN_MISMATCH)
            return "board"

    monkeypatch.setattr(reader_module, "KiCad", FakeKiCad)
    monkeypatch.setenv("KICAD_API_TOKEN", "old-instance")
    monkeypatch.setattr(reader_module, "_follow_new_kicad", False)
    monkeypatch.setattr(reader_module, "_reached_kicad", False)
    with pytest.raises(ApiError):  # never let in: a second KiCad's plugin stays refused
        reader_module.connect_board()
    monkeypatch.setattr(reader_module, "_reached_kicad", True)
    with pytest.raises(reader_module.NewKiCad):  # let in before: a new KiCad waits for the user
        reader_module.connect_board()
    assert os.environ["KICAD_API_TOKEN"] == "old-instance"
    reader_module.follow_new_kicad()
    assert reader_module.connect_board() == "board"
    assert os.environ["KICAD_API_TOKEN"] == "new-instance"
    calls.clear()
    assert reader_module.connect_board() == "board" and calls == ["new-instance"]
