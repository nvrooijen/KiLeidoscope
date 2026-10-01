"""What laminate looks like up close: woven glass in epoxy, layered as the stackup.

One shared shader group ("KLS_Laminate_v2") of plain math nodes, no textures or noise:
the cut plane's section (cut.py) and, in Realistic mode, the board's edges and bare drill
walls use it. A cross section of glass cloth shows, per ply, a row of yarns cut across
(lens-shaped bundles of filament ends) and a yarn running along the cut that weaves over
one bundle and under the next (a sinusoid of filaments lying along it); every other ply
is staggered. Glass reads a little lighter than resin, its filaments as fine darker dots
or lines. The edges also take the stackup's bands by height (section.stack_layout), as a
routed edge shows its layers.
"""

import math

import bpy

from . import section, shading
from .state import board

GROUP = "KLS_Laminate_v2"
BUNDLE_PITCH_M = 200e-6  # yarn spacing in the cloth (1080/2116-style weaves: 150-250 um)
PLY_PITCH_M = 65e-6  # one glass ply per this much laminate height
# In ply and pitch units: a bundle's half-height and half-width, the crossing yarn's swing
# above and below the bundles' middle, and its half-thickness (yarn and bundles never overlap).
LENS_HALF_HEIGHT = 0.16
LENS_HALF_WIDTH = 0.40
YARN_SWING = 0.30
YARN_HALF = 0.13
FIBRE_PITCH_M = 6e-6  # glass filaments: 5-7 um across
FIBRE_JITTER = 0.35  # filament ends sit up to this far (in pitches) off a regular grid
LINE_PITCH_M = 3e-6  # filament edges along a crossing yarn
GLASS_LIFT = 0.15  # glass this much lighter than the resin
FIBRE_DARK = 0.30  # a filament's end or edge this much darker than the glass around it
RAMP_STOPS = 32  # a colour ramp holds at most this many
EDGE_NODE = "KLS laminate"

_bands_set = None


def _math(tree, operation, a, b=None, c=None):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    for index, value in enumerate((a, b, c)):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = value
        else:
            tree.links.new(value, node.inputs[index])
    return node.outputs[0]


def _ease(tree, value, inner, outer):
    """1 up to `inner`, easing to 0 at `outer`."""
    node = tree.nodes.new("ShaderNodeMapRange")
    node.interpolation_type = "SMOOTHSTEP"
    tree.links.new(value, node.inputs["Value"])
    node.inputs["From Min"].default_value = inner
    node.inputs["From Max"].default_value = outer
    node.inputs["To Min"].default_value = 1.0
    node.inputs["To Max"].default_value = 0.0
    return node.outputs["Result"]


def _centred_fract(tree, value):
    """-0.5..0.5 within each unit cell."""
    return _math(tree, "SUBTRACT", _math(tree, "FRACT", value), 0.5)


def _socket(sockets, name, kind="RGBA"):
    return next(socket for socket in sockets if socket.name == name and socket.type == kind)


def group():
    """Color = Base (or the stackup band at Height, by Banded) shaded by the weave at
    (Along, Height), by Weave (0: flat)."""
    found = bpy.data.node_groups.get(GROUP)
    if found is not None:
        return found
    tree = bpy.data.node_groups.new(GROUP, "ShaderNodeTree")
    for name, kind in (("Base", "NodeSocketColor"), ("Along", "NodeSocketFloat"), ("Height", "NodeSocketFloat"),
                       ("Banded", "NodeSocketFloat"), ("Weave", "NodeSocketFloat")):
        tree.interface.new_socket(name=name, in_out="INPUT", socket_type=kind)
    tree.interface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    source, sink = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    along, height = source.outputs["Along"], source.outputs["Height"]

    # The stackup band at this height (board z runs 0 at the bottom copper's bottom to the thickness).
    span = tree.nodes.new("ShaderNodeValue")
    span.name = "Thickness"
    span.outputs[0].default_value = 0.0016
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.name = "Bands"
    ramp.color_ramp.interpolation = "CONSTANT"
    tree.links.new(_math(tree, "DIVIDE", height, span.outputs[0]), ramp.inputs["Fac"])
    banded = tree.nodes.new("ShaderNodeMix")  # A, B and Result exist once per data type: use the colour ones
    banded.data_type = "RGBA"
    tree.links.new(source.outputs["Banded"], banded.inputs[0])
    tree.links.new(source.outputs["Base"], _socket(banded.inputs, "A"))
    tree.links.new(ramp.outputs["Color"], _socket(banded.inputs, "B"))

    # Cells: one ply high, one yarn pitch wide; every other ply shifted half a pitch.
    v = _math(tree, "DIVIDE", height, PLY_PITCH_M)
    across_ply = _centred_fract(tree, v)
    u = _math(tree, "ADD", _math(tree, "DIVIDE", along, BUNDLE_PITCH_M),
              _math(tree, "MULTIPLY", _math(tree, "FLOOR", v), 0.5))
    along_pitch = _centred_fract(tree, u)  # 0 in a bundle's middle
    # A bundle cut across: a lens, |v| / h + (u / w)^2 < 1, pointed at both ends.
    lens = _math(tree, "ADD", _math(tree, "DIVIDE", _math(tree, "ABSOLUTE", across_ply), LENS_HALF_HEIGHT),
                 _math(tree, "POWER", _math(tree, "DIVIDE", along_pitch, LENS_HALF_WIDTH), 2.0))
    bundle = _ease(tree, lens, 0.8, 1.0)
    # The yarn along the cut, over one bundle and under the next: cos(pi (u - 1/2)) is
    # +1, -1, +1, ... at the bundles' middles.
    phase = _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", u, 0.5), math.pi)
    swing = _math(tree, "MULTIPLY", _math(tree, "COSINE", phase), YARN_SWING)
    off_yarn = _math(tree, "SUBTRACT", across_ply, swing)  # 0 on the yarn's middle line
    yarn = _ease(tree, _math(tree, "DIVIDE", _math(tree, "ABSOLUTE", off_yarn), YARN_HALF), 0.75, 1.0)
    # Filaments: ends in a bundle (dots, each nudged off a staggered grid by a sine hash of its
    # cell, so they pack irregularly), lengths in a yarn (lines along it).
    w = _math(tree, "DIVIDE", height, FIBRE_PITCH_M)
    row_w = _math(tree, "FLOOR", w)
    grid_u = _math(tree, "ADD", _math(tree, "DIVIDE", along, FIBRE_PITCH_M), _math(tree, "MULTIPLY", row_w, 0.5))
    cell_u = _math(tree, "FLOOR", grid_u)
    nudge_u, nudge_v = (_math(tree, "MULTIPLY", _centred_fract(tree, _math(tree, "MULTIPLY", _math(
        tree, "SINE", _math(tree, "MULTIPLY_ADD", cell_u, a, _math(tree, "MULTIPLY", row_w, b))), 43758.5453)),
        FIBRE_JITTER) for a, b in ((12.9898, 78.233), (39.3468, 11.135)))
    dot_u = _math(tree, "SUBTRACT", _centred_fract(tree, grid_u), nudge_u)
    dot_v = _math(tree, "SUBTRACT", _centred_fract(tree, w), nudge_v)
    dot = _ease(tree, _math(tree, "ADD", _math(tree, "MULTIPLY", dot_u, dot_u), _math(tree, "MULTIPLY", dot_v, dot_v)),
                0.04, 0.10)
    across_line = _centred_fract(tree, _math(tree, "MULTIPLY", off_yarn, PLY_PITCH_M / LINE_PITCH_M))
    line = _ease(tree, _math(tree, "ABSOLUTE", across_line), 0.10, 0.22)
    # Shade = 1 + Weave * (glass lift - filament darkening).
    glass = _math(tree, "MINIMUM", _math(tree, "ADD", bundle, yarn), 1.0)
    fibres = _math(tree, "ADD", _math(tree, "MULTIPLY", dot, bundle), _math(tree, "MULTIPLY", line, yarn))
    change = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", glass, GLASS_LIFT),
                   _math(tree, "MULTIPLY", fibres, FIBRE_DARK))
    shade = _math(tree, "MULTIPLY_ADD", change, source.outputs["Weave"], 1.0)
    shaded = tree.nodes.new("ShaderNodeMix")
    shaded.data_type = "RGBA"
    shaded.blend_type = "MULTIPLY"
    _socket(shaded.inputs, "Factor", "VALUE").default_value = 1.0
    tree.links.new(_socket(banded.outputs, "Result"), _socket(shaded.inputs, "A"))
    grey = tree.nodes.new("ShaderNodeCombineColor")
    for channel in ("Red", "Green", "Blue"):
        tree.links.new(shade, grey.inputs[channel])
    tree.links.new(grey.outputs[0], _socket(shaded.inputs, "B"))
    tree.links.new(_socket(shaded.outputs, "Result"), sink.inputs["Color"])
    update_bands(tree)
    return tree


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
    if stage.node_tree != group():
        stage.node_tree = group()  # a saved file's older weave: this one (same sockets, links kept)
    stage.inputs["Banded"].default_value = 1.0
    stage.inputs["Weave"].default_value = 1.0
    stage.inputs["Base"].default_value = tuple(principled.inputs["Base Color"].default_value)
    update_bands()
    tree.links.new(stage.outputs[0], principled.inputs["Base Color"])
