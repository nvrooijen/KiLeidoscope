"""Synthetic differential pairs for the dynamic-phase tests (pytest and headless Blender).

Coordinates in mm here, nm in the records. A 4-layer board: F.Cu, In1.Cu, In2.Cu,
B.Cu, εr 4.0 everywhere. Pairs run along x at a 1 mm pitch (P at y = 0, N at y = 1)
from U1 (x = 0) to J1 (x = 50).
"""

import json
import math
from pathlib import Path

from kileido_bridge import delays, model

MM = 1_000_000
WIDTH = 150_000
PAD = 400_000

STACKUP = model.Stackup((
    model.StackupLayer("F.Cu", "copper", 35_000, None, None, None),
    model.StackupLayer("dielectric 1", "dielectric", 200_000, "FR4", 4.0, 0.02),
    model.StackupLayer("In1.Cu", "copper", 35_000, None, None, None),
    model.StackupLayer("dielectric 2", "dielectric", 1_000_000, "FR4", 4.0, 0.02),
    model.StackupLayer("In2.Cu", "copper", 35_000, None, None, None),
    model.StackupLayer("dielectric 3", "dielectric", 200_000, "FR4", 4.0, 0.02),
    model.StackupLayer("B.Cu", "copper", 35_000, None, None, None),
))


# KiCad's numbers for the tests: one tuning profile, on every net's netclass.
PROFILE = delays.TuningProfile("Test", {"F.Cu": 6.0, "In1.Cu": 7.0, "In2.Cu": 7.0, "B.Cu": 6.0}, via_ps_per_mm=7.0)
PROJECT = {"net_settings": {"classes": [{"name": "Default", "tuning_profile": "Test"}]},
           "tuning_profiles": {"tuning_profiles_impedance_geometric": [{
               "profile_name": "Test", "via_prop_delay": 7_000_000,
               "layer_entries": [{"signal_layer": layer, "delay": round(ps * delays.AS_PER_PS)}
                                 for layer, ps in PROFILE.layers.items()]}]}}


class EveryNet(dict):
    """net -> profile name: `name` for every net."""

    def __init__(self, name):
        super().__init__()
        self.name = name

    def get(self, net, default=None):
        return self.name


def delay_model(profile=PROFILE):
    """Every net on `profile`, as its netclass's tuning profile would put it."""
    return delays.ProfileDelays({profile.name: profile}, EveryNet(profile.name))


def project_file(folder) -> str:
    """PROJECT written as a .kicad_pro in `folder`: what the live tracker reads."""
    path = Path(folder) / "board.kicad_pro"
    path.write_text(json.dumps(PROJECT), encoding="utf-8")
    return str(path)


def nm(point):
    return round(point[0] * MM), round(point[1] * MM)


class Board:
    def __init__(self):
        self.tracks, self.arcs, self.vias, self.pads, self.footprints, self.zones = [], [], [], [], [], []
        self._count = 0

    def _id(self, prefix):
        self._count += 1
        return f"{prefix}-{self._count}"

    def part(self, reference, *pins, layer="F.Cu", pad=PAD):
        """A footprint; each pin is (number, net, (x, y) mm) with a square pad `pad` nm wide."""
        footprint_id = self._id(reference)
        x0, y0 = nm(pins[0][2])
        self.footprints.append(model.Footprint(footprint_id, reference, (x0, y0), 0.0, "top", ()))
        for number, net, where in pins:
            x, y = nm(where)
            square = ((x - pad // 2, y - pad // 2), (x + pad // 2, y - pad // 2),
                      (x + pad // 2, y + pad // 2), (x - pad // 2, y + pad // 2))
            self.pads.append(model.Pad(f"{reference}.{number}", footprint_id, str(number), net, (x, y), None,
                                       {layer: ((square,),)}))
        return self

    def path(self, net, *points, layer="F.Cu"):
        """Tracks through `points` (mm)."""
        for a, b in zip(points, points[1:]):
            self.tracks.append(model.Track(self._id("t"), layer, net, nm(a), nm(b), WIDTH))
        return self

    def arc(self, net, start, mid, end, layer="F.Cu"):
        """An arc through three points (mm)."""
        self.arcs.append(model.Arc(self._id("a"), layer, net, nm(start), nm(mid), nm(end), WIDTH))
        return self

    def turn(self, net, centre, radius, from_deg, to_deg, layer="F.Cu"):
        """An arc of `radius` (mm) about `centre`, from one bearing to another (degrees,
        counter-clockwise positive), as one arc per 90 degrees or less."""
        bearing = from_deg
        while bearing != to_deg:
            step = max(-90, min(90, to_deg - bearing))
            points = [(centre[0] + radius * math.cos(math.radians(bearing + part * step / 2)),
                       centre[1] + radius * math.sin(math.radians(bearing + part * step / 2))) for part in range(3)]
            self.arc(net, *points, layer=layer)
            bearing += step
        return self

    def via(self, net, where, top="F.Cu", bottom="B.Cu"):
        self.vias.append(model.Via(self._id("v"), net, nm(where), 600_000, 300_000, top, bottom))
        return self

    def pour(self, net, x0, y0, x1, y1, layer="F.Cu"):
        """A filled zone: the rectangle (mm) between the two corners."""
        corners = tuple(nm(point) for point in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))
        self.zones.append(model.ZoneFill(self._id("z"), net, layer, ((corners,),)))
        return self

    def snapshot(self):
        outline = model.Outline(((tuple(nm(p) for p in ((-5, -5), (55, -5), (55, 15), (-5, 15))),),))
        return model.BoardSnapshot("phase.kicad_pcb", {}, tuple(self.tracks), tuple(self.arcs), tuple(self.vias),
                                   tuple(self.pads), tuple(self.footprints), tuple(self.zones), outline, STACKUP, (), {})


def ends(board, p="D_P", n="D_N", start="U1", end="J1"):
    """The pair's pads at x = 0 and x = 50."""
    board.part(start, (1, p, (0, 0)), (2, n, (0, 1)))
    board.part(end, (1, p, (50, 0)), (2, n, (50, 1)))
    return board


def bump(x, y, away):
    """A 0.25 mm detour away from the partner at x: 0.5 mm of extra length."""
    return (x, y), (x, y + away), (x + 0.2, y + away), (x + 0.2, y)


def matched():
    board = ends(Board())
    return board.path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (50, 1)).snapshot()


def compensated_far():
    """P's extra length at x = 2 is made up on N only at x = 47."""
    board = ends(Board())
    board.path("D_P", (0, 0), *bump(2, 0, -0.25), (50, 0))
    board.path("D_N", (0, 1), *bump(47, 1, 0.25), (50, 1))
    return board.snapshot()


def compensated_locally():
    """P's extra length at x = 2 is made up on N at x = 4."""
    board = ends(Board())
    board.path("D_P", (0, 0), *bump(2, 0, -0.25), (50, 0))
    board.path("D_N", (0, 1), *bump(4, 1, 0.25), (50, 1))
    return board.snapshot()


def layer_change():
    """F.Cu to x = 20, a through via each, B.Cu on to J1's pads on the bottom."""
    board = Board().part("U1", (1, "D_P", (0, 0)), (2, "D_N", (0, 1)))
    board.part("J1", (1, "D_P", (50, 0)), (2, "D_N", (50, 1)), layer="B.Cu")
    for net, y in (("D_P", 0), ("D_N", 1)):
        board.path(net, (0, y), (20, y)).via(net, (20, y)).path(net, (20, y), (50, y), layer="B.Cu")
    return board.snapshot()


def ac_coupled():
    """U1 -> TX_P/N -> C1, C2 -> TX_C_P/N -> J1."""
    board = Board()
    board.part("U1", (1, "TX_P", (0, 0)), (2, "TX_N", (0, 1)))
    board.part("J1", (1, "TX_C_P", (50, 0)), (2, "TX_C_N", (50, 1)))
    board.part("C1", (1, "TX_P", (20, 0)), (2, "TX_C_P", (21, 0)))
    board.part("C2", (1, "TX_N", (20, 1)), (2, "TX_C_N", (21, 1)))
    board.path("TX_P", (0, 0), (20, 0)).path("TX_C_P", (21, 0), (50, 0))
    board.path("TX_N", (0, 1), (20, 1)).path("TX_C_N", (21, 1), (50, 1))
    return board.snapshot()


def esd_stub():
    """A TVS array D1 on stubs that tee off the middle of both tracks at x = 25."""
    board = ends(Board()).part("D1", (1, "D_P", (25, -3)), (2, "D_N", (26, 4)))
    board.path("D_P", (0, 0), (50, 0)).path("D_P", (25, 0), (25, -3))  # tees into the long track
    board.path("D_N", (0, 1), (50, 1)).path("D_N", (26, 1), (26, 4))
    return board.snapshot()


def multi_drop():
    """A second receiver U3 on stubs: P's tees off at x = 25 (10 mm), N's at x = 20
    (30 mm), so on its own N's farthest ends are J1 and U3, not U1 and J1."""
    board = ends(Board()).part("U3", (1, "D_P", (25, -10)), (2, "D_N", (20, 31)))
    board.path("D_P", (0, 0), (50, 0)).path("D_P", (25, 0), (25, -10))
    board.path("D_N", (0, 1), (50, 1)).path("D_N", (20, 1), (20, 31))
    return board.snapshot()


def pass_through():
    """P runs through an ESD part's pads (in at D1.1, out at D1.2) and ends at J1 with a
    second short track inside its pad: both pads connect twice, as on real boards."""
    board = ends(Board()).part("D1", (1, "D_P", (20, 0)), (2, "D_P", (22, 0)), (3, "D_N", (26, 4)))
    board.path("D_P", (0, 0), (20, 0)).path("D_P", (20, 0), (22, 0)).path("D_P", (22, 0), (50, 0))
    board.path("D_P", (49.9, 0), (50, 0))  # a second end in J1.1's pad
    board.path("D_N", (0, 1), (50, 1)).path("D_N", (26, 1), (26, 4))
    return board.snapshot()


def grounded(gap_mm=0.1):
    """The matched pair with a GND pour on F.Cu `gap_mm` from each trace's outer edge."""
    board = ends(Board())
    board.path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (50, 1))
    edge = WIDTH / 2 / MM + gap_mm
    board.pour("GND", 5, -3, 45, -edge).pour("GND", 5, 1 + edge, 45, 4)
    return board.snapshot()


def shorted():
    """The matched pair with a P track across to N at x = 20."""
    board = ends(Board())
    board.path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (50, 1)).path("D_P", (20, 0), (20, 1))
    return board.snapshot()


def looped():
    """The matched pair with a second P path from x = 10 to 40, 3 mm out: an old route
    left beside the new one, closing a loop."""
    board = ends(Board())
    board.path("D_P", (0, 0), (50, 0)).path("D_N", (0, 1), (50, 1))
    board.path("D_P", (10, 0), (10, -3), (40, -3), (40, 0))
    return board.snapshot()


def two_channels():
    """The far-compensated pair D, and a pair E at y = 10 that dives to B.Cu at x = 20."""
    board = ends(Board())
    board.path("D_P", (0, 0), *bump(2, 0, -0.25), (50, 0))
    board.path("D_N", (0, 1), *bump(47, 1, 0.25), (50, 1))
    board.part("U2", (1, "E_P", (0, 10)), (2, "E_N", (0, 11)))
    board.part("J2", (1, "E_P", (50, 10)), (2, "E_N", (50, 11)), layer="B.Cu")
    for net, y in (("E_P", 10), ("E_N", 11)):
        board.path(net, (0, y), (20, y)).via(net, (20, y)).path(net, (20, y), (50, y), layer="B.Cu")
    return board.snapshot()
