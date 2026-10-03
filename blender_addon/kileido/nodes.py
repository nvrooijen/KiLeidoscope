"""Geometry Nodes groups. Input geometry stays as edges and points."""

import math

import bpy

from .placement import PLACEHOLDER_HEIGHT_M

VIA_VERTICES = 48  # around a via's land, barrel and core: within 0.1 % of the circle the cut draws
PLUG_GAP_M = 1e-6  # a plug's or fill's core, this far inside the barrel's plating
PLUG_INSET_M = 2e-6  # and this far short of each end, inside the lands
TENT_LIFT_M = 0.5e-6  # a via's tent, above its land's surface
TENT_SCALE = 1.02  # of the bore: over the edge of the see-through hole, onto the land


def _group(name, extra=()):
    old = bpy.data.node_groups.get(name)
    if old is not None:
        return old, None, None
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    for socket_name, socket_type, *default in extra:
        item = group.interface.new_socket(name=socket_name, in_out="INPUT", socket_type=socket_type)
        if default:
            item.default_value = default[0]
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    return group, group.nodes.new("NodeGroupInput"), group.nodes.new("NodeGroupOutput")


def _is_thick(nodes, links, thickness):
    """True when a signed thickness is not zero (inner layers stay flat sheets)."""
    absolute = nodes.new("ShaderNodeMath")
    absolute.operation = "ABSOLUTE"
    links.new(thickness, absolute.inputs[0])
    thick = nodes.new("ShaderNodeMath")
    thick.operation = "GREATER_THAN"
    links.new(absolute.outputs[0], thick.inputs[0])
    thick.inputs[1].default_value = 1e-9
    return thick.outputs[0]


def _switch(nodes, links, kind, condition, false, true):
    switch = nodes.new("GeometryNodeSwitch")
    switch.input_type = kind
    links.new(condition, switch.inputs["Switch"])
    for name, value in (("False", false), ("True", true)):
        if isinstance(value, (int, float)):
            switch.inputs[name].default_value = value
        else:
            links.new(value, switch.inputs[name])
    return switch.outputs[0]


def _thicken(nodes, links, mesh, thickness, outward):
    """Face `outward` (+1 up, -1 down), then extrude along Z by a signed thickness
    (unchanged when it is 0).

    The object sits on its inner surface (the laminate side) and extrudes
    outward, so the moved faces become the outer surface. The faces it started from stay
    as its inner side, turned to face into the board: with the board cut open, hidden or
    see-through, outer copper is looked at from inside, and an open shell showed what lies
    beyond it (the mask) instead. Face extrusion is Blender's own; no triangulation here.
    """
    oriented = _face_along(nodes, links, mesh, outward)
    up = nodes.new("ShaderNodeCombineXYZ")
    up.inputs["Z"].default_value = 1.0
    extrude = nodes.new("GeometryNodeExtrudeMesh")
    extrude.mode = "FACES"
    extrude.inputs["Individual"].default_value = False
    links.new(oriented, extrude.inputs["Mesh"])
    links.new(up.outputs["Vector"], extrude.inputs["Offset"])
    links.new(thickness, extrude.inputs["Offset Scale"])
    inner = nodes.new("GeometryNodeFlipFaces")
    links.new(oriented, inner.inputs["Mesh"])
    closed = nodes.new("GeometryNodeJoinGeometry")
    links.new(extrude.outputs["Mesh"], closed.inputs["Geometry"])
    links.new(inner.outputs["Mesh"], closed.inputs["Geometry"])
    solid = _switch(nodes, links, "GEOMETRY", _is_thick(nodes, links, thickness),
                    oriented, closed.outputs["Geometry"])
    # Copper is flat-faced. Curve-to-mesh ribbons come out smooth-shaded, averaging
    # each vertex normal over top, side and flipped faces: black patches in Cycles.
    flat = nodes.new("GeometryNodeSetShadeSmooth")
    flat.inputs["Shade Smooth"].default_value = False
    links.new(solid, flat.inputs["Mesh"])
    return flat.outputs["Mesh"]


def _face_along(nodes, links, mesh, thickness):
    """Point every face's normal along the sign of `thickness` (+Z up, -Z down).

    Curve-to-mesh ribbons face up or down depending on segment direction. Where a
    down-facing ribbon overlapped an up-facing end cap at the same height, Cycles
    shaded the two differently and picked between them per triangle (striped
    track ends on the reference board), flat or extruded.
    """
    normal = nodes.new("GeometryNodeInputNormal")
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(normal.outputs["Normal"], split.inputs[0])
    along = nodes.new("ShaderNodeMath")
    along.operation = "MULTIPLY"
    links.new(split.outputs["Z"], along.inputs[0])
    links.new(thickness, along.inputs[1])
    wrong = nodes.new("ShaderNodeMath")
    wrong.operation = "LESS_THAN"
    links.new(along.outputs[0], wrong.inputs[0])
    wrong.inputs[1].default_value = 0.0
    flip = nodes.new("GeometryNodeFlipFaces")
    links.new(mesh, flip.inputs["Mesh"])
    links.new(wrong.outputs[0], flip.inputs["Selection"])
    return flip.outputs["Mesh"]


def _disk_or_cylinder(nodes, links, thickness, vertices, outward, shape_thickness=None):
    """A unit disk, or a unit cylinder spanning z = 0..1 (scaled by the thickness).

    The returned height scales the disk by `outward` (±1), so a flat disk faces
    away from the board like the faces around it (a negative scale flips normals).
    A geometry switch takes one value, never a per-point field: with a per-point
    `thickness`, `shape_thickness` (one value) picks the disk or the cylinder.
    """
    circle = nodes.new("GeometryNodeMeshCircle")
    circle.fill_type = "NGON"
    circle.inputs["Vertices"].default_value = vertices
    circle.inputs["Radius"].default_value = 0.5
    cylinder = nodes.new("GeometryNodeMeshCylinder")
    cylinder.fill_type = "NGON"
    cylinder.inputs["Vertices"].default_value = vertices
    cylinder.inputs["Radius"].default_value = 0.5
    cylinder.inputs["Depth"].default_value = 1.0
    flat = nodes.new("GeometryNodeSetShadeSmooth")  # smooth normals streak the flat caps
    flat.inputs["Shade Smooth"].default_value = False
    links.new(cylinder.outputs["Mesh"], flat.inputs["Mesh"])
    lift = nodes.new("GeometryNodeTransform")
    lift.inputs["Translation"].default_value = (0, 0, 0.5)
    links.new(flat.outputs["Mesh"], lift.inputs["Geometry"])
    thick = _is_thick(nodes, links, thickness if shape_thickness is None else shape_thickness)
    shape = _switch(nodes, links, "GEOMETRY", thick, circle.outputs["Mesh"], lift.outputs["Geometry"])
    height = _switch(nodes, links, "FLOAT", thick, outward, thickness)
    return shape, height


def _input_slot(modifier, identifier):
    """Blender 5.2+ keeps a modifier's inputs in `properties.inputs`; 5.1 in ID properties."""
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    return getattr(inputs, identifier, None) if inputs is not None else None


def modifier_value(modifier, identifier, default=None):
    slot = _input_slot(modifier, identifier)
    if slot is not None:
        return getattr(slot, "value", default)  # a Geometry input holds no value
    return modifier.get(identifier, default)


def set_modifier_value(modifier, identifier, value):
    slot = _input_slot(modifier, identifier)
    if slot is not None:
        slot.value = value
    else:
        modifier[identifier] = value


def modifier_input(modifier, group, name, value):
    socket = next(item for item in group.interface.items_tree
                  if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name)
    set_modifier_value(modifier, socket.identifier, value)


def tracks():
    group, source, sink = _group("KLS_Tracks_v4", (("Material", "NodeSocketMaterial"),
                                                   ("Thickness", "NodeSocketFloat"),
                                                   ("Up", "NodeSocketFloat", 1.0)))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    mesh_to_curve = nodes.new("GeometryNodeMeshToCurve")
    links.new(source.outputs["Geometry"], mesh_to_curve.inputs["Mesh"])
    normal = nodes.new("GeometryNodeSetCurveNormal")
    normal.inputs["Mode"].default_value = "Z Up"
    links.new(mesh_to_curve.outputs["Curve"], normal.inputs["Curve"])
    width = nodes.new("GeometryNodeInputNamedAttribute")
    width.data_type = "FLOAT"
    width.inputs["Name"].default_value = "width"
    profile = nodes.new("GeometryNodeCurvePrimitiveLine")
    profile.mode = "POINTS"
    profile.inputs["Start"].default_value = (-0.5, 0, 0)
    profile.inputs["End"].default_value = (0.5, 0, 0)
    ribbon = nodes.new("GeometryNodeCurveToMesh")
    links.new(normal.outputs["Curve"], ribbon.inputs["Curve"])
    links.new(profile.outputs["Curve"], ribbon.inputs["Profile Curve"])
    links.new(width.outputs["Attribute"], ribbon.inputs["Scale"])
    body = _thicken(nodes, links, ribbon.outputs["Mesh"], source.outputs["Thickness"], source.outputs["Up"])
    cap, height = _disk_or_cylinder(nodes, links, source.outputs["Thickness"], 20, source.outputs["Up"])
    cap_scale = nodes.new("ShaderNodeCombineXYZ")
    links.new(width.outputs["Attribute"], cap_scale.inputs["X"])
    links.new(width.outputs["Attribute"], cap_scale.inputs["Y"])
    links.new(height, cap_scale.inputs["Z"])
    caps = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(source.outputs["Geometry"], caps.inputs["Points"])
    links.new(cap, caps.inputs["Instance"])
    links.new(cap_scale.outputs["Vector"], caps.inputs["Scale"])
    join = nodes.new("GeometryNodeJoinGeometry")
    links.new(body, join.inputs["Geometry"])
    links.new(caps.outputs["Instances"], join.inputs["Geometry"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(join.outputs["Geometry"], painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def fill(name="KLS_Fill_v4", solder=False):
    """Pads (one ring group per item) as filled faces, thickened along Z.

    `solder`: the same shapes as stencil deposits: extruded, top faces shrunk
    toward each deposit's centre and shaded smooth, like reflowed solder.
    """
    extra = (("Material", "NodeSocketMaterial"), ("Thickness", "NodeSocketFloat"),
             ("Up", "NodeSocketFloat", 1.0))
    if solder:
        extra += (("Top Scale", "NodeSocketFloat"),)
    group, source, sink = _group(name, extra)
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    to_curve = nodes.new("GeometryNodeMeshToCurve")
    links.new(source.outputs["Geometry"], to_curve.inputs["Mesh"])
    cyclic = nodes.new("GeometryNodeSetSplineCyclic")
    cyclic.inputs["Cyclic"].default_value = True
    links.new(to_curve.outputs["Curve"], cyclic.inputs["Curve"])
    poly = nodes.new("GeometryNodeCurveSplineType")
    poly.spline_type = "POLY"
    links.new(cyclic.outputs["Curve"], poly.inputs["Curve"])
    item = nodes.new("GeometryNodeInputNamedAttribute")
    item.data_type = "INT"
    item.inputs["Name"].default_value = "item"
    split = nodes.new("GeometryNodeSplitToInstances")
    split.domain = "CURVE"
    links.new(poly.outputs["Curve"], split.inputs["Geometry"])
    links.new(item.outputs["Attribute"], split.inputs["Group ID"])
    each_input = nodes.new("GeometryNodeForeachGeometryElementInput")
    each_output = nodes.new("GeometryNodeForeachGeometryElementOutput")
    each_input.pair_with_output(each_output)
    each_output.domain = "INSTANCE"
    links.new(split.outputs["Instances"], each_input.inputs["Geometry"])
    realize = nodes.new("GeometryNodeRealizeInstances")
    links.new(each_input.outputs["Element"], realize.inputs["Geometry"])
    filled = nodes.new("GeometryNodeFillCurve")
    filled.inputs["Fill Rule"].default_value = "Even-Odd"
    links.new(realize.outputs["Geometry"], filled.inputs["Curve"])
    links.new(filled.outputs["Mesh"], each_output.inputs["Generation_0"])
    shape = each_output.outputs["Generation_0"]
    if solder:
        up = nodes.new("ShaderNodeCombineXYZ")
        up.inputs["Z"].default_value = 1.0
        extrude = nodes.new("GeometryNodeExtrudeMesh")
        extrude.mode = "FACES"
        extrude.inputs["Individual"].default_value = False
        links.new(_face_along(nodes, links, shape, source.outputs["Thickness"]), extrude.inputs["Mesh"])
        links.new(up.outputs["Vector"], extrude.inputs["Offset"])
        links.new(source.outputs["Thickness"], extrude.inputs["Offset Scale"])
        shrink = nodes.new("GeometryNodeScaleElements")
        shrink.domain = "FACE"
        links.new(extrude.outputs["Mesh"], shrink.inputs["Geometry"])
        links.new(extrude.outputs["Top"], shrink.inputs["Selection"])
        links.new(source.outputs["Top Scale"], shrink.inputs["Scale"])
        smooth = nodes.new("GeometryNodeSetShadeSmooth")
        links.new(shrink.outputs["Geometry"], smooth.inputs["Mesh"])
        shape = smooth.outputs["Mesh"]
    else:
        shape = _thicken(nodes, links, shape, source.outputs["Thickness"], source.outputs["Up"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(shape, painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def fill_single():
    """Fill all rings of one copper item, preserving its own holes."""
    group, source, sink = _group("KLS_FillSingle_v4", (("Material", "NodeSocketMaterial"),
                                                       ("Thickness", "NodeSocketFloat"),
                                                       ("Up", "NodeSocketFloat", 1.0)))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    to_curve = nodes.new("GeometryNodeMeshToCurve")
    links.new(source.outputs["Geometry"], to_curve.inputs["Mesh"])
    cyclic = nodes.new("GeometryNodeSetSplineCyclic")
    cyclic.inputs["Cyclic"].default_value = True
    links.new(to_curve.outputs["Curve"], cyclic.inputs["Curve"])
    filled = nodes.new("GeometryNodeFillCurve")
    filled.inputs["Fill Rule"].default_value = "Even-Odd"
    links.new(cyclic.outputs["Curve"], filled.inputs["Curve"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(_thicken(nodes, links, filled.outputs["Mesh"], source.outputs["Thickness"],
                       source.outputs["Up"]), painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def drills():
    """The wall of a round or oblong drill: a rounded-rectangle outline swept from
    the object's z up by Depth. The hole itself is see-through (holes.py)."""
    group, source, sink = _group("KLS_Drills_v2", (("Material", "NodeSocketMaterial"),
                                                    ("Width", "NodeSocketFloat"),
                                                    ("Height", "NodeSocketFloat"),
                                                    ("Depth", "NodeSocketFloat")))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    rectangle = nodes.new("GeometryNodeCurvePrimitiveQuadrilateral")
    rectangle.mode = "RECTANGLE"
    links.new(source.outputs["Width"], rectangle.inputs["Width"])
    links.new(source.outputs["Height"], rectangle.inputs["Height"])
    minimum = nodes.new("ShaderNodeMath")
    minimum.operation = "MINIMUM"
    links.new(source.outputs["Width"], minimum.inputs[0])
    links.new(source.outputs["Height"], minimum.inputs[1])
    radius = nodes.new("ShaderNodeMath")
    radius.operation = "MULTIPLY"
    links.new(minimum.outputs[0], radius.inputs[0])
    radius.inputs[1].default_value = 0.5
    fillet = nodes.new("GeometryNodeFilletCurve")
    fillet.inputs["Mode"].default_value = "Poly"
    fillet.inputs["Count"].default_value = 24
    links.new(rectangle.outputs["Curve"], fillet.inputs["Curve"])
    links.new(radius.outputs[0], fillet.inputs["Radius"])
    normal = nodes.new("GeometryNodeSetCurveNormal")
    normal.inputs["Mode"].default_value = "Z Up"
    links.new(fillet.outputs["Curve"], normal.inputs["Curve"])
    down = nodes.new("ShaderNodeMath")
    down.operation = "MULTIPLY"
    links.new(source.outputs["Depth"], down.inputs[0])
    down.inputs[1].default_value = -1.0  # measured: for this primitive's winding, profile +Y is world -Z
    rise = nodes.new("ShaderNodeCombineXYZ")
    links.new(down.outputs[0], rise.inputs["Y"])
    profile = nodes.new("GeometryNodeCurvePrimitiveLine")
    profile.mode = "POINTS"
    profile.inputs["Start"].default_value = (0, 0, 0)
    links.new(rise.outputs["Vector"], profile.inputs["End"])
    wall = nodes.new("GeometryNodeCurveToMesh")
    links.new(normal.outputs["Curve"], wall.inputs["Curve"])
    links.new(profile.outputs["Curve"], wall.inputs["Profile Curve"])
    flat = nodes.new("GeometryNodeSetShadeSmooth")
    flat.inputs["Shade Smooth"].default_value = False
    links.new(wall.outputs["Mesh"], flat.inputs["Mesh"])
    on_points = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(source.outputs["Geometry"], on_points.inputs["Points"])
    links.new(flat.outputs["Mesh"], on_points.inputs["Instance"])
    realized = nodes.new("GeometryNodeRealizeInstances")
    links.new(on_points.outputs["Instances"], realized.inputs["Geometry"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(realized.outputs["Geometry"], painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def footprint_placeholder():
    """A simple 3D envelope for a footprint without an imported component model."""
    group, source, sink = _group("KLS_FootprintPlaceholder", (("Material", "NodeSocketMaterial"),
                                                          ("Width", "NodeSocketFloat"),
                                                          ("Height", "NodeSocketFloat")))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    size = nodes.new("ShaderNodeCombineXYZ")
    links.new(source.outputs["Width"], size.inputs["X"])
    links.new(source.outputs["Height"], size.inputs["Y"])
    size.inputs["Z"].default_value = PLACEHOLDER_HEIGHT_M
    cube = nodes.new("GeometryNodeMeshCube")
    links.new(size.outputs["Vector"], cube.inputs["Size"])
    on_points = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(source.outputs["Geometry"], on_points.inputs["Points"])
    links.new(cube.outputs["Mesh"], on_points.inputs["Instance"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(on_points.outputs["Instances"], painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def highlight_box():
    """A translucent glow box around a selected component (size in local axes)."""
    group, source, sink = _group("KLS_HighlightBox", (("Material", "NodeSocketMaterial"),
                                                     ("Size", "NodeSocketVector")))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    cube = nodes.new("GeometryNodeMeshCube")
    links.new(source.outputs["Size"], cube.inputs["Size"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(cube.outputs["Mesh"], painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def board():
    group, source, sink = _group("KLS_Board_v3", (("Material", "NodeSocketMaterial"),
                                                  ("Bottom Material", "NodeSocketMaterial"),
                                                  ("Core Material", "NodeSocketMaterial"),
                                                  ("Thickness", "NodeSocketFloat")))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    to_curve = nodes.new("GeometryNodeMeshToCurve")
    links.new(source.outputs["Geometry"], to_curve.inputs["Mesh"])
    cyclic = nodes.new("GeometryNodeSetSplineCyclic")
    cyclic.inputs["Cyclic"].default_value = True
    links.new(to_curve.outputs["Curve"], cyclic.inputs["Curve"])
    poly = nodes.new("GeometryNodeCurveSplineType")
    poly.spline_type = "POLY"
    links.new(cyclic.outputs["Curve"], poly.inputs["Curve"])
    filled = nodes.new("GeometryNodeFillCurve")
    filled.inputs["Fill Rule"].default_value = "Even-Odd"
    links.new(poly.outputs["Curve"], filled.inputs["Curve"])
    top = nodes.new("GeometryNodeSetMaterial")
    links.new(filled.outputs["Mesh"], top.inputs["Geometry"])
    links.new(source.outputs["Material"], top.inputs["Material"])
    extrude = nodes.new("GeometryNodeExtrudeMesh")
    extrude.mode = "FACES"
    # One solid, walls on the outline only. Per-face extrusion (the default, v2)
    # left walls along every fill triangle inside the board: hidden while opaque,
    # bright wedges once focus mode made the board see-through.
    extrude.inputs["Individual"].default_value = False
    # Face extrusion follows the filled loop's normal. The modifier supplies a
    # negative scale, placing the solid below the top copper surface.
    extrude.inputs["Offset"].default_value = (0, 0, 1)
    links.new(source.outputs["Thickness"], extrude.inputs["Offset Scale"])
    links.new(top.outputs["Geometry"], extrude.inputs["Mesh"])
    sides = nodes.new("GeometryNodeSetMaterial")
    links.new(extrude.outputs["Mesh"], sides.inputs["Geometry"])
    links.new(extrude.outputs["Side"], sides.inputs["Selection"])
    links.new(source.outputs["Core Material"], sides.inputs["Material"])
    bottom = nodes.new("GeometryNodeSetMaterial")
    links.new(sides.outputs["Geometry"], bottom.inputs["Geometry"])
    links.new(extrude.outputs["Top"], bottom.inputs["Selection"])
    links.new(source.outputs["Bottom Material"], bottom.inputs["Material"])
    closed = nodes.new("GeometryNodeJoinGeometry")
    links.new(bottom.outputs["Geometry"], closed.inputs["Geometry"])
    links.new(top.outputs["Geometry"], closed.inputs["Geometry"])
    links.new(closed.outputs["Geometry"], sink.inputs["Geometry"])
    return group


SMOOTH_UP_TO = math.radians(30)  # a curved wall's facets meet flatter than this; rims and corners do not


def smooth_walls():
    """Smooth shading on a solid's curved walls, sharp rims and corners: flat facets
    streak a reflective metal (the IMS base)."""
    group, source, sink = _group("KLS_SmoothWalls_v1")
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    faces = nodes.new("GeometryNodeSetShadeSmooth")
    faces.domain = "FACE"
    links.new(source.outputs["Geometry"], faces.inputs["Mesh"])
    gentle = nodes.new("FunctionNodeCompare")
    gentle.data_type, gentle.operation = "FLOAT", "LESS_THAN"
    links.new(nodes.new("GeometryNodeInputMeshEdgeAngle").outputs["Unsigned Angle"], gentle.inputs[0])
    gentle.inputs[1].default_value = SMOOTH_UP_TO
    edges = nodes.new("GeometryNodeSetShadeSmooth")
    edges.domain = "EDGE"
    links.new(faces.outputs["Mesh"], edges.inputs["Mesh"])
    links.new(gentle.outputs["Result"], edges.inputs["Shade Smooth"])
    links.new(edges.outputs["Mesh"], sink.inputs["Geometry"])
    return group


def _named(nodes, name):
    node = nodes.new("GeometryNodeInputNamedAttribute")
    node.data_type = "FLOAT"
    node.inputs["Name"].default_value = name
    return node.outputs["Attribute"]


def _xyz(nodes, links, xy, z):
    result = nodes.new("ShaderNodeCombineXYZ")
    links.new(xy, result.inputs["X"])
    links.new(xy, result.inputs["Y"])
    if isinstance(z, (int, float)):
        result.inputs["Z"].default_value = z
    else:
        links.new(z, result.inputs["Z"])
    return result.outputs["Vector"]


def vias():
    """Lands, barrels and, per via (protection.py, as point attributes): its plug or fill
    (`core_top`/`core_bottom`: the barrel's halves it fills; `fill_copper`: a copper fill;
    `plug_ink`: a plug of solder mask ink; else the resin), its cap plating (`cap`, metres,
    on its outer lands), its mask tents (`tent_top`, `tent_bottom`) and a bare barrel
    (`bare_barrel`). `outer_top`/`outer_bottom`: the via reaches F.Cu / B.Cu, and
    `ring_top`/`ring_bottom`: it has an annular ring there (KiCad's "Annular rings"). An end on
    inner copper (a blind via's floor, a buried via's ends) is a flat land on that copper,
    plated like the barrel; `z_top`/`z_bottom` stand `Land Lift` outside the copper they end
    on. A `drilled` via is see-through in its bore (holes.py), so its lands read as rings
    and its core reaches the surface. `Plating`: the barrel's wall, inside the drill (the
    bore is the drill less twice that)."""
    group, source, sink = _group("KLS_Vias_v14", (("Material", "NodeSocketMaterial"),
                                                   ("Drill Material", "NodeSocketMaterial"),
                                                   ("Bare Drill Material", "NodeSocketMaterial"),
                                                   ("Top Thickness", "NodeSocketFloat"),
                                                   ("Bottom Thickness", "NodeSocketFloat"),
                                                   ("Fill Material", "NodeSocketMaterial"),
                                                   ("Copper Fill Material", "NodeSocketMaterial"),
                                                   ("Plug Material", "NodeSocketMaterial"),
                                                   ("Tent Top Material", "NodeSocketMaterial"),
                                                   ("Tent Bottom Material", "NodeSocketMaterial"),
                                                   ("Plating", "NodeSocketFloat", 25e-6),
                                                   ("Land Lift", "NodeSocketFloat", 3e-6)))
    if source is None:
        return group
    nodes, links = group.nodes, group.links

    def math(operation, *values):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        for socket, value in zip(node.inputs, values):
            if isinstance(value, float):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs[0]

    def logic(operation, *values):
        node = nodes.new("FunctionNodeBooleanMath")
        node.operation = operation
        for socket, value in zip(node.inputs, values):
            links.new(value, socket)
        return node.outputs[0]

    def is_set(value):
        compare = nodes.new("FunctionNodeCompare")
        compare.data_type = "FLOAT"
        compare.operation = "GREATER_THAN"
        links.new(value, compare.inputs[0])
        compare.inputs[1].default_value = 0.5
        return compare.outputs["Result"]

    flags = {}

    def flag(name):
        """A 0/1 point attribute as a boolean, one node per attribute."""
        if name not in flags:
            flags[name] = is_set(_named(nodes, name))
        return flags[name]

    def shifted_points(z):
        set_position = nodes.new("GeometryNodeSetPosition")
        links.new(source.outputs["Geometry"], set_position.inputs["Geometry"])
        vector = nodes.new("ShaderNodeCombineXYZ")
        links.new(z, vector.inputs["Z"])
        links.new(vector.outputs["Vector"], set_position.inputs["Offset"])
        return set_position.outputs["Geometry"]

    def on_points(points, selection, instance, scale):
        placed = nodes.new("GeometryNodeInstanceOnPoints")
        links.new(points, placed.inputs["Points"])
        links.new(selection, placed.inputs["Selection"])
        links.new(instance, placed.inputs["Instance"])
        links.new(scale, placed.inputs["Scale"])
        return placed.outputs["Instances"]

    def painted(material, *geometry):
        """The `geometry` (joined, if several) in the group input `material`."""
        if len(geometry) > 1:
            join = nodes.new("GeometryNodeJoinGeometry")
            for part in geometry:
                links.new(part, join.inputs["Geometry"])
            geometry = (join.outputs["Geometry"],)
        node = nodes.new("GeometryNodeSetMaterial")
        links.new(geometry[0], node.inputs["Geometry"])
        links.new(source.outputs[material], node.inputs["Material"])
        return node.outputs["Geometry"]

    diameter = _named(nodes, "diameter")
    drill = _named(nodes, "drill")
    top = _named(nodes, "z_top")
    bottom = _named(nodes, "z_bottom")
    height = math("SUBTRACT", top, bottom)
    bore = math("MAXIMUM", math("MULTIPLY_ADD", source.outputs["Plating"], -2.0, drill), math("MULTIPLY", drill, 0.2))
    circle = nodes.new("GeometryNodeMeshCircle")
    circle.fill_type = "NGON"
    circle.inputs["Vertices"].default_value = VIA_VERTICES
    circle.inputs["Radius"].default_value = 0.5

    def outer_land(z, copper, selection, sign, width):
        """A land on outer copper (sign -1 on top, +1 at the bottom), `width` across, on the
        `selection` vias: a disk at the copper surface, or, for thick `copper`, a cylinder from
        its inner face out through it and a capped via's `cap` plating, which stands proud."""
        shape, scale_z = _disk_or_cylinder(nodes, links, math("ADD", copper, _named(nodes, "cap")), VIA_VERTICES,
                                           1.0, copper)
        return on_points(shifted_points(math("MULTIPLY_ADD", copper, sign, z)), selection, shape,
                         _xyz(nodes, links, width, math("MULTIPLY", scale_z, -sign)))  # grown back outward

    # Top and bottom, so vias read correctly from either side: a land where the via has a
    # ring, else (a capped via) its cap over the drill alone.
    capped = is_set(math("MULTIPLY", _named(nodes, "cap"), 1e6))  # cap > 0.5 um
    ends = []
    for z, thickness, end, sign in ((top, "Top Thickness", "top", -1.0), (bottom, "Bottom Thickness", "bottom", 1.0)):
        copper = source.outputs[thickness]
        ringless = logic("NIMPLY", flag(f"outer_{end}"), flag(f"ring_{end}"))
        ends += [outer_land(z, copper, flag(f"ring_{end}"), sign, diameter),
                 outer_land(z, copper, logic("AND", ringless, capped), sign, drill)]
    lands = painted("Material", *ends)

    # Finished like the pads, or bare copper where a tent, plug or fill kept the finish out.
    bare = flag("bare_barrel")
    plated = {"Drill Material": logic("NOT", bare), "Bare Drill Material": bare}

    # An end on inner copper: a flat land on that copper's face, facing into the hole.
    inner_lands = {material: [] for material in plated}
    for name, z, sign in (("outer_top", top, -1.0), ("outer_bottom", bottom, 1.0)):
        inner = logic("NOT", flag(name))
        points = shifted_points(math("MULTIPLY_ADD", source.outputs["Land Lift"], 2 * sign, z))  # to its face
        for material, selection in plated.items():
            inner_lands[material].append(on_points(points, logic("AND", inner, selection), circle.outputs["Mesh"],
                                                   _xyz(nodes, links, diameter, sign)))

    # The barrel: the plating's inner face (the bore), top land to bottom land.
    # The lands and board are see-through inside the drill (holes.py); no boolean.
    wall_circle = nodes.new("GeometryNodeMeshCircle")
    wall_circle.fill_type = "NONE"
    wall_circle.inputs["Vertices"].default_value = VIA_VERTICES
    wall_circle.inputs["Radius"].default_value = 0.5
    wall_curve = nodes.new("GeometryNodeMeshToCurve")
    links.new(wall_circle.outputs["Mesh"], wall_curve.inputs["Mesh"])
    normal = nodes.new("GeometryNodeSetCurveNormal")
    normal.inputs["Mode"].default_value = "Z Up"
    links.new(wall_curve.outputs["Curve"], normal.inputs["Curve"])
    vertical_profile = nodes.new("GeometryNodeCurvePrimitiveLine")
    vertical_profile.mode = "POINTS"
    vertical_profile.inputs["Start"].default_value = (0, -0.5, 0)
    vertical_profile.inputs["End"].default_value = (0, 0.5, 0)
    wall_mesh = nodes.new("GeometryNodeCurveToMesh")
    links.new(normal.outputs["Curve"], wall_mesh.inputs["Curve"])
    links.new(vertical_profile.outputs["Curve"], wall_mesh.inputs["Profile Curve"])
    middle_points = shifted_points(math("MULTIPLY_ADD", height, 0.5, bottom))
    barrel_scale = _xyz(nodes, links, bore, height)
    barrels = [on_points(middle_points, selection, painted(material, wall_mesh.outputs["Mesh"]), barrel_scale)
               for material, selection in plated.items()]

    # A tent: the mask spanning the hole, a flat disk just outside the land (facing out).
    tent_width = math("MULTIPLY", bore, TENT_SCALE)
    tents = [on_points(shifted_points(math("ADD", z, sign * TENT_LIFT_M)), flag(f"tent_{name}"),
                       painted(f"Tent {name.capitalize()} Material", circle.outputs["Mesh"]),
                       _xyz(nodes, links, tent_width, sign))
             for name, z, sign in (("top", top, 1.0), ("bottom", bottom, -1.0))]

    # A plug or fill: a closed cylinder a hair inside the wall, short of each end (it never
    # shares a face with a land), over the halves of the barrel it fills (from the middle up
    # for `core_top`, down for `core_bottom`).
    core = nodes.new("GeometryNodeMeshCylinder")
    core.fill_type = "NGON"
    core.inputs["Vertices"].default_value = VIA_VERTICES
    core.inputs["Radius"].default_value = 0.5
    core.inputs["Depth"].default_value = 1.0
    core_flat = nodes.new("GeometryNodeSetShadeSmooth")  # smooth normals streak the flat ends
    core_flat.inputs["Shade Smooth"].default_value = False
    links.new(core.outputs["Mesh"], core_flat.inputs["Mesh"])
    # Its ends: at the surface in a drilled via (inside the ring), else under the land.
    solid = math("SUBTRACT", 1.0, _named(nodes, "drilled"))
    under_top = math("SUBTRACT", top, math("MULTIPLY_ADD", source.outputs["Top Thickness"], solid, PLUG_INSET_M))
    over_bottom = math("ADD", bottom, math("MULTIPLY_ADD", source.outputs["Bottom Thickness"], solid, PLUG_INSET_M))
    core_middle = math("MULTIPLY", math("ADD", under_top, over_bottom), 0.5)
    reach = math("MAXIMUM", math("MULTIPLY", math("SUBTRACT", under_top, over_bottom), 0.5), 0.0)
    upper, lower = _named(nodes, "core_top"), _named(nodes, "core_bottom")
    halves = math("ADD", upper, lower)
    shift = math("MULTIPLY", math("SUBTRACT", upper, lower), reach)  # +reach upper half only, -reach lower
    core_points = shifted_points(math("MULTIPLY_ADD", shift, 0.5, core_middle))
    core_scale = _xyz(nodes, links, math("SUBTRACT", bore, 2 * PLUG_GAP_M), math("MULTIPLY", halves, reach))
    has_core = is_set(halves)
    copper_fill, plug = flag("fill_copper"), flag("plug_ink")
    cores = [on_points(core_points, logic(operation, has_core, kind), painted(material, core_flat.outputs["Mesh"]),
                       core_scale)
             for material, kind, operation in (("Fill Material", logic("OR", copper_fill, plug), "NIMPLY"),  # resin
                                               ("Copper Fill Material", copper_fill, "AND"),
                                               ("Plug Material", plug, "AND"))]

    together = nodes.new("GeometryNodeJoinGeometry")
    for geometry in (lands, *(painted(material, *found) for material, found in inner_lands.items()),
                     *barrels, *tents, *cores):
        links.new(geometry, together.inputs["Geometry"])
    links.new(together.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def plot_walls():
    """Walls along plot outlines (silkscreen ink), z = 0 to Height.

    The input is the outlines KiCad drew, as counter-clockwise edge loops. Where
    outlines overlap (joined silkscreen strokes, text on a box), a wall
    would stand inside the drawn area. Each wall point therefore samples the plot
    image `Probe` metres outside itself, and wall faces whose points both see the
    plot drawn there too are dropped. No outline is merged or cut.

    With `Clip` on, wall faces with a point outside the `Outline` object (filled
    even-odd, like the clipped sheet) are dropped too: ink past the board edge.
    """
    group, source, sink = _group("KLS_PlotWalls_v2", (("Material", "NodeSocketMaterial"),
                                                     ("Image", "NodeSocketImage"),
                                                     ("Plot Offset", "NodeSocketVector"),
                                                     ("Plot Size", "NodeSocketVector", (1.0, 1.0, 1.0)),
                                                     ("Height", "NodeSocketFloat"),
                                                     ("Probe", "NodeSocketFloat"),
                                                     ("Outline", "NodeSocketObject"),
                                                     ("Clip", "NodeSocketFloat")))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    curve = nodes.new("GeometryNodeMeshToCurve")
    links.new(source.outputs["Geometry"], curve.inputs["Mesh"])
    normal = nodes.new("GeometryNodeSetCurveNormal")
    normal.inputs["Mode"].default_value = "Z Up"
    links.new(curve.outputs["Curve"], normal.inputs["Curve"])
    # Outward from a counter-clockwise outline: tangent x Z.
    outward = nodes.new("ShaderNodeVectorMath")
    outward.operation = "CROSS_PRODUCT"
    links.new(nodes.new("GeometryNodeInputTangent").outputs["Tangent"], outward.inputs[0])
    outward.inputs[1].default_value = (0.0, 0.0, 1.0)
    unit = nodes.new("ShaderNodeVectorMath")
    unit.operation = "NORMALIZE"
    links.new(outward.outputs["Vector"], unit.inputs[0])
    step = nodes.new("ShaderNodeVectorMath")
    step.operation = "SCALE"
    links.new(unit.outputs["Vector"], step.inputs[0])
    links.new(source.outputs["Probe"], step.inputs["Scale"])
    probe = nodes.new("ShaderNodeVectorMath")
    probe.operation = "ADD"
    links.new(nodes.new("GeometryNodeInputPosition").outputs["Position"], probe.inputs[0])
    links.new(step.outputs["Vector"], probe.inputs[1])
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "SUBTRACT"
    links.new(probe.outputs["Vector"], offset.inputs[0])
    links.new(source.outputs["Plot Offset"], offset.inputs[1])
    uv = nodes.new("ShaderNodeVectorMath")
    uv.operation = "DIVIDE"
    links.new(offset.outputs["Vector"], uv.inputs[0])
    links.new(source.outputs["Plot Size"], uv.inputs[1])
    image = nodes.new("GeometryNodeImageTexture")
    image.interpolation = "Linear"
    image.extension = "EXTEND"
    links.new(source.outputs["Image"], image.inputs["Image"])
    links.new(uv.outputs["Vector"], image.inputs["Vector"])
    drawn = nodes.new("ShaderNodeMath")
    drawn.operation = "GREATER_THAN"
    links.new(image.outputs["Alpha"], drawn.inputs[0])
    drawn.inputs[1].default_value = 0.5
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type = "FLOAT"
    store.domain = "POINT"
    store.inputs["Name"].default_value = "kls_drawn_beyond"
    links.new(normal.outputs["Curve"], store.inputs["Geometry"])
    links.new(drawn.outputs["Value"], store.inputs["Value"])
    # Inside the board: a ray straight down from above hits the filled outline.
    board_outline = nodes.new("GeometryNodeObjectInfo")
    board_outline.transform_space = "RELATIVE"
    links.new(source.outputs["Outline"], board_outline.inputs["Object"])
    outline_curve = nodes.new("GeometryNodeMeshToCurve")
    links.new(board_outline.outputs["Geometry"], outline_curve.inputs["Mesh"])
    cyclic = nodes.new("GeometryNodeSetSplineCyclic")
    cyclic.inputs["Cyclic"].default_value = True
    links.new(outline_curve.outputs["Curve"], cyclic.inputs["Curve"])
    board_area = nodes.new("GeometryNodeFillCurve")
    board_area.inputs["Fill Rule"].default_value = "Even-Odd"
    links.new(cyclic.outputs["Curve"], board_area.inputs["Curve"])
    above = nodes.new("ShaderNodeVectorMath")
    above.operation = "ADD"
    links.new(nodes.new("GeometryNodeInputPosition").outputs["Position"], above.inputs[0])
    above.inputs[1].default_value = (0.0, 0.0, 1.0)
    ray = nodes.new("GeometryNodeRaycast")
    links.new(board_area.outputs["Mesh"], ray.inputs["Target Geometry"])
    links.new(above.outputs["Vector"], ray.inputs["Source Position"])
    ray.inputs["Ray Direction"].default_value = (0.0, 0.0, -1.0)
    ray.inputs["Ray Length"].default_value = 2.0
    missed = nodes.new("ShaderNodeMath")
    missed.operation = "SUBTRACT"
    missed.inputs[0].default_value = 1.0
    links.new(ray.outputs["Is Hit"], missed.inputs[1])
    outside = nodes.new("ShaderNodeMath")
    outside.operation = "MULTIPLY"
    links.new(missed.outputs[0], outside.inputs[0])
    links.new(source.outputs["Clip"], outside.inputs[1])
    store_outside = nodes.new("GeometryNodeStoreNamedAttribute")
    store_outside.data_type = "FLOAT"
    store_outside.domain = "POINT"
    store_outside.inputs["Name"].default_value = "kls_outside_board"
    links.new(store.outputs["Geometry"], store_outside.inputs["Geometry"])
    links.new(outside.outputs[0], store_outside.inputs["Value"])
    store = store_outside
    down = nodes.new("ShaderNodeMath")
    down.operation = "MULTIPLY"
    links.new(source.outputs["Height"], down.inputs[0])
    down.inputs[1].default_value = -1.0  # measured: on counter-clockwise outlines profile +Y is world -Z
    rise = nodes.new("ShaderNodeCombineXYZ")
    links.new(down.outputs[0], rise.inputs["Y"])
    profile = nodes.new("GeometryNodeCurvePrimitiveLine")
    profile.mode = "POINTS"
    profile.inputs["Start"].default_value = (0, 0, 0)
    links.new(rise.outputs["Vector"], profile.inputs["End"])
    wall = nodes.new("GeometryNodeCurveToMesh")
    links.new(store.outputs["Geometry"], wall.inputs["Curve"])
    links.new(profile.outputs["Curve"], wall.inputs["Profile Curve"])
    inner = nodes.new("ShaderNodeMath")
    inner.operation = "GREATER_THAN"
    inner.inputs[1].default_value = 0.75  # both ends of the face (face value = mean of its corners)
    links.new(_named(nodes, "kls_drawn_beyond"), inner.inputs[0])
    beyond_edge = nodes.new("ShaderNodeMath")
    beyond_edge.operation = "GREATER_THAN"
    beyond_edge.inputs[1].default_value = 0.25  # either end of the face
    links.new(_named(nodes, "kls_outside_board"), beyond_edge.inputs[0])
    dropped = nodes.new("ShaderNodeMath")
    dropped.operation = "MAXIMUM"
    links.new(inner.outputs["Value"], dropped.inputs[0])
    links.new(beyond_edge.outputs["Value"], dropped.inputs[1])
    delete = nodes.new("GeometryNodeDeleteGeometry")
    delete.domain = "FACE"
    links.new(wall.outputs["Mesh"], delete.inputs["Geometry"])
    links.new(dropped.outputs["Value"], delete.inputs["Selection"])
    flat = nodes.new("GeometryNodeSetShadeSmooth")
    flat.inputs["Shade Smooth"].default_value = False
    links.new(delete.outputs["Geometry"], flat.inputs["Mesh"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(flat.outputs["Mesh"], painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def via_rings():
    """A via's annular rings on inner copper: a flat disk `diameter` across on each point
    (apply._apply_via_rings: one per via and ringed inner layer, at that copper)."""
    group, source, sink = _group("KLS_ViaRings_v1", (("Material", "NodeSocketMaterial"),))
    if source is None:
        return group
    nodes, links = group.nodes, group.links
    circle = nodes.new("GeometryNodeMeshCircle")
    circle.fill_type = "NGON"
    circle.inputs["Vertices"].default_value = VIA_VERTICES
    circle.inputs["Radius"].default_value = 0.5
    rings = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(source.outputs["Geometry"], rings.inputs["Points"])
    links.new(circle.outputs["Mesh"], rings.inputs["Instance"])
    links.new(_xyz(nodes, links, _named(nodes, "diameter"), 1.0), rings.inputs["Scale"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(rings.outputs["Instances"], painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def ensure_all():
    return {"tracks": tracks(), "fill": fill(), "solder": fill("KLS_Solder", solder=True),
            "fill_single": fill_single(),
            "drills": drills(), "vias": vias(), "via_rings": via_rings(), "board": board(),
            "smooth_walls": smooth_walls(),
            "footprint_placeholder": footprint_placeholder(), "highlight_box": highlight_box()}
