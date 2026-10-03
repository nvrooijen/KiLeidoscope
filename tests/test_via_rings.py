"""Which layers a via has an annular ring on (kileido_bridge.via_rings), on a 4-layer board."""

import math
import random
import time

import numpy as np
import pytest

from kileido_bridge import model
from kileido_bridge.protocol import FrameDecoder, messages_for, vias_message
from kileido_bridge.via_rings import _Edges, copper_order, ringed_masks

MM = 1_000_000
ORDER = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
SQUARE = (((-5 * MM, -5 * MM), (5 * MM, -5 * MM), (5 * MM, 5 * MM), (-5 * MM, 5 * MM)),)


def board(rings, net="GND", **copper):
    """A through via at the origin (0.6 mm land) and the given copper."""
    via = model.Via("via", net, (0, 0), 600_000, 300_000, "F.Cu", "B.Cu", rings=rings)
    stack = model.Stackup(tuple(model.StackupLayer(name, "copper", 35_000, None, None, None) for name in ORDER))
    return model.BoardSnapshot("test", {}, tuple(copper.get("tracks", ())), (), (via,), tuple(copper.get("pads", ())),
                               (), tuple(copper.get("zones", ())), model.Outline(()), stack, (), {})


def layers(snapshot):
    mask = int(ringed_masks(snapshot, copper_order(snapshot))[0])
    return [name for i, name in enumerate(ORDER) if mask >> i & 1]


TRACK = model.Track("t", "F.Cu", "GND", (0, 0), (3 * MM, 0), 200_000)  # ends on the via
POUR = model.ZoneFill("z", "GND", "In1.Cu", (SQUARE,))  # the via inside it


@pytest.mark.parametrize("rings, expected", [
    (model.RINGS_ALL, ORDER),
    (model.RINGS_ENDS, ["F.Cu", "B.Cu"]),
    (model.RINGS_CONNECTED, ["F.Cu", "In1.Cu"]),
    (model.RINGS_ENDS_AND_CONNECTED, ["F.Cu", "In1.Cu", "B.Cu"]),
])
def test_rings_follow_kicads_setting(rings, expected):
    assert layers(board(rings, tracks=[TRACK], zones=[POUR])) == expected


def test_only_copper_of_the_via_net_that_reaches_its_land_connects():
    other_net = model.Track("t", "F.Cu", "VCC", (0, 0), (3 * MM, 0), 200_000)
    # A 0.2 mm track touches the 0.6 mm land within 0.4 mm of its centre.
    passing_by = model.Track("t", "F.Cu", "GND", (0, 500_000), (3 * MM, 500_000), 200_000)
    touching = model.Track("t", "F.Cu", "GND", (0, 350_000), (3 * MM, 350_000), 200_000)
    assert layers(board(model.RINGS_CONNECTED, tracks=[other_net])) == []
    assert layers(board(model.RINGS_CONNECTED, tracks=[passing_by])) == []
    assert layers(board(model.RINGS_CONNECTED, tracks=[touching])) == ["F.Cu"]
    assert layers(board(model.RINGS_CONNECTED, net="", tracks=[TRACK])) == []  # no net: nothing connects


def test_a_thermal_spoke_or_pad_connects_a_via_in_a_hole_of_the_fill():
    hole = ((-1 * MM, -1 * MM), (-1 * MM, 1 * MM), (1 * MM, 1 * MM), (1 * MM, -1 * MM))
    isolated = model.ZoneFill("z", "GND", "In2.Cu", ((SQUARE[0], hole),))  # the via in a 2 mm hole
    assert layers(board(model.RINGS_CONNECTED, zones=[isolated])) == []
    spoke = (((-150_000, 0), (1 * MM, 0), (1 * MM, 100_000), (-150_000, 100_000)),)  # reaches into the land
    assert layers(board(model.RINGS_CONNECTED, zones=[isolated, model.ZoneFill("s", "GND", "In2.Cu", (spoke,))])) \
        == ["In2.Cu"]
    pad = model.Pad("p", "fp", "1", "GND", (0, 0), None, {"B.Cu": (SQUARE,)})  # via in pad
    assert layers(board(model.RINGS_CONNECTED, pads=[pad])) == ["B.Cu"]


def test_the_vias_frame_carries_the_rings_and_resends_with_the_copper():
    snapshot = board(model.RINGS_CONNECTED, tracks=[TRACK], zones=[POUR])
    (header, arrays), = FrameDecoder().feed(vias_message(snapshot, 1))
    assert header["copper"] == ORDER
    assert int(arrays["ringed"].view("<u4")[0]) == 0b0011  # F.Cu and In1.Cu
    kinds = [header["kind"] for header, _ in FrameDecoder().feed(b"".join(
        messages_for(snapshot, frozenset({("F.Cu", "tracks")}), 2)))]
    assert kinds == ["tracks", "vias"]  # a track moved: the via's rings may have changed
    plain = board(model.RINGS_ALL, tracks=[TRACK])
    kinds = [header["kind"] for header, _ in FrameDecoder().feed(b"".join(
        messages_for(plain, frozenset({("F.Cu", "tracks")}), 2)))]
    assert kinds == ["tracks"]


def circle(cx, cy, radius, sides):
    return tuple((int(cx + radius * math.cos(2 * math.pi * k / sides)), int(cy + radius * math.sin(2 * math.pi * k / sides)))
                 for k in range(sides))


def touches_testing_every_edge(polygons, x, y, radius):
    """The plain way: per polygon, even-odd over all its rings, then every edge within the radius."""
    for polygon in polygons:
        crossings, nearest = 0, math.inf
        for ring in polygon:
            for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
                if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                    crossings += 1
                dx, dy = x1 - x0, y1 - y0
                along = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / max(dx * dx + dy * dy, 1e-12)))
                nearest = min(nearest, math.hypot(x0 + along * dx - x, y0 + along * dy - y))
        if crossings % 2 == 1 or nearest <= radius:
            return True
    return False


def test_listing_edges_by_rows_gives_the_same_answer_as_testing_every_edge():
    rng = random.Random(7)
    for _ in range(60):
        polygons = []
        for _ in range(rng.randrange(1, 5)):  # pours with holes, some overlapping each other
            cx, cy, radius = rng.randrange(0, 30 * MM), rng.randrange(0, 30 * MM), rng.randrange(MM, 12 * MM)
            holes = [circle(cx + rng.randrange(-radius, radius) // 3, cy + rng.randrange(-radius, radius) // 3,
                            rng.randrange(50_000, max(60_000, radius // 4)), rng.randrange(3, 20))
                     for _ in range(rng.randrange(0, 12))]
            polygons.append((circle(cx, cy, radius, rng.randrange(8, 40)), *holes))
        edges = _Edges(polygons)
        for _ in range(60):  # on and around the polygons, and well outside them
            x, y, radius = rng.randrange(-15 * MM, 45 * MM), rng.randrange(-15 * MM, 45 * MM), rng.randrange(1, MM)
            assert edges.touch(x, y, radius) == touches_testing_every_edge(polygons, x, y, radius), (x, y, radius)
    assert not _Edges([]).touch(0, 0, MM) and not _Edges([((),)]).touch(0, 0, MM)  # nothing to touch


def test_a_pour_with_an_antipad_per_foreign_via_stays_fast():
    """1500 vias, 450 of them in four pours that have an antipad for each of the others:
    this took half a minute when every via was tested against every edge."""
    side = 39
    vias, holes = [], []
    for k in range(1500):
        x, y = (k % side) * 2 * MM + MM, (k // side) * 2 * MM + MM
        net = "GND" if k < 450 else f"N{k}"
        vias.append(model.Via(f"v{k}", net, (x, y), 600_000, 300_000, "F.Cu", "B.Cu", rings=model.RINGS_CONNECTED))
        if net != "GND":
            holes.append(circle(x, y, 500_000, 16))
    size = side * 2 * MM + MM
    outline = ((0, 0), (size, 0), (size, size), (0, size))
    zones = tuple(model.ZoneFill(f"z{layer}", "GND", layer, ((outline, *holes),)) for layer in ORDER)
    stack = model.Stackup(tuple(model.StackupLayer(name, "copper", 35_000, None, None, None) for name in ORDER))
    snapshot = model.BoardSnapshot("test", {}, (), (), tuple(vias), (), (), zones, model.Outline(()), stack, (), {})
    started = time.perf_counter()
    masks = ringed_masks(snapshot, ORDER)
    assert time.perf_counter() - started < 3.0  # about 0.1 s; generous for a slow machine
    assert np.all(masks[:450] == 0b1111) and not masks[450:].any()  # in the pours; alone in an antipad
