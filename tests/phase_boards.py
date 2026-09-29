"""Synthetic differential pairs for the dynamic-phase tests (pytest and headless Blender).

Coordinates in mm here, nm in the records. A 4-layer board: F.Cu, In1.Cu, In2.Cu,
B.Cu, εr 4.0 everywhere. Pairs run along x at a 1 mm pitch (P at y = 0, N at y = 1)
from U1 (x = 0) to J1 (x = 50).
"""

from kileido_bridge import model

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


def nm(point):
    return round(point[0] * MM), round(point[1] * MM)


class Board:
    def __init__(self):
        self.tracks, self.vias, self.pads, self.footprints = [], [], [], []
        self._count = 0

    def _id(self, prefix):
        self._count += 1
        return f"{prefix}-{self._count}"

    def part(self, reference, *pins, layer="F.Cu"):
        """A footprint; each pin is (number, net, (x, y) mm)."""
        footprint_id = self._id(reference)
        x0, y0 = nm(pins[0][2])
        self.footprints.append(model.Footprint(footprint_id, reference, (x0, y0), 0.0, "top", ()))
        for number, net, where in pins:
            x, y = nm(where)
            square = ((x - PAD // 2, y - PAD // 2), (x + PAD // 2, y - PAD // 2),
                      (x + PAD // 2, y + PAD // 2), (x - PAD // 2, y + PAD // 2))
            self.pads.append(model.Pad(f"{reference}.{number}", footprint_id, str(number), net, (x, y), None,
                                       {layer: ((square,),)}))
        return self

    def path(self, net, *points, layer="F.Cu"):
        """Tracks through `points` (mm)."""
        for a, b in zip(points, points[1:]):
            self.tracks.append(model.Track(self._id("t"), layer, net, nm(a), nm(b), WIDTH))
        return self

    def via(self, net, where, top="F.Cu", bottom="B.Cu"):
        self.vias.append(model.Via(self._id("v"), net, nm(where), 600_000, 300_000, top, bottom))
        return self

    def snapshot(self):
        outline = model.Outline(((tuple(nm(p) for p in ((-5, -5), (55, -5), (55, 15), (-5, 15))),),))
        return model.BoardSnapshot("phase.kicad_pcb", {}, tuple(self.tracks), (), tuple(self.vias),
                                   tuple(self.pads), tuple(self.footprints), (), outline, STACKUP, (), {})


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
