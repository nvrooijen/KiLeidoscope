"""The High speed column: signal-integrity views left of the KiLeidoscope panel.

The KiLeidoscope panel draws it as its left column (`draw`); the High speed button in
the panel's header opens and closes it and widens the sidebar to make room
(`KILEIDO_OT_high_speed`). Sections: Dynamic phase.

Blender has no API for a region's width. The sidebar's stored width (ARegion.sizex,
unscaled pixels) is written directly, at the offset measured for each Blender version
(SIZEX_OFFSET), and only when the value there is the sidebar's current width. Anything
else, and the column still opens; the sidebar is then widened by hand.
"""

import ctypes
import textwrap

import bpy
from bpy.props import FloatProperty, IntProperty, StringProperty

from . import live, phase
from .state import board

SIZEX_OFFSET = {(5, 1): 198}  # bytes into ARegion; measured by scanning at two UI scales
MIN_SIDEBAR_PX = 200  # unscaled: the narrowest a closing column leaves the sidebar
MAX_AREA_SHARE = 0.7  # the widened sidebar takes at most this much of the 3D view


def _width_cell(region):
    """The sidebar's stored width as a ctypes cell, or None when this Blender's layout
    is unknown or the value there is not the width Blender draws the sidebar at."""
    offset = SIZEX_OFFSET.get(tuple(bpy.app.version[:2]))
    if offset is None or region is None or region.type != "UI":
        return None
    cell = ctypes.c_short.from_address(region.as_pointer() + offset)
    scale = bpy.context.preferences.system.ui_scale or 1.0
    if cell.value <= 0 or abs(cell.value * scale - region.width) > 2 * scale + 1:
        return None
    return cell


def _relayout(area):
    """Blender lays every region out again, at its stored width, when the UI scale is
    set; setting it to its own value does that without changing anything else."""
    view = bpy.context.preferences.view
    view.ui_scale = view.ui_scale


class KILEIDO_OT_high_speed(bpy.types.Operator):
    bl_idname = "kileido.high_speed"
    bl_label = "High speed"
    bl_description = "Open or close the High speed column left of this panel; the sidebar widens to make room"

    def execute(self, context):
        scene = context.scene
        opening = not scene.kileido_high_speed
        scene.kileido_high_speed = opening
        cell = _width_cell(context.region)
        if cell is not None:
            scale = context.preferences.system.ui_scale or 1.0
            widest = int(context.area.width * MAX_AREA_SHARE / scale)
            cell.value = min(cell.value * 2, widest) if opening else max(cell.value // 2, MIN_SIDEBAR_PX)
            _relayout(context.area)
        elif opening:
            self.report({"INFO"}, "Drag the sidebar's edge to make room for the High speed column")
        return {"FINISHED"}


# --- Dynamic phase -------------------------------------------------------------------------

class KILEIDO_PG_phase_pair(bpy.types.PropertyGroup):
    """One row of the dynamic-phase pair list (filled from the bridge's frames, phase.sync_list)."""
    key: StringProperty()
    name: StringProperty()
    route: StringProperty()
    delay_ps: FloatProperty()
    skew_ps: FloatProperty()
    max_ps: FloatProperty()
    excursions: IntProperty()


def _phase_columns(layout):
    """The pair list's columns: pair, total delay, end-to-end skew, largest |Δt| (ps)."""
    split = layout.split(factor=0.4, align=True)
    first = split.row(align=True)
    numbers = split.grid_flow(columns=3, even_columns=True, align=True)
    return first, numbers


def _ps(value, signed=False):
    """Picoseconds for the panel, without a "-0.00" for a rounding residue."""
    value = round(float(value), 2) + 0.0
    return f"{value:+.2f}" if signed else f"{value:.2f}"


class KILEIDO_UL_phase_pairs(bpy.types.UIList):
    """A warning icon when the pair runs out of phase somewhere; its terminals are in the box below."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        first, numbers = _phase_columns(layout)
        first.label(text=item.name, icon="ERROR" if item.excursions else "BLANK1")
        for value in (f"{item.delay_ps:.1f}", _ps(item.skew_ps, True), _ps(item.max_ps)):
            numbers.label(text=value)


class KILEIDO_OT_phase_flip(bpy.types.Operator):
    bl_idname = "kileido.phase_flip"
    bl_label = "Flip start"
    bl_description = "Walk this pair from its other end: Δt then counts from there"

    key: StringProperty()

    def execute(self, context):
        phase.flip(self.key)
        return {"FINISHED"}


class KILEIDO_OT_phase_select(bpy.types.Operator):
    bl_idname = "kileido.phase_select"
    bl_label = "Select in KiCad"
    bl_description = "Select this in KiCad"

    ids: StringProperty()  # KiCad ids, one per line

    @classmethod
    def poll(cls, context):
        return live.linked()

    def execute(self, context):
        live.request_select([item for item in self.ids.splitlines() if item])
        return {"FINISHED"}


PHASE_HELP = (
    "A differential pair carries its signal as the difference between P and N. KiCad matches their total "
    "delay end to end; this view shows how far apart they are along the way.",
    "Where P and N are out of phase, part of the differential signal turns into common mode. Common-mode "
    "current radiates far more than differential current (its fields no longer cancel), above all once it "
    "reaches a connector or cable. The same imbalance lets outside noise turn into differential noise at the "
    "receiver. Skew made up only at the far end does not undo what the stretch in between radiated or picked up.",
    "Colours: P red, N blue, the stronger the further apart they are there. Pulses start together at the start "
    "terminal and travel at each trace's own delay, slowed down: the one in front is ahead.",
    "Amber markers: |Δt| above your tolerance for longer than your distance. Delays come from the netclass's "
    "tuning profile, else an estimate from the stackup (see Delay for each pair).",
)


class KILEIDO_OT_phase_help(bpy.types.Operator):
    bl_idname = "kileido.phase_help"
    bl_label = "About dynamic phase"
    bl_description = "\n\n".join(PHASE_HELP)

    def execute(self, context):
        def draw(menu, _context):
            for paragraph in PHASE_HELP:
                for line in textwrap.wrap(paragraph, 70):
                    menu.layout.label(text=line)
                menu.layout.separator()
        context.window_manager.popup_menu(draw, title="Dynamic phase", icon="QUESTION")
        return {"FINISHED"}


def draw(layout, context, wrap, swatch):
    """The High speed column. `wrap(context, text)` and `swatch(kind, color)` are the
    KiLeidoscope panel's own helpers."""
    header, body = layout.panel("KILEIDO_dynamic_phase", default_closed=False)
    header.label(text="Dynamic phase")
    header.operator(KILEIDO_OT_phase_help.bl_idname, text="", icon="QUESTION", emboss=False)
    if body is not None:
        _draw_dynamic_phase(body, context, wrap, swatch)


def _draw_dynamic_phase(layout, context, wrap, swatch):
    scene = context.scene
    row = layout.row(align=True)
    row.prop(scene, "kileido_phase_show", text="Colours")
    row.prop(scene, "kileido_phase_labels", text="Labels")
    row.prop(scene, "kileido_phase_pulses", text="Pulses")
    layout.prop(scene, "kileido_phase_follow_series", text="Through series parts (AC caps)")
    column = layout.column(align=True)
    column.prop(scene, "kileido_phase_scale_ps", text="Colour scale (ps)")
    column.prop(scene, "kileido_phase_tolerance_ps", text="Marker above (ps)")
    column.prop(scene, "kileido_phase_min_mm", text="Marker longer than (mm)")
    pulses = layout.column(align=True)
    pulses.active = scene.kileido_phase_pulses
    pulses.row().prop(scene, "kileido_phase_pulse_clock", expand=True)
    pulses.prop(scene, "kileido_phase_pulse_rate", text="Pulse speed (ps/s)")
    pulses.prop(scene, "kileido_phase_pulse_width_ps", text="Pulse width (ps)")
    pulses.prop(scene, "kileido_phase_pulse_tail_ps", text="Pulse tail (ps)")
    layout.prop(scene, "kileido_phase_bloom", text="Bloom (glow halo)")
    blocked = phase.bloom_blocked()
    if blocked and scene.kileido_phase_bloom:
        layout.label(text=f"The scene composites with {blocked}: bloom left out", icon="INFO")
    legend = layout.row(align=True)
    for color, text in ((phase.COLORS["p"], "P"), (phase.COLORS["n"], "N")):
        legend.label(text=text, icon_value=swatch("tile", color))
    info = layout.column(align=True)
    for line in wrap(context, "The stronger the colour, the further P and N are out of phase there. "
                              "Pulses start together: the one in front is ahead."):
        info.label(text=line)
    guess = phase.sources_warning()
    if guess:
        for index, line in enumerate(wrap(context, f"Delay: {guess}, no tuning profile or "
                                                   "dielectric constant found")):
            info.label(text=line, icon="ERROR" if index == 0 else "BLANK1")
    pending = board.phase_list.get("pending", 0)
    if pending:
        info.label(text=f"Measuring {pending} more pair{'s' if pending > 1 else ''}…", icon="TIME")
    for warning in board.phase_list.get("warnings", ())[:4]:
        for index, line in enumerate(wrap(context, warning)):
            info.label(text=line, icon="INFO" if index == 0 else "BLANK1")
    if not len(scene.kileido_phase_pairs):
        layout.label(text="No routed differential pairs" if live.connected() else "Live with KiCad only")
        return
    first, numbers = _phase_columns(layout)
    first.label(text="Pair (ps)")
    for title in ("Delay", "Skew", "Max |Δt|"):
        numbers.label(text=title)
    layout.template_list("KILEIDO_UL_phase_pairs", "", scene, "kileido_phase_pairs", scene,
                         "kileido_phase_index", rows=4)
    data = phase.active_pair()
    if data is not None:
        _draw_pair(context, layout.box(), data, wrap)


def _draw_pair(context, box, data, wrap):
    row = box.row(align=True)
    row.label(text=f"{data['start']['label']} → {data['end']['label']}")
    row.operator(KILEIDO_OT_phase_flip.bl_idname, text="", icon="ARROW_LEFTRIGHT").key = data["key"]
    column = box.column(align=True)
    for side in ("p", "n"):
        column.label(text=f"{side.upper()}: {_ps(data[f'delay_{side}_ps'])} ps, "
                          f"{data[f'length_{side}_mm']:.2f} mm")
    column.label(text=f"End-to-end skew: {_ps(data['skew_ps'], True)} ps")
    column.label(text=f"Largest |Δt|: {_ps(data['max_ps'])} ps")
    lines = [f"Nets: {', '.join(data['p_nets'] + data['n_nets'])}"]
    if data.get("parts"):
        lines.append("Through " + ", ".join(data["parts"]))
    lines.append("Delay: " + "; ".join(data.get("sources", ())))
    for text in lines:
        for line in wrap(context, text):
            column.label(text=line)
    for excursion in data.get("excursions", ()):
        line = box.row(align=True)
        line.label(text=f"{excursion['at_mm']:.1f} mm: {_ps(excursion['peak_ps'], True)} ps "
                        f"over {excursion['length_mm']:.1f} mm", icon="ERROR")
        line.operator(KILEIDO_OT_phase_select.bl_idname, text="",
                      icon="RESTRICT_SELECT_OFF").ids = excursion["id"]
    for branch in data.get("branches", ()):
        line = box.row(align=True)
        line.label(text=f"{branch['side']} branch to {branch['label']} at {branch['at_mm']:.1f} mm, "
                        f"{branch['length_mm']:.1f} mm long")
        line.operator(KILEIDO_OT_phase_select.bl_idname, text="",
                      icon="RESTRICT_SELECT_OFF").ids = branch["id"]
    for warning in data.get("warnings", ()):
        box.label(text=warning, icon="INFO")


CLASSES = (KILEIDO_OT_high_speed, KILEIDO_PG_phase_pair, KILEIDO_UL_phase_pairs, KILEIDO_OT_phase_flip,
           KILEIDO_OT_phase_select, KILEIDO_OT_phase_help)
