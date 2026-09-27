"""A synthetic 4-layer board for the reference-plane and return-path tests (not KiCad output).

Stackup: F.Cu, 0.2 mm, In1.Cu, 1.0 mm, In2.Cu, 0.2 mm, B.Cu. In1.Cu is split at
x = 50 mm: GND left, +3V3 right, 0.5 mm apart. In2.Cu is one GND plane.

- USB_D+/USB_D- (F.Cu, y = 10 mm) cross the split.
- ETH_P/ETH_N (F.Cu, y = 30 mm) stay over GND.
- SATA_P/SATA_N (F.Cu, y = 35 mm) cross a 1 x 2 mm void in the GND plane at x = 25 mm.
- LVDS_P/LVDS_N change from F.Cu to B.Cu through vias at x = 20 mm, whose antipads
  are cut out of both planes; `with_return_via` adds a GND via next to them.
- CLK (F.Cu, y = 5 mm) crosses the split but is no pair: checked only when selected.
"""

from dataclasses import replace

from kileido_bridge import model

MM = 1_000_000
WIDTH = 150_000
PITCH = 250_000  # centre to centre within a pair
VIA_X = 20 * MM
LVDS_Y = 20 * MM
LVDS_PITCH = 1_200_000  # the LVDS lines spread apart for their vias
ANTIPAD = 550_000  # half-size of the square cut around each LVDS via


def rect(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def square(x, y, half):
    return rect(x - half, y - half, x + half, y + half)


def pair(name_p, name_n, layer, x0, x1, y, prefix):
    return tuple(model.Track(f"{prefix}-{index}", layer, net, (x0, y + index * PITCH), (x1, y + index * PITCH), WIDTH)
                 for index, net in enumerate((name_p, name_n)))


def _stackup():
    layer = model.StackupLayer
    return model.Stackup((
        layer("F.Cu", "copper", 35_000, "copper", None, None),
        layer("Dielectric 1", "dielectric", 200_000, None, None, None),
        layer("In1.Cu", "copper", 35_000, "copper", None, None),
        layer("Dielectric 2", "dielectric", 1_000_000, None, None, None),
        layer("In2.Cu", "copper", 35_000, "copper", None, None),
        layer("Dielectric 3", "dielectric", 200_000, None, None, None),
        layer("B.Cu", "copper", 35_000, "copper", None, None),
    ))


def board() -> model.BoardSnapshot:
    via_holes = tuple(square(VIA_X, LVDS_Y + index * LVDS_PITCH, ANTIPAD) for index in range(2))
    void = rect(25 * MM, 34 * MM, 26 * MM, 36 * MM)
    zones = (
        model.ZoneFill("zone-gnd-in1", "GND", "In1.Cu",
                       ((rect(1 * MM, 1 * MM, 49_750_000, 39 * MM), void, *via_holes),)),
        model.ZoneFill("zone-3v3-in1", "+3V3", "In1.Cu", ((rect(50_250_000, 1 * MM, 99 * MM, 39 * MM),),)),
        model.ZoneFill("zone-gnd-in2", "GND", "In2.Cu", ((rect(1 * MM, 1 * MM, 99 * MM, 39 * MM), *via_holes),)),
    )
    lvds_front = tuple(model.Track(f"lvds-f-{index}", "F.Cu", net, (10 * MM, LVDS_Y + index * LVDS_PITCH),
                                   (VIA_X, LVDS_Y + index * LVDS_PITCH), WIDTH)
                       for index, net in enumerate(("LVDS_P", "LVDS_N")))
    lvds_back = tuple(model.Track(f"lvds-b-{index}", "B.Cu", net, (VIA_X, LVDS_Y + index * LVDS_PITCH),
                                  (30 * MM, LVDS_Y + index * LVDS_PITCH), WIDTH)
                      for index, net in enumerate(("LVDS_P", "LVDS_N")))
    vias = tuple(model.Via(f"via-lvds-{index}", net, (VIA_X, LVDS_Y + index * LVDS_PITCH), 600_000, 300_000,
                           "F.Cu", "B.Cu")
                 for index, net in enumerate(("LVDS_P", "LVDS_N")))
    tracks = (*pair("USB_D+", "USB_D-", "F.Cu", 10 * MM, 90 * MM, 10 * MM, "usb"),
              *pair("ETH_P", "ETH_N", "F.Cu", 10 * MM, 40 * MM, 30 * MM, "eth"),
              *pair("SATA_P", "SATA_N", "F.Cu", 10 * MM, 40 * MM, 35 * MM, "sata"),
              *lvds_front, *lvds_back,
              model.Track("clk", "F.Cu", "CLK", (10 * MM, 5 * MM), (90 * MM, 5 * MM), WIDTH))
    outline = model.Outline(((rect(0, 0, 100 * MM, 40 * MM),),))
    return model.BoardSnapshot("split_board.kicad_pcb", {}, tracks, (), vias, (), (), zones, outline, _stackup(),
                               (), {})


def with_return_via(snapshot: model.BoardSnapshot, distance_nm: int = 1 * MM) -> model.BoardSnapshot:
    """A through GND via `distance_nm` beside the LVDS pair's vias."""
    via = model.Via("via-gnd", "GND", (VIA_X, LVDS_Y - distance_nm), 600_000, 300_000, "F.Cu", "B.Cu")
    return replace(snapshot, vias=(*snapshot.vias, via))
