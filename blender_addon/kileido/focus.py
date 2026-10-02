"""X-ray mode (panel name; "focus" in code): with a KiCad selection highlighted, or a
malformed board outline drawn red, everything else fades.

One shared shader group ("KLS_Focus_v2") sits between each material's surface and
its output. Its "Amount" value is set once for all materials: 0 passes the
surface through unchanged, 1 replaces it with a faint unlit grey. The highlight
materials never get it, so the selection stays vivid and solid.

While focused, EEVEE blends the faded materials instead of dithering them:
dithered low opacity through a stack of layers stayed grainy at the viewport's
16 samples. Blended lit layers were 2.5x slower per frame than unfocused (1,500
synthetic parts: 1.79 s vs 0.71 s per 16 samples), since every stacked layer was
fully shaded; the unlit grey takes 0.49 s, and replaces the muted lit colours.
Transparency overlap stays on: without it each faded object writes
depth, and a faint part hid the faded board layers behind it as a dark blob.
Unfocused materials go back to dithered, as before.
"""

import bpy

from .state import board

GROUP = "KLS_Focus_v2"
NODE = "KLS focus"
CUT_NODE = "KLS cut"  # the cut plane's stage (cut.py) follows this one, last before the output
CUT_FACE = "KLS cut face"  # the cut plane's section (cut.py): it fades with the board
GREY = (0.5, 0.5, 0.5, 1.0)  # unlit colour of everything that is not highlighted
VISIBLE = 0.05  # opacity of everything that is not highlighted (0.15 looked too hazy)
_active = False


def _group():
    group = bpy.data.node_groups.get(GROUP)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(GROUP, "ShaderNodeTree")
    group.interface.new_socket(name="Shader", in_out="INPUT", socket_type="NodeSocketShader")
    group.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
    nodes, links = group.nodes, group.links
    source, sink = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
    amount = nodes.new("ShaderNodeValue")
    amount.name = "Amount"
    amount.outputs[0].default_value = 0.0
    grey = nodes.new("ShaderNodeEmission")
    grey.inputs["Color"].default_value = GREY
    faint = nodes.new("ShaderNodeMixShader")
    faint.inputs[0].default_value = VISIBLE
    links.new(nodes.new("ShaderNodeBsdfTransparent").outputs[0], faint.inputs[1])
    links.new(grey.outputs[0], faint.inputs[2])
    # EEVEE skips the zero-weight side, so the lit surface costs nothing while faded.
    faded = nodes.new("ShaderNodeMixShader")
    links.new(amount.outputs[0], faded.inputs[0])
    links.new(source.outputs["Shader"], faded.inputs[1])
    links.new(faint.outputs[0], faded.inputs[2])
    links.new(faded.outputs[0], sink.inputs["Shader"])
    return group


def surface_input(material):
    """Where a material's final surface shader goes: the first stage after it (focus,
    then the cut plane) if present, else the output. `materials.set_surface` and
    `holes.add_to` link here."""
    nodes = material.node_tree.nodes
    for name in (NODE, CUT_NODE):
        stage = nodes.get(name)
        if stage is not None:
            return stage.inputs[0]
    output = next(node for node in nodes if node.type == "OUTPUT_MATERIAL")
    return output.inputs["Surface"]


def add_to(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return
    nodes, links = material.node_tree.nodes, material.node_tree.links
    if nodes.get(NODE) is not None:
        return
    output = next((node for node in nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output), None)
    if output is None:
        return
    cut = nodes.get(CUT_NODE)
    into = cut.inputs[0] if cut is not None else output.inputs["Surface"]  # the cut plane stays last
    source = into.links[0].from_socket if into.is_linked else None
    focus = nodes.new("ShaderNodeGroup")
    focus.name = NODE
    focus.node_tree = _group()
    if source is not None:
        links.new(source, focus.inputs[0])
    links.new(focus.outputs[0], into)
    set_render_method(material)


def set_render_method(material):
    """EEVEE: dithered normally (see-through holes, masks), blended while faded."""
    blended = _active and material.node_tree is not None and material.node_tree.nodes.get(NODE) is not None
    method = "BLENDED" if blended else "DITHERED"
    if material.surface_render_method != method:
        material.surface_render_method = method
    if blended and not material.use_transparency_overlap:
        material.use_transparency_overlap = True  # off, faded parts hid the layers behind them


def shown_materials():
    """Every material KiLeidoscope shows except the highlights, including model parts."""
    found = {material for key, material in board.materials.items() if not key.startswith("highlight")}
    found |= {material for material in bpy.data.materials
              if material.name.startswith("KLS overlay") or material.name == CUT_FACE}
    if board.collection is not None:
        for obj in tuple(board.collection.all_objects):
            if obj.get("kls_model_fp_id") is not None:
                found |= {slot.material for slot in obj.material_slots if slot.material is not None}
    return found


def refresh():
    """Apply the tick box: focused only while something is highlighted."""
    global _active
    highlighted = (any(board.highlight.get(kind) for kind in ("selected", "pair")) or
                   bool(board.highlight_components.get("footprints")) or board.outline_problem)
    _active = bool(getattr(bpy.context.scene, "kileido_focus", False) and highlighted)
    for material in shown_materials():
        add_to(material)
        set_render_method(material)
    amount = _group().nodes["Amount"].outputs[0]
    if amount.default_value != float(_active):
        amount.default_value = float(_active)
