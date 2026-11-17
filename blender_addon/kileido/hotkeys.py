"""Hotkeys in the 3D View: Alt X switches X-ray mode, Alt C the cut plane, Shift Alt C the
side it removes. Keys Blender's Object Mode leaves free (checked in Blender 5.2); each can be
changed under Preferences, Keymap, 3D View.

Object Mode keys that delete, copy, join, re-parent, apply or re-collect objects are stopped
while the selection holds part of a live board: it follows KiCad, so a part is changed there,
not here (a Shift D copy would stay on the board for good). With nothing of a live board
selected they pass to Blender. The Object menu and the Outliner are not guarded.
"""

import bpy
from bpy.props import EnumProperty

from . import pick

TOGGLES = {  # key: (Scene property, label)
    "XRAY": ("kileido_focus", "X-ray mode"),
    "CUT": ("kileido_cut", "Cut plane"),
    "CUT_FLIP": ("kileido_cut_flip", "Cut plane flip"),
}
KEYS = (  # (setting, key, shift)
    ("XRAY", "X", False),
    ("CUT", "C", False),
    ("CUT_FLIP", "C", True),
)
GUARDED = (  # (key, shift, ctrl, alt): Blender 5.2's Object Mode keymap (blender_default.km_object_mode)
    ("X", False, False, False), ("X", True, False, False),  # delete, delete globally
    ("DEL", False, False, False), ("DEL", True, False, False),
    ("D", True, False, False), ("D", False, False, True),  # duplicate, linked duplicate
    ("J", False, True, False),  # join
    ("P", False, True, False), ("P", False, False, True),  # parent, clear parent
    ("A", False, True, False),  # apply transforms
    ("M", False, False, False), ("M", True, False, False),  # move or link to a collection
    ("G", False, True, True),  # remove from a collection
)
_keymaps = []


def holds_live_board(objects):
    """Whether any of `objects` belongs to a board that follows KiCad."""
    return any(pick.live_slot(obj) is not None for obj in objects)


class KILEIDO_OT_toggle(bpy.types.Operator):
    bl_idname = "kileido.toggle"
    bl_label = "Switch KiLeidoscope setting"
    bl_description = "Switch X-ray mode, the cut plane or the side the cut plane removes"

    setting: EnumProperty(name="Setting", items=tuple((key, label, "") for key, (_, label) in TOGGLES.items()))

    def execute(self, context):
        prop, label = TOGGLES[self.setting]
        scene = context.scene
        if self.setting == "CUT_FLIP" and not scene.kileido_cut:
            self.report({"INFO"}, "KiLeidoscope: the cut plane is off (Alt C switches it on)")
            return {"CANCELLED"}
        value = not getattr(scene, prop)
        setattr(scene, prop, value)
        self.report({"INFO"}, f"KiLeidoscope: {label} {'on' if value else 'off'}")
        return {"FINISHED"}


class KILEIDO_OT_keep_live_board(bpy.types.Operator):
    bl_idname = "kileido.keep_live_board"
    bl_label = "Keep live board"
    bl_description = "Leave a board that follows KiCad as it is (change its parts in KiCad); otherwise Blender's own " \
                     "operator runs"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def invoke(self, context, event):
        if not holds_live_board(context.selected_objects):
            return {"PASS_THROUGH"}  # Blender's own operator
        self.report({"WARNING"}, "KiLeidoscope: the live board follows KiCad; change its parts in KiCad")
        return {"CANCELLED"}


CLASSES = (KILEIDO_OT_toggle, KILEIDO_OT_keep_live_board)


def bound(setting, keyconfigs):
    """The key that switches `setting` now, as Blender names it on this computer ("Alt X";
    Option on a Mac), or None when it is switched off or removed in Preferences, Keymap."""
    keymap = keyconfigs.user.keymaps.get("3D View") if keyconfigs.user is not None else None
    for item in keymap.keymap_items if keymap is not None else ():
        if item.idname == KILEIDO_OT_toggle.bl_idname and item.properties.setting == setting:
            return item.to_string() if item.active else None
    return None


class Section:
    """The panel's Hotkeys section, below Status (closed at first): each key as it is bound
    now, so a key changed in Preferences shows changed. The keys the live board stops are not
    listed: pressing one says why in the status bar."""

    def __init__(self, layout):
        self.layout = layout

    def draw(self, context):
        keyconfigs = context.window_manager.keyconfigs
        column = self.layout.column(align=True)
        rows = [("Click", "Select here and in KiCad")]
        rows += [(bound(setting, keyconfigs) or "Off", label) for setting, (_, label) in TOGGLES.items()]
        for key, action in rows:
            split = column.split(factor=0.35)
            split.label(text=key)
            split.label(text=action)
        self.layout.operator("screen.userpref_show", text="Change keys", icon="PREFERENCES").section = "KEYMAP"


def install(keyconfig):
    """Add the hotkeys to the add-on keyconfig (None in background mode: nothing to add)."""
    if keyconfig is None:
        return
    view = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
    for setting, key, shift in KEYS:
        entry = view.keymap_items.new(KILEIDO_OT_toggle.bl_idname, key, "PRESS", alt=True, shift=shift)
        entry.properties.setting = setting
        _keymaps.append((view, entry))
    # Blender puts add-on items ahead of its own in the same keymap: these run first.
    objects = keyconfig.keymaps.new(name="Object Mode", space_type="EMPTY")
    for key, shift, ctrl, alt in GUARDED:
        _keymaps.append((objects, objects.keymap_items.new(KILEIDO_OT_keep_live_board.bl_idname, key, "PRESS",
                                                           shift=shift, ctrl=ctrl, alt=alt)))


def uninstall():
    for keymap, entry in _keymaps:
        keymap.keymap_items.remove(entry)
    _keymaps.clear()
