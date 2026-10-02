"""What laminate looks like up close: woven glass in epoxy, layered as the stackup.

One shared shader group ("KLS_Laminate_v3") of plain math nodes, no textures or noise:
the cut plane's section (cut.py) and, in Realistic mode, the board's edges and bare drill
walls use it. A cross section of glass cloth shows, per ply, a row of yarns cut across
(lens-shaped bundles of filament ends) and a yarn running along the cut that weaves over
one bundle and under the next (a sinusoid of filaments lying along it); every other ply
is staggered, and the cloth is a core's heavy weave or a prepreg's lighter one, at real
size. Glass reads a little lighter than resin, its filaments as fine darker dots or lines,
with a little depth (lit from above: lighter tops, darker lower rims, a soft shadow in the
resin under each bundle and yarn). The edges also take the stackup's bands by height
(section.stack_layout), as a routed edge shows its layers.
"""

import math

import bpy

from . import section, shading
from .state import board

GROUP = "KLS_Laminate_v3"
# Glass cloth (yarn pitch, ply thickness) in mm: a core's heavy 7628 and a prepreg's 2116
# (44 and 60 yarns per inch; about 0.17 and 0.10 mm a ply).
CORE_CLOTH = (0.58, 0.175)
PREPREG_CLOTH = (0.42, 0.10)
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
# Depth, as if lit from above: glass a little lighter at its top than its bottom, darker
# along its lower rim, and a soft shadow in the resin just under it (reaching this far, in plies).
EMBOSS = 0.06
EDGE_SHADOW = 0.15
DROP_SHADOW = 0.12
DROP_REACH = 0.08
LIGHTEST = 1 + GLASS_LIFT + EMBOSS  # the shade's range, for checks
DARKEST = 1 - FIBRE_DARK - EDGE_SHADOW - DROP_SHADOW - EMBOSS
RAMP_STOPS = 32  # a colour ramp holds at most this many
EDGE_NODE = "KLS laminate"

_bands_set = None


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
    tree.links.new(shading.math_node(tree, "DIVIDE", height, span.outputs[0]), ramp.inputs["Fac"])
    banded = tree.nodes.new("ShaderNodeMix")  # A, B and Result exist once per data type: use the colour ones
    banded.data_type = "RGBA"
    tree.links.new(source.outputs["Banded"], banded.inputs[0])
    tree.links.new(source.outputs["Base"], shading.typed_socket(banded.inputs, "A"))
    tree.links.new(ramp.outputs["Color"], shading.typed_socket(banded.inputs, "B"))

    # The cloth at this height: core or prepreg (the stackup's bands, red = pitch, green = ply, in mm).
    cloth_ramp = tree.nodes.new("ShaderNodeValToRGB")
    cloth_ramp.name = "Cloth"
    cloth_ramp.color_ramp.interpolation = "CONSTANT"
    cloth_ramp.color_ramp.elements[0].color = (*PREPREG_CLOTH, 0.0, 1.0)
    tree.links.new(shading.math_node(tree, "DIVIDE", height, span.outputs[0]), cloth_ramp.inputs["Fac"])
    cloth = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(cloth_ramp.outputs["Color"], cloth.inputs[0])
    pitch = shading.math_node(tree, "MULTIPLY", cloth.outputs["Red"], 1e-3)
    ply = shading.math_node(tree, "MULTIPLY", cloth.outputs["Green"], 1e-3)

    # Cells: one ply high, one yarn pitch wide; every other ply shifted half a pitch.
    v = shading.math_node(tree, "DIVIDE", height, ply)
    across_ply = shading.centred_fract(tree, v)
    u = shading.math_node(tree, "ADD", shading.math_node(tree, "DIVIDE", along, pitch), shading.math_node(tree, "MULTIPLY", shading.math_node(tree, "FLOOR", v), 0.5))
    along_pitch = shading.centred_fract(tree, u)  # 0 in a bundle's middle
    widthwise = shading.math_node(tree, "POWER", shading.math_node(tree, "DIVIDE", along_pitch, LENS_HALF_WIDTH), 2.0)

    def lens(raise_by=0.0):
        """A bundle cut across: a lens, |v| / h + (u / w)^2 < 1, pointed at both ends (or the
        one just above, `raise_by` plies up: what shades the resin under it)."""
        across = shading.math_node(tree, "ADD", across_ply, raise_by) if raise_by else across_ply
        return shading.ease(tree, shading.math_node(tree, "ADD", shading.math_node(tree, "DIVIDE", shading.math_node(tree, "ABSOLUTE", across),
                                                   LENS_HALF_HEIGHT), widthwise), 0.8, 1.0)

    bundle = lens()
    # The yarn along the cut, over one bundle and under the next: cos(pi (u - 1/2)) is
    # +1, -1, +1, ... at the bundles' middles.
    phase = shading.math_node(tree, "MULTIPLY", shading.math_node(tree, "SUBTRACT", u, 0.5), math.pi)
    swing = shading.math_node(tree, "MULTIPLY", shading.math_node(tree, "COSINE", phase), YARN_SWING)
    off_yarn = shading.math_node(tree, "SUBTRACT", across_ply, swing)  # 0 on the yarn's middle line

    def strand(raise_by=0.0):
        across = shading.math_node(tree, "ADD", off_yarn, raise_by) if raise_by else off_yarn
        return shading.ease(tree, shading.math_node(tree, "DIVIDE", shading.math_node(tree, "ABSOLUTE", across), YARN_HALF), 0.75, 1.0)

    yarn = strand()
    # Filaments: ends in a bundle (dots, each nudged off a staggered grid by a sine hash of its
    # cell, so they pack irregularly), lengths in a yarn (lines along it).
    w = shading.math_node(tree, "DIVIDE", height, FIBRE_PITCH_M)
    row_w = shading.math_node(tree, "FLOOR", w)
    grid_u = shading.math_node(tree, "ADD", shading.math_node(tree, "DIVIDE", along, FIBRE_PITCH_M), shading.math_node(tree, "MULTIPLY", row_w, 0.5))
    cell_u = shading.math_node(tree, "FLOOR", grid_u)
    nudge_u, nudge_v = (shading.math_node(tree, "MULTIPLY", shading.centred_fract(tree, shading.math_node(tree, "MULTIPLY", shading.math_node(
        tree, "SINE", shading.math_node(tree, "MULTIPLY_ADD", cell_u, a, shading.math_node(tree, "MULTIPLY", row_w, b))), 43758.5453)),
        FIBRE_JITTER) for a, b in ((12.9898, 78.233), (39.3468, 11.135)))
    dot_u = shading.math_node(tree, "SUBTRACT", shading.centred_fract(tree, grid_u), nudge_u)
    dot_v = shading.math_node(tree, "SUBTRACT", shading.centred_fract(tree, w), nudge_v)
    dot = shading.ease(tree, shading.math_node(tree, "ADD", shading.math_node(tree, "MULTIPLY", dot_u, dot_u), shading.math_node(tree, "MULTIPLY", dot_v, dot_v)),
                0.04, 0.10)
    across_line = shading.centred_fract(tree, shading.math_node(tree, "DIVIDE", shading.math_node(tree, "MULTIPLY", off_yarn, ply), LINE_PITCH_M))
    line = shading.ease(tree, shading.math_node(tree, "ABSOLUTE", across_line), 0.10, 0.22)

    # Depth: top-lit relief, a darker lower rim, a soft shadow under each bundle and yarn.
    def relief(mask, position):
        """(emboss, lower rim) of a shape: `position` runs -1 at its bottom to +1 at its top."""
        rim = shading.math_node(tree, "MULTIPLY", shading.math_node(tree, "MULTIPLY", mask, shading.math_node(tree, "SUBTRACT", 1.0, mask)), 4.0)
        return (shading.math_node(tree, "MULTIPLY", mask, position),
                shading.math_node(tree, "MULTIPLY", rim, shading.math_node(tree, "LESS_THAN", position, 0.0)))

    bundle_relief, bundle_rim = relief(bundle, shading.math_node(tree, "DIVIDE", across_ply, LENS_HALF_HEIGHT))
    yarn_relief, yarn_rim = relief(yarn, shading.math_node(tree, "DIVIDE", off_yarn, YARN_HALF))
    glass = shading.math_node(tree, "MINIMUM", shading.math_node(tree, "ADD", bundle, yarn), 1.0)
    shadowed = shading.math_node(tree, "MULTIPLY", shading.math_node(tree, "MINIMUM", shading.math_node(tree, "ADD", lens(DROP_REACH), strand(DROP_REACH)),
                                             1.0), shading.math_node(tree, "SUBTRACT", 1.0, glass))  # only in resin

    # Shade = 1 + Weave * (glass lift + relief - filaments - rims - shadows).
    fibres = shading.math_node(tree, "ADD", shading.math_node(tree, "MULTIPLY", dot, bundle), shading.math_node(tree, "MULTIPLY", line, yarn))
    change = shading.math_node(tree, "SUBTRACT", shading.math_node(tree, "MULTIPLY", glass, GLASS_LIFT),
                   shading.math_node(tree, "MULTIPLY", fibres, FIBRE_DARK))
    change = shading.math_node(tree, "MULTIPLY_ADD", shading.math_node(tree, "ADD", bundle_relief, yarn_relief), EMBOSS, change)
    change = shading.math_node(tree, "SUBTRACT", change, shading.math_node(tree, "MULTIPLY", shading.math_node(tree, "ADD", bundle_rim, yarn_rim),
                                                   EDGE_SHADOW))
    change = shading.math_node(tree, "SUBTRACT", change, shading.math_node(tree, "MULTIPLY", shadowed, DROP_SHADOW))
    shade = shading.math_node(tree, "MULTIPLY_ADD", change, source.outputs["Weave"], 1.0)
    shaded = tree.nodes.new("ShaderNodeMix")
    shaded.data_type = "RGBA"
    shaded.blend_type = "MULTIPLY"
    shading.typed_socket(shaded.inputs, "Factor", "VALUE").default_value = 1.0
    tree.links.new(shading.typed_socket(banded.outputs, "Result"), shading.typed_socket(shaded.inputs, "A"))
    grey = tree.nodes.new("ShaderNodeCombineColor")
    for channel in ("Red", "Green", "Blue"):
        tree.links.new(shade, grey.inputs[channel])
    tree.links.new(grey.outputs[0], shading.typed_socket(shaded.inputs, "B"))
    tree.links.new(shading.typed_socket(shaded.outputs, "Result"), sink.inputs["Color"])
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
    stops = [(z0, color) for z0, _, color in bands][:RAMP_STOPS] or [(0.0, section.CORE)]
    # The bands' colours, and their cloth: prepreg's light glass, else a core's heavy glass.
    for ramp, value in (("Bands", lambda color: (*shading.srgb_to_linear(color), 1.0)),
                        ("Cloth", lambda color: (*(PREPREG_CLOTH if color == section.PREPREG else CORE_CLOTH),
                                                 0.0, 1.0))):
        elements = tree.nodes[ramp].color_ramp.elements
        while len(elements) > 1:
            elements.remove(elements[-1])
        for index, (z0, color) in enumerate(stops):
            element = elements[0] if index == 0 else elements.new(min(max(z0 / total, 0.0), 0.999))
            element.position = 0.0 if index == 0 else min(max(z0 / total, 0.0), 0.999)
            element.color = value(color)


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
        along = shading.math_node(tree, "SUBTRACT", shading.math_node(tree, "MULTIPLY", position.outputs["Y"], normal.outputs["X"]),
                      shading.math_node(tree, "MULTIPLY", position.outputs["X"], normal.outputs["Y"]))
        tree.links.new(along, stage.inputs["Along"])
        tree.links.new(position.outputs["Z"], stage.inputs["Height"])
    if stage.node_tree != group():
        stage.node_tree = group()  # a saved file's older weave: this one (same sockets, links kept)
    stage.inputs["Banded"].default_value = 1.0
    stage.inputs["Weave"].default_value = 1.0
    stage.inputs["Base"].default_value = tuple(principled.inputs["Base Color"].default_value)
    update_bands()
    tree.links.new(stage.outputs[0], principled.inputs["Base Color"])
