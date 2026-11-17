"""X-ray mode (panel name; "focus" in code): with a KiCad selection highlighted, a
finding shown from the DRC or LLM column, or a malformed board outline drawn red, everything
else fades. A finding that wants X-ray (an isolate draw, a reference-plane check) ticks the
box when shown and unticks it when hidden; unticking it leaves X-ray with the finding shown.

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

A library part's materials (parts.py) are STEADY: one stage ("KLS_Steady_v1") does X-ray's fade and
the cut plane's clip for them, stays in them, and they stay dithered. Switching X-ray mode
or the cut plane then changes only values, never their shaders: the parts' big shaders
took 30 to 70 s to compile again on each switch. One stage rather than the two others get
also lets the driver cache them: with two stacked transparent mixes they compiled afresh
(30 to 40 s for a library's 114 materials) in every session. Faded library parts are dithered
rather than blended: grainier while the view moves.

A library part opens up when selected: its insides, contacts and clear cover set
KEEP on their objects, so they stay as they are while its case fades with the rest
(the steady stage fades by Amount x (1 - KEEP)).

Transparent cases (the Parts tab's switch) fade every library part's case at
once, with nothing selected and the rest of the board as it is: SEE_THROUGH on the case
objects, and the steady stage fades by the larger of the two.
"""

import bpy

from .objects import lookup, owned_name
from .state import board

GROUP = "KLS_Focus_v2"
NODE = "KLS focus"
CUT_NODE = "KLS cut"  # the cut plane's stage (cut.py) follows this one, last before the output
CUT_FACE = "KLS cut face"  # the cut plane's section (cut.py): it fades with the board
GREY = (0.5, 0.5, 0.5, 1.0)  # unlit colour of everything that is not highlighted
VISIBLE = 0.05  # opacity of everything that is not highlighted (0.15 looked too hazy)
STEADY = "kls_steady_shader"  # on a material: its shader never changes with a switch (library parts)
STEADY_GROUP = "KLS_Steady_v3"  # v2: SEE_THROUGH; v3: both read from FOCUS
STEADY_NODE = "KLS steady"
KEEP = "kls_focus_keep"  # on an object: 1 keeps it unfaded (a selected library part's insides)
SEE_THROUGH = "kls_see_through"  # on an object: 1 fades it whatever X-ray does (library parts' cases)
FOCUS = "kls_focus"  # on an object: (KEEP, SEE_THROUGH, 0), what the steady stage reads: one
# object attribute, not two, as Eevee reads at most eight per material and a part's own may need most
OPENED = ("insides", "contacts", "cover", "light", "field")  # what of a selected library part stays: all but its case
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


def steady_group():
    """X-ray's fade and the cut plane's clip in one stage, for library parts' materials: the
    surface fades by Amount x (1 - KEEP) towards the faint grey (VISIBLE grey over clear),
    and is clear on the cut's removed side. Its values are set with the other two groups'
    (refresh here, cut.push), and taken from them when it is made."""
    group = bpy.data.node_groups.get(STEADY_GROUP)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(STEADY_GROUP, "ShaderNodeTree")
    group.interface.new_socket(name="Shader", in_out="INPUT", socket_type="NodeSocketShader")
    group.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
    nodes, links = group.nodes, group.links
    source, sink = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")

    def math(operation, a, b):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        for socket, value in zip(node.inputs, (a, b)):
            if isinstance(value, float):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs[0]

    def value(name):
        node = nodes.new("ShaderNodeValue")
        node.name = name
        node.outputs[0].default_value = 0.0
        return node.outputs[0]

    def vector(name, default):
        node = nodes.new("ShaderNodeCombineXYZ")
        node.name = name
        for axis, number in zip("XYZ", default):
            node.inputs[axis].default_value = number
        return node.outputs["Vector"]

    focus = nodes.new("ShaderNodeAttribute")
    focus.attribute_type, focus.attribute_name = "INSTANCER", FOCUS  # the object's own, unless instanced
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(focus.outputs["Vector"], split.inputs[0])
    keep, see = split.outputs["X"], split.outputs["Y"]
    fade = math("MAXIMUM", math("MULTIPLY", value("Amount"), math("SUBTRACT", 1.0, keep)), see)
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "SUBTRACT"
    links.new(nodes.new("ShaderNodeNewGeometry").outputs["Position"], offset.inputs[0])
    links.new(vector("Origin", (0.0, 0.0, 0.0)), offset.inputs[1])
    height = nodes.new("ShaderNodeVectorMath")
    height.operation = "DOT_PRODUCT"
    links.new(offset.outputs["Vector"], height.inputs[0])
    links.new(vector("Normal", (0.0, 0.0, 1.0)), height.inputs[1])
    gone = math("MULTIPLY", math("GREATER_THAN", height.outputs["Value"], 0.0), value("Enabled"))
    grey = nodes.new("ShaderNodeEmission")
    grey.inputs["Color"].default_value = GREY
    lit = nodes.new("ShaderNodeMixShader")  # the surface, or the grey once faded
    links.new(fade, lit.inputs[0])
    links.new(source.outputs["Shader"], lit.inputs[1])
    links.new(grey.outputs[0], lit.inputs[2])
    out = nodes.new("ShaderNodeMixShader")  # then see-through: faded to VISIBLE, gone wholly
    links.new(math("MAXIMUM", math("MULTIPLY", fade, 1.0 - VISIBLE), gone), out.inputs[0])
    links.new(lit.outputs[0], out.inputs[1])
    links.new(nodes.new("ShaderNodeBsdfTransparent").outputs[0], out.inputs[2])
    links.new(out.outputs[0], sink.inputs["Shader"])
    nodes["Amount"].outputs[0].default_value = _group().nodes["Amount"].outputs[0].default_value
    clip = bpy.data.node_groups.get("KLS_Clip_v1")  # cut.GROUP (cut.py imports this module)
    if clip is not None:
        nodes["Enabled"].outputs[0].default_value = clip.nodes["Enabled"].outputs[0].default_value
        for name in ("Origin", "Normal"):
            for axis in "XYZ":
                nodes[name].inputs[axis].default_value = clip.nodes[name].inputs[axis].default_value
    return group


def _add_steady(material):
    nodes, links = material.node_tree.nodes, material.node_tree.links
    stage = nodes.get(STEADY_NODE)
    if stage is not None:
        if stage.node_tree is not steady_group():  # an older stage, from a file saved before
            stage.node_tree = steady_group()
        return
    output = next((node for node in nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output), None)
    if output is None:
        return
    into = output.inputs["Surface"]
    source = into.links[0].from_socket if into.is_linked else None
    stage = nodes.new("ShaderNodeGroup")
    stage.name = STEADY_NODE
    stage.node_tree = steady_group()
    if source is not None:
        links.new(source, stage.inputs[0])
    links.new(stage.outputs[0], into)
    set_render_method(material)


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
    if material.get(STEADY):
        _add_steady(material)
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
    blended = (_active and material.node_tree is not None and material.node_tree.nodes.get(NODE) is not None
               and not material.get(STEADY))
    method = "BLENDED" if blended else "DITHERED"
    if material.surface_render_method != method:
        material.surface_render_method = method
    if blended and not material.use_transparency_overlap:
        material.use_transparency_overlap = True  # off, faded parts hid the layers behind them


def shown_materials():
    """Every material KiLeidoscope shows on this board except the highlights, including model
    parts; its own overlays (another board's carry that board's prefix, and stay whole)."""
    found = {material for key, material in board.materials.items() if not key.startswith("highlight")}
    found |= {material for material in bpy.data.materials
              if material.name.startswith(owned_name("KLS overlay")) or material.name == CUT_FACE}
    if board.collection is not None:
        for obj in tuple(board.collection.all_objects):
            if obj.get("kls_model_fp_id") is not None:
                found |= {slot.material for slot in obj.material_slots if slot.material is not None}
    return found


def refresh():
    """Apply the tick box: focused only while something is highlighted (or a finding
    isolates its items)."""
    global _active
    highlighted = (any(board.highlight.get(kind) for kind in ("selected", "pair", "finding")) or
                   bool(board.highlight_components.get("footprints")) or board.outline_problem)
    _active = bool(getattr(bpy.context.scene, "kileido_focus", False) and (highlighted or board.findings_isolate))
    for material in shown_materials():
        add_to(material)
        set_render_method(material)
    for group in (_group(), bpy.data.node_groups.get(STEADY_GROUP)):
        amount = group.nodes["Amount"].outputs[0] if group is not None else None
        if amount is not None and amount.default_value != float(_active):
            amount.default_value = float(_active)
    _open_selected_parts()


def opened(footprint_id):
    """A selected footprint whose library part X-ray opens: it shows as the one thing not
    faded, so it goes without the selection box (which would tint its insides). A part
    with nothing marked to stay (no roles: a part saved to My parts from KiCad's model)
    fades whole, as KiCad's models do, and keeps its box."""
    if not _active or footprint_id not in board.swapped:
        return False
    for name in board.swapped[footprint_id]["names"]:
        obj = lookup(name)
        if obj is not None and obj.get("kls_role") in OPENED and obj.get("kls_role") != "field":
            return True
    return False


def _open_selected_parts():
    """A selected footprint's library part: everything but its case stays unfaded. With
    Transparent cases on, every library part's case fades."""
    if board.collection is None:
        return
    chosen = board.highlight_components.get("footprints", set())
    see_through = bool(getattr(bpy.context.scene, "kileido_see_through", False))
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_library_part") != 1:
            continue
        keep = float(_active and obj.get("kls_model_fp_id") in chosen and obj.get("kls_role") in OPENED)
        see = float(see_through and obj.get("kls_role") == "case")
        if obj.get(KEEP, 0.0) != keep or obj.get(SEE_THROUGH, 0.0) != see or FOCUS not in obj:
            obj[KEEP], obj[SEE_THROUGH] = keep, see
            obj[FOCUS] = (keep, see, 0.0)
            obj.update_tag()
    if chosen and not board.in_snapshot:
        from . import highlight  # imports this module
        highlight.refresh_components()  # the boxes of opened parts go, and come back
