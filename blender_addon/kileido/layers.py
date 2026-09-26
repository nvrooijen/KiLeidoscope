"""The Layers list: KiCad's layers top to bottom through the board, each with an eye.

Top Overlay, Top Solder, Top Copper, the inner copper, the board and vias, Bottom
Copper, Bottom Solder, Bottom Overlay, components, then fabrication and user
drawings. Rows use KiCad's own layer names (the bridge sends them with the stackup).

Every KiLeidoscope object belongs to at most one row (`row_of`). `objects.set_visible`
asks `hidden` first, so a hidden row stays hidden through live updates. A row that
comes back shows only what it hid (`kls_layer_hidden`), never an empty highlight or
a model KiCad hides.
"""

import re

import bpy

from .objects import hide
from .state import board

_COPPER = re.compile(r"^KLS ((?:F|B|In\d+)\.Cu) (?:tracks|pads|graphics|zone |highlight)")
EXTRA = {"Board": "kileido_show_board", "Vias": "kileido_show_vias", "Components": "kileido_show_components",
         "Placeholders": "kileido_show_placeholders"}
LABELS = {"Placeholders": "Missing models"}  # boxes for components whose model file KiCad cannot find
COPPER_LAYERS = ("F.Cu", *(f"In{number}.Cu" for number in range(1, 31)), "B.Cu")


def property_name(row):
    """The Scene property behind a row (the overlay layers' existing names included)."""
    return EXTRA.get(row) or "kileido_show_" + row.replace(".", "_")


def row_of(obj):
    """The row an object belongs to, or None (always shown, e.g. solder and lights)."""
    layer = obj.get("kls_cosmetic_layer") or obj.get("kls_overlay_walls")
    if layer:
        return layer
    name = obj.name
    if name.startswith("KLS vias"):
        return "Vias"
    if obj.get("kls_footprint_placeholder") == 1:
        return "Placeholders"
    if obj.get("kls_model_fp_id") is not None or name.startswith("KLS footprint highlight"):
        return "Components"
    match = _COPPER.match(name)
    return match[1] if match else None


def shown(row):
    return bool(getattr(bpy.context.scene, property_name(row), True))


def hidden(obj):
    """True when the object's row is switched off in the panel. A placeholder stands in
    for a component, so switching Components off hides it too."""
    row = row_of(obj)
    if row == "Placeholders" and not shown("Components"):
        return True
    return row is not None and row != "Board" and not shown(row)


def refresh(row):
    """A row's eye changed: hide what is visible, or bring back what the row hid."""
    if board.collection is None:
        return
    if row == "Board":
        from . import apply
        apply.set_board_visible(shown("Board"))
        return
    for obj in tuple(board.collection.all_objects):
        own = row_of(obj)
        if own == row or (row == "Components" and own == "Placeholders"):
            switch(obj, not hidden(obj))


def rows():
    """(row, label) top to bottom, for the layers this board has."""
    from . import cosmetics
    names = board.layer_names
    overlays = {layer: label for layer, label, _ in cosmetics.visible_layers()}
    copper = sorted(board.heights, key=lambda layer: board.heights[layer], reverse=True)
    inner = [layer for layer in copper if layer not in ("F.Cu", "B.Cu")]
    order = ["F.SilkS", "F.Mask", "F.Cu", *inner, "Board", "Vias", "B.Cu", "B.Mask", "B.SilkS", "Components",
             "Placeholders"]
    order += [layer for layer in overlays if layer not in order]  # fabrication and user drawings
    result = []
    for row in order:
        if row in EXTRA:
            result.append((row, LABELS.get(row, row)))
        elif row in overlays or row in board.heights:
            result.append((row, names.get(row) or overlays.get(row) or row))
    return result


class Entry:
    """One line of the Layers panel: the eye's row (None: no eye), what it is, how thick."""

    def __init__(self, row, label, kind, thickness_m=None, detail=""):
        self.row, self.label, self.kind = row, label, kind
        self.thickness_m, self.detail = thickness_m, detail


def sections():
    """The Layers panel as (title, entries): the stackup top to bottom with its
    dielectrics (their eyes all switch the one board solid), then vias and
    components, then fabrication and user drawings."""
    rows_here = dict(rows())
    stack = []

    def add(row, kind, thickness_m=None):
        if row in rows_here:
            stack.append(Entry(row, rows_here[row], kind, thickness_m))

    add("F.SilkS", "silk")
    add("F.Mask", "mask", board.layer_thickness.get("F.Mask"))
    layers = [layer for layer in board.stackup if layer.get("type") in ("copper", "dielectric")]
    if not any(layer.get("type") == "dielectric" for layer in layers):
        # KiCad sent no stackup: copper by height, one board between.
        copper = [row for row, _ in rows() if row in board.heights]
        layers = [{"name": name, "type": "copper"} for name in copper[:1]]
        layers += [{"name": "Board", "type": "dielectric", "thickness_nm": None}]
        layers += [{"name": name, "type": "copper"} for name in copper[1:]]
    for layer in layers:
        name = layer.get("name", "")
        thickness = layer.get("thickness_nm")
        thickness_m = thickness * 1e-9 if thickness else board.layer_thickness.get(name)
        if layer.get("type") == "copper":
            add(name, "copper", thickness_m)
            continue
        detail = [layer.get("material") or ""]
        if layer.get("epsilon_r"):
            detail.append(f"εr {layer['epsilon_r']:.2f}")
        stack.append(Entry("Board", "Dielectric", "dielectric", thickness_m,
                           "  ".join(part for part in detail if part)))
    add("B.Mask", "mask", board.layer_thickness.get("B.Mask"))
    add("B.SilkS", "silk")
    objects = [Entry(row, rows_here[row], row.lower()) for row in ("Vias", "Components", "Placeholders")
               if row in rows_here]
    listed = {entry.row for entry in (*stack, *objects)}
    drawings = [Entry(row, label, "drawing") for row, label in rows() if row not in listed]
    return [(title, entries) for title, entries in
            (("Stackup", stack), ("Objects", objects), ("Drawings", drawings)) if entries]


def recorded():
    """The Layers list as the board shows it now, for an exported board package: its
    sections with colours, which rows are off, and the board's total thickness."""
    return {"sections": [{"title": title,
                          "entries": [{"row": entry.row, "label": entry.label, "kind": entry.kind,
                                       "thickness_m": entry.thickness_m or 0.0, "detail": entry.detail,
                                       "color": list(swatch_color(entry))} for entry in entries]}
                         for title, entries in sections()],
            "off": [row for row, _ in rows() if not shown(row)],
            "thickness_m": board.thickness_m}


def swatch_color(entry):
    """sRGB of an entry's swatch in the current colour mode (as the board shows it)."""
    from . import cosmetics, materials, shading
    viewer = board.appearance.get("viewer", {})
    core = viewer.get("core") or materials.FALLBACK_CORE
    if entry.kind == "copper":
        return materials._copper_color(entry.row)
    if entry.kind == "vias":
        editor = board.appearance.get("editor_via") if board.color_mode == "EDITOR" else None
        return tuple(editor[:3]) if editor else materials._copper_color("vias")
    if entry.kind == "dielectric":
        return core[:3]
    if entry.kind == "components":
        return (0.16, 0.16, 0.17)
    if entry.kind == "placeholders":
        return materials.PLACEHOLDER_COLOR
    color = cosmetics._layer_color(entry.row)
    if color and entry.kind == "mask":
        return shading.blend_srgb(color, core)  # the mask as seen on the laminate
    return tuple(color[:3]) if color else cosmetics.FALLBACK_COLOR


def format_thickness(metres):
    if not metres:
        return ""
    microns = metres * 1e6
    return f"{microns:.0f} µm" if microns < 100 else f"{metres * 1e3:.2f} mm"


def switch(obj, visible):
    """Hide an object for its row, or bring it back if its row hid it."""
    if not visible and not obj.hide_get():
        obj["kls_layer_hidden"] = True
        hide(obj, True)
        obj.hide_render = True
    elif visible and obj.get("kls_layer_hidden"):
        obj["kls_layer_hidden"] = False
        hide(obj, False)
        obj.hide_render = False


def set_all(visible):
    """The Layers list's master eye."""
    scene = bpy.context.scene
    for row, _ in rows():
        setattr(scene, property_name(row), visible)
