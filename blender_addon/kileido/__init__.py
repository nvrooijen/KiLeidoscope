"""KiLeidoscope board viewer for Blender 5.1."""

bl_info = {
    "name": "KiLeidoscope",
    "author": "KiLeidoscope contributors",
    "version": (0, 4, 4),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > KiLeidoscope",
    "description": "View read-only KiCad board geometry from a dump or live bridge",
    "category": "3D View",
}

import re
import textwrap
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, StringProperty)
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import (apply, collisions, columns, cosmetics, cut, dump, edge_plating, focus, fold, footprints, holes, ims,
               layers, lighting, live, models, packages, pick, protection, render_depth, watcher)
from .objects import view3d_spaces
from .state import board


class KILEIDO_OT_load_dump(bpy.types.Operator, ImportHelper):
    bl_idname = "kileido.load_dump"
    bl_label = "KiLeidoscope: Load dump"
    bl_description = "Open a saved .kls board dump without KiCad (F3 search)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".kls"
    filter_glob: StringProperty(default="*.kls", options={"HIDDEN"})

    def execute(self, context):
        dump.load_async(self.filepath)
        self.report({"INFO"}, "KiLeidoscope is loading the dump")
        return {"FINISHED"}


class KILEIDO_OT_export_board(bpy.types.Operator, ExportHelper):
    bl_idname = "kileido.export_board"
    bl_label = "Export board"
    bl_description = ("Save the board shown now as a .blend package, with its models and overlays, "
                      "to import view-only in any KiLeidoscope session")

    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        if board.collection is None or board.in_snapshot:
            cls.poll_message_set("No complete board loaded")
            return False
        return True

    def invoke(self, context, event):
        self.filepath = f"{board.board_name or 'board'}.blend"
        return super().invoke(context, event)

    def execute(self, context):
        try:
            packages.export_board(self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, f"KiLeidoscope: export failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"KiLeidoscope: exported {board.board_name}")
        return {"FINISHED"}


class KILEIDO_OT_import_board(bpy.types.Operator, ImportHelper):
    bl_idname = "kileido.import_board"
    bl_label = "Import board"
    bl_description = "Add an exported board package beside the others, view-only"

    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={"HIDDEN"})

    def execute(self, context):
        try:
            root = packages.import_board(self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, f"KiLeidoscope: import failed: {exc}")
            return {"CANCELLED"}
        collisions.schedule()
        self.report({"INFO"}, f"KiLeidoscope: imported {root['kls_board_name']} (view-only)")
        return {"FINISHED"}


class KILEIDO_OT_view_only_board(bpy.types.Operator):
    bl_idname = "kileido.view_only_board"
    bl_label = "View-only board"
    bl_description = "Show, hide or remove this view-only board"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()
    action: EnumProperty(items=(("TOGGLE", "Show or hide", ""), ("REMOVE", "Remove", "")))

    def execute(self, context):
        if self.action == "REMOVE":
            packages.remove(self.index)
        else:
            collection = packages.collection_of(self.index)
            if collection is not None:
                packages.set_visible(self.index, collection.hide_viewport)
        collisions.schedule()
        return {"FINISHED"}


class KILEIDO_OT_resync(bpy.types.Operator):
    bl_idname = "kileido.resync"
    bl_label = "KiLeidoscope: Resync with KiCad"
    bl_description = ("Ask KiCad for the whole board again and rebuild it. When a new KiCad was "
                      "detected (after a crash or restart), follow it")

    def execute(self, context):
        live.request_resync(adopt=True)
        return {"FINISHED"}


class KILEIDO_OT_pick(bpy.types.Operator):
    bl_idname = "kileido.pick"
    bl_label = "Select in KiCad"
    bl_description = "Select the track, via, pad's component or component under the mouse in KiCad"

    extend: BoolProperty(name="Extend", default=False)

    @classmethod
    def poll(cls, context):
        if not live.linked():  # the click falls through to Blender's own selection
            cls.poll_message_set("Not connected to KiCad")
            return False
        return context.area is not None and context.area.type == "VIEW_3D"

    def invoke(self, context, event):
        from bpy_extras import view3d_utils
        coordinate = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(context.region, context.region_data, coordinate)
        direction = view3d_utils.region_2d_to_vector_3d(context.region, context.region_data, coordinate)
        # The cut plane is an object to move, not a KiCad item: a click on its wireframe selects it
        # (this operator takes the click before Blender can).
        if cut.clicked(lambda point: view3d_utils.location_3d_to_region_2d(context.region, context.region_data,
                                                                            point), coordinate):
            cut.select(self.extend)
            self.report({"INFO"}, "KiLeidoscope: cut plane selected (G moves it, R then Z turns it)")
            return {"FINISHED"}
        grip = fold.clicked(lambda point: view3d_utils.location_3d_to_region_2d(context.region, context.region_data,
                                                                               point), coordinate)
        if grip is not None:  # a bend's handle, to turn: the board folds with it
            fold.select(grip, self.extend)
            self.report({"INFO"}, f"KiLeidoscope: {grip.name} selected (R turns it, folding the board)")
            return {"FINISHED"}
        item = pick.item_at(context.scene, context.evaluated_depsgraph_get(), origin, direction)
        # Say what happened in the status bar: a click with no visible result is otherwise
        # impossible to tell apart from one that never arrived.
        if item is not None:
            live.request_select([item], self.extend, context.scene.kileido_center_in_kicad)
            self.report({"INFO"}, f"KiLeidoscope: selecting {pick.describe(item)} in KiCad")
        elif not self.extend:
            live.request_select([], False)  # clicking bare board clears KiCad's selection
            self.report({"INFO"}, "KiLeidoscope: nothing to select here; KiCad's selection cleared")
        return {"FINISHED"}


class KILEIDO_OT_viewport(bpy.types.Operator):
    bl_idname = "kileido.viewport"
    bl_label = "KiLeidoscope viewport shading"
    bl_description = "Material Preview (EEVEE, real-time) or Cycles (path traced) in the 3D views"

    mode: EnumProperty(items=(("MATERIAL", "Preview", "EEVEE Material Preview: real-time"),
                              ("RENDERED", "Cycles", "Cycles rendered viewport: slower, physically lit")))

    def execute(self, context):
        if self.mode == "RENDERED":
            context.scene.render.engine = "CYCLES"
        for space in view3d_spaces():
            space.shading.type = self.mode
        return {"FINISHED"}


class KILEIDO_OT_cut_plane(bpy.types.Operator):
    bl_idname = "kileido.cut_plane"
    bl_label = "Cut plane"
    bl_description = "Turn the cut plane across X or Y (keeping its position), or put it back across the middle"

    axis: EnumProperty(items=(("X", "X", "Across X: the right side removed, seen from the right view"),
                              ("Y", "Y", "Across Y: the front side removed, seen from the front view"),
                              ("RESET", "Reset", "Across Y through the middle of the board, front half removed")))

    def execute(self, context):
        if self.axis == "RESET":
            cut.place("Y", centered=True)
        else:
            cut.place(self.axis)
        return {"FINISHED"}


def _viewport_mode(context):
    space = context.space_data
    return getattr(getattr(space, "shading", None), "type", "")


class KILEIDO_OT_all_layers(bpy.types.Operator):
    bl_idname = "kileido.all_layers"
    bl_label = "All layers"
    bl_description = "Show every layer, or hide them all when all are shown"

    def execute(self, context):
        rows = layers.rows()
        layers.set_all(not all(layers.shown(row) for row, _ in rows))
        return {"FINISHED"}


class KILEIDO_PT_panel(bpy.types.Panel):
    bl_label = "KiLeidoscope"
    bl_idname = "KILEIDO_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "KiLeidoscope"

    def draw_header(self, context):
        icon = _icons.get("logo") if _icons is not None else None
        if icon is not None:
            self.layout.label(text="", icon_value=icon.icon_id)

    def draw(self, context):
        """With a column open (its bookmark tab, columns.py), two: that column left, the rest right."""
        global _share
        layout = self.layout
        _share = 1.0
        column = COLUMNS.get(context.scene.kileido_column)
        if column is not None:
            split = layout.split(factor=0.5)
            left, layout = split.column(), split.column()
            _share = 0.5
            column(left, context.scene)
        self._draw_main(context, layout)
        for idname, title, section in (("KILEIDO_boards", "Boards", _Boards),
                                       ("KILEIDO_status", "Status", _Status)):
            header, body = layout.panel(idname, default_closed=False)
            header.label(text=title)
            if body is not None:
                section(body).draw(context)

    def _draw_main(self, context, layout):
        icon = _icons.get("logo") if _icons is not None else None
        if icon is not None:
            logo = layout.row()
            logo.alignment = "CENTER"
            logo.template_icon(icon_value=icon.icon_id, scale=LOGO_SCALE)
        self._draw_link(context, layout)
        self._draw_outline_warnings(context, layout)
        row = layout.row(align=True)
        current = _viewport_mode(context)
        for mode, label in (("MATERIAL", "Preview"), ("RENDERED", "Cycles")):
            row.operator(KILEIDO_OT_viewport.bl_idname, text=label, depress=current == mode).mode = mode
        row = layout.row(align=True)
        row.prop(context.scene, "kileido_color_mode", text="Colors")
        if context.scene.kileido_color_mode != "REALISTIC":
            row.prop(context.scene, "kileido_shaded", text="", icon="SHADING_SOLID")
        finish = board.appearance.get("copper_finish")
        if finish:
            layout.label(text="Board finish: " + ("Bare copper" if finish.casefold() == "none" else finish))
        row = layout.row(align=True)
        row.prop(context.scene, "kileido_light_power", text="Light")
        row.prop(context.scene, "kileido_fill_color", text="")
        layout.prop(context.scene, "kileido_reflections", text="Reflections")
        layout.prop(context.scene, "kileido_mask_opacity", text="Solder mask opacity", slider=True)
        layout.prop(context.scene, "kileido_silk_opacity", text="Silkscreen opacity", slider=True)
        self._draw_vias(context, layout)
        for prop, label, icon in (("kileido_focus", "X-ray mode", "xray"),
                                  ("kileido_center_in_kicad", "Center KiCad on click", None),
                                  ("kileido_clip_silkscreen", "Clip silkscreen to board outline", "scissors")):
            row = layout.row(align=True)
            row.prop(context.scene, prop, text=label)
            if _icons is not None and icon in _icons:
                row.label(text="", icon_value=_icons[icon].icon_id)
        self._draw_cut(context, layout)

    def _draw_vias(self, context, layout):
        """Via protection comes from KiCad; only what KiCad does not store is set here."""
        scene = context.scene
        row = layout.row(align=True)
        row.prop(scene, "kileido_via_fill_material", text="Via fill")
        if _icons is not None and "bucket" in _icons:
            row.label(text="", icon_value=_icons["bucket"].icon_id)
        row = layout.row(align=True)
        row.prop(scene, "kileido_max_tent_mm", text="Max tent hole")
        row.prop(scene, "kileido_via_plating_um", text="Via wall")
        count = board.via_too_big
        if count:
            layout.label(text=f"{count} via{'s' if count > 1 else ''} too large to tent: shown open", icon="ERROR")

    def _draw_cut(self, context, layout):
        scene = context.scene
        box = layout.box()
        box.prop(scene, "kileido_cut", text="Cut plane")
        if not scene.kileido_cut:
            return
        row = box.row(align=True)
        for axis in ("X", "Y"):
            row.operator(KILEIDO_OT_cut_plane.bl_idname, text=axis).axis = axis
        row.operator(KILEIDO_OT_cut_plane.bl_idname, text="Reset").axis = "RESET"
        row.prop(scene, "kileido_cut_flip", text="Flip", toggle=True)
        row = box.row(align=True)
        row.prop(scene, "kileido_cut_face", text="Section face", toggle=True)
        row.prop(scene, "kileido_cut_light", text="Cut light", icon="LIGHT_AREA", toggle=True)
        if not cut.upright(scene):
            box.label(text="Turn the plane upright for a cross section", icon="INFO")

    def _draw_outline_warnings(self, context, layout):
        """KiCad's own words when a board has no usable Edge.Cuts outline, then where."""
        found = [(board.board_name, warning) for warning in packages.outline_warnings()]
        for root in packages.roots():
            index = root[packages.ROOT_TAG]
            found += [(root["kls_board_name"], warning) for warning in packages.outline_warnings(index)]
        if not found:
            return
        box = layout.box()
        box.label(text="Board outline is missing or malformed.", icon="ERROR")
        box.label(text="Run DRC in KiCad for a full analysis.", icon="BLANK1")
        named = len({name for name, _ in found}) > 1 or bool(packages.roots())
        for name, warning in found[:6]:
            detail = warning.removeprefix(packages.OUTLINE_PROBLEM).lstrip(": ")
            for line in _wrap(context, f"{name}: {detail}" if named else detail):
                box.label(text=line, icon="BLANK1")

    def _draw_link(self, context, layout):
        """A status LED: green live, amber waiting or KiCad editing, red lost, grey offline."""
        health = live.health()
        text = {"ok": "Live with KiCad",
                "off": "Not live: use Open in Blender in KiCad"}.get(health, live.status_text())
        layout.label(text=text, icon_value=_swatch("led", LED_COLORS[health]))
        error = live.error_text()
        if error:  # e.g. a second KiCad on Windows: say why the scene is empty
            box = layout.box()
            for index, line in enumerate(_wrap(context, error)):
                box.label(text=line, icon="ERROR" if index == 0 else "BLANK1")


_share = 1.0  # of the sidebar's width the column being drawn has (a column open: half)


def _wrap(context, text):
    """Lines that fit the sidebar at its current width (Blender cuts a longer label in
    its middle): ~7 px per character and an icon and box margins, at the UI scale."""
    scale = context.preferences.system.ui_scale or 1.0  # 0 without a window
    width = (context.region.width if context.region else 300 * scale) * _share
    return textwrap.wrap(text, max(16, int((width - 50 * scale) / (7 * scale))))


def _draw_thickness(layout, scene, copper_m):
    """The 3D heights in one box, for every board: each toggle beside the value it
    raises by (copper: the shown board's stackup)."""
    box = layout.box()
    box.label(text="Thickness (3D)")
    column = box.column(align=True)
    for toggle, label, value, value_label in (
            ("kileido_copper_3d", "Copper", None, layers.format_thickness(copper_m)),
            ("kileido_silk_3d", "Silkscreen", "kileido_silk_um", "µm"),
            ("kileido_show_solder", "Solder paste", "kileido_stencil_mm", "mm")):
        split = column.split(factor=0.5, align=True)
        split.prop(scene, toggle, text=label)
        right = split.row(align=True)
        right.active = getattr(scene, toggle)
        if value is None:  # copper comes from the stackup, not a setting
            right.alignment = "RIGHT"
            right.label(text=value_label or "stackup")
        else:
            right.prop(scene, value, text=value_label)


def _draw_wrapped(layout, text, icon="INFO"):
    for index, line in enumerate(_wrap(bpy.context, text)):
        layout.label(text=line, icon=icon if index == 0 else "BLANK1")


def _draw_switched(layout, mode):
    """Say so when switching this mode on turned the other one off."""
    if _switched_off.get(mode):
        _draw_wrapped(layout, f"{_switched_off[mode]} turned off: IMS and Flex cannot be on together")


def _draw_ims(layout, scene):
    """The IMS column, for a 2-layer live board: switched on here, then the base's metal and
    finish and the epoxy; the board keeps KiCad's thickness, and the base takes what the
    epoxy and copper leave."""
    layout.label(text="IMS (metal base)")
    box = layout.box()
    ok, why = ims.eligible(board.heights)
    if not ok:  # unticked whatever the setting: it waits for the next 2-layer board
        toggle = box.row()
        toggle.enabled = False
        toggle.label(text="Enable", icon="CHECKBOX_DEHLT")
        _draw_wrapped(box, why)
        return
    box.prop(scene, "kileido_ims", text="Enable")
    _draw_switched(box, "IMS")
    stack = board.ims
    if scene.kileido_ims != (stack is not None) and _ims_source() is None:  # nothing to rebuild it from
        _draw_wrapped(box, "Connect KiCad or load the dump again to redraw the board")
    if not scene.kileido_ims:
        return
    box.row(align=True).prop(scene, "kileido_ims_metal", expand=True)
    box.prop(scene, "kileido_ims_finish", text="Finish")
    box.prop(scene, "kileido_ims_epoxy_um", text="Epoxy (µm)")
    if stack is None:  # the board is on its way again, with the base (_ims_update)
        if _ims_source() is not None:
            box.label(text="Rebuilding the board…", icon="TIME")
        return
    column = box.column(align=True)
    for label, metres, source in (("Base", stack.base[1] - stack.base[0], "rest"),
                                  ("Copper", board.layer_thickness.get("F.Cu"), "stackup"),
                                  ("Board", stack.thickness_m, "KiCad")):
        split = column.split(factor=0.5, align=True)
        split.label(text=label)
        right = split.row(align=True)
        right.alignment = "RIGHT"
        right.label(text=f"{layers.format_thickness(metres) or '–'} {source}")
    for warning in board.ims_warnings:
        _draw_wrapped(box, warning, "ERROR")


FLEX_HOW = "KiCad marks flex with Polyimide in the board's stackup, or Flex, Bend and Stiffener user layers"


def _draw_flex_column(layout, scene):
    """The Flex column: switched on here when the board has flex, then the flex box."""
    layout.label(text="Flex and rigid-flex")
    box = layout.box()
    if not board.flex:
        toggle = box.row()
        toggle.enabled = False
        toggle.label(text="Enable", icon="CHECKBOX_DEHLT")
        _draw_wrapped(box, "This board has no flex. " + FLEX_HOW)
        return
    box.prop(scene, "kileido_flex", text="Enable")
    _draw_switched(box, "FLEX")
    if not scene.kileido_flex:
        _draw_wrapped(box, "Enable to fold the board, see its stiffeners and coverlay, and run the flex checks")
        return
    _draw_flex(layout, scene)


def _ims_state():
    if not ims.eligible(board.heights)[0]:
        return "unavailable"
    return "on" if bpy.context.scene.kileido_ims else "off"


def _flex_state():
    if not board.flex:
        return "unavailable"
    return "on" if bpy.context.scene.kileido_flex else "off"


COLUMNS = {"IMS": _draw_ims, "FLEX": _draw_flex_column}  # columns.TABS keys -> what the column draws
columns.STATES.update(IMS=_ims_state, FLEX=_flex_state)


def _draw_flex(layout, scene):
    """Flex mode, for a board with Polyimide in its stackup or Flex/Bend/Stiffener layers: the
    flex stack, coverlay, bends and stiffeners as KiCad says them (nothing here overrides
    KiCad: each has a button copying its text, to paste in KiCad), and the bridge's checks.
    A check with items selects them in KiCad."""
    found = board.flex
    if not found:
        return
    box = layout.box()
    title = box.split(factor=0.45)
    title.label(text="Use")
    title.row(align=True).prop(scene, "kileido_flex_use", expand=True)
    use = scene.kileido_flex_use.lower()
    total, need = found["total_nm"] * 1e-9, found["limits"][use]
    column = box.column(align=True)
    for label, value in (("Flex layers", " + ".join(found["layers"]) or "–"),
                         ("Flex + coverlay", f"{found['total_nm'] / 1000:.0f} µm")):  # in µm, as the checks say it
        right = _value_row(column, label)
        right.label(text=value)
    _coverlay_row(column, found.get("coverlay", "AMBER"))
    for index, bend in enumerate(found["bends"]):
        if bend.get("kind") == "twist":
            right = _value_row(column, f"Twist {index + 1}  {bend['angle_deg']:g}°")
            right.label(text=f"over {bend['length_nm'] / 1e6:g} mm")
            _copy_button(right, bend["note"], f"beside twist {index + 1} on the Bend layer")
            continue
        ratio = bend["radius_nm"] * 1e-9 / total  # a cone's: at its narrow end
        radius = f"R{bend['radius_nm'] / 1e6:g}"
        if bend.get("kind") == "cone":
            radius += f"–{bend['radius_max_nm'] / 1e6:.3g}"
        name = "Wrap" if bend.get("closed") else bend.get("kind", "bend").capitalize()
        if bend.get("kind") == "dome":
            said = f"{radius}, {len(bend.get('fingers', ()))} fingers"
        else:
            said = f"{bend['angle_deg']:g}° {radius}"
        right = _value_row(column, f"{name} {index + 1}  {said}")
        right.label(text=f"{ratio:.1f}×", icon="CHECKMARK" if need is not None and ratio >= need else "ERROR")
        _copy_button(right, bend["note"], f"beside {name.lower()} {index + 1} on the Bend layer")
    if any(bend.get("kind", "bend") == "bend" for bend in found["bends"]):
        column.label(text=f"{use.capitalize()} flex needs {need}× or more" if need is not None
                     else "Dynamic flex: 1 or 2 copper layers only")
    _draw_stiffeners(box, found.get("stiffeners", ()))
    if fold.foldable():
        box.prop(scene, "kileido_fold", text="Fold", slider=True)
        _draw_steps(box, scene)
        _draw_animation(box, scene)
    lines = [(problem["message"], (), "INFO" if problem.get("level") == "note" else "ERROR", "")
             for problem in found["problems"]]
    lines += [(message, (), "ERROR", _fold_group(message)) for message in board.fold_findings]
    lines += [(finding["message"], tuple(finding["items"]), "ERROR", finding.get("group", ""))
              for finding in found["findings"] if finding["use"] in ("", use)]
    if not lines:
        box.label(text="No flex problems found", icon="CHECKMARK")
        return
    box.label(text=f"Flex checks: {len(lines)} found")
    clickable = live.connected()  # a check with items selects them in KiCad
    groups = {}
    for line in lines:  # one kind of check together, in the order they come
        groups.setdefault(line[3] or line[0], []).append(line)
    opened = set(scene.kileido_flex_open.split("\n"))
    for name, members in groups.items():
        if len(members) == 1:
            _draw_check(box, *members[0][:3], clickable)
            continue
        shown = name in opened
        operator = box.operator(KILEIDO_OT_flex_group.bl_idname, text=f"{name} ({len(members)})", emboss=False,
                                icon="DISCLOSURE_TRI_DOWN" if shown else "DISCLOSURE_TRI_RIGHT")
        operator.group = name
        if shown:
            indent = box.split(factor=0.06)
            indent.label(text="")
            column = indent.column()
            for message, items, icon, _ in members:
                _draw_check(column, message, items, icon, clickable)


_FINGERS = re.compile(r"^(Step \d+|Folded): (\w+ \d+) \(finger \d+\) runs into \2 \(finger \d+\)$")


def _fold_group(message):
    """A collision's kind, for the checks list: a dome's fingers hitting each other are one."""
    found = _FINGERS.match(message)
    return f"{found.group(1)}: {found.group(2)}'s fingers run into each other" if found else ""


def _draw_check(layout, message, items, icon, clickable):
    """One check: its message wrapped, selecting its items in KiCad when live."""
    wrapped = _wrap(bpy.context, message)
    if items and clickable:
        operator = layout.operator(KILEIDO_OT_flex_show.bl_idname, text=wrapped[0], icon=icon, emboss=False)
        operator.ids = " ".join(items)  # KiCad ids: no spaces
    else:
        layout.label(text=wrapped[0], icon=icon)
    for line in wrapped[1:]:
        layout.label(text=line, icon="BLANK1")


class KILEIDO_OT_flex_group(bpy.types.Operator):
    bl_idname = "kileido.flex_group"
    bl_label = "Show or hide"
    bl_description = "Show or hide the checks of this kind"
    bl_options = {"INTERNAL"}

    group: StringProperty()

    def execute(self, context):
        opened = set(filter(None, context.scene.kileido_flex_open.split("\n")))
        opened ^= {self.group}
        context.scene.kileido_flex_open = "\n".join(sorted(opened))
        return {"FINISHED"}


def _draw_steps(box, scene):
    """The folding sequence, one row per step (with two or more): what folds in it, where
    the Fold slider is (done, folding, to come), and a button folding to its end."""
    plan = fold._plan()
    sequence = fold.foldmath.steps(plan) if plan is not None else []
    if len(sequence) < 2:
        return
    progress = scene.kileido_fold
    column = box.column(align=True)
    for count, (number, members) in enumerate(sequence, start=1):
        start, end = (count - 1) / len(sequence), count / len(sequence)
        icon = "CHECKMARK" if progress >= end - 1e-6 else ("PLAY" if progress > start + 1e-6 else "BLANK1")
        names = ", ".join(dict.fromkeys(fold.foldmath.handle_name(plan, plan.bends[k].handle) for k in members))
        row = column.row(align=True)
        row.alignment = "LEFT"  # as the rows above it read
        operator = row.operator(KILEIDO_OT_fold_step.bl_idname, text=f"Step {number}: {names}", icon=icon,
                                emboss=False)
        operator.progress = end


def _draw_animation(box, scene):
    """Key the fold on the timeline for rendering, or go back to live folding."""
    row = box.row(align=True)
    row.prop(scene, "kileido_fold_frames", text="Frames")
    row.prop(scene, "kileido_fold_fps", text="fps")
    row = box.row(align=True)
    keyed = fold.keyed()
    row.operator(KILEIDO_OT_fold_animation.bl_idname, text="Re-key fold animation" if keyed else "Key fold animation",
                 icon="KEYFRAME_HLT" if keyed else "KEYFRAME").action = "KEY"
    if keyed:
        row.operator(KILEIDO_OT_fold_animation.bl_idname, text="", icon="X").action = "CLEAR"
        box.label(text="Keyed: the Fold slider moves along the timeline", icon="INFO")


class KILEIDO_OT_fold_animation(bpy.types.Operator):
    bl_idname = "kileido.fold_animation"
    bl_label = "Fold animation"
    bl_description = ("Key the fold on the timeline for rendering: flat at frame 1, folded at the last frame, "
                      "the steps one after another. Key it again after an edit in KiCad")

    action: EnumProperty(items=(("KEY", "Key", "Key the fold on the timeline"),
                                ("CLEAR", "Clear", "Remove the keys: back to live folding")))

    @classmethod
    def description(cls, context, properties):
        if properties.action == "CLEAR":
            return "Remove the fold's keys: back to live folding with the Fold slider"
        return cls.bl_description

    def execute(self, context):
        if self.action == "CLEAR":
            fold.clear_animation()
            return {"FINISHED"}
        scene = context.scene
        if not fold.key_animation(scene.kileido_fold_frames, scene.kileido_fold_fps):
            self.report({"WARNING"}, "Nothing to fold: no bends on this board")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Fold keyed over frames 1-{scene.kileido_fold_frames} at {scene.kileido_fold_fps} fps")
        return {"FINISHED"}


class KILEIDO_OT_fold_step(bpy.types.Operator):
    bl_idname = "kileido.fold_step"
    bl_label = "Fold to this step"
    bl_description = "Fold the flex up to the end of this step of the folding sequence"

    progress: FloatProperty()

    def execute(self, context):
        context.scene.kileido_fold = self.progress
        return {"FINISHED"}


def _value_row(column, label):
    """A label on the left; returns the right-aligned row for its value."""
    split = column.split(factor=0.55, align=True)
    split.label(text=label)
    right = split.row(align=True)
    right.alignment = "RIGHT"
    return right


def _copy_button(row, text, where):
    operator = row.operator(KILEIDO_OT_copy_note.bl_idname, text="", icon="COPYDOWN", emboss=False)
    operator.text, operator.where = text, where


COVERLAY_NAMES = {"AMBER": "Amber", "BLACK": "Black", "WHITE": "White"}


def _coverlay_row(column, colour):
    """The coverlay's colour from KiCad (a "Coverlay black" text on the Flex layer; amber
    without one), with a menu copying the texts that set each colour."""
    right = _value_row(column, "Coverlay")
    right.label(text=COVERLAY_NAMES.get(colour, colour))
    right.menu(KILEIDO_MT_coverlay_texts.bl_idname, text="", icon="COPYDOWN")


class KILEIDO_MT_coverlay_texts(bpy.types.Menu):
    bl_idname = "KILEIDO_MT_coverlay_texts"
    bl_label = "Copy a coverlay text, to paste on the Flex layer in KiCad"

    def draw(self, context):
        for name in COVERLAY_NAMES.values():
            operator = self.layout.operator(KILEIDO_OT_copy_note.bl_idname, text=f"Coverlay {name.lower()}")
            operator.text, operator.where = f"Coverlay {name.lower()}", "on the Flex layer"


def _draw_stiffeners(box, stiffeners):
    """Each stiffener as its KiCad text says (the only source: nothing here overrides it); a
    material KiLeidoscope does not know says what it is drawn as instead."""
    clickable = live.connected()
    for index, stiffener in enumerate(stiffeners):
        row = box.row(align=True)
        text = f"Stiffener {index + 1}"
        if clickable:
            operator = row.operator(KILEIDO_OT_flex_show.bl_idname, text=text, icon="MOD_SOLIDIFY", emboss=False)
            operator.ids = stiffener["id"]
        else:
            row.label(text=text, icon="MOD_SOLIDIFY")
        _copy_button(row, stiffener["note"], f"in or beside stiffener {index + 1} on the Stiffener layer")
        for line in _wrap(bpy.context, stiffener["note"]):  # its own line: a long material reads in full
            box.label(text=line, icon="BLANK1")
        look, known = fold.foldmath.stiffener_look(stiffener.get("material") or "")
        if not known:
            note = (f'"{stiffener.get("material")}" is not a material KiLeidoscope knows: shown as '
                    f'{fold.foldmath.STIFFENER_LOOKS[look]}')
            for number, line in enumerate(_wrap(bpy.context, note)):
                box.label(text=line, icon="INFO" if number == 0 else "BLANK1")


class KILEIDO_OT_copy_note(bpy.types.Operator):
    bl_idname = "kileido.copy_note"
    bl_label = "Copy text for KiCad"

    text: StringProperty()
    where: StringProperty()

    @classmethod
    def description(cls, context, properties):
        return f'Copy "{properties.text}", to paste as a text {properties.where} in KiCad'

    def execute(self, context):
        context.window_manager.clipboard = self.text
        self.report({"INFO"}, f'Copied "{self.text}": paste it as a text {self.where} in KiCad')
        return {"FINISHED"}


class KILEIDO_OT_flex_show(bpy.types.Operator):
    bl_idname = "kileido.flex_show"
    bl_label = "Show in KiCad"
    bl_description = "Select these items in KiCad (and centre KiCad on them)"

    ids: StringProperty()

    @classmethod
    def poll(cls, context):
        if not live.connected():
            cls.poll_message_set("Not live with KiCad")
            return False
        return True

    def execute(self, context):
        live.request_select(self.ids.split(), False, context.scene.kileido_center_in_kicad)
        return {"FINISHED"}


def _layers_board(scene):
    """The board the Layers list shows: None for the live board, "ALL" for every board,
    else a view-only index."""
    value = getattr(scene, "kileido_layers_board", "LIVE")
    if value == "ALL" and packages.roots():
        return "ALL"
    if value in ("", "LIVE", "ALL") or packages.collection_of(int(value)) is None:
        return None
    return int(value)


_board_items = []  # Blender needs the enum item strings kept alive


def _layers_board_items(_scene, _context):
    _board_items[:] = [("ALL", "All boards", "Every board's layers together; an eye switches that layer on all")]
    _board_items.append(("LIVE", board.board_name or "Live board", "The board that follows KiCad"))
    _board_items.extend((str(root[packages.ROOT_TAG]), f"{root['kls_board_name']} (view-only)", "An imported board")
                        for root in packages.roots())
    return _board_items


class KILEIDO_OT_view_only_row(bpy.types.Operator):
    bl_idname = "kileido.view_only_row"
    bl_label = "Layer"
    bl_description = "Show or hide this layer of the view-only board (no row: all its layers)"

    index: IntProperty()
    row: StringProperty()

    def execute(self, context):
        recorded = packages.layer_list(self.index) or {"sections": []}
        rows = {entry["row"] for section in recorded["sections"] for entry in section["entries"]}
        if self.row:
            packages.set_row_visible(self.index, self.row, not packages.row_shown(self.index, self.row))
        else:
            everything = all(packages.row_shown(self.index, row) for row in rows)
            for row in rows:
                packages.set_row_visible(self.index, row, not everything)
        return {"FINISHED"}


class KILEIDO_board_row(bpy.types.PropertyGroup):
    """A row of the Boards list: "ALL", "LIVE" or a view-only board's index, and its label."""
    value: StringProperty()
    label: StringProperty()


def _board_rows_wanted():
    """(value, label) per row of the Boards list, as the boards are now."""
    rows = [("ALL", "All boards"), ("LIVE", board.board_name or "Live board")]
    rows.extend((str(root[packages.ROOT_TAG]), root["kls_board_name"]) for root in packages.roots())
    return rows


_rows_sync_pending = False


def _sync_board_rows():
    """Rebuild the Boards list's rows (a timer: a panel cannot write scene properties while
    drawing) and point its highlight at the chosen board; redraw the 3D Views."""
    global _rows_sync_pending
    _rows_sync_pending = False
    scene = bpy.context.scene
    wanted = _board_rows_wanted()
    rows = scene.kileido_board_rows
    if [(row.value, row.label) for row in rows] != wanted:
        rows.clear()
        for value, label in wanted:
            row = rows.add()
            row.value, row.label = value, label
    _layers_board_changed(scene, None)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _board_row_chosen(scene, _context):
    """The Boards list's highlight moved (a click on a row): show that board's layers."""
    rows = scene.kileido_board_rows
    if 0 <= scene.kileido_board_row < len(rows):
        value = rows[scene.kileido_board_row].value
        if scene.kileido_layers_board != value:
            scene.kileido_layers_board = value


def _layers_board_changed(scene, _context):
    """The chosen board changed (a click, a test, a removed board): move the highlight."""
    value = getattr(scene, "kileido_layers_board", "LIVE")
    for index, row in enumerate(scene.kileido_board_rows):
        if row.value == value and scene.kileido_board_row != index:
            scene.kileido_board_row = index


def _hint(line, text):
    """A dim note at the right end of a row."""
    hint = line.row(align=True)
    hint.alignment = "RIGHT"
    hint.active = False
    hint.label(text=text)


def _live_board_cells(line):
    """The live board's icon, name and source: the assembly variant selected in
    KiCad, else "live", or "dump"."""
    line.label(text="", icon="LINKED" if live.connected() else "FILE")
    line.label(text=board.board_name)
    variant = (board.export or {}).get("variant", "")
    _hint(line, (variant or "live") if live.connected() else "dump")


class KILEIDO_UL_boards(bpy.types.UIList):
    """The Boards list: a row per board in columns (select arrow, eye or icon, name, hint,
    remove), "All boards" first. A click on a row shows that board's layers below."""
    bl_idname = "KILEIDO_UL_boards"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        line = layout.row(align=True)
        if item.value == "ALL":
            line.label(text="", icon="BLANK1")
            line.label(text="", icon="OUTLINER_COLLECTION")
            line.label(text=item.label)
            return
        if item.value == "LIVE":
            arrow = line.row(align=True)
            arrow.enabled = False  # the live board stays where KiCad puts it
            arrow.operator(KILEIDO_OT_select_board.bl_idname, text="", emboss=False, icon="RESTRICT_SELECT_ON")
            _live_board_cells(line)
            return
        board_index = int(item.value)
        collection = packages.collection_of(board_index)
        shown = collection is not None and not collection.hide_viewport
        arrow = line.row(align=True)
        arrow.enabled = shown  # select needs its objects visible
        arrow.operator(KILEIDO_OT_select_board.bl_idname, text="", emboss=False,
                       icon="RESTRICT_SELECT_OFF").index = board_index
        toggle = line.operator(KILEIDO_OT_view_only_board.bl_idname, text="", emboss=False,
                               icon="HIDE_OFF" if shown else "HIDE_ON")
        toggle.index, toggle.action = board_index, "TOGGLE"
        name = line.row(align=True)
        name.active = shown
        name.label(text=item.label)
        _hint(line, "view-only")
        drop = line.operator(KILEIDO_OT_view_only_board.bl_idname, text="", emboss=False, icon="X")
        drop.index, drop.action = board_index, "REMOVE"

    def draw_filter(self, context, layout):
        pass  # a few rows: nothing to filter or sort


class KILEIDO_OT_select_board(bpy.types.Operator):
    bl_idname = "kileido.select_board"
    bl_label = "Select board"
    bl_description = ("Select this view-only board to move (G), rotate (R) or scale (S) it. "
                      "The live board stays where KiCad puts it")
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()

    def execute(self, context):
        if not packages.select(self.index):
            self.report({"WARNING"}, "KiLeidoscope: show the board first")
            return {"CANCELLED"}
        return {"FINISHED"}


class KILEIDO_OT_all_boards_row(bpy.types.Operator):
    bl_idname = "kileido.all_boards_row"
    bl_label = "Layer on all boards"
    bl_description = "Show or hide this layer on every board that has it (no row: all layers)"

    row: StringProperty()

    def execute(self, context):
        rows = [row for _, entries in packages.all_boards_sections() for row, *_ in entries]
        chosen = [self.row] if self.row else rows
        visible = not all(packages.shown_everywhere(row) for row in chosen)
        for row in chosen:
            packages.set_row_everywhere(row, visible)
        return {"FINISHED"}


class _Boards:
    """The Boards section: the live board (it follows KiCad) and the view-only boards
    beside it; then one board's layers top to bottom, an eye each, like KiCad's layer
    list: the live board's (layers.rows), a view-only board's as exported
    (layers.recorded), or all."""

    def __init__(self, layout):
        self.layout = layout

    def draw(self, context):
        scene = context.scene
        self._draw_boards(context)
        index = _layers_board(scene)
        if packages.roots():
            self.layout.separator()
        if index == "ALL":
            self._draw_all_boards(scene)
        elif index is None:
            self._draw_live(scene)
        else:
            self._draw_view_only(index)

    def _draw_boards(self, context):
        """The live board alone: its name. With view-only boards: the Boards list
        (KILEIDO_UL_boards), its rows refreshed by a timer when the boards changed."""
        global _rows_sync_pending
        layout = self.layout
        scene = context.scene
        if not packages.roots():
            if board.collection is not None:
                _live_board_cells(layout.row(align=True))
        else:
            rows = scene.kileido_board_rows
            if [(row.value, row.label) for row in rows] != _board_rows_wanted() and not _rows_sync_pending:
                _rows_sync_pending = True
                bpy.app.timers.register(_sync_board_rows)
            layout.template_list(KILEIDO_UL_boards.bl_idname, "", scene, "kileido_board_rows",
                                 scene, "kileido_board_row", rows=len(rows), maxrows=len(rows))
        if packages.roots():
            row = layout.row(align=True)
            row.prop(context.scene, "kileido_collisions", text="Collision check")
            text = collisions.status() if context.scene.kileido_collisions else None
            if text:
                found = row.row(align=True)
                found.alignment = "RIGHT"
                found.alert = text != "No collisions"
                found.label(text=text, icon="ERROR" if found.alert else "CHECKMARK")
        row = layout.row(align=True)
        row.operator(KILEIDO_OT_export_board.bl_idname, text="Export…", icon="EXPORT")
        row.operator(KILEIDO_OT_import_board.bl_idname, text="Import…", icon="IMPORT")

    def _draw_live(self, scene):
        rows = layers.rows()
        if board.collection is None or not rows:
            self.layout.label(text="No board loaded")
            return
        sections = [(title, [(entry, layers.swatch_color(entry)) for entry in entries])
                    for title, entries in layers.sections()]

        def eye(line, row, on):
            line.prop(scene, layers.property_name(row), text="", emboss=False,
                      icon="HIDE_OFF" if on else "HIDE_ON")

        def all_eye(header, everything):
            header.operator(KILEIDO_OT_all_layers.bl_idname, text="All layers",
                            icon="HIDE_OFF" if everything else "HIDE_ON", emboss=False)

        def objects_end(title, column):
            if title == "Objects":  # not a row of its own: DNP parts belong to Components
                line = column.row(align=True)
                on = scene.kileido_show_dnp
                line.prop(scene, "kileido_show_dnp", text="", emboss=False, icon="HIDE_OFF" if on else "HIDE_ON")
                name = line.row(align=True)
                name.active = on
                entry = layers.Entry(None, "DNP components", "components")
                name.label(text=entry.label, icon_value=_swatch(entry.kind, layers.swatch_color(entry)))

        self._draw_rows(sections, board.thickness_m, layers.shown, eye, all_eye, objects_end)
        _draw_thickness(self.layout, scene, board.layer_thickness.get("F.Cu"))

    def _draw_view_only(self, index):
        recorded = packages.layer_list(index)
        if recorded is None:
            self.layout.label(text="This export has no layer list")
            return
        sections = [(section["title"], [(layers.Entry(item["row"], item["label"], item["kind"],
                                                      item["thickness_m"], item["detail"]), item["color"])
                                        for item in section["entries"]])
                    for section in recorded["sections"]]

        def eye(line, row, on):
            button = line.operator(KILEIDO_OT_view_only_row.bl_idname, text="", emboss=False,
                                   icon="HIDE_OFF" if on else "HIDE_ON")
            button.index, button.row = index, row

        def all_eye(header, everything):
            button = header.operator(KILEIDO_OT_view_only_row.bl_idname, text="All layers",
                                     icon="HIDE_OFF" if everything else "HIDE_ON", emboss=False)
            button.index, button.row = index, ""

        self._draw_rows(sections, recorded["thickness_m"], lambda row: packages.row_shown(index, row), eye,
                        all_eye)
        _draw_thickness(self.layout, bpy.context.scene, packages.copper_thickness_of(index))
        found = packages.ims_of(index)
        if found:  # as exported: a view-only board keeps its own base
            base = found["base"][1] - found["base"][0]
            self.layout.box().label(text=f"IMS: {ims.METALS.get(found['metal'], found['metal'])} base, "
                                         f"{layers.format_thickness(base)}", icon="INFO")

    def _draw_all_boards(self, scene):
        sections = [(title, [(layers.Entry(row, label, kind), color) for row, label, kind, color in entries])
                    for title, entries in packages.all_boards_sections()]

        def eye(line, row, on):
            button = line.operator(KILEIDO_OT_all_boards_row.bl_idname, text="", emboss=False,
                                   icon="HIDE_OFF" if on else "HIDE_ON")
            button.row = row

        def all_eye(header, everything):
            header.operator(KILEIDO_OT_all_boards_row.bl_idname, text="All layers",
                            icon="HIDE_OFF" if everything else "HIDE_ON", emboss=False).row = ""

        self._draw_rows(sections, None, packages.shown_everywhere, eye, all_eye)
        _draw_thickness(self.layout, scene, None)  # copper: each board's own stackup

    def _draw_rows(self, sections, thickness_m, shown, eye, all_eye, section_end=None):
        everything = all(shown(entry.row) for _, entries in sections for entry, _ in entries)
        header = self.layout.row()
        all_eye(header, everything)
        total = header.row()
        total.alignment = "RIGHT"
        total.active = False
        total.label(text=layers.format_thickness(thickness_m))
        for title, entries in sections:
            box = self.layout.box()
            box.label(text=title)
            column = box.column(align=True)
            for entry, color in entries:
                line = column.row(align=True)
                on = shown(entry.row)
                eye(line, entry.row, on)
                name = line.row(align=True)
                name.active = on
                name.label(text=entry.label, icon_value=_swatch(entry.kind, color))
                values = " · ".join(text for text in (entry.detail, layers.format_thickness(entry.thickness_m))
                                    if text)
                if values:
                    right = line.row(align=True)
                    right.alignment = "RIGHT"
                    right.active = False
                    right.label(text=values)
            if section_end is not None:
                section_end(title, column)


class _Status:
    """The Status section: link, loading progress and warnings, below Boards."""

    def __init__(self, layout):
        self.layout = layout

    def draw(self, context):
        column = self.layout.column(align=True)
        lines = [live.status_text(), live.outdated_pads_text(), models.export_status(), cosmetics.status(),
                 live.last_apply_text()]
        # The dielectric warning concerns RF numbers only; the viewer never uses them.
        lines += [warning for warning in board.warnings
                  if "no dielectric properties from KiCad IPC" not in warning and
                  not warning.startswith(packages.OUTLINE_PROBLEM)][:4]  # outline: the top box
        for line in lines:
            if line:
                column.label(text=line[:80])
        if live.connected():  # a new KiCad is never followed on its own: it may be an unrelated one
            self.layout.operator("kileido.resync", icon="FILE_REFRESH",
                                 text="Resync with new KiCad" if live.new_kicad() else "Resync")


LED_COLORS = {"ok": (0.2, 0.85, 0.3), "busy": (1.0, 0.68, 0.1), "down": (0.92, 0.2, 0.18),
              "off": (0.45, 0.45, 0.45)}
SWATCH_PX = 32
# Rows of the swatch the layer fills, like a cross-section: thin films, copper, laminate.
SWATCH_BAND = {"silk": 8, "mask": 10, "copper": 12, "dielectric": 26}


def _swatch(kind, color):
    """A preview icon in `color`: a band for stackup layers (thickness by kind), a ring
    for vias, a tile for anything else. Cached by kind and colour."""
    if _icons is None:
        return 0
    rgb = tuple(round(float(channel), 3) for channel in color[:3])
    key = f"swatch {kind} {rgb}"
    if key in _icons:
        return _icons[key].icon_id
    import numpy as np
    size = SWATCH_PX
    centre = (size - 1) / 2
    y, x = np.mgrid[0:size, 0:size]
    if kind in SWATCH_BAND:
        half = SWATCH_BAND[kind] / 2
        inside = (np.abs(y - centre) <= half) & (x >= 1) & (x <= size - 2)
        edge = inside & ((np.abs(y - centre) > half - 1) | (x < 2) | (x > size - 3))
    elif kind == "led":  # a lit dome: bright centre fading to the rim
        radius = np.hypot(x - centre, y - centre)
        inside = radius <= 9
        edge = inside & (radius > 8)
    elif kind == "vias":
        radius = np.hypot(x - centre, y - centre)
        inside = (radius <= 12) & (radius >= 5)
        edge = inside & ((radius > 11) | (radius < 6))
    else:
        inside = (np.abs(x - centre) <= 11) & (np.abs(y - centre) <= 11)
        edge = inside & ((np.abs(x - centre) > 10) | (np.abs(y - centre) > 10))
    pixels = np.zeros((size, size, 4), dtype=np.float32)
    pixels[inside] = (*rgb, 1.0)
    pixels[edge, :3] *= 0.6  # a darker rim keeps light and dark swatches apart from the panel
    preview = _icons.new(key)
    for prefix in ("image", "icon"):
        setattr(preview, f"{prefix}_size", (size, size))
        getattr(preview, f"{prefix}_pixels_float").foreach_set(pixels.ravel())
    return preview.icon_id


CLASSES = (KILEIDO_OT_load_dump, KILEIDO_OT_export_board, KILEIDO_OT_import_board, KILEIDO_OT_view_only_board,
           KILEIDO_OT_view_only_row, KILEIDO_OT_all_boards_row, KILEIDO_OT_select_board, KILEIDO_board_row,
           KILEIDO_UL_boards, KILEIDO_OT_resync,
           KILEIDO_OT_viewport, KILEIDO_OT_cut_plane, KILEIDO_OT_pick, KILEIDO_OT_all_layers, KILEIDO_OT_flex_show,
           KILEIDO_OT_copy_note, KILEIDO_MT_coverlay_texts, KILEIDO_OT_fold_step, KILEIDO_OT_fold_animation,
           KILEIDO_OT_flex_group, *columns.CLASSES, KILEIDO_PT_panel)
_icons = None  # bpy.utils.previews collection with the logo and ICON_FILES
ICON_FILES = ("logo", "xray", "scissors", "bucket")
LOGO_SCALE = 6.0  # the logo at the top of the panel, in icon heights
_KEYMAPS = []


def _layer_update(layer):
    def update(scene, _context):
        cosmetics.set_layer_visible(layer, getattr(scene, cosmetics.property_name(layer)))
    return update


def _row_update(row):
    def update(_scene, _context):
        layers.refresh(row)
    return update


def _show_dnp_update():
    footprints.refresh_dnp()
    cut.invalidate()
    collisions.schedule()


def _thickness_update(refresh_live):
    """A Thickness setting changed: the live board, then every view-only board."""
    def update(_scene, _context):
        refresh_live()
        packages.refresh_thickness()
    return update


IMS_REBUILD_DELAY_S = 0.4  # after the last change: dragging the epoxy value rebuilds once


_switched_off = {}  # mode switched on -> the other mode it turned off, for its column until it closes


def _ims_switched(scene, _context):
    """IMS on or off: Flex goes off when IMS comes on (never both), then the board rebuilds."""
    _switched_off.clear()
    if scene.kileido_ims and scene.kileido_flex:
        scene.kileido_flex = False  # removes the folded copies
        _switched_off["IMS"] = "Flex"
    _ims_update(scene, _context)


def _flex_switched(scene, _context):
    """Flex on or off: IMS goes off when Flex comes on (never both); the folded copies are
    baked again, or removed (fold._plan follows the setting)."""
    _switched_off.clear()
    if scene.kileido_flex and scene.kileido_ims:
        scene.kileido_ims = False  # rebuilds the board without its base
        _switched_off["FLEX"] = "IMS"
    if not scene.kileido_flex:
        fold.clear_animation()  # the keys turn handles that are about to go
        scene.kileido_fold = 0.0  # back on, the handles start flat: so does the slider
    fold.invalidate()


def _ims_update(_scene, _context):
    """IMS mode or its base changed: every height moves, so the board comes again (from
    KiCad, or the dump file) and apply._shape_ims fits it to the new stack as it arrives."""
    if bpy.app.timers.is_registered(_ims_rebuild):
        bpy.app.timers.unregister(_ims_rebuild)
    bpy.app.timers.register(_ims_rebuild, first_interval=IMS_REBUILD_DELAY_S)


def _ims_source():
    """Where the board can come from again: "live", "dump", or None (the panel says so)."""
    return "live" if live.connected() else "dump" if dump.last_path else None


def _ims_rebuild():
    if board.collection is None or not ims.eligible(board.heights)[0]:
        return None
    source = _ims_source()
    if source == "live":
        live.request_resync()
    elif source == "dump":
        dump.load_async(dump.last_path)
    return None


def _mask_opacity_update():
    """The solder mask opacity changed: recolour the live board, then the view-only ones."""
    if board.materials:
        apply.set_color_mode(board.color_mode)
    packages.refresh_mask_opacity()


def _scene_properties():
    """Every Scene property the add-on registers (and removes again), by attribute name."""
    properties = {
        "kileido_layers_board": EnumProperty(
            name="Board", description="Whose layers the list below shows and switches",
            items=_layers_board_items, update=_layers_board_changed),
        "kileido_board_rows": CollectionProperty(type=KILEIDO_board_row),
        "kileido_board_row": IntProperty(
            name="Board", description="The highlighted row of the Boards list: whose layers show below",
            update=_board_row_chosen),
        "kileido_collisions": BoolProperty(
            name="Collision check", default=True,
            description="Mark where the live board and the view-only boards overlap, with a red box",
            update=lambda self, context: collisions.schedule()),
        "kileido_color_mode": EnumProperty(
            name="Colors", items=(("FAB", "Board stackup", "Saved mask colors and KiCad 3D copper color"),
                                  ("REALISTIC", "Realistic", "Lit board materials, solder mask and exposed metal"),
                                  ("EDITOR", "PCB Editor", "Active KiCad PCB Editor layer colors")),
            default="FAB", update=lambda self, context: apply.set_color_mode(self.kileido_color_mode)),
        "kileido_shaded": BoolProperty(
            name="Shaded", default=True,
            description="Board stackup and PCB Editor colors lit by the studio lights (and the cut light), "
                        "with shading and shadows, so depth shows. Off: flat colors, as KiCad draws them",
            update=lambda self, context: apply.set_color_mode(self.kileido_color_mode)),
        "kileido_show_board": BoolProperty(
            name="Show board solid", default=True,
            update=lambda self, context: apply.set_board_visible(self.kileido_show_board)),
        "kileido_light_power": FloatProperty(
            name="Light strength", default=lighting.SOFTBOX_POWER_W, min=0.0, soft_max=1.0, step=1,
            precision=3, unit="POWER",
            description="Power of each studio softbox (one above and one below the board); "
                        "dimmed automatically for light solder masks",
            update=lighting.apply_settings),
        "kileido_fill_color": FloatVectorProperty(
            name="Fill color", subtype="COLOR", size=3, min=0.0, max=1.0, default=lighting.FILL_COLOR,
            description="Surroundings seen in shadows and metal reflections (the background stays black)",
            update=lighting.apply_settings),
        "kileido_reflections": EnumProperty(
            name="Reflections",
            items=(("NONE", "None", "Reflections see the fill colour, as KiLeidoscope always looked"),
                   *((name, name.capitalize(), f"Blender's bundled \"{name}\" studio light, seen only in "
                      "reflections (glossy solder mask, metal finishes, parts)") for name in lighting.REFLECTIONS)),
            default=lighting.DEFAULT_REFLECTIONS,
            description="What glossy surfaces reflect: a studio environment seen only in reflections, so the "
                        "softboxes' light and the black background stay as they are",
            update=lighting.apply_settings),
        "kileido_focus": BoolProperty(
            name="X-ray mode", default=False,
            description="While something is selected in KiCad, everything else turns see-through and grey",
            update=lambda self, context: focus.refresh()),
        "kileido_cut": BoolProperty(
            name="Cut plane", default=False,
            description="Cut the board open along a plane (the \"KLS cut plane\" object: move or turn it) and, "
                        "while it stands upright, show the cross section: laminate, copper layers, vias",
            update=lambda self, context: cut.refresh()),
        "kileido_cut_flip": BoolProperty(
            name="Flip", default=False, description="Remove the other side of the cut plane",
            update=lambda self, context: cut.push()),
        "kileido_cut_face": BoolProperty(
            name="Section face", default=True,
            description="Draw the cross section on the plane (the \"KLS cut face\" object). Off: look into the "
                        "cut board itself; hide Board under Layers to see its copper and vias",
            update=lambda self, context: cut.rebuild()),
        "kileido_cut_light": BoolProperty(
            name="Cut light", default=False,
            description="A light on the removed side shining into the cut along the plane's normal (the "
                        "\"KLS cut light\" object, as strong as the Light setting). It lights Realistic and "
                        "Shaded colors, and parts; flat colors ignore lights",
            update=lambda self, context: cut.place_light()),
        "kileido_via_fill_material": EnumProperty(
            name="Via fill", items=(("RESIN", "Resin", "Epoxy-filled barrels (milky)"),
                                    ("COPPER", "Copper", "Copper-filled barrels")),
            default="RESIN",
            description="What fills the vias KiCad marks filled (or capped). KiCad sets each via's protection "
                        "(select it, E, Protection features); it does not store the fill material",
            update=lambda self, context: apply.refresh_protection()),
        "kileido_max_tent_mm": FloatProperty(
            name="Max tent hole", default=protection.MAX_TENT_M * 1e3, min=0.05, soft_max=1.0, max=5.0,
            step=1, precision=2,
            description="Largest finished hole (mm; the drill less twice the Via wall) a solder mask tent can span. "
                        "A via KiCad tents or covers over a larger empty hole is shown open, and listed here",
            update=lambda self, context: apply.refresh_protection()),
        "kileido_via_plating_um": FloatProperty(
            name="Via wall (µm)", default=25.0, min=5.0, max=100.0, step=100, precision=0,
            description="Plating thickness of a via's barrel, in 3D and in the cross section (KiCad stores none)",
            update=lambda self, context: apply.refresh_plating()),
        "kileido_show_dnp": BoolProperty(
            name="DNP components", default=True,
            description="Show the 3D models of footprints KiCad marks \"Do not populate\". "
                        "Off: hide them (their pads stay)",
            update=lambda self, context: _show_dnp_update()),
        "kileido_center_in_kicad": BoolProperty(
            name="Center KiCad on click", default=True,
            description="Clicking an item here also pans KiCad's PCB editor to centre it, keeping its zoom"),
        "kileido_copper_3d": BoolProperty(
            name="Copper thickness", default=True,
            description="Give outer copper its stackup thickness; the mask sits on the laminate between it",
            update=_thickness_update(apply.refresh_thickness)),
        "kileido_ims": BoolProperty(
            name="IMS", default=False,
            description="Insulated metal substrate: a 2-layer board on an aluminum or copper base. The board "
                        "keeps KiCad's thickness; the panel's epoxy sits under F.Cu and the base takes the rest, "
                        "B.Cu's place included. Not with Flex",
            update=_ims_switched),
        "kileido_flex": BoolProperty(
            name="Flex", default=False,
            description="Flex mode, for a board KiCad marks as flex: the board folds at its bends, with its "
                        "stiffeners, coverlay and the flex checks. Not with IMS",
            update=_flex_switched),
        "kileido_column": EnumProperty(
            name="Column", items=columns.column_items(), default=columns.NONE, options={"HIDDEN"},
            description="The column open left of the KiLeidoscope panel (its bookmark tab)",
            update=lambda self, context: _switched_off.clear()),  # its note was for the column then open
        "kileido_ims_metal": EnumProperty(
            name="Base metal", items=tuple((key, name, f"{name} base") for key, name in ims.METALS.items()),
            default="AL", description="The metal of the IMS base (KiCad stores none)",
            update=lambda self, context: apply.refresh_ims_metal()),
        "kileido_ims_finish": EnumProperty(
            name="Base finish", items=tuple((key, label, f"{label} base (Realistic colours)")
                                            for key, (label, _, _) in ims.FINISHES.items()),
            default="MILL", description="The IMS base's outer faces in Realistic colours; the cut plane always "
                                        "shows a polished section, as a micrograph does",
            update=lambda self, context: apply.refresh_ims_finish()),
        "kileido_ims_epoxy_um": IntProperty(
            name="Epoxy", default=ims.EPOXY_UM[2], min=ims.EPOXY_UM[0], max=ims.EPOXY_UM[1],
            description="The IMS's thermal dielectric under F.Cu, in µm (the fab's; KiCad's 2-layer stackup "
                        "holds the board's thickness). The metal base takes the rest of the board",
            update=_ims_update),
        "kileido_fold": FloatProperty(
            name="Fold", default=0.0, min=0.0, max=1.0, step=5, precision=2, subtype="FACTOR",
            description="Fold the flex at its bends: 0 flat, 1 every bend at its angle from KiCad's Bend layer; "
                        "bends numbered #1, #2, ... fold one step after another",
            update=lambda self, context: fold.set_progress(self.kileido_fold)),
        "kileido_fold_frames": IntProperty(
            name="Frames", default=72, min=2, max=10000,
            description="How many frames the fold animation takes, from flat to folded"),
        "kileido_fold_fps": IntProperty(
            name="Frame rate", default=24, min=1, max=240,
            description="The scene's frames per second for the fold animation"),
        "kileido_flex_open": StringProperty(
            name="Open flex checks", default="",
            description="The kinds of flex check shown in full in the panel, one per line"),
        "kileido_flex_use": EnumProperty(
            name="Flex use", items=(("STATIC", "Static", "Bent once, at assembly"),
                                    ("DYNAMIC", "Dynamic", "Flexing again and again in use")),
            default="STATIC", description="How the flex is used: dynamic flex needs much larger bend radii"),
        "kileido_show_solder": BoolProperty(
            name="Solder paste", default=False,
            description="Stencil deposits on pads with a paste aperture (KiCad's F.Paste/B.Paste pad shapes)",
            update=_thickness_update(apply.refresh_solder)),
        "kileido_stencil_mm": FloatProperty(
            name="Stencil thickness", default=0.12, min=0.03, max=0.3, step=1, precision=3, unit="NONE",
            description="Paste deposit height in mm (the board file does not store the stencil thickness)",
            update=_thickness_update(apply.refresh_solder)),
        "kileido_silk_3d": BoolProperty(
            name="Silkscreen thickness", default=True,
            description="Raise the silkscreen ink by a thickness, with walls along its edges "
                        "(KiCad stores no silkscreen thickness)",
            update=_thickness_update(cosmetics.recolor)),
        "kileido_silk_um": FloatProperty(
            name="Silkscreen thickness (µm)", default=15.0, min=1.0, max=60.0, step=100, precision=0,
            description="Silkscreen ink height in micrometres",
            update=_thickness_update(cosmetics.recolor)),
        "kileido_mask_opacity": FloatProperty(
            name="Solder mask opacity", default=1.0, min=0.0, max=1.0, step=5, precision=2, subtype="FACTOR",
            description="How much the solder mask hides the copper and laminate under it "
                        "(1: KiCad's mask colour; lower shows the traces under the mask more clearly)",
            update=lambda self, context: _mask_opacity_update()),
        "kileido_silk_opacity": FloatProperty(
            name="Silkscreen opacity", default=1.0, min=0.0, max=1.0, step=5, precision=2, subtype="FACTOR",
            description="How much the silkscreen ink hides what lies under it. Printed on the mask, the "
                        "ink follows the copper, so traces under it show by their relief at any opacity",
            update=lambda self, context: cosmetics.apply_silk_settings()),
        "kileido_clip_silkscreen": BoolProperty(
            name="Clip silkscreen to board outline", default=True,
            description="Hide silkscreen outside Edge.Cuts and inside board cutouts",
            update=lambda self, context: cosmetics.refresh_geometry()),
    }
    for layer, label, default in cosmetics.LAYERS:
        properties[cosmetics.property_name(layer)] = BoolProperty(
            name=label, default=default, update=_layer_update(layer))
    for row in (*layers.COPPER_LAYERS, "Vias", "Components", "Placeholders"):
        properties[layers.property_name(row)] = BoolProperty(
            name=row, default=True, update=_row_update(row))
    return properties


@persistent
def _file_loaded(_):
    """Loading a file frees every ID the board state refers to: start over, and have a live
    bridge send the board again. A reflection image saved by another Blender install is
    found again in this one."""
    board.reset()
    lighting.refresh_reflections()
    live.request_resync()


def register():
    global _icons
    _icons = bpy.utils.previews.new()
    for icon in ICON_FILES:  # icons/<name>.png: the logo and the panel's own symbols
        path = Path(__file__).with_name("icons") / f"{icon}.png"
        if path.is_file():
            _icons.load(icon, str(path), "IMAGE")
    watcher.sweep_stale()  # scratch folders a crashed Blender left behind
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for name, prop in _scene_properties().items():
        setattr(bpy.types.Scene, name, prop)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is not None:  # None in background mode
        keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
        # A press on a bookmark tab opens its column: handled on the press, it never
        # becomes the click that selects in KiCad below. Elsewhere it passes through.
        _KEYMAPS.append((keymap, keymap.keymap_items.new(columns.KILEIDO_OT_column_tab.bl_idname,
                                                         "LEFTMOUSE", "PRESS")))
        # A click (press and release without moving) selects in KiCad while connected;
        # dragging still box-selects or navigates. Not connected, the operator's poll
        # fails and the click falls through to Blender's own selection.
        for shift in (False, True):
            entry = keymap.keymap_items.new(KILEIDO_OT_pick.bl_idname, "LEFTMOUSE", "CLICK", shift=shift)
            entry.properties.extend = shift
            _KEYMAPS.append((keymap, entry))
    collisions.install()
    columns.install()
    cut.install()
    fold.install()
    bpy.app.handlers.load_post.append(_file_loaded)
    bpy.app.handlers.save_pre.append(holes.pack_for_save)


def unregister():
    if _file_loaded in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_file_loaded)
    if holes.pack_for_save in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(holes.pack_for_save)
    for timer in (dump.drain, _ims_rebuild):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    for keymap, entry in _KEYMAPS:
        keymap.keymap_items.remove(entry)
    _KEYMAPS.clear()
    live.disconnect()
    models.stop_following()
    cosmetics.stop_following()
    render_depth.uninstall()
    collisions.uninstall()
    columns.uninstall()
    cut.uninstall()
    fold.uninstall()
    edge_plating.uninstall()
    for name in _scene_properties():
        delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    global _icons
    if _icons is not None:
        bpy.utils.previews.remove(_icons)
        _icons = None
