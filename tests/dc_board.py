"""Synthetic boards for the DC analysis tests (not KiCad output).

`strip`: one F.Cu pour, 50 x 10 mm, with an SMD pad over each end (J1.1 at
x = 0..2 mm, J2.1 at x = 48..50 mm) on net +3V3.
`via_chain`: 10 x 1 mm strips on F.Cu and B.Cu joined by one through via at
x = 5.5 mm; J1.1 sits on F.Cu at x = 0..1, J2.1 on B.Cu at x = 9..10 mm.
Copper 35 um; F.Cu and B.Cu 1.53 mm apart.
"""

from kileido_bridge import model

MM = 1_000_000
COPPER_NM = 35_000
CORE_NM = 1_530_000
NET = "+3V3"


def rect(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def stackup(inner=()):
    layer = model.StackupLayer
    names = ("F.Cu", *inner, "B.Cu")
    entries = []
    for index, name in enumerate(names):
        entries.append(layer(name, "copper", COPPER_NM, "copper", None, None))
        if index + 1 < len(names):
            entries.append(layer(f"Dielectric {index + 1}", "dielectric", CORE_NM // (len(names) - 1),
                                 None, None, None))
    return model.Stackup(tuple(entries))


def smd_pad(pad_id, footprint_id, number, layer, x0, y0, x1, y1, net=NET):
    return model.Pad(pad_id, footprint_id, number, net, ((x0 + x1) // 2, (y0 + y1) // 2), None,
                     {layer: ((rect(x0, y0, x1, y1),),)})


def footprint(footprint_id, reference, x, y, side="top"):
    return model.Footprint(footprint_id, reference, (x, y), 0.0, side, ())


def snapshot(zones=(), pads=(), footprints=(), vias=(), tracks=(), arcs=(), graphics=(), inner=(),
             outline=None, name="dc_board.kicad_pcb"):
    points = [p for zone in zones for polygon in zone.polygons for p in polygon[0]]
    xs, ys = zip(*points) if points else ((0, 50 * MM), (0, 10 * MM))
    outline = outline or model.Outline(((rect(min(xs) - MM, min(ys) - MM, max(xs) + MM, max(ys) + MM),),))
    return model.BoardSnapshot(name, {}, tuple(tracks), tuple(arcs), tuple(vias), tuple(pads), tuple(footprints),
                               tuple(zones), outline, stackup(inner), (), {}, tuple(graphics))


def strip(length=50 * MM, width=10 * MM, pad=2 * MM):
    zone = model.ZoneFill("zone-strip", NET, "F.Cu", ((rect(0, 0, length, width),),))
    pads = (smd_pad("pad-j1", "fp-j1", "1", "F.Cu", 0, 0, pad, width),
            smd_pad("pad-j2", "fp-j2", "1", "F.Cu", length - pad, 0, length, width))
    footprints = (footprint("fp-j1", "J1", pad // 2, width // 2),
                  footprint("fp-j2", "J2", length - pad // 2, width // 2))
    return snapshot((zone,), pads, footprints)


def via_chain():
    zones = (model.ZoneFill("zone-front", NET, "F.Cu", ((rect(0, 0, 10 * MM, MM),),)),
             model.ZoneFill("zone-back", NET, "B.Cu", ((rect(0, 0, 10 * MM, MM),),)))
    pads = (smd_pad("pad-j1", "fp-j1", "1", "F.Cu", 0, 0, MM, MM),
            smd_pad("pad-j2", "fp-j2", "1", "B.Cu", 9 * MM, 0, 10 * MM, MM))
    footprints = (footprint("fp-j1", "J1", MM // 2, MM // 2),
                  footprint("fp-j2", "J2", 9 * MM + MM // 2, MM // 2, side="bottom"))
    via = model.Via("via-1", NET, (5_500_000, 500_000), 600_000, 300_000, "F.Cu", "B.Cu")
    return snapshot(zones, pads, footprints, (via,))
