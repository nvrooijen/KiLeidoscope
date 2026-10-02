"""Cut plane: the board cut open along a vertical plane, showing its cross section.

Two parts, no booleans:
- Everything on the removed side of the plane turns transparent: one shared shader
  group ("KLS_Clip_v1") sits last in every shown material (after X-ray mode, focus.py).
- The cross section is drawn on the plane as one flat mesh ("KLS cut face"): laminate
  bands from the stackup, copper at each layer's stackup thickness, via walls and plugs
  (section.py works it out from the board's own edges and points). It covers what lies
  behind it, so the cut reads as a solid face, like a micrograph.

The plane is an ordinary mesh object ("KLS cut plane"; move it, or turn it about Z) and
its arrow points to the removed side. A section needs the plane upright; tilted, only the
removed side's transparency applies. The board's geometry is read once per change and
kept, so moving the plane only redoes the crossing.
"""

import math

import bpy
import numpy as np
from mathutils import Vector

from . import focus, laminate, metal, nodes, section, shading, transform
from .placement import CAP_PLATING_M
from .objects import camera_rays_only, hide, node_modifier, outline_bounds, read_attribute, read_coordinates, read_edges
from .state import board

GROUP = "KLS_Clip_v1"
NODE = focus.CUT_NODE
PLANE = "KLS cut plane"
FACE = focus.CUT_FACE  # the section's object and material
FACE_COLOR = "kls_color"
FACE_ALONG = "kls_along"  # metres along the cut: the laminate weave's horizontal coordinate
FACE_WEAVE = "kls_laminate"  # 1 on laminate, 0 on copper and plugs
FACE_METAL = "kls_metal"  # 1 on copper: polished metal
FACE_MATERIAL_VERSION = 8
RESIN_OPACITY = 0.55  # a resin plug is milky: the barrel behind it shows through, faded
# Plane rotations (Euler, rad) whose arrow points at the removed side: Y removes the
# front half (seen in the front view), X the right half (seen from the right).
ORIENTATIONS = {"X": (0.0, math.pi / 2, 0.0), "Y": (math.pi / 2, 0.0, 0.0)}
UPRIGHT = 1e-4  # |normal z| below this: the plane is vertical and gets a section
FACE_OFFSET_M = 1e-6  # the face sits this far on the removed side, clear of the board's own faces
FALLBACK_EXTENT_M = 0.1
SHEET_MARGIN = 1.2  # the drawn plane over the board's size
REBUILD_DELAY_S = 0.05  # live edits arriving together rebuild the face once

_pushed = None  # (on, origin, normal) the clip group holds now
_data = None  # the board's geometry for the section (`_gather`), until it changes
_edges = None  # the plated board edge's stretches (`plated_edges`), until the board changes
# How the section's copper is lit (`set_look`): Realistic, its linear colour, metallic, roughness.
_look = (False, (0.6, 0.3, 0.15), 1.0, 0.25)


def enabled(scene=None):
    return bool(getattr(scene or bpy.context.scene, "kileido_cut", False))


# --- Removed side: transparent ------------------------------------------------------------

def _group():
    group = bpy.data.node_groups.get(GROUP)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(GROUP, "ShaderNodeTree")
    group.interface.new_socket(name="Shader", in_out="INPUT", socket_type="NodeSocketShader")
    group.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
    tree_nodes, links = group.nodes, group.links
    source, sink = tree_nodes.new("NodeGroupInput"), tree_nodes.new("NodeGroupOutput")
    origin = tree_nodes.new("ShaderNodeCombineXYZ")
    origin.name = "Origin"
    normal = tree_nodes.new("ShaderNodeCombineXYZ")
    normal.name = "Normal"
    normal.inputs["Z"].default_value = 1.0
    switch = tree_nodes.new("ShaderNodeValue")
    switch.name = "Enabled"
    switch.outputs[0].default_value = 0.0
    offset = tree_nodes.new("ShaderNodeVectorMath")
    offset.operation = "SUBTRACT"
    links.new(tree_nodes.new("ShaderNodeNewGeometry").outputs["Position"], offset.inputs[0])
    links.new(origin.outputs["Vector"], offset.inputs[1])
    height = tree_nodes.new("ShaderNodeVectorMath")
    height.operation = "DOT_PRODUCT"
    links.new(offset.outputs["Vector"], height.inputs[0])
    links.new(normal.outputs["Vector"], height.inputs[1])
    beyond = tree_nodes.new("ShaderNodeMath")
    beyond.operation = "GREATER_THAN"
    links.new(height.outputs["Value"], beyond.inputs[0])
    beyond.inputs[1].default_value = 0.0
    gone = tree_nodes.new("ShaderNodeMath")
    gone.operation = "MULTIPLY"
    links.new(beyond.outputs[0], gone.inputs[0])
    links.new(switch.outputs[0], gone.inputs[1])
    removed = tree_nodes.new("ShaderNodeMixShader")
    links.new(gone.outputs[0], removed.inputs[0])
    links.new(source.outputs["Shader"], removed.inputs[1])
    links.new(tree_nodes.new("ShaderNodeBsdfTransparent").outputs[0], removed.inputs[2])
    links.new(removed.outputs[0], sink.inputs["Shader"])
    return group


def materials():
    """Every material the cut applies to: all the board's (highlights too) and model parts,
    not the section itself (it sits just on the removed side)."""
    found = {material for material in focus.shown_materials() if material.name != FACE}
    found |= {material for key, material in board.materials.items() if key.startswith("highlight")}
    return found


def _usable(material):
    return material is not None and material.use_nodes and material.node_tree is not None


def add_to(material):
    """Insert the clip stage before the output (after X-ray mode's)."""
    if not _usable(material) or material.node_tree.nodes.get(NODE) is not None:
        return
    tree_nodes, links = material.node_tree.nodes, material.node_tree.links
    output = next((node for node in tree_nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output), None)
    if output is None:
        return
    source = output.inputs["Surface"].links[0].from_socket if output.inputs["Surface"].is_linked else None
    stage = tree_nodes.new("ShaderNodeGroup")
    stage.name = NODE
    stage.node_tree = _group()
    if source is not None:
        links.new(source, stage.inputs[0])
    links.new(stage.outputs[0], output.inputs["Surface"])


def remove_from(material):
    if not _usable(material):
        return
    tree_nodes, links = material.node_tree.nodes, material.node_tree.links
    stage = tree_nodes.get(NODE)
    if stage is None:
        return
    source = stage.inputs[0].links[0].from_socket if stage.inputs[0].is_linked else None
    for link in tuple(stage.outputs[0].links):
        if source is not None:
            links.new(source, link.to_socket)
    tree_nodes.remove(stage)


def add_materials():
    """New materials appeared (overlays, models): clip them too while the cut is on."""
    if enabled():
        for material in materials():
            add_to(material)


# --- The plane ------------------------------------------------------------------------------

def _extents():
    """(width, depth, thickness) of the board in Blender metres."""
    bounds = outline_bounds()
    thickness = board.thickness_m or 0.0016
    if bounds is None:
        return FALLBACK_EXTENT_M, FALLBACK_EXTENT_M, thickness
    xmin, ymin, xmax, ymax = bounds
    return xmax - xmin, ymax - ymin, thickness


def _plane_mesh():
    mesh = bpy.data.meshes.get(PLANE)
    if mesh is None:
        mesh = bpy.data.meshes.new(PLANE)
        # A unit sheet, and an arrow along +Z (the removed side) from its middle.
        mesh.from_pydata([(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0), (0, 0, 0), (0, 0, 1)],
                         [(4, 5)], [(0, 1, 2, 3)])
    return mesh


def ensure_plane():
    plane = bpy.data.objects.get(PLANE)
    if plane is None:
        plane = bpy.data.objects.new(PLANE, _plane_mesh())
        bpy.context.scene.collection.objects.link(plane)
        place("Y", centered=True)
    elif board.board_name and plane.get("kls_board") != board.board_name:
        place("Y", centered=True)  # another board: back to its middle
    # A guide, only ever its wireframe: hide_render keeps it out of final renders, but a
    # rendered viewport (Cycles) still drew its sheet in grey unless no ray sees it.
    plane.display_type = "WIRE"
    plane.hide_render = True
    for ray in ("camera", "diffuse", "glossy", "transmission", "volume_scatter", "shadow"):
        setattr(plane, f"visible_{ray}", False)
    return plane


def place(axis, centered=False):
    """Turn the plane across the board (`axis`: the one its arrow follows) and size it to
    the board; it keeps its position unless `centered` (or new): then the middle."""
    plane = bpy.data.objects.get(PLANE) or ensure_plane()
    width, depth, thickness = _extents()
    sheet_height = max(thickness * 10, 0.2 * max(width, depth))
    plane.rotation_euler = ORIENTATIONS[axis]
    # Turned about X, the sheet's local y stands up; turned about Y, its local x does.
    size = (sheet_height, depth) if axis == "X" else (width, sheet_height)
    plane.scale = (size[0] * SHEET_MARGIN, size[1] * SHEET_MARGIN, 0.1 * max(width, depth))
    if centered:
        plane.location = (0.0, 0.0, thickness / 2)
    plane["kls_board"] = board.board_name
    bpy.context.view_layer.update()
    push()


def _state(scene):
    """(on, origin, unit normal towards the removed side)."""
    plane = bpy.data.objects.get(PLANE)
    if plane is None or not enabled(scene):
        return (False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    matrix = plane.matrix_world
    normal = matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))
    if normal.length < 1e-12:
        normal = Vector((0.0, 0.0, 1.0))
    normal.normalize()
    if getattr(scene, "kileido_cut_flip", False):
        normal.negate()
    return (True, tuple(matrix.translation), tuple(normal))


def upright(scene=None):
    on, _, normal = _state(scene or bpy.context.scene)
    return on and abs(normal[2]) < UPRIGHT


def push(scene=None):
    """Hand the clip group the plane's origin and normal and redo the section, when they changed."""
    global _pushed
    scene = scene or bpy.context.scene
    state = _state(scene)
    if state == _pushed:
        return
    _pushed = state
    group = bpy.data.node_groups.get(GROUP)
    if group is not None:
        on, origin, normal = state
        group.nodes["Enabled"].outputs[0].default_value = 1.0 if on else 0.0
        for name, vector in (("Origin", origin), ("Normal", normal)):
            for axis, value in zip("XYZ", vector):
                group.nodes[name].inputs[axis].default_value = value
    rebuild(scene)


# --- The section face -----------------------------------------------------------------------

def _world_xy(obj):
    coordinates = read_coordinates(obj.data).astype(np.float64)
    matrix = np.array(obj.matrix_world)
    return (coordinates @ matrix[:3, :3].T + matrix[:3, 3])[:, :2]


def _shown(obj):
    try:
        return not obj.hide_get()
    except RuntimeError:  # outside the view layer
        return False


def _modifier_input(obj, name):
    modifier = node_modifier(obj)
    item = next((item for item in modifier.node_group.interface.items_tree
                 if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name), None)
    return nodes.modifier_value(modifier, item.identifier) if item is not None else None


def _layers_at(heights):
    """The copper layer at each height (vias store heights, not their layers' names)."""
    names = list(board.heights)
    known = np.array([board.heights[name] for name in names])
    return [names[index] for index in np.abs(known[None, :] - np.asarray(heights)[:, None]).argmin(axis=1)]


def _gather():
    """The board's geometry the section crosses, in world XY (metres)."""
    copper = {}  # layer -> {"rings": [(a, b, item)], "tracks": [(a, b, radius)]}
    next_item = 0
    outline = None
    vias = None
    drills = []
    for obj in tuple(board.collection.all_objects):
        if obj.type != "MESH" or not len(obj.data.vertices) or not _shown(obj):
            continue
        placed = obj.get("kls_copper")
        if placed:
            layer, kind = placed[0], placed[1]
            xy, edges = _world_xy(obj), read_edges(obj.data)
            if not len(edges):
                continue
            shapes = copper.setdefault(layer, {"rings": [], "tracks": []})
            if kind == "tracks":
                width = read_attribute(obj.data, "width", np.float32).astype(np.float64)
                shapes["tracks"].append((xy[edges[:, 0]], xy[edges[:, 1]], width[edges[:, 0]] / 2))
            else:  # pads, graphics, zones: closed rings, even-odd per item
                item = (read_attribute(obj.data, "item", np.int32) if "item" in obj.data.attributes
                        else np.zeros(len(xy), np.int32))[edges[:, 0]].astype(np.int64) + next_item
                next_item = int(item.max()) + 1
                shapes["rings"].append((xy[edges[:, 0]], xy[edges[:, 1]], item))
        elif obj.name == "KLS outline":
            xy, edges = _world_xy(obj), read_edges(obj.data)
            outline = (xy[edges[:, 0]], xy[edges[:, 1]], np.zeros(len(edges), np.int64))
        elif obj.name == "KLS vias":
            mesh = obj.data
            land = transform.copper_z("F.Cu", "drills", {"F.Cu": 0.0})  # a via's z sits this far outside its copper
            vias = {"xy": _world_xy(obj),
                    "diameter": read_attribute(mesh, "diameter", np.float32).astype(np.float64),
                    "drill": read_attribute(mesh, "drill", np.float32).astype(np.float64),
                    "top": _layers_at(read_attribute(mesh, "z_top", np.float32) - land),
                    "bottom": _layers_at(read_attribute(mesh, "z_bottom", np.float32) + land)}
        elif obj.get("kls_drill"):
            width, height = _modifier_input(obj, "Width"), _modifier_input(obj, "Height")
            plated = _modifier_input(obj, "Material") == board.materials.get("plating")
            x, y = obj.matrix_world.translation[:2]
            drills.append((x, y, width, height, obj.rotation_euler.z, 1.0 if plated else 0.0))
    return {"copper": copper, "outline": outline, "vias": vias, "drills": np.array(drills).reshape(-1, 6)}


def _attribute(tree, name):
    node = tree.nodes.new("ShaderNodeAttribute")
    node.attribute_type = "GEOMETRY"
    node.attribute_name = name
    return node


def _face_material():
    """Flat, as a micrograph reads: the section's colours, with the glass weave in laminate
    and polished metal in copper. In Realistic mode its copper is lit metal, as all copper is."""
    material = bpy.data.materials.get(FACE) or bpy.data.materials.new(FACE)
    if material.get("kls_version") == FACE_MATERIAL_VERSION:
        return material
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    weave = tree.nodes.new("ShaderNodeGroup")
    weave.node_tree = laminate.group()
    tree.links.new(_attribute(tree, FACE_COLOR).outputs["Color"], weave.inputs["Base"])
    tree.links.new(_attribute(tree, FACE_ALONG).outputs["Fac"], weave.inputs["Along"])
    height = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"], height.inputs[0])
    tree.links.new(height.outputs["Z"], weave.inputs["Height"])
    tree.links.new(_attribute(tree, FACE_WEAVE).outputs["Fac"], weave.inputs["Weave"])
    polish = tree.nodes.new("ShaderNodeGroup")
    polish.node_tree = metal.group()
    tree.links.new(weave.outputs["Color"], polish.inputs["Base"])
    tree.links.new(_attribute(tree, FACE_ALONG).outputs["Fac"], polish.inputs["Along"])
    tree.links.new(height.outputs["Z"], polish.inputs["Height"])
    tree.links.new(_attribute(tree, FACE_METAL).outputs["Fac"], polish.inputs["Metal"])
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(polish.outputs["Color"], emission.inputs["Color"])
    # Copper lit as metal (bare: a cut never carries the finish), with the same polish.
    lit_color = tree.nodes.new("ShaderNodeRGB")
    lit_color.name = "KLS cut copper colour"
    lit_polish = tree.nodes.new("ShaderNodeGroup")
    lit_polish.node_tree = metal.group()
    tree.links.new(lit_color.outputs[0], lit_polish.inputs["Base"])
    tree.links.new(_attribute(tree, FACE_ALONG).outputs["Fac"], lit_polish.inputs["Along"])
    tree.links.new(height.outputs["Z"], lit_polish.inputs["Height"])
    lit_polish.inputs["Metal"].default_value = 1.0
    lit = tree.nodes.new("ShaderNodeBsdfPrincipled")
    lit.name = "KLS cut copper"
    tree.links.new(lit_polish.outputs["Color"], lit.inputs["Base Color"])
    realistic = tree.nodes.new("ShaderNodeValue")
    realistic.name = "KLS realistic"
    use_lit = tree.nodes.new("ShaderNodeMath")
    use_lit.operation = "MULTIPLY"
    tree.links.new(_attribute(tree, FACE_METAL).outputs["Fac"], use_lit.inputs[0])
    tree.links.new(realistic.outputs[0], use_lit.inputs[1])
    surface = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(use_lit.outputs[0], surface.inputs[0])
    tree.links.new(emission.outputs[0], surface.inputs[1])
    tree.links.new(lit.outputs[0], surface.inputs[2])
    # Resin (neither laminate nor copper) is milky: partly see-through to the barrel behind it.
    resin = tree.nodes.new("ShaderNodeMath")
    resin.operation = "MULTIPLY"
    for index, flag in enumerate((FACE_WEAVE, FACE_METAL)):
        not_flag = tree.nodes.new("ShaderNodeMath")
        not_flag.operation = "SUBTRACT"
        not_flag.inputs[0].default_value = 1.0
        tree.links.new(_attribute(tree, flag).outputs["Fac"], not_flag.inputs[1])
        tree.links.new(not_flag.outputs[0], resin.inputs[index])
    milky_colour = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(_attribute(tree, FACE_COLOR).outputs["Color"], milky_colour.inputs["Color"])
    milky = tree.nodes.new("ShaderNodeMixShader")
    milky.inputs[0].default_value = RESIN_OPACITY
    tree.links.new(tree.nodes.new("ShaderNodeBsdfTransparent").outputs[0], milky.inputs[1])
    tree.links.new(milky_colour.outputs[0], milky.inputs[2])
    plugged = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(resin.outputs[0], plugged.inputs[0])
    tree.links.new(surface.outputs[0], plugged.inputs[1])
    tree.links.new(milky.outputs[0], plugged.inputs[2])
    tree.links.new(plugged.outputs[0], tree.nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])
    focus.add_to(material)  # X-ray mode fades the section with the board
    material["kls_version"] = FACE_MATERIAL_VERSION
    _apply_look(material)
    return material


def _apply_look(material):
    realistic, color, metallic, roughness = _look
    nodes = material.node_tree.nodes
    nodes["KLS realistic"].outputs[0].default_value = 1.0 if realistic else 0.0
    nodes["KLS cut copper colour"].outputs[0].default_value = (*color, 1.0)
    nodes["KLS cut copper"].inputs["Metallic"].default_value = metallic
    nodes["KLS cut copper"].inputs["Roughness"].default_value = roughness


def set_look(realistic, color, metallic, roughness):
    """The colour mode changed (materials.set_color_mode): the section's copper is lit metal
    in Realistic mode (`color` linear), flat otherwise, like the board's own copper."""
    global _look
    _look = (bool(realistic), tuple(color), float(metallic), float(roughness))
    material = bpy.data.materials.get(FACE)
    if material is not None and material.get("kls_version") == FACE_MATERIAL_VERSION:
        _apply_look(material)


def _face_object(create):
    face = bpy.data.objects.get(FACE)
    if face is None and create:
        face = bpy.data.objects.new(FACE, bpy.data.meshes.new(FACE))
        face.data.materials.append(None)
        face.hide_select = True
        camera_rays_only(face)  # a drawing of the cut, not something that casts light or shadow
        bpy.context.scene.collection.objects.link(face)
    return face


def shapes():
    """The board's geometry as the section reads it (read once per board change)."""
    global _data
    if _data is None:
        _data = _gather()
    return _data


def plated_edges():
    """Stretches of a plated board edge (KiCad's Board Setup: Plated board edge), where
    copper on both outer layers reaches the edge; empty when the board has none."""
    global _edges
    if not board.appearance.get("edge_plating") or board.collection is None or board.in_snapshot:
        return []
    if _edges is None:
        data = shapes()
        copper = data["copper"]
        _edges = (section.plated_edges(data["outline"], copper.get("F.Cu", {}), copper.get("B.Cu", {}))
                  if data["outline"] is not None else [])
    return _edges


def rectangles(scene=None):
    """The section's (s0, s1, z0, z1, sRGB) rectangles and the plane's (origin, normal),
    or None when there is no section to draw."""
    scene = scene or bpy.context.scene
    on, origin, normal = _state(scene)
    if not on or abs(normal[2]) >= UPRIGHT or board.collection is None or board.in_snapshot or not board.heights:
        return None
    data = shapes()
    line = section.Line(origin[:2], normal[:2])
    layers = {layer: section.copper_along(line, found) for layer, found in data["copper"].items()}
    copper, bands = section.stack_layout(board.heights, board.layer_thickness, board.stackup,
                                         board.appearance.get("dielectrics"))
    plug = getattr(scene, "kileido_via_plug", "NONE")
    rects = section.cross_section(line, data["outline"], layers, copper, bands, vias=data["vias"],
                                  pad_drills=data["drills"], plated_edges=plated_edges(),
                                  plating=float(getattr(scene, "kileido_via_plating_um", 25.0)) * 1e-6,
                                  plug=None if plug == "NONE" else plug,
                                  capped=bool(getattr(scene, "kileido_via_fill", False)), cap_plating=CAP_PLATING_M)
    return rects, (line, normal)


def rebuild(scene=None):
    """Draw the section on the plane, or clear it."""
    face = _face_object(create=enabled(scene))
    if face is None:
        return
    result = rectangles(scene)
    mesh = face.data
    mesh.clear_geometry()
    if result is None:
        hide(face, True)
        face.hide_render = True
        return
    rects, (line, normal) = result
    face.data.materials[0] = _face_material()  # (re)built when its version changed
    count = len(rects)
    s = np.array([(r[0], r[1], r[1], r[0]) for r in rects], np.float64).reshape(-1)
    z = np.array([(r[2], r[2], r[3], r[3]) for r in rects], np.float64).reshape(-1)
    along = np.array((*line.along, 0.0))
    base = np.array((*line.origin, 0.0)) + np.asarray(normal) * FACE_OFFSET_M
    points = base + s[:, None] * along + np.column_stack((np.zeros_like(z), np.zeros_like(z), z))
    # (s0, z0) -> (s1, z0) -> (s1, z1) -> (s0, z1): along x up is the plane normal, towards the viewer.
    mesh.from_pydata(points.tolist(), [], np.arange(count * 4).reshape(-1, 4).tolist())
    colors = np.ones((count * 4, 4), np.float32)
    if count:
        colors[:, :3] = np.repeat([shading.srgb_to_linear(rect[4]) for rect in rects], 4, axis=0)
    attribute = mesh.color_attributes.get(FACE_COLOR) or mesh.color_attributes.new(FACE_COLOR, "FLOAT_COLOR", "POINT")
    attribute.data.foreach_set("color", colors.ravel())
    copper = np.repeat([rect[4] == section.COPPER for rect in rects], 4).astype(np.float32)
    weave = np.repeat([rect[4] not in (section.COPPER, section.RESIN) for rect in rects], 4).astype(np.float32)
    for name, values in ((FACE_ALONG, s.astype(np.float32)), (FACE_WEAVE, weave), (FACE_METAL, copper)):
        found = mesh.attributes.get(name) or mesh.attributes.new(name, "FLOAT", "POINT")
        found.data.foreach_set("value", values)
    mesh.update()
    hide(face, False)
    face.hide_render = False


def invalidate():
    """The board changed: read it again for the next section (soon, once per burst of edits)."""
    global _data, _edges
    _data = _edges = None
    if enabled() and not bpy.app.timers.is_registered(_rebuild_soon):
        bpy.app.timers.register(_rebuild_soon, first_interval=REBUILD_DELAY_S)


def _rebuild_soon():
    rebuild()
    return None


# --- Switching ------------------------------------------------------------------------------

def refresh():
    """The tick box, or a whole new board: bring materials, plane and face in line."""
    global _pushed, _data, _edges
    on = enabled()
    plane = ensure_plane() if on else bpy.data.objects.get(PLANE)
    if plane is not None:
        hide(plane, not on)
    for material in materials():
        (add_to if on else remove_from)(material)
    _pushed, _data, _edges = None, None, None
    push()


def _on_depsgraph(scene, _depsgraph):
    if enabled(scene):
        push(scene)


def install():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def uninstall():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    if bpy.app.timers.is_registered(_rebuild_soon):
        bpy.app.timers.unregister(_rebuild_soon)
