"""The copper balance analysis (`blender_addon/kileido/copper_balance.py`), without Blender.

Boards are built from the bridge's own records and frame builders, decoded as the
viewer decodes them, and fed to the analysis. Expected areas are worked out by hand
from the shapes' dimensions. Every test runs with the C scanline library (when built
for this platform) and with the numpy fallback.
"""

import importlib
import math
import sys
import types
from pathlib import Path

import pytest

from kileido_bridge import model, protocol

# The add-on package's __init__ needs bpy: load its modules under a bare package instead.
_ADDON = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido"
_PACKAGE = types.ModuleType("kls_addon_balance")
_PACKAGE.__path__ = [str(_ADDON)]
sys.modules[_PACKAGE.__name__] = _PACKAGE
balance = importlib.import_module(f"{_PACKAGE.__name__}.copper_balance")
gerber = importlib.import_module(f"{_PACKAGE.__name__}.gerber")
_NATIVE = gerber._native_coverage


@pytest.fixture(autouse=True, params=["native", "numpy"])
def backend(request, monkeypatch):
    if request.param == "native" and _NATIVE is None:
        pytest.skip("C scanline library not built for this platform")
    monkeypatch.setattr(gerber, "_native_coverage", _NATIVE if request.param == "native" else None)
    return request.param


def mm(value):
    return int(round(value * 1e6))


def rect(x0, y0, x1, y1):
    return tuple((mm(x), mm(y)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


STACKUP = model.Stackup(tuple(model.StackupLayer(name, kind, 35_000 if kind == "copper" else 500_000, None, None, None)
                              for name, kind in (("F.Cu", "copper"), ("core 1", "dielectric"), ("In1.Cu", "copper"),
                                                 ("core 2", "dielectric"), ("In2.Cu", "copper"),
                                                 ("core 3", "dielectric"), ("B.Cu", "copper"))))
OUTLINE = model.Outline(((rect(0, 0, 20, 10),),))  # 200 mm2: 4 x 2 tiles of 5 mm


def track(layer, start, end, width, name="t"):
    return model.Track(name, layer, "", (mm(start[0]), mm(start[1])), (mm(end[0]), mm(end[1])), mm(width))


def zone(layer, *polygon, name="z"):
    return model.ZoneFill(name, "", layer, (tuple(polygon),))


def board(tracks=(), zones=(), vias=(), pads=(), outline=OUTLINE, stackup=STACKUP):
    return model.BoardSnapshot("balance", {}, tuple(tracks), (), tuple(vias), tuple(pads), (), tuple(zones),
                               outline, stackup, (), {})


def analyze(snapshot, tile_mm=5.0):
    frames = balance.CopperFrames()
    for header, arrays in protocol.FrameDecoder().feed(b"".join(protocol.snapshot_frames(snapshot))):
        frames.observe(header, arrays)
    return balance.analyze(*frames.inputs(), tile_mm=tile_mm)


def capsule_mm2(length, width):
    return length * width + math.pi * width ** 2 / 4


def test_overlapping_copper_counts_once():
    result = analyze(board(zones=[zone("F.Cu", rect(0, 0, 10, 10))],
                           tracks=[track("F.Cu", (2, 5), (8, 5), 1.0), track("F.Cu", (5, 2), (5, 8), 1.0)]))
    assert result.board_mm2 == pytest.approx(200, rel=1e-4)
    assert result.layers["F.Cu"].copper_mm2 == pytest.approx(100, rel=1e-4)
    assert result.layers["F.Cu"].percent == pytest.approx(50, rel=1e-4)
    assert result.order == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert result.layers["B.Cu"].percent == 0  # a layer with no copper still has a row


def test_track_area_and_tile_density():
    result = analyze(board(tracks=[track("B.Cu", (11, 2.5), (19, 2.5), 1.0)]))
    expected = capsule_mm2(8, 1.0)
    assert result.layers["B.Cu"].copper_mm2 == pytest.approx(expected, rel=1e-3)
    density = result.layers["B.Cu"].density
    assert density.shape == (2, 4)
    # The track runs through the top row's third and fourth tiles (x 10-15 and 15-20 mm), half each.
    assert density[0, 2] == pytest.approx(100 * expected / 2 / 25, rel=1e-3)
    assert density[0, 3] == pytest.approx(density[0, 2], rel=1e-3)
    assert density[1].sum() == 0 and density[0, :2].sum() == 0


def test_zone_holes_outline_cutouts_and_clipping():
    cutout = model.Outline(((rect(0, 0, 20, 10), rect(12, 2, 16, 6)),))  # 200 - 16 mm2
    result = analyze(board(outline=cutout, zones=[
        zone("F.Cu", rect(0, 0, 10, 10), rect(2, 2, 4, 4)),  # 100 - 4 mm2
        zone("In1.Cu", rect(-5, -5, 25, 15), name="z2"),  # over the whole board and beyond it
    ]))
    assert result.board_mm2 == pytest.approx(184, rel=1e-4)
    assert result.layers["F.Cu"].copper_mm2 == pytest.approx(96, rel=1e-4)
    assert result.layers["In1.Cu"].percent == pytest.approx(100, rel=1e-4)  # clipped to the board
    assert result.board_fraction[0, 2] == pytest.approx(16 / 25, rel=1e-4)  # 3 x 3 mm of the cutout in it


def test_nested_outline_shapes_are_cutouts():
    """Each closed Edge.Cuts shape is its own polygon; one inside another is a cutout,
    and an island inside that cutout is board again (the viewer fills them even-odd)."""
    outline = model.Outline(((rect(0, 0, 20, 10),), (rect(11, 1, 19, 9),), (rect(13, 3, 17, 7),)))
    result = analyze(board(outline=outline, zones=[zone("F.Cu", rect(0, 0, 20, 10))]))
    assert result.board_mm2 == pytest.approx(200 - 64 + 16, rel=1e-4)
    assert result.layers["F.Cu"].percent == pytest.approx(100, rel=1e-4)


def test_pad_and_via_drills_are_cut_out():
    pad = model.Pad("p", "fp", "1", "", (mm(15), mm(5)), (mm(1.0), mm(1.0)),
                    {layer: ((rect(14, 4, 16, 6),),) for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")},
                    "round")
    via = model.Via("v", "", (mm(5), mm(5)), mm(0.8), mm(0.4), "F.Cu", "In1.Cu")  # blind: two layers
    result = analyze(board(pads=[pad], vias=[via]))
    land, hole = math.pi * 0.4 ** 2, math.pi * 0.2 ** 2
    assert result.layers["B.Cu"].copper_mm2 == pytest.approx(4 - math.pi * 0.25, rel=1e-3)
    assert result.layers["F.Cu"].copper_mm2 == pytest.approx(4 - math.pi * 0.25 + land - hole, rel=1e-3)
    assert result.layers["In1.Cu"].copper_mm2 == pytest.approx(result.layers["F.Cu"].copper_mm2, rel=1e-6)
    assert result.layers["In2.Cu"].copper_mm2 == pytest.approx(result.layers["B.Cu"].copper_mm2, rel=1e-6)


def test_oval_drill_is_a_slot():
    pad = model.Pad("p", "fp", "1", "", (mm(10), mm(5)), (mm(2.0), mm(1.0)),
                    {"F.Cu": ((rect(8, 3, 12, 7),),)}, "oval", math.pi / 2)
    result = analyze(board(pads=[pad]))
    slot = 1.0 * 1.0 + math.pi * 0.25  # 2 x 1 mm stadium: a square and two half circles
    assert result.layers["F.Cu"].copper_mm2 == pytest.approx(16 - slot, rel=1e-3)


def test_mirrored_pairs_and_worst_tiles():
    result = analyze(board(zones=[zone("F.Cu", rect(0, 0, 20, 10)), zone("B.Cu", rect(0, 0, 15, 10), name="z2"),
                                  zone("In1.Cu", rect(0, 0, 5, 5), name="z3")]))
    found = balance.pairs(result, threshold_pct=15)
    assert [(p.top, p.bottom) for p in found] == [("F.Cu", "B.Cu"), ("In1.Cu", "In2.Cu")]
    outer, inner = found
    assert outer.difference == pytest.approx(25, rel=1e-4) and outer.flagged
    assert {(t.row, t.column) for t in outer.worst} == {(0, 3), (1, 3)}  # B.Cu is bare there
    assert outer.worst[0].top_pct == pytest.approx(100) and outer.worst[0].bottom_pct == pytest.approx(0, abs=1e-6)
    assert inner.difference == pytest.approx(12.5, rel=1e-4) and not inner.flagged
    assert [(t.row, t.column) for t in inner.worst] == [(0, 0)]
    assert inner.worst[0].center_nm == (mm(2.5), mm(2.5))
    assert not balance.pairs(result, threshold_pct=30)[0].flagged


def test_tile_size_and_edge_slivers():
    result = analyze(board(zones=[zone("F.Cu", rect(0, 0, 20, 10))]), tile_mm=3.0)
    assert result.layers["F.Cu"].density.shape == (4, 7)  # 20 x 10 mm in 3 mm tiles, the last ones partial
    assert result.board_fraction[3, 6] == pytest.approx(2 / 9, rel=1e-4)
    assert result.layers["F.Cu"].density[3, 6] == pytest.approx(100, rel=1e-4)  # of its board area
    assert result.layers["F.Cu"].percent == pytest.approx(100, rel=1e-4)


def test_live_edit_replaces_its_group():
    frames = balance.CopperFrames()
    snapshot = board(tracks=[track("F.Cu", (2, 5), (8, 5), 1.0)])
    for header, arrays in protocol.FrameDecoder().feed(b"".join(protocol.snapshot_frames(snapshot))):
        frames.observe(header, arrays)
    before = frames.version
    edited = board(tracks=[track("F.Cu", (2, 5), (18, 5), 1.0)])
    for header, arrays in protocol.FrameDecoder().feed(b"".join(protocol.messages_for(edited, {("F.Cu", "tracks")}, 2))):
        assert frames.observe(header, arrays)
    assert frames.version > before
    status = protocol.FrameDecoder().feed(protocol.encode_frame({"type": "status", "kicad": "connected"}))[0]
    assert not frames.observe(*status)
    result = balance.analyze(*frames.inputs())
    assert result.layers["F.Cu"].copper_mm2 == pytest.approx(capsule_mm2(16, 1.0), rel=1e-3)


def test_no_outline_is_an_error():
    with pytest.raises(ValueError, match="outline"):
        analyze(board(outline=model.Outline(()), tracks=[track("F.Cu", (2, 5), (8, 5), 1.0)]))


def test_heatmap_colours_and_mask():
    result = analyze(board(zones=[zone("F.Cu", rect(0, 0, 10, 10))]))
    image = balance.heatmap(result, "F.Cu", opacity=0.5)
    rows, columns = result.mask.shape
    assert image.shape == (rows, columns, 4) and rows % 2 == 0 and columns % 4 == 0
    assert image[0, 0, :3].tolist() == pytest.approx(balance.RAMP[-1].tolist())  # full tile: darkest
    assert image[-1, -1, :3].tolist() == pytest.approx(balance.RAMP[0].tolist())  # bare tile: lightest
    assert float(image[..., 3].max()) == pytest.approx(0.5)
