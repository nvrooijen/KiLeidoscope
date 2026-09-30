"""The High speed column: signal-integrity views left of the KiLeidoscope panel,
opened by its bookmark tab (columns.py). Sections: Dynamic phase."""

import textwrap

import bpy
from bpy.props import FloatProperty, IntProperty, StringProperty

from . import live, phase
from .state import board


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
    """A warning icon when the pair runs out of phase somewhere; its terminals are in the box
    below. The last button walks the pair from its other end (pressed while it does)."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        row = layout.row(align=True)
        first, numbers = _phase_columns(row)
        first.label(text=item.name, icon="ERROR" if item.excursions else "BLANK1")
        for value in (f"{item.delay_ps:.1f}", _ps(item.skew_ps, True), _ps(item.max_ps)):
            numbers.label(text=value)
        row.operator(KILEIDO_OT_phase_flip.bl_idname, text="", icon="ARROW_LEFTRIGHT",
                     depress=item.key in phase.flipped()).key = item.key


class KILEIDO_OT_phase_flip(bpy.types.Operator):
    bl_idname = "kileido.phase_flip"
    bl_label = "Flip start"
    bl_description = "Walk this pair from its other end: Δt then counts from there"

    key: StringProperty()

    def execute(self, context):
        phase.flip(self.key)
        return {"FINISHED"}


class KILEIDO_OT_phase_flip_selected(bpy.types.Operator):
    bl_idname = "kileido.phase_flip_selected"
    bl_label = "Reverse selected"
    bl_description = ("Walk the selected pairs from their other end (select a trace in KiCad, or click it "
                      "here): Δt then counts from there")

    @classmethod
    def poll(cls, context):
        if not phase.selected_pairs():
            cls.poll_message_set("Select a trace of a pair in KiCad, or click one here")
            return False
        return True

    def execute(self, context):
        phase.flip_selected()
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
    header.prop(context.scene, "kileido_phase_enabled", text="")
    header.label(text="Dynamic phase")
    header.operator(KILEIDO_OT_phase_help.bl_idname, text="", icon="QUESTION", emboss=False)
    if body is not None:
        body.active = context.scene.kileido_phase_enabled
        _draw_dynamic_phase(body, context, wrap, swatch)


def _draw_dynamic_phase(layout, context, wrap, swatch):
    scene = context.scene
    row = layout.row(align=True)
    row.prop(scene, "kileido_phase_show", text="Colours")
    row.prop(scene, "kileido_phase_labels", text="Labels")
    row.prop(scene, "kileido_phase_pulses", text="Pulses")
    layout.prop(scene, "kileido_phase_follow_series", text="Through series parts (AC caps)")
    layout.operator(KILEIDO_OT_phase_flip_selected.bl_idname, icon="ARROW_LEFTRIGHT")
    column = layout.column(align=True)
    column.prop(scene, "kileido_phase_scale_ps", text="Colour scale (ps)")
    column.prop(scene, "kileido_phase_tolerance_ps", text="Marker above (ps)")
    column.prop(scene, "kileido_phase_min_mm", text="Marker longer than (mm)")
    pulses = layout.column(align=True)
    pulses.active = scene.kileido_phase_pulses
    pulses.prop(scene, "kileido_phase_pulse_rate", text="Pulse speed (ps/s)")
    pulses.prop(scene, "kileido_phase_pulse_width_ps", text="Pulse width (ps)")
    pulses.prop(scene, "kileido_phase_pulse_tail_ps", text="Pulse tail (ps)")
    pulses.prop(scene, "kileido_phase_pulse_glow", text="Pulse brightness")
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
    header = layout.row(align=True)
    first, numbers = _phase_columns(header)
    first.label(text="Pair (ps)")
    for title in ("Delay", "Skew", "Max |Δt|"):
        numbers.label(text=title)
    header.label(text="", icon="BLANK1")  # over the rows' flip buttons
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
    kinds = data.get("lines") or ()
    total = sum(float(line.get("mm", 0.0)) for line in kinds) or 1.0
    if kinds:
        lines.append("Line: " + ", ".join(f"{line['kind']} {line['layer']} {100 * float(line['mm']) / total:.0f} %"
                                          for line in kinds if float(line["mm"]) / total >= 0.005))
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


CLASSES = (KILEIDO_PG_phase_pair, KILEIDO_UL_phase_pairs, KILEIDO_OT_phase_flip, KILEIDO_OT_phase_flip_selected,
           KILEIDO_OT_phase_select, KILEIDO_OT_phase_help)
