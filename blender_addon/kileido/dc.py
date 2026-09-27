"""DC analysis results from the bridge (protocol.dc_setup/dc_status/dc_result_message).

The bridge solves one power net with Fill Resistance's solver (Janik Oltmanns /
B4L, GPL-3.0-or-later) and sends per copper layer the current density |J|, the
potential and the current's direction on a regular grid. Shown on the real layers:

- a colour map per layer ("KLS <layer> dc map"): a flat quad just out from the
  copper, textured with the field; see-through where there is no copper. Current
  density (log scale by default) or voltage drop from the highest supply.
- particles along the current ("KLS <layer> dc arrows"): streamlines traced
  through the current's direction here (flow_paths), one cone walking each with
  the scene time plus a viewport clock, faster where the current is denser. The
  speed is illustrative, not the drift velocity.
- via currents ("KLS vias dc"): a column around each via and plated hole, coloured
  by the current through it.
- the marked supplies and loads ("KLS dc markers ..."): cones pointing at their
  pads and vias, red for supplies, blue for loads.

Maps and arrows follow their copper layer's eye in the Layers list, via currents
the Vias eye. The panel's check box hides them all. Colours are computed here
(numpy), so a new range or field only rewrites the images.
"""

import math
import time

import bpy
import numpy as np

from . import transform
from .highlight import hide_copy, show_copy
from .nodes import _group
from .objects import owned_object, set_modifier, set_visible, write_attribute
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
LEGEND_TEXT = (0.92, 0.92, 0.92)  # sRGB
LEGEND_NOTE = (0.6, 0.6, 0.6)
FLOW_GROUP = "KLS_DC_Flow_v1"
FLOW_STEPS = 40  # streamline points after each seed
FLOW_MIN_POINTS = 4  # shorter streamlines get no particle
FLOW_POINTS_PER_S = 10.0  # a particle passes this many streamline points a second at speed 1
FLOW_FADE = 6.0  # particles grow in and fade out over the first and last 1/6 of their way
FLOW_TICK_S = 1 / 30  # the viewport clock's step
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
_clock = {"now": 0.0, "last": 0.0}  # seconds of viewport flow (flow_group's Clock)


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
                name = _draw_flow(result, index, layer)
                if name:
                    wanted.add(name)
        if bpy.context.scene.kileido_dc_vias and len(result["via"]):
            wanted.add(_draw_vias(result))
        if bpy.context.scene.kileido_dc_legend:
            wanted |= _draw_legend(result)
    wanted |= refresh_markers()
    for obj in tuple(board.collection.all_objects):
        if obj.get(TAG) and obj.name not in wanted:
            _hide(obj)


def _hide(obj):
    if obj.type == "MESH":
        hide_copy(obj)
    else:  # a legend text: nothing to clear
        set_visible(obj, False)


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
    if bpy.context.scene.kileido_dc_legend:
        stale = {obj.name for obj in board.collection.all_objects if obj.get(TAG) == "legend"} - _draw_legend(result)
        for name in stale:  # fewer ticks than before
            _hide(board.collection.all_objects[name])


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


def flow_group():
    """Particles along streamlines. The input holds every streamline's points in order;
    each streamline's first point (`kls_head`) also carries where it starts in that
    list (`kls_start`), how many points it has (`kls_count`) and a phase. Each head
    becomes one cone that walks its streamline at `Rate` points per second, the
    position interpolated between the two points around it, the cone turned along the
    step; it grows in at the start and fades out at the end, then starts over. Time
    is the scene time plus `Clock` (the viewport's clock while the timeline stands)."""
    group, source, sink = _group(FLOW_GROUP, (("Material", "NodeSocketMaterial"),
                                              ("Size", "NodeSocketFloat", 1e-3),
                                              ("Rate", "NodeSocketFloat", FLOW_POINTS_PER_S),
                                              ("Clock", "NodeSocketFloat", 0.0)))
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

    def vector_node(operation, first, second):
        node = nodes.new("ShaderNodeVectorMath")
        node.operation = operation
        links.new(first, node.inputs[0])
        links.new(second, node.inputs["Scale"] if operation == "SCALE" else node.inputs[1])
        return node.outputs["Vector"]

    def attribute(name, data_type="FLOAT"):
        node = nodes.new("GeometryNodeInputNamedAttribute")
        node.data_type = data_type
        node.inputs["Name"].default_value = name
        return node.outputs["Attribute"]

    def sample(index):
        node = nodes.new("GeometryNodeSampleIndex")
        node.data_type, node.domain = "FLOAT_VECTOR", "POINT"
        links.new(source.outputs["Geometry"], node.inputs["Geometry"])
        links.new(nodes.new("GeometryNodeInputPosition").outputs["Position"], node.inputs["Value"])
        links.new(index, node.inputs["Index"])
        return node.outputs["Value"]

    heads = nodes.new("GeometryNodeSeparateGeometry")
    heads.domain = "POINT"
    links.new(source.outputs["Geometry"], heads.inputs["Geometry"])
    links.new(attribute("kls_head", "BOOLEAN"), heads.inputs["Selection"])
    count, start = attribute("kls_count"), attribute("kls_start")
    time = math_node("ADD", nodes.new("GeometryNodeInputSceneTime").outputs["Seconds"], source.outputs["Clock"])
    progress = math_node("FRACT", math_node("ADD", math_node("DIVIDE", math_node(
        "MULTIPLY", time, source.outputs["Rate"]), count), attribute("kls_phase")))
    along = math_node("MULTIPLY", progress, math_node("SUBTRACT", count, 1.0))
    first = math_node("FLOOR", along)
    second = math_node("MINIMUM", math_node("ADD", first, 1.0), math_node("SUBTRACT", count, 1.0))
    here = sample(math_node("ADD", start, first))
    step = vector_node("SUBTRACT", sample(math_node("ADD", start, second)), here)
    position = vector_node("ADD", here, vector_node("SCALE", step, math_node("SUBTRACT", along, first)))
    fade = math_node("MINIMUM", math_node("MULTIPLY", math_node("MINIMUM", progress, math_node(
        "SUBTRACT", 1.0, progress)), FLOW_FADE), 1.0)
    moved = nodes.new("GeometryNodeSetPosition")
    links.new(heads.outputs["Selection"], moved.inputs["Geometry"])
    links.new(position, moved.inputs["Position"])
    cone = nodes.new("GeometryNodeMeshCone")
    cone.inputs["Vertices"].default_value = 10
    cone.inputs["Radius Top"].default_value = 0.0
    cone.inputs["Radius Bottom"].default_value = 0.3
    cone.inputs["Depth"].default_value = 1.0
    align = nodes.new("FunctionNodeAlignRotationToVector")
    align.axis = "Z"
    links.new(step, align.inputs["Vector"])
    instances = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(moved.outputs["Geometry"], instances.inputs["Points"])
    links.new(cone.outputs["Mesh"], instances.inputs["Instance"])
    links.new(align.outputs["Rotation"], instances.inputs["Rotation"])
    links.new(math_node("MULTIPLY", source.outputs["Size"], fade), instances.inputs["Scale"])
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


def flow_paths(result, index, spacing_nm):
    """Streamlines of one layer's current, as (n, 2) arrays of KiCad nm.

    Seeds lie every `spacing_nm` (a staggered, slightly jittered lattice) on copper
    where the current is not weak; each is traced FLOW_STEPS steps along J (bilinear
    field, midpoint rule) until it leaves the copper or the current fades. The
    steps are equal in time, at a speed of sqrt(|J| / top) clipped to 0.1 .. 1, so a
    particle walking the points at a steady rate runs faster where the current is
    denser (illustrative, not the electrons' drift velocity)."""
    grid = result["grid"]
    pitch = float(grid["pitch_nm"])
    fields = [np.asarray(result[key][index], np.float64) for key in ("j", "jx", "jy")]
    copper = np.isfinite(fields[0])
    j, jx, jy = (np.nan_to_num(values) for values in fields)
    rows, columns = j.shape
    top = max(float(result["j_max"]), 1e-12)
    spacing = max(1.0, spacing_nm / pitch)  # in cells
    random = np.random.default_rng(1)
    seeds = []
    for number, row in enumerate(np.arange(spacing / 2, rows, spacing)):
        for column in np.arange(spacing / 2 + (number % 2) * spacing / 2, columns, spacing):
            seeds.append((column, row))
    if not seeds:
        return []
    position = np.asarray(seeds) + random.uniform(-0.35, 0.35, (len(seeds), 2)) * spacing

    def velocity(points):
        """Speed-scaled direction and whether the point is on copper with current."""
        x, y = points[:, 0] - 0.5, points[:, 1] - 0.5  # cell centres at integer + 0.5
        x0 = np.clip(np.floor(x).astype(np.int64), 0, columns - 2)
        y0 = np.clip(np.floor(y).astype(np.int64), 0, rows - 2)
        fx, fy = np.clip(x - x0, 0, 1), np.clip(y - y0, 0, 1)
        total, weight = np.zeros((len(points), 3)), np.zeros(len(points))
        for dy, dx, w in ((0, 0, (1 - fx) * (1 - fy)), (0, 1, fx * (1 - fy)), (1, 0, (1 - fx) * fy),
                          (1, 1, fx * fy)):
            on = copper[y0 + dy, x0 + dx]
            w = w * on
            total += w[:, None] * np.column_stack([j[y0 + dy, x0 + dx], jx[y0 + dy, x0 + dx], jy[y0 + dy, x0 + dx]])
            weight += w
        near = copper[np.clip(np.round(y).astype(np.int64), 0, rows - 1), np.clip(np.round(x).astype(np.int64),
                                                                                  0, columns - 1)]
        with np.errstate(invalid="ignore", divide="ignore"):
            magnitude, vx, vy = (total / weight[:, None]).T
            norm = np.hypot(vx, vy)
            speed = np.clip(np.sqrt(np.maximum(magnitude, 0) / top), 0.1, 1.0)
            direction = np.column_stack([vx, vy]) / norm[:, None]
        alive = near & (weight > 0) & (norm > 0) & (magnitude >= WEAK_ARROW * top)
        return np.nan_to_num(direction * speed[:, None]), alive  # no current: no step

    _, alive = velocity(position)
    position = position[alive]
    step = spacing / 5  # cells per time step at full speed
    path = np.empty((len(position), FLOW_STEPS + 1, 2))
    path[:, 0] = position
    count = np.ones(len(position), np.int64)
    alive = np.ones(len(position), bool)
    for number in range(FLOW_STEPS):
        first, ok_first = velocity(position)
        middle, ok_middle = velocity(position + 0.5 * step * first)
        moved = position + step * middle
        _, ok_moved = velocity(moved)
        alive &= ok_first & ok_middle & ok_moved
        position = np.where(alive[:, None], moved, position)
        path[:, number + 1] = position
        count += alive
    origin = np.array([grid["x0_nm"], grid["y0_nm"]])
    return [origin + path[n, :count[n]] * pitch for n in range(len(path)) if count[n] >= FLOW_MIN_POINTS]


def _write(mesh, name, data_type, key, values):
    """A per-vertex vector or colour attribute (objects.write_attribute writes scalars)."""
    attribute = mesh.attributes.get(name) or mesh.attributes.new(name, data_type, "POINT")
    attribute.data.foreach_set(key, np.ascontiguousarray(values, np.float32).ravel())


def _draw_flow(result, index, layer):
    spacing = bpy.context.scene.kileido_dc_arrow_mm * 1e6
    paths = flow_paths(result, index, spacing)
    name = f"KLS {layer} dc arrows"
    if not paths:
        return None
    counts = np.array([len(path) for path in paths])
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    points = np.concatenate(paths)
    obj = owned_object(name)
    obj[TAG] = layer
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(len(points))
    coordinates = np.zeros((len(points), 3), np.float32)
    coordinates[:, :2] = transform.xy_m(points, board.origin_nm)
    size = 0.7 * spacing * 1e-9
    coordinates[:, 2] = outward(layer) * 0.3 * size  # cones lie on the map
    mesh.vertices.foreach_set("co", coordinates.ravel())
    head = np.zeros(len(points), bool)
    head[starts] = True
    write_attribute(mesh, "kls_head", "BOOLEAN", head)
    write_attribute(mesh, "kls_start", "FLOAT", np.repeat(starts, counts).astype(np.float32))
    write_attribute(mesh, "kls_count", "FLOAT", np.repeat(counts, counts).astype(np.float32))
    phase = np.random.default_rng(2).uniform(0, 1, len(paths))
    write_attribute(mesh, "kls_phase", "FLOAT", np.repeat(phase, counts).astype(np.float32))
    mesh.update()
    obj.location.z = _surface_z(layer, ARROW_LIFT_M)
    set_modifier(obj, flow_group(), _flat_material("KLS DC arrows", ARROW_COLOR),
                 {"Size": size, "Rate": flow_rate(), "Clock": _clock["now"]})
    show_copy(obj)
    start_flow()
    return name


def flow_rate():
    return FLOW_POINTS_PER_S * float(bpy.context.scene.kileido_dc_flow_speed)


def _flow_objects():
    if board.collection is None:
        return []
    return [obj for obj in board.collection.all_objects
            if obj.get(TAG) and obj.name.endswith(" dc arrows") and obj.modifiers and not obj.hide_get()]


def set_flow_speed():
    from .nodes import modifier_input
    for obj in _flow_objects():
        modifier = obj.modifiers[0]
        modifier_input(modifier, modifier.node_group, "Rate", flow_rate())
        obj.update_tag()


def start_flow():
    """Keep the particles moving in the viewport (a timer advances `Clock`)."""
    if bpy.app.background or bpy.app.timers.is_registered(_flow_tick):
        return
    _clock["last"] = time.monotonic()
    bpy.app.timers.register(_flow_tick, first_interval=FLOW_TICK_S)


def stop_flow():
    if bpy.app.timers.is_registered(_flow_tick):
        bpy.app.timers.unregister(_flow_tick)


def _cycles_viewport():
    from .objects import view3d_spaces
    return any(space.shading.type == "RENDERED" for space in view3d_spaces())


def _flow_tick():
    """While the timeline stands still, advance the particles' clock: the flow runs
    in the viewport. Paused in a Cycles viewport (each step would restart its
    samples) and while the timeline plays (the scene time moves them then)."""
    try:
        objects = _flow_objects()
        if not objects:
            return None  # no arrows: start_flow starts again with the next ones
        now = time.monotonic()
        elapsed, _clock["last"] = now - _clock["last"], now
        scene = bpy.context.scene
        screen = bpy.context.screen
        if (not scene.kileido_dc_flow or _cycles_viewport() or
                (screen is not None and screen.is_animation_playing)):
            return FLOW_TICK_S
        _clock["now"] += min(elapsed, 0.2)
        from .nodes import modifier_input
        for obj in objects:
            modifier = obj.modifiers[0]
            modifier_input(modifier, modifier.node_group, "Clock", _clock["now"])
            obj.update_tag()
    except ReferenceError:  # the board's data went away (a file load)
        return None
    return FLOW_TICK_S


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
            _hide(obj)


# --- The legend beside the board ------------------------------------------------------

def legend_ticks(low, high, log):
    """(position 0..1 up the bar, label) of the tick values: both ends of the range,
    and between them every decade on a log scale, round steps on a linear one."""
    if log:
        values = [10.0 ** k for k in range(math.floor(math.log10(low)), math.ceil(math.log10(high)) + 1)]
        position = lambda value: math.log(value / low) / math.log(high / low)  # noqa: E731
    else:
        raw = (high - low) / 4
        step = 10 ** math.floor(math.log10(raw))
        step *= next(factor for factor in (1, 2, 5, 10) if factor * step >= raw)
        values = [step * k for k in range(math.ceil(low / step), math.floor(high / step) + 1)]
        position = lambda value: (value - low) / (high - low)  # noqa: E731
    ticks = [(position(value), f"{value:g}") for value in values if 0.07 <= position(value) <= 0.93]
    return [(0.0, _format(low))] + ticks + [(1.0, _format(high))]


def legend_lines(result):
    """(title, unit line, footer lines) of the legend card."""
    low, high, log = value_range(result)
    if field() == "CURRENT":
        title, scale = "Current density |J|", "A/mm², log scale" if log else "A/mm²"
    else:
        title, scale = "Voltage drop", f"mV below {float(result['v_ref']):.3g} V"
    draw = sum(load["i_a"] for load in result["loads"])
    footer = [f"DC, steady current: {result['net']}, {_si(draw, 'A')}"]
    if len(result["via"]) and bpy.context.scene.kileido_dc_vias:
        footer.append(f"Vias: 0 to {_si(via_range(result)[1], 'A')} (same colours)")
    if bpy.context.scene.kileido_dc_arrows:
        footer.append("Arrows: current direction")
    return title, scale, footer


def _legend_image(name, log):
    image = bpy.data.images.get("KLS DC legend")
    if image is None:
        image = bpy.data.images.new("KLS DC legend", 1, 256, alpha=True, float_buffer=True)
    pixels = np.ones((256, 1, 4), np.float32)
    pixels[:, 0, :3] = _linear(_lut(name))
    image.pixels.foreach_set(pixels.ravel())
    image.update()
    return image


def _text(name, body, x, y, z, size, color=LEGEND_TEXT):
    """A flat text object (the board collection's), left-aligned, centred on y."""
    obj = board.collection.all_objects.get(name)
    if obj is None:
        from .objects import link_owned
        obj = bpy.data.objects.new(name, bpy.data.curves.new(name, "FONT"))
        obj["kileido_owned"] = 1
        link_owned(obj)
    obj[TAG] = "legend"
    curve = obj.data
    curve.body, curve.size = body, size
    curve.align_x, curve.align_y = "LEFT", "CENTER"
    material = _flat_material("KLS DC legend text", color)
    if curve.materials:
        curve.materials[0] = material
    else:
        curve.materials.append(material)
    obj.location = (x, y, z)
    show_copy(obj)
    return name


def _draw_legend(result):
    """The colour bar with its values, right of the board outline, on its top face."""
    from .objects import outline_bounds
    bounds = outline_bounds()
    if bounds is None:
        return set()
    left, bottom, right, top = bounds
    extent = max(right - left, top - bottom)
    width, height = 0.035 * extent, 0.55 * (top - bottom)
    x0, y0 = right + 0.06 * extent, (bottom + top) / 2 - height / 2
    z = board.heights.get("F.Cu", board.thickness_m) + LIFT_M
    text = 0.026 * extent
    low, high, log = value_range(result)
    name = "KLS dc legend"
    obj = owned_object(name)
    obj[TAG] = "legend"
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(4)
    mesh.vertices.foreach_set("co", np.array([(x0, y0, 0), (x0 + width, y0, 0), (x0 + width, y0 + height, 0),
                                              (x0, y0 + height, 0)], np.float32).ravel())
    mesh.loops.add(4)
    mesh.loops.foreach_set("vertex_index", np.arange(4, dtype=np.int32))
    mesh.polygons.add(1)
    mesh.polygons.foreach_set("loop_start", np.zeros(1, np.int32))
    mesh.polygons.foreach_set("loop_total", np.full(1, 4, np.int32))
    uv = mesh.uv_layers.new(name="UVMap") if not mesh.uv_layers else mesh.uv_layers[0]
    uv.data.foreach_set("uv", np.array([(0, 0), (1, 0), (1, 1), (0, 1)], np.float32).ravel())
    mesh.update()
    material = _map_material("legend", _legend_image(field(), log))
    material.node_tree.nodes["KLS map"].interpolation = "Linear"
    if mesh.materials:
        mesh.materials[0] = material
    else:
        mesh.materials.append(material)
    obj.location.z = z
    show_copy(obj)
    names = {name}
    title, scale, footer = legend_lines(result)
    names.add(_text("KLS dc legend title", title, x0, y0 + height + 2.6 * text, z, 1.2 * text))
    names.add(_text("KLS dc legend unit", scale, x0, y0 + height + 1.2 * text, z, text))
    for number, (position, label) in enumerate(legend_ticks(low, high, log)):
        names.add(_text(f"KLS dc legend tick {number}", f"– {label}", x0 + width, y0 + position * height, z, text))
    for number, line in enumerate(footer):
        names.add(_text(f"KLS dc legend note {number}", line, x0, y0 - (1.4 + 1.3 * number) * text, z,
                        0.8 * text, LEGEND_NOTE))
    return names


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
