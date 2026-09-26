"""The Blender boundary: integer KiCad nanometres become Blender metres here."""

import numpy as np


def xy_m(points_nm, origin_nm) -> np.ndarray:
    points = np.asarray(points_nm, dtype=np.float64).reshape(-1, 2)
    origin = np.asarray(origin_nm, dtype=np.float64)
    out = (points - origin) * 1e-9
    out[:, 1] *= -1
    return out.astype(np.float32)


def copper_z(layer: str, kind: str, heights: dict[str, float]) -> float:
    base = heights.get(layer, 0.0)
    offset = {"zones": 0.0, "tracks": 1e-6, "pads": 2e-6, "drills": 3e-6}.get(kind, 0.0)
    return base - offset if layer == "B.Cu" else base + offset
