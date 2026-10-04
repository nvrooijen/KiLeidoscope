"""Bookmark tabs on the KiLeidoscope sidebar's left edge. Each opens its column left of
the KiLeidoscope panel (IMS, Flex), one at a time, and the sidebar widens to make room;
clicking the open tab again closes it. A mode switched on in its column keeps its tab lit
(a bright stripe on its outer edge) once the column is closed; one the board cannot have
is greyed.

The tabs are drawn in the 3D view (Blender's panels cannot colour a button or turn its
text), so they have no tooltip or hover. A press on one reaches `KILEIDO_OT_column_tab`
through the add-on's 3D View keymap, before it can become the click that selects in
KiCad; anywhere else the press passes through.

Blender has no API for a region's width. The sidebar's stored width (ARegion.sizex,
unscaled pixels) is written directly, at the offset measured for each Blender version
(SIZEX_OFFSET), and only when the value there is the sidebar's current width. Anything
else, and the column still opens; the sidebar is then widened by hand.
"""

import ctypes
import math

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

# (key, label, colour): the order they hang from the top.
TABS = (("IMS", "IMS", (0.38, 0.56, 0.78)),  # aluminium blue
        ("FLEX", "Flex", (0.82, 0.52, 0.14)))  # polyimide amber
NONE = "NONE"
GREY = (0.42, 0.42, 0.42)  # a tab whose mode this board cannot have
# key -> a function telling the tab's mode: "on", "off" or "unavailable" (set by the add-on).
STATES = {}
CATEGORY = "KiLeidoscope"  # the sidebar tab the bookmarks belong to

SIZEX_OFFSET = {(5, 1): 198}  # bytes into ARegion; measured by scanning at two UI scales
MIN_SIDEBAR_PX = 200  # unscaled: the narrowest a closing column leaves the sidebar
MAX_AREA_SHARE = 0.7  # the widened sidebar takes at most this much of the 3D view

# Unscaled pixels.
TAB_TOP = 320  # below the navigation gizmo and its buttons
TAB_WIDTH = 22
TAB_TUCK = 8  # how far a tab runs on under the panel
TAB_OUT = 5  # how much further the open tab sticks out
TAB_PAD = 12  # text to tab end
TAB_GAP = 6
TAB_RADIUS = 5
TAB_STRIPE = 3  # the lit stripe of a mode that is on
TEXT_SIZE = 12
FONT = 0

_handle = None


def _scale():
    return bpy.context.preferences.system.ui_scale or 1.0


def _regions(area):
    """The 3D view's main region and its sidebar."""
    found = {region.type: region for region in area.regions}
    return found.get("WINDOW"), found.get("UI")


def column_items():
    return [(NONE, "None", "No column open")] + [(key, label, f"The {label} column") for key, label, _ in TABS]


def tab_rects(area):
    """(key, label, colour, (x0, y0, x1, y1)) of each tab in the main region's pixels;
    none while the sidebar is hidden or shows another tab."""
    window, ui = _regions(area)
    if window is None or ui is None or ui.width <= 1 or ui.active_panel_category != CATEGORY:
        return []
    scale = _scale()
    right = ui.x - window.x + TAB_TUCK * scale
    left = right - (TAB_WIDTH + TAB_TUCK) * scale
    top = window.height - TAB_TOP * scale
    blf.size(FONT, TEXT_SIZE * scale)
    rects = []
    for key, label, color in TABS:
        length = blf.dimensions(FONT, label)[0] + 2 * TAB_PAD * scale
        rects.append((key, label, color, (left, top - length, right, top)))
        top -= length + TAB_GAP * scale
    return rects


def _outline(x0, y0, x1, y1, radius, steps=6):
    """A tab's outline: rounded on the left, square where it meets the panel."""
    points = [(x1, y0), (x1, y1)]
    for centre_y, start in ((y1 - radius, 90), (y0 + radius, 180)):
        for step in range(steps + 1):
            angle = math.radians(start + 90 * step / steps)
            points.append((x0 + radius + radius * math.cos(angle), centre_y + radius * math.sin(angle)))
    return points


def _quad(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]


def tab_state(key):
    """"on", "off" or "unavailable": what the tab's mode is for the board shown."""
    state = STATES.get(key)
    return state() if state is not None else "off"


def _draw():
    area = bpy.context.area
    if area is None:
        return
    rects = tab_rects(area)
    if not rects:
        return
    scale = _scale()
    active = bpy.context.scene.kileido_column
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    blf.size(FONT, TEXT_SIZE * scale)
    ascent = blf.dimensions(FONT, "H")[1]
    for key, label, color, (x0, y0, x1, y1) in rects:
        is_open = key == active
        mode = tab_state(key)
        if is_open:
            x0 -= TAB_OUT * scale
        if mode == "unavailable":
            color = GREY
        outline = _outline(x0, y0, x1, y1, TAB_RADIUS * scale)
        centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        triangles = [vertex for a, b in zip(outline, outline[1:] + outline[:1]) for vertex in (centre, a, b)]
        shader.uniform_float("color", (*color, 1.0 if is_open or mode == "on" else 0.55))
        batch_for_shader(shader, "TRIS", {"pos": triangles}).draw(shader)
        if mode == "on":  # lit along its outer edge, open or closed
            inset = TAB_RADIUS * scale
            stripe = (x0, y0 + inset, x0 + TAB_STRIPE * scale, y1 - inset)
            shader.uniform_float("color", (1.0, 0.95, 0.75, 1.0))
            batch_for_shader(shader, "TRIS", {"pos": _quad(*stripe)}).draw(shader)
        # Read top to bottom, like Blender's own sidebar tabs: glyph tops face the panel.
        text_x = (x0 + x1 - TAB_TUCK * scale) / 2 - ascent / 2
        blf.color(FONT, 1.0, 1.0, 1.0, 1.0 if is_open or mode == "on" else 0.8)
        blf.enable(FONT, blf.ROTATION)
        blf.rotation(FONT, -math.pi / 2)
        blf.position(FONT, text_x, y1 - TAB_PAD * scale, 0)
        blf.draw(FONT, label)
        blf.disable(FONT, blf.ROTATION)
    gpu.state.blend_set("NONE")


def _width_cell(region):
    """The sidebar's stored width as a ctypes cell, or None when this Blender's layout
    is unknown or the value there is not the width Blender draws the sidebar at."""
    offset = SIZEX_OFFSET.get(tuple(bpy.app.version[:2]))
    if offset is None or region is None or region.type != "UI":
        return None
    cell = ctypes.c_short.from_address(region.as_pointer() + offset)
    scale = _scale()
    if cell.value <= 0 or abs(cell.value * scale - region.width) > 2 * scale + 1:
        return None
    return cell


def _relayout():
    """Blender lays every region out again, at its stored width, when the UI scale is
    set; setting it to its own value does that without changing anything else."""
    view = bpy.context.preferences.view
    view.ui_scale = view.ui_scale


def open_column(context, key) -> bool:
    """Open `key`'s column, or close it when it is open; switching between columns keeps
    the width. False when the sidebar could not be resized."""
    scene = context.scene
    before = scene.kileido_column
    after = NONE if before == key else key
    scene.kileido_column = after
    context.area.tag_redraw()
    if (before == NONE) == (after == NONE):
        return True
    cell = _width_cell(_regions(context.area)[1])
    if cell is None:
        return False
    if after != NONE:
        cell.value = min(cell.value * 2, int(context.area.width * MAX_AREA_SHARE / _scale()))
    else:
        cell.value = max(cell.value // 2, MIN_SIDEBAR_PX)
    _relayout()
    return True


class KILEIDO_OT_column_tab(bpy.types.Operator):
    bl_idname = "kileido.column_tab"
    bl_label = "Open column"
    bl_description = "Open or close a column left of the KiLeidoscope panel"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"

    def invoke(self, context, event):
        scale = _scale()
        for key, label, _, (x0, y0, x1, y1) in tab_rects(context.area):
            if x0 - TAB_OUT * scale <= event.mouse_region_x <= x1 and y0 <= event.mouse_region_y <= y1:
                if not open_column(context, key) and context.scene.kileido_column == key:
                    self.report({"INFO"}, f"Drag the sidebar's edge to make room for the {label} column")
                return {"FINISHED"}
        return {"PASS_THROUGH"}


CLASSES = (KILEIDO_OT_column_tab,)


def install():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")


def uninstall():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
