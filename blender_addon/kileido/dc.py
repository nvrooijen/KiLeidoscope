"""DC analysis results from the bridge (protocol.dc_setup/dc_status/dc_result_message).

The bridge solves one power net with Fill Resistance's solver (Janik Oltmanns /
B4L, GPL-3.0-or-later) and sends per copper layer the current density |J|, the
potential and the current's direction on a regular grid. Shown on the real layers:

- a colour map per layer ("KLS <layer> dc map"): a flat quad just out from the
  copper, textured with the field; see-through where there is no copper. Current
  density (log scale by default) or voltage drop from the highest supply.
- arrows along the current ("KLS <layer> dc arrows"): points with a direction,
  turned into cones by a Geometry Nodes group that marches them along it with the
  scene time. The speed is illustrative, not the drift velocity.
- via currents ("KLS vias dc"): a column around each via and plated hole, coloured
  by the current through it.
- the marked supplies and loads ("KLS dc markers ..."): cones pointing at their
  pads and vias, red for supplies, blue for loads.

Maps and arrows follow their copper layer's eye in the Layers list, via currents
the Vias eye. The panel's check box hides them all. Colours are computed here
(numpy), so a new range or field only rewrites the images.
"""

import math

import bpy
import numpy as np

from . import transform
from .highlight import hide_copy, show_copy
from .nodes import _group
from .objects import owned_object, set_modifier
from .placement import outward
from .state import board

TAG = "kls_dc"
LIFT_M = 4e-6  # maps sit this far out from the copper's outer surface (via lands are at 3 um)
ARROW_LIFT_M = 6e-6
VIA_HALO_M = 30e-6  # via current columns: this much wider than the land
LOG_DECADES = 3.0  # the log |J| scale spans this many decades below the top (Fill Resistance's plots)
WEAK_ARROW = 0.02  # no arrows where |J| is below this fraction of the maximum
MARKER_COLORS = {"supply": (1.0, 0.12, 0.08), "load": (0.1, 0.35, 1.0)}  # sRGB
ARROW_COLOR = (1.0, 1.0, 1.0)
ARROW_GROUP = "KLS_DC_Arrows_v1"
# Colour maps as sRGB stops, low to high: plasma for current density (no black, which reads
# as unlit inner copper), viridis for voltage drop.
CMAPS = {
    "CURRENT": ((13, 8, 135), (75, 3, 161), (125, 3, 168), (168, 34, 150), (203, 70, 121), (229, 107, 93),
                (248, 148, 65), (253, 195, 40), (240, 249, 33)),
    "DROP": ((68, 1, 84), (72, 40, 120), (62, 74, 137), (49, 104, 142), (38, 130, 142), (31, 158, 137),
             (53, 183, 121), (109, 205, 89), (180, 222, 44), (253, 231, 37)),
}
_syncing = False  # applying the bridge's setup to the panel's properties: do not echo it back
_eyes = {}  # the Layers list's eyes before a layer was isolated (isolate), restored for "All"
_layer_items = []  # Blender needs the enum item strings kept alive


# --- Frames ---------------------------------------------------------------------------

def apply_setup(header, arrays):
    board.dc["setup"] = header
    board.dc["markers"] = (np.array(arrays.get("marker", ()), np.int64).reshape(-1, 3),
                           np.array(arrays.get("marker_terminal", ()), np.int32),
                           np.array(arrays.get("marker_side", ()), np.int32))
    _sync_properties(header)
    refresh_markers()
    redraw()


def apply_status(header):
    board.dc["status"] = header
    redraw()


def apply_result(header, arrays):
    if not header.get("net"):
        board.dc["result"] = None
    else:
        board.dc["result"] = {**header, **{key: np.asarray(arrays[key]) for key in
                                           ("j", "v", "jx", "jy", "via", "via_current", "via_power")}}
    refresh()
    redraw()


def syncing():
    return _syncing


def _sync_properties(header):
    """The table and settings in the panel show the bridge's setup."""
    global _syncing
    scene = bpy.context.scene
    _syncing = True
    try:
        items = scene.kileido_dc_terminals
        terminals = header.get("terminals", ())
        while len(items) > len(terminals):
            items.remove(len(items) - 1)
        while len(items) < len(terminals):
            items.add()
        for index, (item, terminal) in enumerate(zip(items, terminals)):
            item.index, item.role = index, terminal["role"]
            item.name, item.value, item.bonded = terminal["name"], float(terminal["value"]), terminal["bonded"]
            item.parts = ", ".join(terminal["parts"])
        scene.kileido_dc_active = max(-1, min(int(header.get("active", -1)), len(terminals) - 1))
        settings = header.get("settings", {})
        scene.kileido_dc_plating_um = float(settings.get("plating_um", scene.kileido_dc_plating_um))
        scene.kileido_dc_cell_um = float(settings.get("cell_um") or 0.0)
        scene.kileido_dc_capped = bool(settings.get("vias_capped", False))
    finally:
        _syncing = False


def redraw():
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# --- Settings -------------------------------------------------------------------------

def enabled():
    return bool(getattr(bpy.context.scene, "kileido_dc", True))


def field():
    return getattr(bpy.context.scene, "kileido_dc_field", "CURRENT")


def value_range(result=None):
    """(low, high, log) of the shown field in its display unit (A/mm2, or mV of drop)."""
    result = result or board.dc.get("result")
    scene = bpy.context.scene
    log = field() == "CURRENT" and scene.kileido_dc_log
    if scene.kileido_dc_auto_range:
        low, high = _auto_range(result, log)
    else:
        low, high = float(scene.kileido_dc_min), float(scene.kileido_dc_max)
    if log:
        low = max(low, 1e-12)
    return low, max(high, low + (1e-12 if log else 1e-9)), log


def _auto_range(result, log):
    """|J|: the largest value down (three decades on the log scale); drop: its extent."""
    if field() == "CURRENT":
        high = max(float(result["j_max"]), 1e-12)
        return (high * 10 ** -LOG_DECADES if log else 0.0), high
    drop = (float(result["v_ref"]) - np.asarray(result["v"], np.float64)) * 1e3
    finite = drop[np.isfinite(drop)]
    return (max(0.0, float(finite.min())), float(finite.max())) if len(finite) else (0.0, 1.0)


def freeze_range():
    """Automatic range switched off: the manual one starts where the automatic one was."""
    global _syncing
    result = board.dc.get("result")
    if not result:
        return
    scene = bpy.context.scene
    low, high = _auto_range(result, field() == "CURRENT" and scene.kileido_dc_log)
    _syncing = True
    try:
        scene.kileido_dc_min, scene.kileido_dc_max = low, high
    finally:
        _syncing = False


def field_values(result, layer):
    """The shown field of one layer, in its display unit."""
    if field() == "CURRENT":
        return np.asarray(result["j"][layer], np.float64)
    return (float(result["v_ref"]) - np.asarray(result["v"][layer], np.float64)) * 1e3


def unit():
    return "A/mm²" if field() == "CURRENT" else "mV"


def _lut(name):
    stops = np.asarray(CMAPS[name], np.float64) / 255
    positions = np.linspace(0, 1, len(stops))
    t = np.linspace(0, 1, 256)
    srgb = np.column_stack([np.interp(t, positions, stops[:, channel]) for channel in range(3)])
    return srgb


def colors_srgb(values, low, high, log, name=None):
    """(n, 3) sRGB colours of values on the colour map (NaN: black)."""
    values = np.asarray(values, np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        if log:
            t = np.log(np.maximum(values, 1e-300) / low) / math.log(high / low)
        else:
            t = (values - low) / (high - low)
    index = np.clip(np.nan_to_num(t, nan=0.0) * 255, 0, 255).astype(np.int32)
    return _lut(name or field())[index]


def _linear(srgb):
    srgb = np.asarray(srgb, np.float64)
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def legend(rows=5):
    """(sRGB colour, label) from the top of the range down, for the panel."""
    result = board.dc.get("result")
    if not result:
        return []
    low, high, log = value_range(result)
    if log:
        values = np.geomspace(high, low, rows)
    else:
        values = np.linspace(high, low, rows)
    colors = colors_srgb(values, low, high, log)
    return [(tuple(color), _format(value)) for color, value in zip(colors, values)]


def _format(value):
    if value == 0:
        return "0"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 0.01:
        return f"{value:.3g}"
    return f"{value:.2e}"


def layer_items(_scene=None, _context=None):
    """"All" and the layers of the last result, for the panel's Layer chooser."""
    result = board.dc.get("result") or {}
    _layer_items[:] = [("ALL", "All layers", "Every layer as the Layers list shows it")]
    _layer_items.extend((layer, board.layer_names.get(layer) or layer,
                         f"Only {layer}: the board, mask, silkscreen, other copper and components hidden")
                        for layer in result.get("layers", ()))
    return _layer_items


def isolate(layer):
    """Show one copper layer's result from above: every other row of the Layers
    list off; "ALL" puts back the eyes as they were before."""
    from . import layers
    scene = bpy.context.scene
    rows = [row for row, _ in layers.rows()]
    if layer == "ALL":
        for row, shown in _eyes.items():
            if row in rows:
                setattr(scene, layers.property_name(row), shown)
        _eyes.clear()
        return
    if not _eyes:
        _eyes.update((row, layers.shown(row)) for row in rows)
    for row in rows:
        setattr(scene, layers.property_name(row), row in (layer, "Vias"))


# --- Display --------------------------------------------------------------------------

def refresh():
    """Rebuild every DC display from the last result and setup. Skipped inside a
    snapshot: `snapshot_end` calls it once."""
    if board.collection is None or board.in_snapshot:
        return
    result = board.dc.get("result") if enabled() else None
    wanted = set()
    if result is not None:
        for index, layer in enumerate(result["layers"]):
            if layer not in board.heights:
                continue
            wanted.add(_draw_map(result, index, layer))
            if bpy.context.scene.kileido_dc_arrows:
                name = _draw_arrows(result, index, layer)
                if name:
                    wanted.add(name)
        if bpy.context.scene.kileido_dc_vias and len(result["via"]):
            wanted.add(_draw_vias(result))
    wanted |= refresh_markers()
    for obj in tuple(board.collection.all_objects):
        if obj.get(TAG) and obj.name not in wanted:
            hide_copy(obj)


def recolor():
    """A new field, range or scale: only the map images change."""
    result = board.dc.get("result")
    if result is None or board.collection is None or not enabled():
        return
    low, high, log = value_range(result)
    for index, layer in enumerate(result["layers"]):
        image = bpy.data.images.get(f"KLS DC {layer}")
        if image is not None:
            _paint(image, field_values(result, index), low, high, log)


def _surface_z(layer, lift):
    """Out from the copper's outer surface (above via lands), away from the board."""
    return transform.copper_z(layer, "drills", board.heights) + outward(layer) * lift


def _map_material(layer, image):
    name = f"KLS DC map {layer}"
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        tree = material.node_tree
        tree.nodes.clear()
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.name = "KLS map"
        texture.interpolation = "Closest"  # the solver's cells, not a blur of them
        texture.extension = "CLIP"
        emission = tree.nodes.new("ShaderNodeEmission")
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        mix = tree.nodes.new("ShaderNodeMixShader")
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
        tree.links.new(texture.outputs["Alpha"], mix.inputs[0])
        tree.links.new(transparent.outputs[0], mix.inputs[1])
        tree.links.new(emission.outputs[0], mix.inputs[2])
        tree.links.new(mix.outputs[0], output.inputs["Surface"])
    material.node_tree.nodes["KLS map"].image = image
    return material


def _image(layer, width, height):
    name = f"KLS DC {layer}"
    image = bpy.data.images.get(name)
    if image is not None and tuple(image.size) != (width, height):
        bpy.data.images.remove(image)
        image = None
    if image is None:
        image = bpy.data.images.new(name, width, height, alpha=True, float_buffer=True)
    return image


def _paint(image, values, low, high, log):
    """Colours of `values` (rows top to bottom in KiCad) into the image (rows bottom up)."""
    rows, columns = values.shape
    pixels = np.zeros((rows, columns, 4), np.float32)
    copper = np.isfinite(values)
    pixels[copper, :3] = _linear(colors_srgb(values[copper], low, high, log))
    pixels[copper, 3] = 1.0
    image.pixels.foreach_set(pixels[::-1].ravel())
    image.update()


def _draw_map(result, index, layer):
    values = field_values(result, index)
    rows, columns = values.shape
    image = _image(layer, columns, rows)
    _paint(image, values, *value_range(result))
    grid = result["grid"]
    pitch = float(grid["pitch_nm"])
    corners = transform.xy_m(np.array([(grid["x0_nm"], grid["y0_nm"] + rows * pitch),
                                       (grid["x0_nm"] + columns * pitch, grid["y0_nm"])]), board.origin_nm)
    (left, bottom), (right, top) = corners
    name = f"KLS {layer} dc map"
    obj = owned_object(name)
    obj[TAG] = layer
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(4)
    mesh.vertices.foreach_set("co", np.array([(left, bottom, 0), (right, bottom, 0), (right, top, 0),
                                              (left, top, 0)], np.float32).ravel())
    mesh.loops.add(4)
    mesh.loops.foreach_set("vertex_index", np.arange(4, dtype=np.int32))
    mesh.polygons.add(1)
    mesh.polygons.foreach_set("loop_start", np.zeros(1, np.int32))
    mesh.polygons.foreach_set("loop_total", np.full(1, 4, np.int32))
    uv = mesh.uv_layers.new(name="UVMap") if not mesh.uv_layers else mesh.uv_layers[0]
    uv.data.foreach_set("uv", np.array([(0, 0), (1, 0), (1, 1), (0, 1)], np.float32).ravel())
    if outward(layer) < 0:
        mesh.flip_normals()  # the bottom layer's map faces down
    mesh.update()
    material = _map_material(layer, image)
    if mesh.materials:
        mesh.materials[0] = material
    else:
        mesh.materials.append(material)
    obj.location.z = _surface_z(layer, LIFT_M)
    show_copy(obj)
    return name


def arrow_group():
    """Cones along each point's "kls_dir", marching back and forth over `Travel` with
    the scene time at `Speed` cycles per second (illustrative)."""
    group, source, sink = _group(ARROW_GROUP, (("Material", "NodeSocketMaterial"),
                                               ("Size", "NodeSocketFloat", 1e-3),
                                               ("Travel", "NodeSocketFloat", 1e-3),
                                               ("Speed", "NodeSocketFloat", 1.0)))
    if source is None:
        return group
    nodes, links = group.nodes, group.links

    def math_node(operation, first, second=None):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        for slot, value in ((0, first), (1, second)):
            if value is None:
                continue
            if isinstance(value, (int, float)):
                node.inputs[slot].default_value = value
            else:
                links.new(value, node.inputs[slot])
        return node.outputs[0]

    time = nodes.new("GeometryNodeInputSceneTime")
    cycle = math_node("FRACT", math_node("MULTIPLY", time.outputs["Seconds"], source.outputs["Speed"]))
    along = math_node("MULTIPLY", math_node("SUBTRACT", cycle, 0.5), source.outputs["Travel"])
    direction = nodes.new("GeometryNodeInputNamedAttribute")
    direction.data_type = "FLOAT_VECTOR"
    direction.inputs["Name"].default_value = "kls_dir"
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "SCALE"
    links.new(direction.outputs["Attribute"], offset.inputs[0])
    links.new(along, offset.inputs["Scale"])
    moved = nodes.new("GeometryNodeSetPosition")
    links.new(source.outputs["Geometry"], moved.inputs["Geometry"])
    links.new(offset.outputs["Vector"], moved.inputs["Offset"])
    cone = nodes.new("GeometryNodeMeshCone")
    cone.inputs["Vertices"].default_value = 10
    cone.inputs["Radius Top"].default_value = 0.0
    cone.inputs["Radius Bottom"].default_value = 0.3
    cone.inputs["Depth"].default_value = 1.0
    align = nodes.new("FunctionNodeAlignRotationToVector")
    align.axis = "Z"
    links.new(direction.outputs["Attribute"], align.inputs["Vector"])
    instances = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(moved.outputs["Geometry"], instances.inputs["Points"])
    links.new(cone.outputs["Mesh"], instances.inputs["Instance"])
    links.new(align.outputs["Rotation"], instances.inputs["Rotation"])
    links.new(source.outputs["Size"], instances.inputs["Scale"])
    realized = nodes.new("GeometryNodeRealizeInstances")
    links.new(instances.outputs["Instances"], realized.inputs["Geometry"])
    painted = nodes.new("GeometryNodeSetMaterial")
    links.new(realized.outputs["Geometry"], painted.inputs["Geometry"])
    links.new(source.outputs["Material"], painted.inputs["Material"])
    links.new(painted.outputs["Geometry"], sink.inputs["Geometry"])
    return group


def _flat_material(name, color):
    material = bpy.data.materials.get(name)
    if material is None:
        from .materials import make
        material = make(name, tuple(_linear(color)))
    return material


def _color_material():
    """Emission in each vertex's "kls_color" (linear), for the via currents."""
    name = "KLS DC colour"
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        tree = material.node_tree
        tree.nodes.clear()
        attribute = tree.nodes.new("ShaderNodeAttribute")
        attribute.attribute_type = "GEOMETRY"
        attribute.attribute_name = "kls_color"
        emission = tree.nodes.new("ShaderNodeEmission")
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        tree.links.new(attribute.outputs["Color"], emission.inputs["Color"])
        tree.links.new(emission.outputs[0], output.inputs["Surface"])
    return material


def arrow_points(result, index):
    """(KiCad nm positions, unit directions in Blender's XY) of one layer's arrows:
    every `kileido_dc_arrow_mm` on the grid, where the current is not weak."""
    grid = result["grid"]
    pitch = float(grid["pitch_nm"])
    j, jx, jy = (np.asarray(result[key][index], np.float64) for key in ("j", "jx", "jy"))
    step = max(1, round(bpy.context.scene.kileido_dc_arrow_mm * 1e6 / pitch))
    rows = np.arange(step // 2, j.shape[0], step)
    columns = np.arange(step // 2, j.shape[1], step)
    r, c = np.meshgrid(rows, columns, indexing="ij")
    r, c = r.ravel(), c.ravel()
    value, x, y = j[r, c], jx[r, c], jy[r, c]
    norm = np.hypot(x, y)
    keep = np.isfinite(value) & np.isfinite(norm) & (norm > 0) & (value >= WEAK_ARROW * float(result["j_max"]))
    r, c, x, y, norm = r[keep], c[keep], x[keep], y[keep], norm[keep]
    positions = np.column_stack([grid["x0_nm"] + (c + 0.5) * pitch, grid["y0_nm"] + (r + 0.5) * pitch])
    directions = np.column_stack([x / norm, -y / norm, np.zeros(len(x))])  # KiCad y points down
    return positions, directions, step * pitch * 1e-9


def _write(mesh, name, data_type, key, values):
    """A per-vertex vector or colour attribute (objects.write_attribute writes scalars)."""
    attribute = mesh.attributes.get(name) or mesh.attributes.new(name, data_type, "POINT")
    attribute.data.foreach_set(key, np.ascontiguousarray(values, np.float32).ravel())


def _draw_arrows(result, index, layer):
    positions, directions, spacing = arrow_points(result, index)
    name = f"KLS {layer} dc arrows"
    if not len(positions):
        return None
    obj = owned_object(name)
    obj[TAG] = layer
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(len(positions))
    coordinates = np.zeros((len(positions), 3), np.float32)
    coordinates[:, :2] = transform.xy_m(positions, board.origin_nm)
    size = 0.7 * spacing
    coordinates[:, 2] = outward(layer) * 0.3 * size  # cones lie on the map
    mesh.vertices.foreach_set("co", coordinates.ravel())
    _write(mesh, "kls_dir", "FLOAT_VECTOR", "vector", directions)
    mesh.update()
    obj.location.z = _surface_z(layer, ARROW_LIFT_M)
    set_modifier(obj, arrow_group(), _flat_material("KLS DC arrows", ARROW_COLOR),
                 {"Size": size, "Travel": spacing * 0.5, "Speed": float(bpy.context.scene.kileido_dc_flow_speed)})
    show_copy(obj)
    return name


def set_flow_speed():
    if board.collection is None:
        return
    from .nodes import modifier_input
    for obj in tuple(board.collection.all_objects):
        if obj.get(TAG) and obj.name.endswith(" dc arrows") and obj.modifiers:
            modifier = obj.modifiers[0]
            modifier_input(modifier, modifier.node_group, "Speed", float(bpy.context.scene.kileido_dc_flow_speed))
            obj.update_tag()


def via_range(result):
    currents = np.asarray(result["via_current"], np.float64)
    return 0.0, max(float(currents.max()) if len(currents) else 0.0, 1e-12)


def _columns(centres, radii, bottoms, tops, sides=16):
    """Closed prisms (vertices, faces as loop starts/totals/indices) around vertical axes."""
    count = len(centres)
    angles = np.linspace(0, 2 * np.pi, sides, endpoint=False)
    ring = np.stack([np.cos(angles), np.sin(angles)], axis=1)
    xy = centres[:, None, :] + radii[:, None, None] * ring[None]
    top = np.concatenate([xy, np.broadcast_to(tops[:, None, None], (count, sides, 1))], axis=2)
    bottom = np.concatenate([xy, np.broadcast_to(bottoms[:, None, None], (count, sides, 1))], axis=2)
    vertices = np.concatenate([top, bottom], axis=1).reshape(-1, 3)
    base = (np.arange(count) * 2 * sides)[:, None]
    k = np.arange(sides)
    quads = np.stack([base + k, base + sides + k, base + sides + (k + 1) % sides, base + (k + 1) % sides], axis=2)
    caps_top = base + k[None]
    caps_bottom = base + sides + k[::-1][None]
    indices = np.concatenate([quads.reshape(count, -1), caps_top, caps_bottom], axis=1).ravel()
    totals = np.tile(np.concatenate([np.full(sides, 4), [sides, sides]]), count)
    starts = np.concatenate([[0], np.cumsum(totals)[:-1]])
    return vertices.astype(np.float32), starts.astype(np.int32), totals.astype(np.int32), indices.astype(np.int32)


def _draw_vias(result):
    """Columns around the barrels, from the top of their top layer's copper to the
    bottom of their bottom layer's, coloured by current (0 to the largest)."""
    rows = np.asarray(result["via"], np.int64)
    spans = result["via_spans"]
    heights = np.array([sorted((board.heights.get(top, 0.0), board.heights.get(bottom, 0.0)))
                        for top, bottom in spans], np.float64).reshape(-1, 2)
    lift = 3e-6 + LIFT_M  # outside the via lands, which stand 3 um out from the copper
    bottoms, tops = heights[:, 0] - lift, heights[:, 1] + lift
    centres = transform.xy_m(rows[:, :2], board.origin_nm).astype(np.float64)
    radii = rows[:, 2] * 0.5e-9 + VIA_HALO_M
    vertices, starts, totals, indices = _columns(centres, radii, bottoms, tops)
    name = "KLS vias dc"
    obj = owned_object(name)
    obj[TAG] = "vias"
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(len(vertices))
    mesh.vertices.foreach_set("co", vertices.ravel())
    mesh.loops.add(len(indices))
    mesh.loops.foreach_set("vertex_index", indices)
    mesh.polygons.add(len(starts))
    mesh.polygons.foreach_set("loop_start", starts)
    mesh.polygons.foreach_set("loop_total", totals)
    low, high = via_range(result)
    colors = _linear(colors_srgb(result["via_current"], low, high, False, "CURRENT"))
    per_vertex = np.repeat(np.column_stack([colors, np.ones(len(colors))]), 32, axis=0).astype(np.float32)
    _write(mesh, "kls_color", "FLOAT_COLOR", "color", per_vertex)
    mesh.update()
    material = _color_material()
    if mesh.materials:
        mesh.materials[0] = material
    else:
        mesh.materials.append(material)
    obj.location.z = 0.0
    show_copy(obj)
    return name


def refresh_markers():
    """Cones at the marked supplies (red) and loads (blue), their tips on the pad or via."""
    if board.collection is None or board.in_snapshot:
        return set()
    setup = board.dc.get("setup") or {}
    rows, owners, sides = board.dc.get("markers") or (np.empty((0, 3)), np.empty(0), np.empty(0))
    terminals = setup.get("terminals", ())
    wanted = set()
    for role, color in MARKER_COLORS.items():
        chosen = np.array([terminals[owner]["role"] == role for owner in owners.tolist()], bool)
        name = f"KLS dc markers {role}"
        if not enabled() or not chosen.any():
            obj = board.collection.all_objects.get(name)
            if obj is not None:
                hide_copy(obj)
            continue
        active = owners[chosen] == bpy.context.scene.kileido_dc_active  # the one clicks add to: larger
        height = np.clip(np.asarray(rows[chosen, 2], np.float64) * 1e-9 * 1.5, 0.8e-3, 3e-3) * np.where(active, 1.4, 1)
        side = sides[chosen]
        up = np.where(side < 0, -1.0, 1.0)
        surface = np.where(side < 0, board.heights.get("B.Cu", 0.0) - 3e-6,
                           board.heights.get("F.Cu", board.thickness_m) + 3e-6)
        tips = transform.xy_m(rows[chosen, :2], board.origin_nm).astype(np.float64)
        _draw_cones(owned_object(name), tips, surface + up * LIFT_M, up, height, color)
        wanted.add(name)
    return wanted


def _draw_cones(obj, tips, tip_z, up, heights, color, sides=12):
    obj[TAG] = "markers"
    count = len(tips)
    angles = np.linspace(0, 2 * np.pi, sides, endpoint=False)
    ring = np.stack([np.cos(angles), np.sin(angles)], axis=1)
    radius = 0.35 * heights
    base = np.concatenate([tips[:, None, :] + radius[:, None, None] * ring[None],
                           np.broadcast_to((tip_z + up * heights)[:, None, None], (count, sides, 1))], axis=2)
    apex = np.concatenate([tips, tip_z[:, None]], axis=1)[:, None, :]
    vertices = np.concatenate([apex, base], axis=1).reshape(-1, 3).astype(np.float32)
    first = (np.arange(count) * (sides + 1))[:, None]
    k = np.arange(sides)
    flip = up[:, None] < 0
    a, b = first + 1 + k, first + 1 + (k + 1) % sides
    sides_faces = np.stack([np.broadcast_to(first, a.shape), np.where(flip, b, a), np.where(flip, a, b)], axis=2)
    cap = np.where(flip, first + 1 + k[None], first + 1 + k[::-1][None])
    indices = np.concatenate([sides_faces.reshape(count, -1), cap], axis=1).ravel().astype(np.int32)
    totals = np.tile(np.concatenate([np.full(sides, 3), [sides]]), count).astype(np.int32)
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(len(vertices))
    mesh.vertices.foreach_set("co", vertices.ravel())
    mesh.loops.add(len(indices))
    mesh.loops.foreach_set("vertex_index", indices)
    mesh.polygons.add(len(totals))
    mesh.polygons.foreach_set("loop_start", np.concatenate([[0], np.cumsum(totals)[:-1]]).astype(np.int32))
    mesh.polygons.foreach_set("loop_total", totals)
    mesh.update()
    material = _flat_material(f"KLS DC marker {obj.name.rsplit(' ', 1)[-1]}", color)
    if mesh.materials:
        mesh.materials[0] = material
    else:
        mesh.materials.append(material)
    obj.location.z = 0.0
    show_copy(obj)


def hide_all():
    """No DC display (an exported package leaves them out, like return-path marks)."""
    if board.collection is None:
        return
    for obj in tuple(board.collection.all_objects):
        if obj.get(TAG):
            hide_copy(obj)


# --- Panel text -----------------------------------------------------------------------

def status_line():
    status = board.dc.get("status") or {}
    state, message = status.get("state", "idle"), status.get("message", "")
    if state == "solving":
        elapsed = status.get("elapsed_s")
        return "SORTTIME", f"Solving: {message}" + (f" ({elapsed:.0f} s)" if elapsed else "")
    return {"done": "CHECKMARK", "error": "ERROR", "waiting": "TIME"}.get(state, "INFO"), message


def _si(value, unit_name):
    for factor, prefix in ((1, ""), (1e-3, "m")):
        if abs(value) >= factor:
            return f"{value / factor:.3g} {prefix}{unit_name}"
    return f"{value / 1e-6:.3g} µ{unit_name}"


def summary_lines():
    """The result in words: supplies, loads with their drop, pair resistances, losses."""
    result = board.dc.get("result")
    if not result:
        return []
    lines = [f"{supply['name']}: {supply['v_oc']:.3g} V source, delivers {_si(supply['i_a'], 'A')}"
             for supply in result["supplies"]]
    for load in result["loads"]:
        lines.append(f"{load['name']}: {_si(load['i_a'], 'A')} at {load['v_mean']:.4g} V, "
                     f"drop {_si(load['drop_mean_v'], 'V')} (worst {_si(load['drop_max_v'], 'V')})")
    for pair in result["pairs"]:
        if pair["r_ohm"] is not None:
            lines.append(f"R {pair['supply']} → {pair['load']}: {_si(pair['r_ohm'], 'Ω')}")
    lines.append(f"Copper loss {_si(result['p_copper_w'], 'W')} (vias {_si(result['p_vias_w'], 'W')})")
    lines.append(f"|J| up to {result['j_max']:.3g} A/mm² in 99.9% of the copper "
                 f"(peak {result.get('j_peak', result['j_max']):.3g} A/mm²)")
    return lines


def timing_line():
    result = board.dc.get("result")
    if not result:
        return ""
    timings = result.get("timings_s", {})
    return (f"{result['cells']:,} cells of {result['cell_nm'] / 1000:.0f} µm, {result['unknowns']:,} unknowns; "
            f"raster {timings.get('raster', 0):.1f} s, solve {timings.get('solve', 0):.1f} s, "
            f"total {timings.get('total', 0):.1f} s")


def top_vias(count=5):
    """(index, text) of the barrels carrying the most current."""
    result = board.dc.get("result")
    if not result or not len(result["via"]):
        return []
    rows = result["via"]
    return [(n, f"({rows[n][0] / 1e6:.2f}, {rows[n][1] / 1e6:.2f}) mm: {_si(float(result['via_current'][n]), 'A')}, "
                f"{_si(float(result['via_power'][n]), 'W')}") for n in range(min(count, len(rows)))]
