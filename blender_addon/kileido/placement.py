"""Heights and thicknesses of displayed layers, from the stackup and the panel settings."""

import bpy

from . import transform
from .state import board

# A display clearance on both board faces, outside the KiCad copper surfaces.
# It changes only the Blender solid, never the measured stackup heights.
BOARD_FACE_CLEARANCE_M = 2e-6
DEFAULT_MASK_M = 10e-6  # KiCad's default solder-mask thickness, if the stackup has none
SOLDER_TOP_SCALE = 0.8  # reflowed-deposit look: top face shrunk toward the pad centre
PLACEHOLDER_HEIGHT_M = 0.0004  # a component without a 3D model: an envelope this tall
CAP_PLATING_M = 20e-6  # a capped via's cap over its land (IPC-4761 type VII: 12 um and up)


def copper_thickness(layer):
    """Displayed copper thickness: the stackup value on the outer layers, 0 (flat) inside.

    Inner layers stay flat sheets: the board hides them and their zones are the
    most expensive geometry. No thickness is invented: a
    stackup without copper thickness shows flat copper.
    """
    if layer not in ("F.Cu", "B.Cu") or not getattr(bpy.context.scene, "kileido_copper_3d", True):
        return 0.0
    return float(board.layer_thickness.get(layer) or 0.0)


def outward(layer):
    """+1 where a layer is seen from above, -1 for the bottom side (face normals)."""
    return -1.0 if layer.startswith("B.") else 1.0


def copper_placement(layer, kind):
    """(object z, signed thickness): the object sits on the laminate side of its
    copper and extrudes outward, so its outer surface stays where it was."""
    surface = transform.copper_z(layer, kind, board.heights)
    thickness = copper_thickness(layer)
    if layer == "B.Cu":
        return surface + thickness, -thickness
    return surface - thickness, thickness


def laminate_faces():
    """z of the dielectric's top and bottom faces (inside the outer copper)."""
    return board.thickness_m - copper_thickness("F.Cu"), copper_thickness("B.Cu")


def mask_thickness(side):
    return float(board.layer_thickness.get(f"{side}.Mask") or DEFAULT_MASK_M)


def stencil_thickness():
    return max(0.0, float(getattr(bpy.context.scene, "kileido_stencil_mm", 0.12))) * 1e-3
