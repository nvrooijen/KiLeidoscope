"""The fixture is KiCad's own output; check it against values known independently.

`synthetic_rf_geometry.kicad_dump.json` was produced by KiCad 10.0.3 through the bridge:
open tests/fixtures/synthetic_rf_geometry.kicad_pcb, press B (refill zones), then
`python -m kileido_bridge dump tests/fixtures/synthetic_rf_geometry.kicad_dump.json`.
Redo that after any change to the .kicad_pcb.

Expected values come from the .kicad_pcb source text and from `kicad-cli pcb export
ipcd356` / `pcb export pos` on the same file, not from our own code.
"""

import math
from pathlib import Path

import json

from kileido_bridge import model

DUMP = Path(__file__).resolve().parent / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
MM = 1_000_000


def snapshot():
    return model.snapshot_from_jsonable(json.loads(DUMP.read_text(encoding="utf-8")))


def mm(x, y):
    return round(x * MM), round(y * MM)


def area_mm2(ring):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2 / MM ** 2


def test_tracks_arcs_and_via_match_the_board_file():
    s = snapshot()
    tracks = {(t.layer, t.start, t.end, t.width) for t in s.tracks}
    assert tracks == {("F.Cu", mm(5, 5), mm(5, 20), 700_000), ("F.Cu", mm(5, 5), mm(12, 5), 700_000),
                      ("F.Cu", mm(5, 11), mm(10, 11), 700_000), ("B.Cu", mm(30, 5), mm(30, 20), 700_000),
                      ("B.Cu", mm(30, 5), mm(23, 5), 700_000), ("B.Cu", mm(30, 11), mm(25, 11), 700_000)}
    (arc,) = s.arcs
    assert (arc.start, arc.mid, arc.end, arc.width) == (mm(15, 5), mm(20, 10), mm(15, 15), 500_000)
    (via,) = s.vias
    assert (via.pos, via.diameter, via.drill, via.layer_top, via.layer_bottom) == (mm(20, 20), 900_000, 350_000, "F.Cu", "B.Cu")


def test_footprints_rotation_and_side_match_kicad_cli_pos():
    expected = {"J1": (mm(12, 23), 0, "top"), "J2": (mm(32, 10), 0, "bottom"),
                "J3": (mm(30, 25), 90, "top"), "J4": (mm(34, 16), 90, "bottom")}
    got = {f.reference: (f.pos, round(math.degrees(f.rotation_rad)) % 360, f.side) for f in snapshot().footprints}
    assert got == expected


def test_rotated_pads_match_kicad_cli_ipcd356():
    pads = {p.id: p for p in snapshot().pads}
    assert pads["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"].pos == mm(29, 27)  # J3, top, 90 deg
    assert pads["bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"].pos == mm(35, 18)  # J4, bottom, 90 deg
    assert set(pads["bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"].polygons) == {"B.Cu"}


def test_through_hole_pad_is_on_every_copper_layer_without_a_drill_ring():
    """Measured KiCad behaviour: pad copper polygons do not include the drill hole."""
    pad = next(p for p in snapshot().pads if p.id.startswith("5555"))
    assert set(pad.polygons) == {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
    for polygons in pad.polygons.values():
        assert len(polygons) == 1 and len(polygons[0]) == 1  # outer ring only
        xs, ys = zip(*polygons[0][0])
        assert (min(xs), min(ys), max(xs), max(ys)) == (*mm(8, 21), *mm(16, 25))
    assert pad.drill == (2 * MM, 1 * MM)  # the hole is only in the drill field


def test_zone_fill_is_one_fractured_ring_with_clearances():
    """Measured KiCad behaviour: a fill with holes arrives as ONE ring (holes joined by slits)."""
    (zone,) = snapshot().zones
    assert zone.layer == "In1.Cu" and len(zone.polygons) == 1 and len(zone.polygons[0]) == 1
    ring = zone.polygons[0][0]
    area = area_mm2(ring)
    # 36 x 26 mm zone minus the 12 x 6 mm keepout = 864 mm^2, minus pad/via/board-hole clearances.
    assert 780 < area < 864, area
    assert any(abs(x - 14 * MM) < 300_000 and abs(y - 12 * MM) < 300_000 for x, y in ring)  # keepout corner


def test_outline_is_board_edge_and_cutout():
    rings = sorted((min(x for x, _ in r), min(y for _, y in r), max(x for x, _ in r), max(y for _, y in r))
                   for polygon in snapshot().outline.polygons for r in polygon)
    assert rings == sorted([(*mm(0, 0), *mm(40, 30)), (*mm(18, 23), *mm(22, 27))])
