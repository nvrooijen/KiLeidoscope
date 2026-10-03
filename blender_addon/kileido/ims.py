"""IMS mode: a 2-layer board on an insulated metal substrate (an aluminum or copper base).

KiCad has no metal-core setting and only even copper-layer counts, so an IMS board is a
2-layer board, B.Cu left empty (or one pour), its thickness the one ordered from the fab
("aluminum, 1.6 mm"): the fab supplies its own thin thermal dielectric. IMS mode keeps
that thickness and F.Cu's copper; under F.Cu it draws a thin epoxy of the panel's
thickness, and the metal base takes the rest, B.Cu's place included.

Pure Python, no bpy. apply.py shapes the live board with `stack` as its board frame arrives,
so every height read later (copper, vias, drills, components) is already the IMS one.
"""

from typing import NamedTuple

import numpy as np

METALS = {"AL": "Aluminum", "CU": "Copper"}
COLORS = {"AL": (0.80, 0.81, 0.83), "CU": (184 / 255, 115 / 255, 50 / 255)}  # sRGB: bare aluminum, bare copper
# The base's outer faces in Realistic mode: (label, roughness, anisotropy). The cut always
# shows a polished section, as a micrograph does.
FINISHES = {"MILL": ("Mill finish", 0.5, 0.0),  # as rolled: dull, the usual IMS base
            "BRUSHED": ("Brushed", 0.32, 0.7),
            "POLISHED": ("Polished", 0.12, 0.0),
            "NICKEL": ("Nickel plated", 0.22, 0.0)}
NICKEL = (0.66, 0.62, 0.55)  # sRGB: electroless nickel over the base metal
EPOXY_UM = (75, 150, 100)  # an IMS thermal dielectric: the fabs' range, and the usual one
FULL_POUR = 0.9  # B.Cu copper over this share of the board is the base, drawn by KiCad users


class Stack(NamedTuple):
    heights: dict  # copper layer -> z (m), as the bridge's: B.Cu's bottom at 0, F.Cu at its top
    layer_thickness: dict  # the stackup's, with B.Cu's set to the base
    thickness_m: float
    base: tuple  # (z0, z1) of the metal base
    dielectric: tuple  # (z0, z1) of the thermal dielectric (the epoxy)


def eligible(heights):
    """(True, "") on a 2-layer board, else (False, why IMS mode is unavailable)."""
    if set(heights) == {"F.Cu", "B.Cu"}:
        return True, ""
    return False, f"IMS needs a 2-layer board; this one has {len(heights)} copper layers"


def stack(heights, layer_thickness, epoxy_m):
    """The board at KiCad's thickness, bottom up: the metal base, an `epoxy_m` thermal
    dielectric, F.Cu. Copper without a stackup thickness counts as flat (none is invented);
    a board thinner than the epoxy is all epoxy."""
    top = float(heights["F.Cu"])
    below = max(0.0, top - float(layer_thickness.get("F.Cu") or 0.0))  # F.Cu's underside
    epoxy = min(max(0.0, float(epoxy_m)), below)
    base = below - epoxy
    thickness = dict(layer_thickness, **{"B.Cu": base})
    return Stack(dict(heights), thickness, top, (0.0, base), (base, below))


def fill_area(xy, following, hole):
    """Area inside closed rings, as apply._ring_edges stores them: point i's edge runs to
    point `following[i]` (i + 1, or back to its ring's first point), and a ring whose
    points have `hole` set is cut out of the rest."""
    xy, following = np.asarray(xy, np.float64), np.asarray(following, np.int64)
    if not len(xy):
        return 0.0
    x, y = xy[:, 0], xy[:, 1]
    cross = x * y[following] - x[following] * y
    ring = np.concatenate(([0], np.cumsum(following[:-1] != np.arange(1, len(xy)))))
    first = np.searchsorted(ring, np.arange(ring[-1] + 1))
    areas = np.abs(np.bincount(ring, cross)) / 2
    return float(np.sum(np.where(np.asarray(hole)[first], -areas, areas)))


def bottom_copper(routed, copper_area, board_area):
    """What B.Cu holds: "empty", "pour" (one area over most of the board: the base drawn in
    KiCad), or "routed" (copper of its own, which the base would short).

    `routed`: B.Cu tracks, arcs and pads; areas in any one unit.
    """
    if routed:
        return "routed"
    if copper_area <= 0:
        return "empty"
    return "pour" if board_area > 0 and copper_area >= FULL_POUR * board_area else "routed"


def _count(n, thing):
    return f"{n} {thing}{'' if n == 1 else 's'}"


def warnings(vias, plated_holes, bottom):
    """Panel lines for what an IMS board cannot have."""
    lines = []
    if vias:
        lines.append(f"{_count(vias, 'via')} would short to the metal base")
    if plated_holes:
        lines.append(f"{_count(plated_holes, 'plated hole')} would short to the metal base")
    if bottom == "routed":
        lines.append("B.Cu has copper of its own; the metal base takes its place")
    return lines
