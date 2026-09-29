"""Sampling only: no copper meshing, triangulation, or polygon unions."""

from __future__ import annotations

import math

import numpy as np

from .model import Point, Polygon, Ring

OUTLINE_PROBLEM = "Board outline is missing or malformed"  # every outline warning starts with it
_CROSSING_BLOCK = 2048  # segments tested at once against all others (memory stays small)


def outline_warning(problem: str, where: Point | None = None) -> str:
    """One board-outline warning, located in KiCad millimetres when a point is known."""
    text = f"{OUTLINE_PROBLEM}: {problem}"
    return text if where is None else f"{text} near ({where[0] / 1e6:.2f}, {where[1] / 1e6:.2f}) mm"


def outline_crossings(polygons: tuple[Polygon, ...] | list[Polygon]) -> list[tuple[str, Point]]:
    """(warning, where) for Edge.Cuts rings that cross themselves or each other (touching is fine).

    Only proper crossings count: segments meeting at an end point, like neighbours in a
    ring or a cutout touching the edge, are not a crossing. One warning per kind.
    """
    rings = [ring for polygon in polygons for ring in polygon if len(ring) >= 3]
    if not rings:
        return []
    starts = np.concatenate([np.asarray(ring, np.float64) for ring in rings])
    ends = np.concatenate([np.roll(np.asarray(ring, np.float64), -1, axis=0) for ring in rings])
    owner = np.concatenate([np.full(len(ring), index) for index, ring in enumerate(rings)])
    low, high = np.minimum(starts, ends), np.maximum(starts, ends)
    direction = ends - starts
    found = {}
    for first in range(0, len(starts), _CROSSING_BLOCK):
        block = slice(first, first + _CROSSING_BLOCK)
        near = np.all((low[block, None] <= high[None]) & (low[None] <= high[block, None]), axis=2)
        near &= np.arange(first, first + near.shape[0])[:, None] < np.arange(len(starts))[None]  # each pair once
        i, j = np.nonzero(near)
        i += first
        if not len(i):
            continue
        r, s = direction[i], direction[j]
        denominator = r[:, 0] * s[:, 1] - r[:, 1] * s[:, 0]
        qp = starts[j] - starts[i]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (qp[:, 0] * s[:, 1] - qp[:, 1] * s[:, 0]) / denominator
            u = (qp[:, 0] * r[:, 1] - qp[:, 1] * r[:, 0]) / denominator
        crossing = (denominator != 0) & (t > 1e-9) & (t < 1 - 1e-9) & (u > 1e-9) & (u < 1 - 1e-9)
        for a, b, share in zip(i[crossing], j[crossing], t[crossing]):
            kind = "self" if owner[a] == owner[b] else "each other"
            if kind not in found:
                point = starts[a] + share * direction[a]
                found[kind] = (round(point[0]), round(point[1]))
    problems = {"self": "Edge.Cuts outline crosses itself", "each other": "Edge.Cuts outlines cross each other"}
    return [(outline_warning(problems[kind], where), where) for kind, where in found.items()]


def arc_step(radius: float, tolerance_nm: int = 5_000) -> float:
    """Largest angle (radians) whose chord stays within `tolerance_nm` of a circle of `radius`."""
    return 2 * math.acos(max(-1.0, 1 - min(tolerance_nm / radius, 2)))


def sample_arc(start: Point, mid: Point, end: Point, tolerance_nm: int = 5_000) -> tuple[Point, ...]:
    """Sample the circular arc through three points to a maximum chord sagitta."""
    x1, y1 = start
    x2, y2 = mid
    x3, y3 = end
    determinant = 2 * ((x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1))
    if determinant == 0:
        return tuple(dict.fromkeys((start, mid, end)))
    a = (x2 - x1) ** 2 + (y2 - y1) ** 2
    b = (x3 - x1) ** 2 + (y3 - y1) ** 2
    cx = x1 + (a * (y3 - y1) - b * (y2 - y1)) / determinant
    cy = y1 + (b * (x2 - x1) - a * (x3 - x1)) / determinant
    radius = math.hypot(x1 - cx, y1 - cy)
    first = math.atan2(y1 - cy, x1 - cx)
    middle = math.atan2(y2 - cy, x2 - cx)
    last = math.atan2(y3 - cy, x3 - cx)
    ccw = (last - first) % math.tau
    sweep = ccw if (middle - first) % math.tau <= ccw else ccw - math.tau
    steps = max(1, math.ceil(abs(sweep) / arc_step(radius, tolerance_nm)))
    points = [(round(cx + radius * math.cos(first + sweep * i / steps)),
               round(cy + radius * math.sin(first + sweep * i / steps)))
              for i in range(steps + 1)]
    points[0], points[-1] = start, end
    return tuple(dict.fromkeys(points))


def sample_bezier(start: Point, control1: Point, control2: Point, end: Point,
                  tolerance_nm: int = 5_000) -> tuple[Point, ...]:
    """Sample a cubic Bezier curve so no chord strays more than `tolerance_nm` from it.

    Uniform steps: a chord of parameter length 1/n deviates at most M / (8 n^2), where
    M = 6 * max(|P0 - 2 P1 + P2|, |P1 - 2 P2 + P3|) bounds the second derivative.
    """
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = start, control1, control2, end
    bend = max(math.hypot(x0 - 2 * x1 + x2, y0 - 2 * y1 + y2),
               math.hypot(x1 - 2 * x2 + x3, y1 - 2 * y2 + y3))
    steps = max(1, math.ceil(math.sqrt(0.75 * bend / tolerance_nm)))
    points = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        point = (round(a * x0 + b * x1 + c * x2 + d * x3), round(a * y0 + b * y1 + c * y2 + d * y3))
        if not points or points[-1] != point:
            points.append(point)
    points[0], points[-1] = start, end  # exact endpoints, so the outline chains stitch
    return tuple(points)


# --- Copper graphics as filled polygons (strokes have round ends, like KiCad draws them) ---

def circle_ring(center: Point, radius: float, tolerance_nm: int = 5_000) -> Ring:
    count = max(8, math.ceil(math.tau / arc_step(radius, tolerance_nm)))
    return tuple((round(center[0] + radius * math.cos(math.tau * i / count)),
                  round(center[1] + radius * math.sin(math.tau * i / count))) for i in range(count))


def stroke(start: Point, end: Point, width: float) -> Polygon:
    """A line of `width` with round ends (a stadium); a dot when both ends meet."""
    radius = width / 2
    dx, dy = end[0] - start[0], end[1] - start[1]
    if not dx and not dy:
        return (circle_ring(start, radius),)
    heading = math.atan2(dy, dx)
    count = max(4, math.ceil(math.pi / arc_step(radius)))
    ring = []
    for centre, turn in ((end, heading - math.pi / 2), (start, heading + math.pi / 2)):
        ring += [(round(centre[0] + radius * math.cos(turn + math.pi * i / count)),
                  round(centre[1] + radius * math.sin(turn + math.pi * i / count))) for i in range(count + 1)]
    return (tuple(ring),)


def polyline_strokes(points, width: float, closed: bool = False) -> tuple[Polygon, ...]:
    """One stroke per segment of a sampled line (each its own item: they overlap at joints)."""
    points = list(points) + ([points[0]] if closed and points else [])
    return tuple(stroke(a, b, width) for a, b in zip(points, points[1:]))
