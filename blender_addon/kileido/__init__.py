"""KiLeidoscope board viewer for Blender 5.1."""

bl_info = {
    "name": "KiLeidoscope",
    "author": "KiLeidoscope contributors",
    "version": (0, 3, 7),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > KiLeidoscope",
    "description": "View read-only KiCad board geometry from a dump or live bridge",
    "category": "3D View",
}

import textwrap
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       StringProperty)
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import (apply, collisions, cosmetics, dump, focus, layers, lighting, live, models, packages, pick,
               render_depth, watcher)
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
        layout = self.layout
        icon = _icons.get("logo") if _icons is not None else None
        if icon is not None:
            logo = layout.row()
            logo.alignment = "CENTER"
            logo.template_icon(icon_value=icon.icon_id, scale=LOGO_SCALE)
        self._draw_link(context)
        self._draw_outline_warnings(context)
        row = layout.row(align=True)
        current = _viewport_mode(context)
        for mode, label in (("MATERIAL", "Preview"), ("RENDERED", "Cycles")):
            row.operator(KILEIDO_OT_viewport.bl_idname, text=label, depress=current == mode).mode = mode
        layout.prop(context.scene, "kileido_color_mode", text="Colors")
        finish = board.appearance.get("copper_finish")
        if finish:
            layout.label(text="Board finish: " + ("Bare copper" if finish.casefold() == "none" else finish))
        row = layout.row(align=True)
        row.prop(context.scene, "kileido_light_power", text="Light")
        row.prop(context.scene, "kileido_fill_color", text="")
        layout.prop(context.scene, "kileido_reflections", text="Reflections")
        layout.prop(context.scene, "kileido_mask_opacity", text="Solder mask opacity", slider=True)
        layout.prop(context.scene, "kileido_silk_opacity", text="Silkscreen opacity", slider=True)
        for prop, label, icon in (("kileido_via_fill", "Via fill (capped vias)", "bucket"),
                                  ("kileido_focus", "X-ray mode", "xray"),
                                  ("kileido_center_in_kicad", "Center KiCad on click", None),
                                  ("kileido_clip_silkscreen", "Clip silkscreen to board outline", "scissors")):
            row = layout.row(align=True)
            row.prop(context.scene, prop, text=label)
            if _icons is not None and icon in _icons:
                row.label(text="", icon_value=_icons[icon].icon_id)

    def _draw_outline_warnings(self, context):
        """KiCad's own words when a board has no usable Edge.Cuts outline, then where."""
        found = [(board.board_name, warning) for warning in packages.outline_warnings()]
        for root in packages.roots():
            index = root[packages.ROOT_TAG]
            found += [(root["kls_board_name"], warning) for warning in packages.outline_warnings(index)]
        if not found:
            return
        box = self.layout.box()
        box.label(text="Board outline is missing or malformed.", icon="ERROR")
        box.label(text="Run DRC in KiCad for a full analysis.", icon="BLANK1")
        named = len({name for name, _ in found}) > 1 or bool(packages.roots())
        for name, warning in found[:6]:
            detail = warning.removeprefix(packages.OUTLINE_PROBLEM).lstrip(": ")
            for line in _wrap(context, f"{name}: {detail}" if named else detail):
                box.label(text=line, icon="BLANK1")

    def _draw_link(self, context):
        """A status LED: green live, amber waiting or KiCad editing, red lost, grey offline."""
        health = live.health()
        text = {"ok": "Live with KiCad",
                "off": "Not live: use Open in Blender in KiCad"}.get(health, live.status_text())
        self.layout.label(text=text, icon_value=_swatch("led", LED_COLORS[health]))
        error = live.error_text()
        if error:  # e.g. a second KiCad on Windows: say why the scene is empty
            box = self.layout.box()
            for index, line in enumerate(_wrap(context, error)):
                box.label(text=line, icon="ERROR" if index == 0 else "BLANK1")


def _wrap(context, text):
    """Lines that fit the sidebar at its current width (Blender cuts a longer label in
    its middle): ~7 px per character and an icon and box margins, at the UI scale."""
    scale = context.preferences.system.ui_scale or 1.0  # 0 without a window
    width = context.region.width if context.region else 300 * scale
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


class KILEIDO_OT_select_board(bpy.types.Operator):
    bl_idname = "kileido.select_board"
    bl_label = "Select board"
    bl_description = ("Select the chosen view-only board to move (G), rotate (R) or scale (S) it. "
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


class KILEIDO_PT_boards(bpy.types.Panel):
    """The live board (it follows KiCad) and the view-only boards beside it; then one
    board's layers top to bottom, an eye each, like KiCad's layer list: the live
    board's (layers.rows), a view-only board's as exported (layers.recorded), or all."""
    bl_label = "Boards"
    bl_idname = "KILEIDO_PT_boards"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "KiLeidoscope"
    bl_parent_id = "KILEIDO_PT_panel"

    def draw(self, context):
        scene = context.scene
        self._draw_boards(context)
        index = _layers_board(scene)
        if packages.roots():
            self.layout.separator()
            row = self.layout.row(align=True)
            row.prop(scene, "kileido_layers_board", text="")
            pick = row.row(align=True)
            pick.enabled = isinstance(index, int)  # the live board stays where KiCad puts it
            button = pick.operator(KILEIDO_OT_select_board.bl_idname, text="", icon="RESTRICT_SELECT_OFF")
            button.index = index if isinstance(index, int) else 0
        if index == "ALL":
            self._draw_all_boards(scene)
        elif index is None:
            self._draw_live(scene)
        else:
            self._draw_view_only(index)

    def _draw_boards(self, context):
        layout = self.layout
        column = layout.column(align=True)
        if board.collection is not None:
            line = column.row(align=True)
            line.label(text=board.board_name, icon="LINKED" if live.connected() else "FILE")
            hint = line.row(align=True)
            hint.alignment = "RIGHT"
            hint.active = False
            hint.label(text="live" if live.connected() else "dump")
        for root in packages.roots():
            index = root[packages.ROOT_TAG]
            collection = packages.collection_of(index)
            if collection is None:
                continue
            line = column.row(align=True)
            shown = not collection.hide_viewport
            toggle = line.operator(KILEIDO_OT_view_only_board.bl_idname, text="", emboss=False,
                                   icon="HIDE_OFF" if shown else "HIDE_ON")
            toggle.index, toggle.action = index, "TOGGLE"
            name = line.row(align=True)
            name.active = shown
            name.label(text=root["kls_board_name"])
            hint = line.row(align=True)
            hint.alignment = "RIGHT"
            hint.active = False
            hint.label(text="view-only")
            drop = line.operator(KILEIDO_OT_view_only_board.bl_idname, text="", emboss=False, icon="X")
            drop.index, drop.action = index, "REMOVE"
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

        self._draw_rows(sections, board.thickness_m, layers.shown, eye, all_eye)
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

    def _draw_rows(self, sections, thickness_m, shown, eye, all_eye):
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


class KILEIDO_PT_status(bpy.types.Panel):
    """Link, loading progress and warnings, below the Boards panel."""
    bl_label = "Status"
    bl_idname = "KILEIDO_PT_status"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "KiLeidoscope"
    bl_parent_id = "KILEIDO_PT_panel"

    def draw(self, context):
        column = self.layout.column(align=True)
        lines = [live.status_text(), models.export_status(), cosmetics.status(), live.last_apply_text()]
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
           KILEIDO_OT_view_only_row, KILEIDO_OT_all_boards_row, KILEIDO_OT_select_board, KILEIDO_OT_resync,
           KILEIDO_OT_viewport, KILEIDO_OT_pick, KILEIDO_OT_all_layers, KILEIDO_PT_panel, KILEIDO_PT_boards,
           KILEIDO_PT_status)
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


def _thickness_update(refresh_live):
    """A Thickness setting changed: the live board, then every view-only board."""
    def update(_scene, _context):
        refresh_live()
        packages.refresh_thickness()
    return update


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
            items=_layers_board_items),
        "kileido_collisions": BoolProperty(
            name="Collision check", default=True,
            description="Mark where the live board and the view-only boards overlap, with a red box",
            update=lambda self, context: collisions.schedule()),
        "kileido_color_mode": EnumProperty(
            name="Colors", items=(("FAB", "Board stackup", "Saved mask colors and KiCad 3D copper color"),
                                  ("REALISTIC", "Realistic", "Lit board materials, solder mask and exposed metal"),
                                  ("EDITOR", "PCB Editor", "Active KiCad PCB Editor layer colors")),
            default="FAB", update=lambda self, context: apply.set_color_mode(self.kileido_color_mode)),
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
        "kileido_center_in_kicad": BoolProperty(
            name="Center KiCad on click", default=True,
            description="Clicking an item here also pans KiCad's PCB editor to centre it, keeping its zoom"),
        "kileido_via_fill": BoolProperty(
            name="Via fill", default=False,
            description="Filled and capped vias: copper (or finish) caps under the mask and silkscreen. "
                        "Off: through vias are open, also through the solder mask",
            update=lambda self, context: apply.refresh_via_fill()),
        "kileido_copper_3d": BoolProperty(
            name="Copper thickness", default=True,
            description="Give outer copper its stackup thickness; the mask sits on the laminate between it",
            update=_thickness_update(apply.refresh_thickness)),
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
        # A click (press and release without moving) selects in KiCad while connected;
        # dragging still box-selects or navigates. Not connected, the operator's poll
        # fails and the click falls through to Blender's own selection.
        for shift in (False, True):
            entry = keymap.keymap_items.new(KILEIDO_OT_pick.bl_idname, "LEFTMOUSE", "CLICK", shift=shift)
            entry.properties.extend = shift
            _KEYMAPS.append((keymap, entry))
    collisions.install()
    bpy.app.handlers.load_post.append(_file_loaded)


def unregister():
    if _file_loaded in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_file_loaded)
    if bpy.app.timers.is_registered(dump.drain):
        bpy.app.timers.unregister(dump.drain)
    for keymap, entry in _KEYMAPS:
        keymap.keymap_items.remove(entry)
    _KEYMAPS.clear()
    live.disconnect()
    models.stop_following()
    cosmetics.stop_following()
    render_depth.uninstall()
    collisions.uninstall()
    for name in _scene_properties():
        delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    global _icons
    if _icons is not None:
        bpy.utils.previews.remove(_icons)
        _icons = None
