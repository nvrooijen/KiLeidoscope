"""Hatched fills of board shapes (`kileido_bridge/hatch.py`): KiCad's own hatch, added to the
copper graphics and baked into the live board copy for kicad-cli."""

import shutil
from pathlib import Path

import pytest

from kileido_bridge import hatch, model

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_rf_geometry.kicad_pcb"
KICAD_CLI = shutil.which("kicad-cli") or r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
RECT = '''\t(gr_rect
\t\t(start 2 2)
\t\t(end 12 6)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(fill {fill})
\t\t(layer "{layer}")
\t\t(uuid "{uid}")
\t)
'''


def with_rects(*rects) -> str:
    text = FIXTURE.read_text(encoding="utf-8")
    end = text.rstrip().rfind(")")
    return text[:end] + "".join(RECT.format(fill=fill, layer=layer, uid=uid) for fill, layer, uid in rects) + text[end:]


def test_only_hatched_fills_count():
    assert not hatch.has_hatch(with_rects(("yes", "F.SilkS", "a"), ("no", "F.SilkS", "b")))
    for fill in ("hatch", "reverse_hatch", "cross_hatch"):
        assert hatch.has_hatch(with_rects((fill, "F.SilkS", "a")))


def test_hatches_join_the_copper_graphic_they_belong_to():
    outline = (((0, 0), (10, 0), (10, 1), (0, 1)),)
    graphics = (model.CopperGraphic("a", "GND", "B.Cu", (outline,)), model.CopperGraphic("b", "", "F.Cu", (outline,)))
    ring = ((1, 1), (5, 1), (5, 2))
    shapes = {"a": ("B.Cu", (ring,)), "b": ("F.SilkS", (ring,))}  # b: another layer, so not this graphic's
    first, second = hatch.apply(graphics, shapes)
    assert first.polygons == (outline, (ring,)) and second == graphics[1]


def test_baked_hatches_are_filled_polygons_on_their_layer():
    text = with_rects(("cross_hatch", "F.SilkS", "11111111-1111-1111-1111-111111111111"))
    shapes = {"11111111-1111-1111-1111-111111111111": ("F.SilkS", (((2_000_000, 2_000_000), (3_000_000, 2_000_000),
                                                                     (3_000_000, 2_500_000)),)),
              "gone": ("F.SilkS", (((0, 0), (1, 0), (1, 1)),))}  # a shape no longer on the board
    baked = hatch.bake(text, shapes)
    added = baked[len(text.rstrip()) - 1:]
    assert added.count("(gr_poly") == 1 and "(xy 2.000000 2.000000) (xy 3.000000 2.000000)" in added
    assert '(fill yes)\n\t\t(layer "F.SilkS")' in added and baked.rstrip().endswith(")")
    assert hatch.bake(text, shapes) == baked and hatch.bake(text, {}) == text


def test_layer_numbers_map_to_the_boards_canonical_names():
    names = hatch.layer_names(FIXTURE.read_text(encoding="utf-8"))
    assert names[0] == "F.Cu" and names[2] == "B.Cu" and names[5] == "F.SilkS"


def test_the_hatcher_works_in_the_background_and_keeps_the_last_hatches():
    calls = []

    def compute(text):
        calls.append(text)
        return {"a": ("F.SilkS", (((0, 0), (1, 0), (1, 1)),))}
    hatcher = hatch.Hatcher(compute_fn=compute)
    plain = with_rects(("yes", "F.SilkS", "a"))
    hatcher.want(plain)
    assert hatcher._thread is None and hatcher.shapes == {}  # nothing hatched: nothing runs
    hatched = with_rects(("cross_hatch", "F.SilkS", "a"))
    hatcher.want(hatched)
    for _ in range(200):
        if hatcher.ready_for(hatched):
            break
        hatcher._wake.wait(0.01)
    assert hatcher.ready_for(hatched) and hatcher.version == 1 and set(hatcher.shapes) == {"a"}
    hatcher.want(hatched)
    assert len(calls) == 1  # the same text: not again
    hatcher.want(plain)
    assert hatcher.shapes == {} and hatcher.version == 2


def test_hatches_worked_out_for_a_board_that_lost_them_meanwhile_are_dropped():
    import threading
    started, release = threading.Event(), threading.Event()

    def compute(text):
        started.set()
        release.wait(5)
        return {"a": ("F.SilkS", (((0, 0), (1, 0), (1, 1)),))}
    hatcher = hatch.Hatcher(compute_fn=compute)
    hatched = with_rects(("cross_hatch", "F.SilkS", "a"))
    hatcher.want(hatched)
    assert started.wait(5)
    hatcher.want(with_rects(("yes", "F.SilkS", "a")))  # the fill made solid while KiCad works
    release.set()
    for _ in range(50):
        hatcher._wake.wait(0.01)
    assert hatcher.result() == (0, {}) and not hatcher.ready_for(hatched)


@pytest.mark.skipif(not hatch.find_python(KICAD_CLI), reason="no KiCad Python with pcbnew")
def test_kicad_works_out_the_hatch():
    text = with_rects(("cross_hatch", "F.SilkS", "11111111-1111-1111-1111-111111111111"),
                      ("hatch", "B.Cu", "22222222-2222-2222-2222-222222222222"),
                      ("yes", "F.Cu", "33333333-3333-3333-3333-333333333333"))  # solid: no hatch
    shapes = hatch.compute(hatch.find_python(KICAD_CLI), text)
    assert set(shapes) == {"11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"}
    silk, copper = shapes["11111111-1111-1111-1111-111111111111"], shapes["22222222-2222-2222-2222-222222222222"]
    assert silk[0] == "F.SilkS" and copper[0] == "B.Cu"
    for _, rings in (silk, copper):  # inside the rectangle, x 2..12, y 2..6 mm
        points = [point for ring in rings for point in ring]
        assert len(points) > 20
        assert all(1_900_000 <= x <= 12_100_000 and 1_900_000 <= y <= 6_100_000 for x, y in points)
    assert sum(len(ring) for ring in silk[1]) > sum(len(ring) for ring in copper[1])  # both ways, not one
