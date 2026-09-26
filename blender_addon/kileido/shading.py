"""Shader-node building blocks shared by KiLeidoscope's materials."""


def srgb_to_linear(rgb):
    """KiCad stores sRGB bytes; Blender shader inputs are scene-linear."""
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb)


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
