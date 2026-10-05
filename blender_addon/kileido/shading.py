"""Shader-node building blocks shared by KiLeidoscope's materials."""

import bpy


def srgb_to_linear(rgb):
    """KiCad stores sRGB bytes; Blender shader inputs are scene-linear."""
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb)


FLAT_POSITION = "KLS flat position"


def flat_position(tree):
    """Where a surface point lies on the flat board: its position, or on a folded board's
    copy (fold.py) the position it had before folding, so plots stay put as it folds. One
    per tree, named FLAT_POSITION."""
    nodes, links = tree.nodes, tree.links
    existing = nodes.get(FLAT_POSITION)
    if existing is not None:
        return existing.outputs["Result"]
    position = nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    flat = nodes.new("ShaderNodeAttribute")
    flat.attribute_name = "kls_flat"
    folded = nodes.new("ShaderNodeAttribute")
    folded.attribute_name = "kls_folded"  # 1 on folded copies; a missing attribute reads 0
    choose = nodes.new("ShaderNodeMix")
    choose.name, choose.data_type = FLAT_POSITION, "VECTOR"
    links.new(folded.outputs["Fac"], choose.inputs["Factor"])
    links.new(position, choose.inputs["A"])
    links.new(flat.outputs["Vector"], choose.inputs["B"])
    return choose.outputs["Result"]


FLAT_NORMAL = "KLS flat normal"


def flat_normal(tree):
    """The surface's normal as it faced on the flat board (towards the viewer, as Blender's
    own): on a folded copy (fold.py) the one stored before folding, so what tells top from
    bottom (bare inner copper, wall weave) stays put as it folds. One per tree."""
    nodes, links = tree.nodes, tree.links
    existing = nodes.get(FLAT_NORMAL)
    if existing is not None:
        return existing.outputs["Result"]
    geometry = nodes.new("ShaderNodeNewGeometry")
    stored = nodes.new("ShaderNodeAttribute")
    stored.attribute_name = "kls_flat_normal"
    folded = nodes.new("ShaderNodeAttribute")
    folded.attribute_name = "kls_folded"
    facing = nodes.new("ShaderNodeMath")  # +1 seen from the front, -1 from behind
    facing.operation = "MULTIPLY_ADD"
    links.new(geometry.outputs["Backfacing"], facing.inputs[0])
    facing.inputs[1].default_value, facing.inputs[2].default_value = -2.0, 1.0
    seen = nodes.new("ShaderNodeVectorMath")
    seen.operation = "SCALE"
    links.new(stored.outputs["Vector"], seen.inputs[0])
    links.new(facing.outputs[0], seen.inputs["Scale"])
    choose = nodes.new("ShaderNodeMix")
    choose.name, choose.data_type = FLAT_NORMAL, "VECTOR"
    links.new(folded.outputs["Fac"], choose.inputs["Factor"])
    links.new(geometry.outputs["Normal"], choose.inputs["A"])
    links.new(seen.outputs["Vector"], choose.inputs["B"])
    return choose.outputs["Result"]


def blend_srgb(top, under):
    """KiCad draws its translucent mask over what lies beneath (sRGB channels)."""
    alpha = top[3] if len(top) > 3 else 1.0
    return tuple(t * alpha + u * (1 - alpha) for t, u in zip(top[:3], under[:3]))


def project_plot(tree, coordinates, texture, offset_name, scale_name):
    """Feed `texture` the XY of `coordinates` mapped onto a rectangle: (xy - offset) / size.

    The rectangle is set (and updated) with `set_plot_rectangle`.
    """
    nodes, links = tree.nodes, tree.links
    offset = nodes.new("ShaderNodeVectorMath")
    offset.name, offset.operation = offset_name, "SUBTRACT"
    scale = nodes.new("ShaderNodeVectorMath")
    scale.name, scale.operation = scale_name, "DIVIDE"
    links.new(coordinates, offset.inputs[0])
    links.new(offset.outputs[0], scale.inputs[0])
    links.new(scale.outputs[0], texture.inputs["Vector"])


def set_plot_rectangle(tree, offset_name, scale_name, xmin, ymin, width, height):
    tree.nodes[offset_name].inputs[1].default_value = (xmin, ymin, 0)
    tree.nodes[scale_name].inputs[1].default_value = (width, height, 1)


def sharp_alpha(material, texture):
    """Crisp plot edges: route an image's alpha through a steep ramp at 50 %.

    The plots are ~22 um per pixel on the reference board; linear filtering blurs
    an edge over a pixel when zoomed in. KiCad's rasterized edge pixels carry
    fractional coverage, so the filtered alpha crosses 0.5 on the true edge and the
    ramp puts a sharp edge there. Re-links every existing use of the alpha.
    """
    nodes, links = material.node_tree.nodes, material.node_tree.links
    name = f"KLS alpha edge {texture.name}"
    edge = nodes.get(name)
    if edge is None:
        edge = nodes.new("ShaderNodeMapRange")
        edge.name = name
        edge.clamp = True
        edge.inputs["From Min"].default_value = 0.4
        edge.inputs["From Max"].default_value = 0.6
        targets = [link.to_socket for link in texture.outputs["Alpha"].links]
        links.new(texture.outputs["Alpha"], edge.inputs["Value"])
        for target in targets:
            links.new(edge.outputs["Result"], target)
    return edge.outputs["Result"]


# --- Building node groups ------------------------------------------------------------------

def math_node(tree, operation, a, b=None, c=None):
    """A Math node in `tree` doing `operation` on sockets or numbers; its output."""
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


def ease(tree, value, inner, outer):
    """1 up to `inner`, easing to 0 at `outer`."""
    node = tree.nodes.new("ShaderNodeMapRange")
    node.interpolation_type = "SMOOTHSTEP"
    tree.links.new(value, node.inputs["Value"])
    node.inputs["From Min"].default_value = inner
    node.inputs["From Max"].default_value = outer
    node.inputs["To Min"].default_value = 1.0
    node.inputs["To Max"].default_value = 0.0
    return node.outputs["Result"]


def centred_fract(tree, value):
    """-0.5..0.5 within each unit cell."""
    return math_node(tree, "SUBTRACT", math_node(tree, "FRACT", value), 0.5)


def typed_socket(sockets, name, kind="RGBA"):
    """A socket by name and type: a Mix node has an A, B and Result per data type."""
    return next(socket for socket in sockets if socket.name == name and socket.type == kind)


# --- Shaded flat colours --------------------------------------------------------------------

LIT_FLAT = "KLS lit flat"  # the node in a material, of the shared group LIT_FLAT_GROUP
LIT_FLAT_GROUP = "KLS_LitFlat_v1"
LIT_AMBIENT = 0.2  # of its colour a surface shows with no light on it (KiCad's 3D viewer has ambient too)


def lit_flat_group():
    """A flat colour lit (the panel's Shaded): plain diffuse, no gloss or metal, so the
    mode's colours stay as they are, plus LIT_AMBIENT of it unlit, so shadows never go
    black. The diffuse part's Gain (`set_lit_gain`) is set so a face the softboxes light
    head-on shows its flat colour."""
    group = bpy.data.node_groups.get(LIT_FLAT_GROUP)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(LIT_FLAT_GROUP, "ShaderNodeTree")
    group.interface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
    group.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
    nodes, links = group.nodes, group.links
    source = nodes.new("NodeGroupInput")
    gain = nodes.new("ShaderNodeValue")
    gain.name = "Gain"
    gain.outputs[0].default_value = 1.0
    scaled = nodes.new("ShaderNodeVectorMath")
    scaled.operation = "SCALE"
    links.new(source.outputs["Color"], scaled.inputs[0])
    links.new(gain.outputs[0], scaled.inputs["Scale"])
    diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    links.new(scaled.outputs["Vector"], diffuse.inputs["Color"])
    ambient = nodes.new("ShaderNodeEmission")
    links.new(source.outputs["Color"], ambient.inputs["Color"])
    ambient.inputs["Strength"].default_value = LIT_AMBIENT
    both = nodes.new("ShaderNodeAddShader")
    links.new(ambient.outputs[0], both.inputs[0])
    links.new(diffuse.outputs[0], both.inputs[1])
    links.new(both.outputs[0], nodes.new("NodeGroupOutput").inputs["Shader"])
    return group


def set_lit_gain(gain):
    lit_flat_group().nodes["Gain"].outputs[0].default_value = gain
