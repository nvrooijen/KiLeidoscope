"""What cut copper looks like up close: polished metal, as in a micrograph.

One shared shader group ("KLS_CutCopper_v1") of plain math nodes, no textures or noise,
for the copper of the cut plane's section (cut.py): fine polishing scratches at a slight
angle, a few of them deeper, over a faint grain of the copper's crystals.
"""

import math

import bpy

from . import shading

GROUP = "KLS_CutCopper_v1"
SCRATCH_PITCH_M = 1.5e-6  # polishing lines
SCRATCH_ANGLE = math.radians(8)  # off the cut's horizontal, as a polishing pass leaves them
SCRATCH_SHARE = 0.25  # this share of the lines shows as a scratch
SCRATCH_DARK = 0.08
GRAIN_M = 3e-6  # electrodeposited and rolled copper grains: a few um
GRAIN_SPREAD = 0.08  # grain brightness, from this much darker to this much lighter / 2
LIGHTEST = 1 + GRAIN_SPREAD / 2  # the shade's range, for checks
DARKEST = 1 - GRAIN_SPREAD / 2 - SCRATCH_DARK


def _hash(tree, cell_a, cell_b, a, b):
    """0..1, the same for a cell and unrelated between neighbours (a sine hash)."""
    seed = shading.math_node(tree, "MULTIPLY_ADD", cell_a, a, shading.math_node(tree, "MULTIPLY", cell_b, b))
    return shading.math_node(tree, "FRACT", shading.math_node(tree, "MULTIPLY", shading.math_node(
        tree, "SINE", seed), 43758.5453))


def group():
    """Color = Base shaded by scratches and grain at (Along, Height), by Metal (0: flat)."""
    found = bpy.data.node_groups.get(GROUP)
    if found is not None:
        return found
    tree = bpy.data.node_groups.new(GROUP, "ShaderNodeTree")
    for name, kind in (("Base", "NodeSocketColor"), ("Along", "NodeSocketFloat"), ("Height", "NodeSocketFloat"),
                       ("Metal", "NodeSocketFloat")):
        tree.interface.new_socket(name=name, in_out="INPUT", socket_type=kind)
    tree.interface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    source, sink = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    along, height = source.outputs["Along"], source.outputs["Height"]
    m = shading.math_node

    # Scratches: lines at a slight angle, every one with its own depth; only some show.
    across = m(tree, "DIVIDE", m(tree, "MULTIPLY_ADD", along, math.sin(SCRATCH_ANGLE),
                                  m(tree, "MULTIPLY", height, math.cos(SCRATCH_ANGLE))), SCRATCH_PITCH_M)
    line = shading.ease(tree, m(tree, "ABSOLUTE", shading.centred_fract(tree, across)), 0.15, 0.4)
    depth = _hash(tree, m(tree, "FLOOR", across), 0.0, 12.9898, 0.0)
    shown = shading.ease(tree, depth, 1 - SCRATCH_SHARE, 1 - SCRATCH_SHARE - 0.02)  # 1 for the deepest share
    scratch = m(tree, "MULTIPLY", line, shown)
    # Grain: cells a few um across, each a little lighter or darker.
    row = m(tree, "FLOOR", m(tree, "DIVIDE", height, GRAIN_M))
    cell = m(tree, "FLOOR", m(tree, "ADD", m(tree, "DIVIDE", along, GRAIN_M), m(tree, "MULTIPLY", row, 0.5)))
    grain = m(tree, "SUBTRACT", _hash(tree, cell, row, 39.3468, 11.135), 0.5)

    change = m(tree, "SUBTRACT", m(tree, "MULTIPLY", grain, GRAIN_SPREAD), m(tree, "MULTIPLY", scratch, SCRATCH_DARK))
    shade = m(tree, "MULTIPLY_ADD", change, source.outputs["Metal"], 1.0)
    shaded = tree.nodes.new("ShaderNodeMix")
    shaded.data_type = "RGBA"
    shaded.blend_type = "MULTIPLY"
    shading.typed_socket(shaded.inputs, "Factor", "VALUE").default_value = 1.0
    tree.links.new(source.outputs["Base"], shading.typed_socket(shaded.inputs, "A"))
    grey = tree.nodes.new("ShaderNodeCombineColor")
    for channel in ("Red", "Green", "Blue"):
        tree.links.new(shade, grey.inputs[channel])
    tree.links.new(grey.outputs[0], shading.typed_socket(shaded.inputs, "B"))
    tree.links.new(shading.typed_socket(shaded.outputs, "Result"), sink.inputs["Color"])
    return tree
