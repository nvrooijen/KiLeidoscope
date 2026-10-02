"""Copper balance in the viewer: the analysis (copper_balance.py) off the live timer,
a heatmap over one copper layer, and the sidebar's Copper balance panel.

Every frame applied passes `observe`; copper frames are kept (no copy) and, while
the panel's check is on, restart a short wait. When copper has been quiet for
DELAY_S the analysis runs on a worker thread (the C scanline pass releases the
GIL) and the main thread shows its result. Routing in KiCad sends copper every
0.5 s, so the analysis waits for the route to settle and never runs per poll.

The heatmap is its own collection outside the board: clicks and board exports
pass it by. It is drawn in front, so an inner layer's map shows through the board.
"""

import threading

import bpy
import numpy as np

from . import copper_balance, layers
from .objects import hide
from .state import board

COLLECTION = "KiLeidoscope copper balance"
OBJECT = "KiLeidoscope copper heatmap"
MATERIAL = "KiLeidoscope copper heatmap"
DELAY_S = 1.0  # quiet copper before an analysis
POLL_S = 0.1  # while the worker runs
LIFT_M = 60e-6  # outer layers' maps: outside the copper, mask and silkscreen
OPACITY = 0.8

_frames = copper_balance.CopperFrames()
_state = {"result": None, "pairs": [], "error": "", "version": None, "tile_mm": None}
_job = {"thread": None, "version": None, "tile_mm": None, "result": None, "error": ""}
_layer_items = []  # Blender keeps only references to enum strings: hold them here


def enabled():
    return bool(getattr(bpy.context.scene, "kileido_balance", False))


def observe(header, arrays):
    """Every applied frame (apply.apply_frame): keep copper, and analyse once it settles."""
    if _frames.observe(header, arrays) and enabled():
        schedule()


def schedule(delay=DELAY_S):
    if bpy.app.timers.is_registered(_start):
        bpy.app.timers.unregister(_start)
    bpy.app.timers.register(_start, first_interval=delay)


def _tile_mm():
    return float(getattr(bpy.context.scene, "kileido_balance_tile_mm", copper_balance.TILE_MM))


def _start():
    """Timer: start the worker on the frames as they are now (later frames start another)."""
    if not enabled():
        return None
    if _job["thread"] is not None and _job["thread"].is_alive():
        return POLL_S  # one analysis at a time
    groups, heights = _frames.inputs()
    tile_mm = _tile_mm()
    _job.update(version=_frames.version, tile_mm=tile_mm, result=None, error="")

    def work():
        try:
            _job["result"] = copper_balance.analyze(groups, heights, tile_mm)
        except Exception as exc:  # no outline yet, a malformed frame: shown in the panel
            _job["error"] = str(exc) or type(exc).__name__

    _job["thread"] = threading.Thread(target=work, name="KiLeidoscope copper balance", daemon=True)
    _job["thread"].start()
    if not bpy.app.timers.is_registered(_collect):
        bpy.app.timers.register(_collect, first_interval=POLL_S)
    return None


def _collect():
    """Timer: show a finished analysis; a stale one (copper or tile size changed meanwhile) runs again."""
    thread = _job["thread"]
    if thread is None:
        return None
    if thread.is_alive():
        return POLL_S
    _job["thread"] = None
    _state.update(result=_job["result"], error=_job["error"], version=_job["version"], tile_mm=_job["tile_mm"])
    if enabled() and (_job["version"] != _frames.version or _job["tile_mm"] != _tile_mm()):
        schedule()
    refresh()
    return None


def wait(timeout=30.0):
    """Analyse now and show the result, without the timers (headless tests and tools)."""
    for timer in (_start, _collect):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    for _ in range(2):  # one already running first, then one on the frames as they are now
        if _job["thread"] is None and _start() is not None:
            break
        if _job["thread"] is not None:
            _job["thread"].join(timeout)
            if bpy.app.timers.is_registered(_collect):
                bpy.app.timers.unregister(_collect)
            _collect()
        if not bpy.app.timers.is_registered(_start):
            break
        bpy.app.timers.unregister(_start)  # stale: analyse again


def status():
    """Panel text: what the analysis is doing, or None with a current result."""
    if _job["thread"] is not None or bpy.app.timers.is_registered(_start):
        return "Analyzing copper…"
    if _state["error"]:
        return f"Copper balance unavailable: {_state['error']}"
    if _state["result"] is None:
        return "Waiting for a board"
    return None


def result():
    return _state["result"]


def pairs():
    return _state["pairs"]


# --- Heatmap ------------------------------------------------------------------------------------

def selected_layer():
    """The panel's heatmap layer, or the top layer when it has none (yet)."""
    found = _state["result"]
    if found is None:
        return None
    layer = getattr(bpy.context.scene, "kileido_balance_layer", "")
    return layer if layer in found.layers else found.order[0]


def refresh():
    """Pairs for the panel's threshold, and the heatmap for its layer; hidden when off."""
    found = _state["result"]
    scene = bpy.context.scene
    threshold = float(getattr(scene, "kileido_balance_threshold", copper_balance.THRESHOLD_PCT))
    _state["pairs"] = copper_balance.pairs(found, threshold) if found is not None else []
    obj = bpy.data.objects.get(OBJECT)
    layer = selected_layer()
    shown = (enabled() and getattr(scene, "kileido_balance_heatmap", True) and layer is not None
             and board.collection is not None)
    if not shown:
        if obj is not None:
            hide(obj, True)
            obj.hide_render = True
        _redraw()
        return
    obj = obj or _new_object(scene)
    if obj.name not in _collection(scene).objects:
        _collection(scene).objects.link(obj)
    _draw(obj, found, layer)
    hide(obj, False)
    obj.hide_render = False
    _redraw()


def _collection(scene):
    collection = bpy.data.collections.get(COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION)
        collection["kileido_owned_balance"] = 1
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    return collection


def _new_object(scene):
    mesh = bpy.data.meshes.get(OBJECT) or bpy.data.meshes.new(OBJECT)
    obj = bpy.data.objects.new(OBJECT, mesh)
    obj.hide_select = True
    obj.show_in_front = True  # an inner layer's map shows through the board
    _collection(scene).objects.link(obj)
    return obj


def _material(image):
    """The image's colour as unlit emission, its alpha as coverage."""
    material = bpy.data.materials.get(MATERIAL)
    if material is None:
        material = bpy.data.materials.new(MATERIAL)
        material.use_nodes = True
        tree = material.node_tree
        tree.nodes.clear()
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        emission = tree.nodes.new("ShaderNodeEmission")
        mix = tree.nodes.new("ShaderNodeMixShader")
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.interpolation = "Closest"  # one colour per tile
        texture.extension = "CLIP"
        tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
        tree.links.new(texture.outputs["Alpha"], mix.inputs[0])
        tree.links.new(transparent.outputs[0], mix.inputs[1])
        tree.links.new(emission.outputs[0], mix.inputs[2])
        tree.links.new(mix.outputs[0], output.inputs["Surface"])
        material.surface_render_method = "BLENDED"
        material.diffuse_color = (*copper_balance.RAMP[len(copper_balance.RAMP) // 2], OPACITY)
    texture = next(node for node in material.node_tree.nodes if node.type == "TEX_IMAGE")
    texture.image = image
    return material


def _image(pixels):
    """`pixels` (rows, columns, 4; row 0 on top) into the heatmap image (Blender: row 0 at the bottom)."""
    rows, columns = pixels.shape[:2]
    image = bpy.data.images.get(OBJECT)
    if image is None:
        image = bpy.data.images.new(OBJECT, columns, rows, alpha=True)
    elif tuple(image.size) != (columns, rows):
        image.scale(columns, rows)
    image.pixels.foreach_set(np.ascontiguousarray(pixels[::-1], np.float32).ravel())
    image.update()
    return image


def _draw(obj, found, layer):
    """A quad over the tile grid at the layer's height, textured with its map."""
    grid = found.grid
    ox, oy = board.origin_nm
    xmin, xmax = (grid.x0 - ox) * 1e-9, (grid.x0 + grid.columns * grid.tile_nm - ox) * 1e-9
    ymax, ymin = -(grid.y0 - oy) * 1e-9, -(grid.y0 + grid.rows * grid.tile_nm - oy) * 1e-9  # KiCad y points down
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(4)
    mesh.vertices.foreach_set("co", np.array((xmin, ymin, 0, xmax, ymin, 0, xmax, ymax, 0, xmin, ymax, 0),
                                             dtype=np.float32))
    mesh.loops.add(4)
    mesh.loops.foreach_set("vertex_index", np.arange(4, dtype=np.int32))
    mesh.polygons.add(1)
    mesh.polygons.foreach_set("loop_start", np.zeros(1, dtype=np.int32))
    mesh.polygons.foreach_set("loop_total", np.full(1, 4, dtype=np.int32))
    uv = mesh.uv_layers.get("KiCad plot") or mesh.uv_layers.new(name="KiCad plot")
    uv.data.foreach_set("uv", np.array((0, 0, 1, 0, 1, 1, 0, 1), dtype=np.float32))
    mesh.update()
    image = _image(copper_balance.heatmap(found, layer, OPACITY))
    material = _material(image)
    if not mesh.materials:
        mesh.materials.append(material)
    obj.location.z = _height(layer)
    obj["kls_balance_layer"] = layer


def _height(layer):
    """At the layer's copper; the outer layers' maps just outside the board's surfaces."""
    heights = board.heights
    if not heights or layer not in heights:
        return 0.0
    if heights[layer] >= max(heights.values()):
        return max(board.thickness_m, heights[layer]) + LIFT_M
    if heights[layer] <= min(heights.values()):
        return min(0.0, heights[layer]) - LIFT_M
    return heights[layer]


def _redraw():
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# --- Panel -------------------------------------------------------------------------------------

def _layer_enum(_scene, _context):
    """The analysed copper layers, top to bottom, under KiCad's names. Numbers follow the
    layer, not its place in the list, so the choice survives a board with other layers."""
    found = _state["result"]
    _layer_items.clear()
    for index, name in enumerate(found.order if found is not None else ("F.Cu",)):
        number = layers.COPPER_LAYERS.index(name) if name in layers.COPPER_LAYERS else 100 + index
        _layer_items.append((name, board.layer_names.get(name, name), f"Copper density of {name}", number))
    return _layer_items


def _tile_text(tile):
    x, y = (value * 1e-6 for value in tile.center_nm)
    return f"({x:.1f}, {y:.1f}) mm: {tile.top_pct:.0f} % vs {tile.bottom_pct:.0f} %"


class KILEIDO_PT_balance(bpy.types.Panel):
    """Copper per layer, the mirrored pairs and a density heatmap (copper_balance.py)."""
    bl_label = "Copper balance"
    bl_idname = "KILEIDO_PT_balance"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "KiLeidoscope"
    bl_parent_id = "KILEIDO_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene, "kileido_balance", text="")

    def draw(self, context):
        layout, scene = self.layout, context.scene
        layout.active = enabled()
        row = layout.row(align=True)
        row.prop(scene, "kileido_balance_heatmap", text="Heatmap")
        row.prop(scene, "kileido_balance_layer", text="")
        row = layout.row(align=True)
        row.prop(scene, "kileido_balance_tile_mm", text="Tile")
        row.prop(scene, "kileido_balance_threshold", text="Pair limit")
        if not enabled():
            return
        text = status()
        if text:
            layout.label(text=text)
        found = _state["result"]
        if found is None:
            return
        layout.label(text=f"Heatmap: light 0 % to dark 100 % copper per {found.grid.tile_nm * 1e-6:g} mm tile")
        box = layout.box()
        column = box.column(align=True)
        for name in found.order:
            split = column.split(factor=0.6)
            split.label(text=board.layer_names.get(name, name))
            split.label(text=f"{found.layers[name].percent:.1f} %")
        for pair in _state["pairs"]:
            box = layout.box()
            column = box.column(align=True)
            names = (board.layer_names.get(pair.top, pair.top), board.layer_names.get(pair.bottom, pair.bottom))
            column.label(text=f"{names[0]} / {names[1]}: {pair.difference:.1f} pp",
                         icon="ERROR" if pair.flagged else "CHECKMARK")
            for tile in pair.worst:
                column.label(text=_tile_text(tile))


def _changed_tile(_scene, _context):
    if enabled():
        schedule(POLL_S)


def _toggled(_scene, _context):
    if enabled() and (_state["version"] != _frames.version or _state["tile_mm"] != _tile_mm()):
        schedule(POLL_S)
    refresh()


def properties():
    """The Scene properties of the panel, by attribute name."""
    return {
        "kileido_balance": bpy.props.BoolProperty(
            name="Copper balance", default=False,
            description="Copper % per layer, a tiled density map and the mirrored-layer check, "
                        "worked out a moment after the copper stops changing",
            update=_toggled),
        "kileido_balance_heatmap": bpy.props.BoolProperty(
            name="Heatmap", default=True, description="Show the chosen layer's copper density over the board",
            update=lambda self, context: refresh()),
        "kileido_balance_layer": bpy.props.EnumProperty(
            name="Layer", items=_layer_enum, default=0, description="Copper layer the heatmap shows",
            update=lambda self, context: refresh()),
        "kileido_balance_tile_mm": bpy.props.FloatProperty(
            name="Tile size", default=copper_balance.TILE_MM, min=1.0, max=50.0, step=50, precision=1,
            description="Side of the density map's square tiles, in mm", update=_changed_tile),
        "kileido_balance_threshold": bpy.props.FloatProperty(
            name="Pair limit", default=copper_balance.THRESHOLD_PCT, min=0.0, max=100.0, step=100, precision=0,
            description="Flag mirrored layers (L1 and Ln, L2 and Ln-1, ...) whose copper differs by more "
                        "than this many percentage points", update=lambda self, context: refresh()),
    }


def register():
    bpy.utils.register_class(KILEIDO_PT_balance)
    for name, prop in properties().items():
        setattr(bpy.types.Scene, name, prop)


def unregister():
    for timer in (_start, _collect):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    for name in properties():
        delattr(bpy.types.Scene, name)
    bpy.utils.unregister_class(KILEIDO_PT_balance)
