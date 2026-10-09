"""Synthetic FakeBoard tests; no KiCad process or proprietary board is needed."""

from types import SimpleNamespace as NS

import numpy as np

from kipy.proto.board.board_pb2 import BoardStackupLayerType
from kipy.proto.board.board_types_pb2 import BoardLayer, DrillShape

from kileido_bridge.geometry import OUTLINE_PROBLEM, sample_arc, sample_bezier
from kileido_bridge.kicad_reader import BoardReader, read_snapshot
from kileido_bridge.model import snapshot_from_jsonable, to_jsonable
from kileido_bridge.protocol import FrameDecoder, footprints_message


def point(x, y):
    return NS(x=x, y=y)


def item_id(value):
    return NS(value=value)


def ring(*xy):
    return NS(nodes=[NS(has_point=True, point=point(x, y)) for x, y in xy])


def polygon(outer, *holes):
    return NS(outline=ring(*outer), holes=[ring(*hole) for hole in holes])


class FakeBoard:
    name = "synthetic.kicad_pcb"

    def __init__(self):
        self.tracks = []
        self.vias = []
        self.pads = []
        self.footprints = []
        self.zones = []
        self.shapes = []
        self.texts = []
        self.layer_names = {}  # user layers: canonical -> the board's name for it
        self.stackup = NS(layers=[])
        self.pad_polygons = {}
        self.outdated = {}  # pad id -> the copy KiCad answers id lookups with (after an undo)
        self.layers = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]

    def get_tracks(self):
        return self.tracks

    def get_vias(self):
        return self.vias

    def get_pads(self):
        return self.pads

    def get_footprints(self):
        return self.footprints

    def get_item_bounding_box(self, items):
        def box(item):
            return NS(pos=point(item.position.x - 1_000_000, item.position.y - 500_000),
                      size=point(2_000_000, 1_000_000))
        return [box(item) for item in items] if isinstance(items, list) else box(items)

    def get_zones(self):
        return self.zones

    def get_shapes(self):
        return self.shapes

    def get_text(self):
        return self.texts

    def get_stackup(self):
        return self.stackup

    def get_enabled_layers(self):
        return self.layers

    def get_layer_name(self, layer):
        return {BoardLayer.BL_F_Cu: "F.Cu", BoardLayer.BL_B_Cu: "B.Cu",
                BoardLayer.BL_Edge_Cuts: "Edge.Cuts", **self.layer_names}[layer]

    def get_items_by_id(self, ids):
        by_id = {pad.id.value: pad for pad in self.pads} | self.outdated
        return [by_id[item.value] for item in ids if item.value in by_id]

    def check_padstack_presence_on_layers(self, pads, layers):
        return {pad: {layer: (pad.id.value, layer) in self.pad_polygons for layer in layers}
                for pad in pads}

    def get_pad_shapes_as_polygons(self, pads, layer):
        if isinstance(pads, list):
            return [self.pad_polygons[(pad.id.value, layer)] for pad in pads
                    if (pad.id.value, layer) in self.pad_polygons]
        return self.pad_polygons.get((pads.id.value, layer))


def track(uid="t1", x=0, layer=BoardLayer.BL_F_Cu):
    return NS(id=item_id(uid), start=point(x, 0), end=point(x + 1_000_000, 0),
              width=160_000, layer=layer, net=NS(name="RF"))


def via(uid="v1"):
    drill = NS(start_layer=BoardLayer.BL_F_Cu, end_layer=BoardLayer.BL_B_Cu,
               diameter=point(300_000, 300_000))
    return NS(id=item_id(uid), position=point(0, 0), net=NS(name="RF"),
              diameter=600_000, drill_diameter=300_000, padstack=NS(drill=drill))


def pad(uid="p1"):
    drill = NS(diameter=point(300_000, 300_000), shape=DrillShape.DS_CIRCLE)
    item = FakePad()
    item.id = item_id(uid)
    item.number = "1"
    item.position = point(0, 0)
    item.net = NS(name="RF")
    item.padstack = NS(drill=drill, angle=NS(to_radians=lambda: 0.0))
    return item


class FakePad:
    pass


def footprint(uid="f1", pads=()):
    return NS(id=item_id(uid), reference_field=NS(text=NS(value="J1")),
              position=point(0, 0), orientation=NS(to_radians=lambda: 0.0),
              layer=BoardLayer.BL_F_Cu, definition=NS(pads=pads, models=[]),
              attributes=NS(do_not_populate=False))


def test_empty_board_and_missing_stackup():
    snapshot = read_snapshot(FakeBoard())
    assert not snapshot.tracks and not snapshot.zones
    assert snapshot.stackup.layers == ()
    assert any("stackup is missing" in warning for warning in snapshot.warnings)


def test_unset_stackup_values_stay_missing():
    board = FakeBoard()
    board.stackup.layers = [NS(
        type=BoardStackupLayerType.BSLT_DIELECTRIC,
        layer=BoardLayer.BL_UNDEFINED, user_name="Core", thickness=800_000,
        material_name="", dielectric=NS(layers=[NS(thickness=800_000,
                                                   material_name="", epsilon_r=0.0,
                                                   loss_tangent=0.0)]),
    )]
    layer = read_snapshot(board).stackup.layers[0]
    assert layer.thickness_nm == 800_000
    assert layer.material is None and layer.epsilon_r is None and layer.loss_tangent is None


def test_absent_dielectric_wrapper_in_kipy_08():
    board = FakeBoard()
    board.stackup.layers = [NS(
        type=BoardStackupLayerType.BSLT_DIELECTRIC,
        layer=BoardLayer.BL_UNDEFINED, user_name="Core", thickness=800_000,
        material_name="", dielectric=None)]
    snapshot = read_snapshot(board)
    layer = snapshot.stackup.layers[0]
    assert layer.thickness_nm == 800_000
    assert layer.epsilon_r is None and layer.loss_tangent is None
    assert any("no dielectric properties" in warning for warning in snapshot.warnings)


def test_explicit_zero_loss_tangent_is_preserved():
    board = FakeBoard()
    properties = NS(thickness=500_000, material_name="test", epsilon_r=4.2,
                    loss_tangent=0.0,
                    proto=NS(HasField=lambda name: True))
    board.stackup.layers = [NS(
        type=BoardStackupLayerType.BSLT_DIELECTRIC,
        layer=BoardLayer.BL_UNDEFINED, user_name="Core", thickness=500_000,
        material_name="", dielectric=NS(layers=[properties]),
    )]
    layer = read_snapshot(board).stackup.layers[0]
    assert layer.epsilon_r == 4.2 and layer.loss_tangent == 0.0


def test_moved_track_only_dirties_its_layer():
    board = FakeBoard()
    board.tracks = [track(), track("t2", layer=BoardLayer.BL_B_Cu)]
    reader = BoardReader(board)
    reader.poll()
    board.tracks[0] = track(x=50_000)
    assert reader.poll().dirty == {("F.Cu", "tracks")}


def test_arc_track_is_kept_as_an_arc_record():
    board = FakeBoard()
    curved = track("a1")
    curved.mid = point(500_000, 500_000)
    board.tracks = [curved]
    snapshot = read_snapshot(board)
    assert snapshot.tracks == ()
    assert snapshot.arcs[0].mid == (500_000, 500_000)
    assert snapshot.arcs[0].layer == "F.Cu"


def test_custom_layer_label_does_not_replace_canonical_name():
    board = FakeBoard()
    board.tracks = [track()]
    board.get_layer_name = lambda layer: "Signal_T"
    snapshot = read_snapshot(board)
    assert snapshot.tracks[0].layer == "F.Cu"
    assert snapshot.layer_display_names["F.Cu"] == "Signal_T"


def test_deleted_via_is_dirty():
    board = FakeBoard()
    board.vias = [via()]
    reader = BoardReader(board)
    reader.poll()
    board.vias = []
    assert reader.poll().dirty == {("", "vias")}


def test_zone_with_hole_and_outline_chains():
    board = FakeBoard()
    outer = [(0, 0), (10_000_000, 0), (10_000_000, 10_000_000), (0, 10_000_000)]
    hole = [(3_000_000, 3_000_000), (7_000_000, 3_000_000),
            (7_000_000, 7_000_000), (3_000_000, 7_000_000)]
    board.zones = [NS(id=item_id("z1"), is_rule_area=lambda: False, net=NS(name="GND"),
                      filled_polygons={BoardLayer.BL_F_Cu: [polygon(outer, hole)]})]
    board.shapes = [NS(layer=BoardLayer.BL_Edge_Cuts,
                       start=point(*outer[i]), end=point(*outer[(i + 1) % 4]))
                    for i in (2, 0, 3, 1)]
    result = BoardReader(board).poll()
    snapshot = result.snapshot
    assert len(snapshot.zones[0].polygons[0]) == 2
    assert len(snapshot.outline.polygons) == 1
    assert len(snapshot.outline.polygons[0][0]) == 4
    assert ("F.Cu", "zones") in result.dirty


def test_bezier_edge_is_sampled_within_tolerance():
    """A Bezier Edge.Cuts side is a curve, not a straight chord between its ends."""
    board = FakeBoard()
    corners = [(0, 0), (10_000_000, 0), (10_000_000, 10_000_000), (0, 10_000_000)]
    straight = [NS(layer=BoardLayer.BL_Edge_Cuts, start=point(*corners[i]), end=point(*corners[i + 1]))
                for i in range(3)]
    bulge = NS(layer=BoardLayer.BL_Edge_Cuts, start=point(*corners[3]), end=point(*corners[0]),
               control1=point(-4_000_000, 7_000_000), control2=point(-4_000_000, 3_000_000))
    board.shapes = [*straight, bulge]
    ring_points = read_snapshot(board).outline.polygons[0][0]
    left = [p for p in ring_points if p[0] < 0]
    assert len(left) > 10  # sampled, not a single chord along x = 0
    # The curve's leftmost point (t = 0.5) is x = 0.75 * -4 mm = -3 mm.
    assert abs(min(x for x, _ in ring_points) + 3_000_000) <= 5_000
    # Every chord midpoint stays within 5 um of the true curve (dense reference).
    samples = sample_bezier((0, 10_000_000), (-4_000_000, 7_000_000), (-4_000_000, 3_000_000), (0, 0))
    assert samples[0] == (0, 10_000_000) and samples[-1] == (0, 0)
    reference = sample_bezier((0, 10_000_000), (-4_000_000, 7_000_000), (-4_000_000, 3_000_000), (0, 0),
                              tolerance_nm=10)
    ref = np.array(reference, dtype=float)
    start, span = ref[:-1], ref[1:] - ref[:-1]
    for a, b in zip(samples, samples[1:]):
        mid = np.array(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        t = np.clip(((mid - start) * span).sum(1) / np.maximum((span * span).sum(1), 1e-9), 0, 1)
        distance = np.hypot(*(start + t[:, None] * span - mid).T).min()
        assert distance <= 5_000 + 20  # 20 nm: the dense reference's own error plus rounding


def test_pad_polygon_and_footprint_link():
    board = FakeBoard()
    first, second = pad("p1"), pad("p2")
    board.pads = [first, second]
    board.footprints = [footprint(pads=[first, second])]
    board.pad_polygons[("p2", BoardLayer.BL_F_Cu)] = polygon(
        [(0, 0), (1_000_000, 0), (1_000_000, 1_000_000), (0, 1_000_000)])
    snapshot = read_snapshot(board)
    assert snapshot.pads[0].polygons == {}
    assert len(snapshot.pads[1].polygons["F.Cu"][0][0]) == 4
    assert all(item.footprint_id == "f1" for item in snapshot.pads)
    assert snapshot.pads[0].drill == (300_000, 300_000)
    assert snapshot.footprints[0].bbox_nm == (-1_000_000, -500_000, 2_000_000, 1_000_000)
    serial = to_jsonable(snapshot)
    assert isinstance(serial["pads"][0]["pos"][0], int)


def test_do_not_populate_reaches_blender():
    board = FakeBoard()
    fitted, dnp = footprint("f1"), footprint("f2")
    dnp.attributes.do_not_populate = True
    board.footprints = [fitted, dnp]
    snapshot = read_snapshot(board)
    assert [item.dnp for item in snapshot.footprints] == [False, True]
    assert snapshot_from_jsonable(to_jsonable(snapshot)) == snapshot
    older = to_jsonable(snapshot)
    for item in older["footprints"]:
        del item["dnp"]  # a dump from before the flag
    assert not any(item.dnp for item in snapshot_from_jsonable(older).footprints)
    (header, _), = FrameDecoder().feed(footprints_message(snapshot, 1))
    assert [record["dnp"] for record in header["footprints"]] == [False, True]


def test_oval_drill_shape_and_padstack_angle_are_read():
    board = FakeBoard()
    item = pad("slot")
    item.padstack.drill.diameter = point(2_000_000, 1_000_000)
    item.padstack.drill.shape = DrillShape.DS_OBLONG
    item.padstack.angle = NS(to_radians=lambda: 0.6)
    board.pads = [item]
    board.footprints = [footprint(pads=[item])]
    board.pad_polygons[("slot", BoardLayer.BL_F_Cu)] = polygon(
        [(0, 0), (3_000_000, 0), (3_000_000, 2_000_000), (0, 2_000_000)])
    record = read_snapshot(board).pads[0]
    assert record.drill == (2_000_000, 1_000_000)
    assert record.drill_shape == "oval"
    assert record.drill_angle_rad == 0.6


def test_bulk_pad_polygon_omission_falls_back_to_individual_reads():
    board = FakeBoard()
    first, second = pad("p1"), pad("p2")
    board.pads = [first, second]
    board.footprints = [footprint(pads=[first, second])]
    board.pad_polygons[("p2", BoardLayer.BL_F_Cu)] = polygon(
        [(0, 0), (1, 0), (1, 1), (0, 1)])
    board.check_padstack_presence_on_layers = lambda pads, layers: {
        pad_item: {layer: layer == BoardLayer.BL_F_Cu for layer in layers}
        for pad_item in pads
    }
    snapshot = read_snapshot(board)
    assert snapshot.pads[0].polygons == {}
    assert "F.Cu" in snapshot.pads[1].polygons


def test_arc_sampling_stays_within_five_micrometres():
    points = sample_arc((10_000_000, 0), (7_071_068, 7_071_068), (0, 10_000_000))
    assert points[0] == (10_000_000, 0) and points[-1] == (0, 10_000_000)
    assert len(points) > 2
    for first, second in zip(points, points[1:]):
        midpoint = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
        radial_error = abs((midpoint[0] ** 2 + midpoint[1] ** 2) ** 0.5 - 10_000_000)
        assert radial_error <= 5_100  # 100 nm allows integer-coordinate rounding.


def _edges(*points):
    """Edge.Cuts lines joining consecutive points (the last one back to the first)."""
    return [NS(layer=BoardLayer.BL_Edge_Cuts, start=point(*points[i]), end=point(*points[(i + 1) % len(points)]))
            for i in range(len(points))]


def _outline_warnings(board):
    return [warning for warning in read_snapshot(board).warnings if warning.startswith(OUTLINE_PROBLEM)]


def test_outline_warnings_name_the_problem_and_where():
    """A missing, open or crossing Edge.Cuts outline is reported in KiCad millimetres;
    a board with a cutout touching nothing is not."""
    square = [(0, 0), (10_000_000, 0), (10_000_000, 10_000_000), (0, 10_000_000)]
    cutout = [(3_000_000, 3_000_000), (7_000_000, 3_000_000), (7_000_000, 7_000_000), (3_000_000, 7_000_000)]
    board = FakeBoard()
    board.shapes = _edges(*square) + _edges(*cutout)
    assert _outline_warnings(board) == []

    board.shapes = []
    assert _outline_warnings(board) == [f"{OUTLINE_PROBLEM}: no closed Edge.Cuts outline"]

    board.shapes = _edges(*square)[:3]  # one side missing
    warnings = _outline_warnings(board)
    assert len(warnings) == 2 and "open Edge.Cuts chain left out near (" in warnings[0]
    assert warnings[1] == f"{OUTLINE_PROBLEM}: no closed Edge.Cuts outline"

    for gap, closes in ((33, True), (9_900, True), (10_100, False)):  # KiCad 10.0.6 DRC: 10 um
        board.shapes = _edges(*square)[:3] + [NS(layer=BoardLayer.BL_Edge_Cuts, start=point(0, 10_000_000),
                                                 end=point(gap, 0))]
        assert (_outline_warnings(board) == []) is closes, gap

    bow_tie = [(0, 0), (10_000_000, 10_000_000), (10_000_000, 0), (0, 10_000_000)]
    board.shapes = _edges(*bow_tie)
    assert _outline_warnings(board) == [f"{OUTLINE_PROBLEM}: Edge.Cuts outline crosses itself near (5.00, 5.00) mm"]

    shifted = [(x + 5_000_000, y + 2_000_000) for x, y in cutout]  # a cutout sticking out of the edge
    board.shapes = _edges(*square) + _edges(*shifted)
    warnings = _outline_warnings(board)
    assert len(warnings) == 1 and "Edge.Cuts outlines cross each other near (10.00, " in warnings[0]


def test_outline_drawn_inside_a_footprint():
    """Edge.Cuts kept in a footprint (multi-board alignment) is part of the outline, as in
    KiCad; moving that footprint moves the outline, moving another part does not resend it."""
    square = [(0, 0), (10_000_000, 0), (10_000_000, 10_000_000), (0, 10_000_000)]
    frame = footprint("frame")
    frame.definition.shapes = _edges(*square)
    part = footprint("part")
    board = FakeBoard()
    board.footprints = [frame, part]
    reader = BoardReader(board)
    first = reader.poll(full=True)
    assert [w for w in first.snapshot.warnings if w.startswith(OUTLINE_PROBLEM)] == []
    assert set(first.snapshot.outline.polygons[0][0]) == set(square)

    part.position = point(2_000_000, 0)
    assert ("", "outline") not in reader.poll(full=True).dirty

    frame.definition.shapes = _edges(*[(x + 1_000_000, y) for x, y in square])
    moved = reader.poll(full=True)
    assert ("", "outline") in moved.dirty
    assert min(x for x, _ in moved.snapshot.outline.polygons[0][0]) == 1_000_000

    # Half the outline on the board, half in the footprint: one closed ring.
    sides = _edges(*square)
    board.shapes, frame.definition.shapes = sides[:2], sides[2:]
    assert len(read_snapshot(board).outline.polygons) == 1
    assert _outline_warnings(board) == []


def _copper_shape(layer=BoardLayer.BL_F_Cu, width=0, filled=False, shape_id="g", **geometry):
    return NS(id=item_id(shape_id), layer=layer, net=NS(name="GND"),
              attributes=NS(stroke=NS(width=width), fill=NS(filled=filled)), **geometry)


def _area(ring):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2


def test_copper_graphics_are_read_as_filled_copper():
    """Copper drawn as graphics (not pads, tracks or zones) is shown: a filled circle,
    a ring, a stroke, and a footprint's filled polygon (a net tie's bridge)."""
    board = FakeBoard()
    square = [(0, 0), (10_000_000, 0), (10_000_000, 10_000_000), (0, 10_000_000)]
    board.shapes = _edges(*square) + [
        _copper_shape(BoardLayer.BL_B_Cu, 100_000, True, "disc", center=point(5_000_000, 5_000_000),
                      radius_point=point(5_800_000, 5_000_000)),
        _copper_shape(BoardLayer.BL_F_Cu, 200_000, False, "ring", center=point(2_000_000, 2_000_000),
                      radius_point=point(3_000_000, 2_000_000)),
        _copper_shape(BoardLayer.BL_F_Cu, 250_000, False, "line", start=point(1_000_000, 8_000_000),
                      end=point(4_000_000, 8_000_000)),
        _copper_shape(BoardLayer.BL_F_Cu, 0, False, "hairline", start=point(0, 0), end=point(1, 1)),  # no copper
    ]
    bridge = [(6_000_000, 6_000_000), (8_000_000, 6_000_000), (8_000_000, 7_000_000), (6_000_000, 7_000_000)]
    net_tie = footprint("nt1")
    net_tie.definition.shapes = [_copper_shape(BoardLayer.BL_F_Cu, 0, True, "tie", polygons=[polygon(bridge)])]
    board.footprints = [net_tie]
    graphics = {graphic.id: graphic for graphic in read_snapshot(board).graphics}
    assert set(graphics) == {"disc", "ring", "line", "tie"}
    assert graphics["disc"].layer == "B.Cu" and graphics["disc"].net == "GND"
    assert abs(_area(graphics["disc"].polygons[0][0]) / (np.pi * 850_000 ** 2) - 1) < 0.01  # radius + half stroke
    outer, hole = graphics["ring"].polygons[0]  # a ring: 0.9 mm to 1.1 mm
    assert abs(_area(outer) / (np.pi * 1_100_000 ** 2) - 1) < 0.01
    assert abs(_area(hole) / (np.pi * 900_000 ** 2) - 1) < 0.01
    stadium = graphics["line"].polygons[0][0]  # 3 mm long, 0.25 mm wide, round ends
    assert abs(_area(stadium) / (3_000_000 * 250_000 + np.pi * 125_000 ** 2) - 1) < 0.01
    assert graphics["tie"].polygons == ((tuple(bridge),),)


def test_a_drilled_pad_with_copper_only_on_b_cu_keeps_its_hole():
    """Every pad drill rides on the F.Cu pads frame. A pad KiCad reports with copper only
    on B.Cu (live, an SMD pad just turned through-hole) still goes there."""
    from kileido_bridge.model import Pad
    from kileido_bridge.protocol import fill_message
    square = (((0, 0), (1_000_000, 0), (1_000_000, 1_000_000), (0, 1_000_000)),)
    pad = Pad("p", "f", "1", "", (500_000, 500_000), (400_000, 400_000), {"B.Cu": (square,)}, "round", 0.0)
    smd = Pad("s", "f", "2", "", (0, 0), None, {"B.Cu": (square,)})
    assert pad.layers == ("F.Cu", "B.Cu") and smd.layers == ("B.Cu",)
    snapshot = read_snapshot(FakeBoard())
    snapshot = type(snapshot)(**{**snapshot.__dict__, "pads": (pad, smd)})
    (header, arrays), = FrameDecoder().feed(fill_message(snapshot, "F.Cu", "pads", 1))
    assert header["ids"] == ["p"] and len(arrays["drill"]) == 1
    assert arrays["drill_plated"][0] == 1  # it has copper (on B.Cu): plated


def test_selected_assembly_variant_is_read_every_poll():
    """KiCad keeps the selected variant in memory only; `${VARIANT}` expands to its
    name ("" for the default). KiCad 9 leaves the variable unexpanded: also default."""
    board = FakeBoard()
    board.variant = "5V Output"
    board.expand_text_variables = lambda text: text.replace("${VARIANT}", board.variant)
    reader = BoardReader(board)
    snapshot = reader.poll().snapshot
    assert snapshot.variant == "5V Output"
    board.variant = ""
    assert reader.poll().snapshot.variant == ""  # a fast poll sees the switch too
    board.expand_text_variables = lambda text: text  # KiCad 9: no such variable
    assert reader.poll().snapshot.variant == ""

    def unsupported(text):
        raise RuntimeError("unhandled message")  # a KiCad without the call keeps the link

    board.expand_text_variables = unsupported
    assert reader.poll().snapshot.variant == ""
    assert read_snapshot(FakeBoard()).variant == ""  # no expand_text_variables at all
    assert snapshot_from_jsonable(to_jsonable(snapshot)).variant == "5V Output"
    older = to_jsonable(snapshot)
    del older["variant"]  # a dump from before variants
    assert snapshot_from_jsonable(older).variant == ""
