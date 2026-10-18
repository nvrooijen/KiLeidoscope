"""The Diff pairs column: the dynamic phase of differential pairs, left of the KiLeidoscope
panel, opened by its bookmark tab (columns.py)."""

import textwrap
from pathlib import Path

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty

from . import live, phase, phase_chart
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
    shorted: BoolProperty()  # P touches N: no numbers
    unprofiled: BoolProperty()  # no tuning profile in KiCad: no numbers
    loops: IntProperty()


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
        if item.shorted:
            row.alert = True
            first.label(text=item.name, icon="CANCEL")
            for value in ("SHORT", "", ""):
                numbers.label(text=value)
        elif item.unprofiled:
            first.label(text=item.name, icon="INFO")
            for value in ("NO PROFILE", "", ""):
                numbers.label(text=value)
        else:
            first.label(text=item.name, icon="ERROR" if item.excursions or item.loops else "BLANK1")
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
    "Amber markers: |Δt| above your tolerance for longer than your distance. Delays are KiCad's: the tuning "
    "profile on each net's netclass (Board Setup › Tuning Profiles), one number per layer, and the pads' "
    "pad-to-die delays. A pair without a profile is listed as NO PROFILE, with nothing drawn.",
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


class KILEIDO_OT_phase_export(bpy.types.Operator):
    bl_idname = "kileido.phase_export"
    bl_label = "Export chart"
    bl_description = ("Write Δt along every measured pair as an SVG chart, one plot per pair with the marker "
                      "tolerance and the excursions drawn, and the numbers as a CSV beside it")
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.svg", options={"HIDDEN"})
    with_csv: BoolProperty(name="CSV beside it", default=True,
                           description="The same numbers as a .csv next to the .svg: pair, mm along the pair, Δt in ps")

    @classmethod
    def poll(cls, context):
        if not phase.measured_pairs():
            cls.poll_message_set("No measured pair to chart")
            return False
        return True

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = phase.export_path()
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        self.layout.prop(self, "with_csv")

    def execute(self, context):
        path = Path(bpy.path.abspath(self.filepath))
        if path.suffix.lower() != ".svg":
            path = path.with_suffix(".svg")
        pairs = phase.measured_pairs()
        settings = phase.settings()
        title = Path(board.board_path).name if board.board_path else ""
        try:
            path.write_text(phase_chart.svg(pairs, settings["tolerance_ps"], settings["min_length_mm"], title),
                            encoding="utf-8")
            if self.with_csv:
                path.with_suffix(".csv").write_text(phase_chart.csv(pairs), encoding="utf-8")
        except OSError as exc:
            self.report({"ERROR"}, f"Could not write {path.name}: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Wrote {path.name}" + (f" and {path.with_suffix('.csv').name}" if self.with_csv else "")
                    + f": {len(pairs)} pair{'' if len(pairs) == 1 else 's'}")
        return {"FINISHED"}


def draw(layout, context, wrap, swatch, width):
    """The Diff pairs column (its tab names it, so no title): Enable and the help, then the
    settings and the pair list. `wrap(context, text)`, `swatch(kind, color)` and
    `width(context)` (px a line has) are the KiLeidoscope panel's own helpers."""
    top = layout.row()
    top.prop(context.scene, "kileido_phase_enabled", text="Enable")
    top.operator(KILEIDO_OT_phase_help.bl_idname, text="", icon="QUESTION", emboss=False)
    body = layout.column()
    body.active = context.scene.kileido_phase_enabled
    _draw_dynamic_phase(body, context, wrap, swatch, width)


def _draw_dynamic_phase(layout, context, wrap, swatch, width):
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
    pulses.row().prop(scene, "kileido_phase_pulse_clock", expand=True)
    if scene.kileido_phase_pulse_clock == "TIMELINE":
        for line in wrap(context, "The frame sets the pulses: Play animates them, and renders show them."):
            pulses.label(text=line, icon="INFO")
    pulses.prop(scene, "kileido_phase_pulse_count", text="Number of pulses")
    pulses.prop(scene, "kileido_phase_pulse_rate", text="Pulse speed (ps/s)")
    pulses.prop(scene, "kileido_phase_pulse_width_ps", text="Pulse width (ps)")
    pulses.prop(scene, "kileido_phase_pulse_tail_ps", text="Pulse tail (ps)")
    pulses.prop(scene, "kileido_phase_pulse_glow", text="Pulse brightness")
    _draw_legend(layout.box().column(align=True), context, wrap, swatch, width)
    info = layout.column(align=True)
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
    header.separator(factor=0.8)  # the list's own margin, so each title sits over its column
    # Every pair on show (up to a dozen): without a scrollbar the rows' columns line up with the titles.
    layout.template_list("KILEIDO_UL_phase_pairs", "", scene, "kileido_phase_pairs", scene,
                         "kileido_phase_index", rows=min(len(scene.kileido_phase_pairs), 12))
    layout.operator(KILEIDO_OT_phase_export.bl_idname, text="Export chart (SVG)", icon="EXPORT")
    data = phase.active_pair()
    if data is not None:
        _draw_pair(context, layout.box(), data, wrap)


BAR_TILES = 48
ICON_PX = 16  # unscaled: the width Blender draws a label's icon at, however narrow the label


def _draw_legend(column, context, wrap, swatch, width):
    """What the colours and markers mean: a colour bar from P red through white (in phase)
    to N blue over the colour scale, then the markers and how to read the pulses. A panel
    has no gradient widget: the bar is BAR_TILES full-bleed icons side by side, which an
    aligned row packs edge to edge. Each icon is drawn ICON_PX wide from its tile's left
    and the next tile paints over the rest, so the last one reaches past its tile: the
    bar leaves it that much room, or it would stick out of the box."""
    scale = context.scene.kileido_phase_scale_ps
    for line in wrap(context, "Dynamic phase: how far P and N are apart"):  # wrapped: a narrow column cut it off
        column.label(text=line)
    icon_px = ICON_PX * (context.preferences.system.ui_scale or 1.0)
    room = column.split(factor=max(0.5, 1.0 - icon_px / width(context)), align=True)
    bar = room.row(align=True)
    room.label(text="")  # under the last tile's icon
    for index in range(BAR_TILES):
        share = index / (BAR_TILES - 1) * 2 - 1  # -1: P at the scale's end; 0: in phase; 1: N at its end
        side = phase.COLORS["p" if share < 0 else "n"]
        bar.label(text="", icon_value=swatch("fill", tuple(1.0 + (c - 1.0) * abs(share) for c in side)))
    ends = column.row(align=True)
    ends.label(text=f"P  {scale:.2f} ps")
    middle = ends.row()
    middle.alignment = "CENTER"
    middle.label(text="in phase")
    right = ends.row()
    right.alignment = "RIGHT"
    right.label(text=f"{scale:.2f} ps  N")
    column.separator()
    tolerance, length = context.scene.kileido_phase_tolerance_ps, context.scene.kileido_phase_min_mm
    for role, text in (("start", "Start of the pair"), ("end", "End of the pair"),
                       ("excursion", f"P and N over {tolerance:g} ps apart for {length:g} mm or more"),
                       ("short", "P and N shorted"), ("loop", "A loop beside the route")):
        for index, line in enumerate(wrap(context, text)):  # wrapped: a narrow column cuts a label in its middle
            column.label(text=line, **({"icon_value": swatch("tile", phase.COLORS[role])} if index == 0
                                       else {"icon": "BLANK1"}))
    for line in wrap(context, "White: the selected pair where it is in phase. Pulses start together: "
                              "the one in front is ahead."):
        column.label(text=line)


def _draw_shorted(context, box, data, wrap):
    """A pair whose P copper touches its N copper: where, and nothing to measure."""
    box.alert = True
    box.label(text="P and N are shorted: not measured", icon="CANCEL")
    for line in wrap(context, f"Nets: {', '.join(data['p_nets'] + data['n_nets'])}"):
        box.label(text=line)
    for short in data.get("shorts", ()):
        line = box.row(align=True)
        line.label(text=f"Touching on {short['layer']} at {short['x'] / 1e6:.2f}, {short['y'] / 1e6:.2f} mm")
        line.operator(KILEIDO_OT_phase_select.bl_idname, text="",
                      icon="RESTRICT_SELECT_OFF").ids = "\n".join(short["ids"])


def _draw_unprofiled(context, box, data, wrap):
    """A pair KiCad has no delay for: why, and where to set one."""
    for index, line in enumerate(wrap(context, f"{data['unprofiled']}: not measured")):
        box.label(text=line, icon="INFO" if index == 0 else "BLANK1")
    for text in ("Set a tuning profile in KiCad (Board Setup › Tuning Profiles) on the pair's netclass, with "
                 "time-domain tuning on and a delay for every layer it runs on.",
                 f"Nets: {', '.join(data['p_nets'] + data['n_nets'])}"):
        for line in wrap(context, text):
            box.label(text=line)


def _draw_pair(context, box, data, wrap):
    if data.get("shorts"):
        _draw_shorted(context, box, data, wrap)
        return
    if data.get("unprofiled"):
        _draw_unprofiled(context, box, data, wrap)
        return
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
        text = f"{excursion['at_mm']:.1f} mm: {_ps(excursion['peak_ps'], True)} ps over {excursion['length_mm']:.1f} mm"
        line.label(text=text, icon="ERROR")
        line.operator(KILEIDO_OT_phase_select.bl_idname, text="",
                      icon="RESTRICT_SELECT_OFF").ids = "\n".join(excursion.get("ids") or [excursion["id"]])
    for branch in data.get("branches", ()):
        line = box.row(align=True)
        line.label(text=f"{branch['side']} branch to {branch['label']} at {branch['at_mm']:.1f} mm, "
                        f"{branch['length_mm']:.1f} mm long")
        line.operator(KILEIDO_OT_phase_select.bl_idname, text="",
                      icon="RESTRICT_SELECT_OFF").ids = branch["id"]
    for loop in data.get("loops", ()):
        line = box.row(align=True)
        line.label(text=f"{loop['side']} loop: {loop['length_mm']:.1f} mm of copper beside the route", icon="ERROR")
        line.operator(KILEIDO_OT_phase_select.bl_idname, text="",
                      icon="RESTRICT_SELECT_OFF").ids = "\n".join(loop["ids"])
    for warning in data.get("warnings", ()):
        if " loop: " not in warning:  # each loop has its own line above
            box.label(text=warning, icon="INFO")


CLASSES = (KILEIDO_PG_phase_pair, KILEIDO_UL_phase_pairs, KILEIDO_OT_phase_flip, KILEIDO_OT_phase_flip_selected,
           KILEIDO_OT_phase_select, KILEIDO_OT_phase_help, KILEIDO_OT_phase_export)
