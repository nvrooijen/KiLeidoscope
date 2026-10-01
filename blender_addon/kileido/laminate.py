"""What laminate looks like up close: glass-fibre weave in epoxy, layered as the stackup.

One shared shader group ("KLS_Laminate_v1"), a few math nodes, no textures: the cut
plane's section (cut.py) and, in Realistic mode, the board's edges and bare drill walls
use it. Glass bundles are drawn as staggered rows of flat ellipses at a real cloth's size,
a little lighter than the resin around them; the edges also take the stackup's bands by
height (section.stack_layout), as a routed edge shows its layers.
"""

import bpy

from . import section, shading
from .state import board

GROUP = "KLS_Laminate_v1"
BUNDLE_PITCH_M = 200e-6  # glass yarn spacing along the cloth (1080/2116-style weaves: 150-250 um)
PLY_PITCH_M = 65e-6  # one glass ply per this much laminate height
BUNDLE_SHAPE = (0.42, 0.30)  # bundle half-width and half-height, as fractions of a cell
WEAVE_CONTRAST = 0.12  # bundles this much lighter than the resin between them
RAMP_STOPS = 32  # a colour ramp holds at most this many

_bands_set = None


def _math(tree, operation, a, b=None):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    for index, value in enumerate((a, b)):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = value
        else:
            tree.links.new(value, node.inputs[index])
    return node.outputs[0]


def group():
    """Color = Base (or the stackup band at Height, by Banded) with the weave at
    (Along, Height) mixed in by Weave."""
    found = bpy.data.node_groups.get(GROUP)
    if found is not None:
        return found
    tree = bpy.data.node_groups.new(GROUP, "ShaderNodeTree")
    for name, kind in (("Base", "NodeSocketColor"), ("Along", "NodeSocketFloat"), ("Height", "NodeSocketFloat"),
                       ("Banded", "NodeSocketFloat"), ("Weave", "NodeSocketFloat")):
        tree.interface.new_socket(name=name, in_out="INPUT", socket_type=kind)
    tree.interface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    source, sink = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")

    # The stackup band at this height (board z runs 0 at B.Cu's bottom to the thickness).
    span = tree.nodes.new("ShaderNodeValue")
    span.name = "Thickness"
    span.outputs[0].default_value = 0.0016
    unit = _math(tree, "DIVIDE", source.outputs["Height"], span.outputs[0])
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.name = "Bands"
    ramp.color_ramp.interpolation = "CONSTANT"
    tree.links.new(unit, ramp.inputs["Fac"])
    banded = tree.nodes.new("ShaderNodeMix")  # A, B and Result exist once per data type: use the colour ones
    banded.data_type = "RGBA"
    tree.links.new(source.outputs["Banded"], banded.inputs[0])
    tree.links.new(source.outputs["Base"], _socket(banded.inputs, "A"))
    tree.links.new(ramp.outputs["Color"], _socket(banded.inputs, "B"))

    # The weave: cells of one bundle pitch by one ply, every other row offset half a cell.
    v = _math(tree, "DIVIDE", source.outputs["Height"], PLY_PITCH_M)
    row = _math(tree, "FLOOR", v)
    u = _math(tree, "ADD", _math(tree, "DIVIDE", source.outputs["Along"], BUNDLE_PITCH_M),
              _math(tree, "MULTIPLY", row, 0.5))
    du = _math(tree, "DIVIDE", _math(tree, "SUBTRACT", _math(tree, "FRACT", u), 0.5), BUNDLE_SHAPE[0])
    dv = _math(tree, "DIVIDE", _math(tree, "SUBTRACT", _math(tree, "FRACT", v), 0.5), BUNDLE_SHAPE[1])
    ellipse = _math(tree, "ADD", _math(tree, "MULTIPLY", du, du), _math(tree, "MULTIPLY", dv, dv))
    soft = tree.nodes.new("ShaderNodeMapRange")  # 1 inside a bundle, easing to 0 at its rim
    soft.interpolation_type = "SMOOTHSTEP"
    tree.links.new(ellipse, soft.inputs["Value"])
    soft.inputs["From Min"].default_value = 0.7
    soft.inputs["From Max"].default_value = 1.0
    soft.inputs["To Min"].default_value = 1.0
    soft.inputs["To Max"].default_value = 0.0
    lift = _math(tree, "MULTIPLY_ADD", _math(tree, "MULTIPLY", soft.outputs["Result"], WEAVE_CONTRAST),
                 source.outputs["Weave"])
    lift.node.inputs[2].default_value = 1.0  # 1 + weave * contrast * bundle
    shaded = tree.nodes.new("ShaderNodeMix")
    shaded.data_type = "RGBA"
    shaded.blend_type = "MULTIPLY"
    _socket(shaded.inputs, "Factor", "VALUE").default_value = 1.0
    tree.links.new(_socket(banded.outputs, "Result"), _socket(shaded.inputs, "A"))
    value = tree.nodes.new("ShaderNodeCombineColor")
    for channel in ("Red", "Green", "Blue"):
        tree.links.new(lift, value.inputs[channel])
    tree.links.new(value.outputs[0], _socket(shaded.inputs, "B"))
    tree.links.new(_socket(shaded.outputs, "Result"), sink.inputs["Color"])
    update_bands(tree)
    return tree


def _socket(sockets, name, kind="RGBA"):
    return next(socket for socket in sockets if socket.name == name and socket.type == kind)


def update_bands(tree=None):
    """Fill the group's stackup bands from the live board (edges in Realistic mode)."""
    global _bands_set
    tree = tree or bpy.data.node_groups.get(GROUP)
    if tree is None or not board.heights:
        return
    _, bands = section.stack_layout(board.heights, board.layer_thickness, board.stackup,
                                    board.appearance.get("dielectrics"))
    signature = (tree.as_pointer(), board.thickness_m, tuple(bands))
    if signature == _bands_set:
        return
    _bands_set = signature
    total = board.thickness_m or 0.0016
    tree.nodes["Thickness"].outputs[0].default_value = total
    elements = tree.nodes["Bands"].color_ramp.elements
    while len(elements) > 1:
        elements.remove(elements[-1])
    stops = [(z0, color) for z0, _, color in bands][:RAMP_STOPS] or [(0.0, section.CORE)]
    for index, (z0, color) in enumerate(stops):
        element = elements[0] if index == 0 else elements.new(min(max(z0 / total, 0.0), 0.999))
        element.position = 0.0 if index == 0 else min(max(z0 / total, 0.0), 0.999)
        element.color = (*shading.srgb_to_linear(color), 1.0)


# --- The board's edges (and bare drill walls) -----------------------------------------------

EDGE_NODE = "KLS laminate"


def set_edges(material, realistic):
    """Realistic: the laminate material's base colour is the stackup bands with the weave,
    along each wall (its horizontal tangent) and up. Otherwise its own flat colour."""
    tree = material.node_tree
    principled = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    stage = tree.nodes.get(EDGE_NODE)
    if not realistic or principled is None:
        if stage is not None:
            for link in tuple(stage.outputs[0].links):
                tree.links.remove(link)
        return
    if stage is None:
        stage = tree.nodes.new("ShaderNodeGroup")
        stage.name = EDGE_NODE
        stage.node_tree = group()
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        position = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(geometry.outputs["Position"], position.inputs[0])
        normal = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(geometry.outputs["Normal"], normal.inputs[0])
        # Along a wall: position . (-ny, nx), the wall's horizontal tangent.
        along = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", position.outputs["Y"], normal.outputs["X"]),
                      _math(tree, "MULTIPLY", position.outputs["X"], normal.outputs["Y"]))
        tree.links.new(along, stage.inputs["Along"])
        tree.links.new(position.outputs["Z"], stage.inputs["Height"])
        stage.inputs["Banded"].default_value = 1.0
        stage.inputs["Weave"].default_value = 1.0
    stage.inputs["Base"].default_value = tuple(principled.inputs["Base Color"].default_value)
    update_bands()
    tree.links.new(stage.outputs[0], principled.inputs["Base Color"])
