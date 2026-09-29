"""View-only boards: a live board exported as a .blend package, imported beside others.

`export_board` writes the live board's collection with everything it uses (meshes,
materials, node groups, component models, overlay plots packed in). `import_board`
appends such a package from any session, KiCad running or not. Every module finds
the live board's data by name ("KLS ...") inside `board.collection`, so an imported
board's data is renamed ("KV<n> KLS ...") and nothing ever looks it up again.
"""

import json
import re
from contextlib import contextmanager

import bpy
import numpy as np
from mathutils import Vector

from . import (apply, cosmetics, dc, highlight, layers, lighting, materials, nodes, render_depth, return_path,
               transform)
from .objects import OUTLINE, find, hide, set_node_input, view3d_spaces
from .placement import BOARD_FACE_CLEARANCE_M, copper_placement, copper_thickness, laminate_faces, stencil_thickness
from .state import board

ROOT_TAG = "kls_view_only_root"
OUTLINE_PROBLEM = "Board outline is missing or malformed"  # kileido_bridge.geometry.OUTLINE_PROBLEM
GAP_M = 0.005  # between boards placed side by side
PACKAGE_PREFIX = "KiLeidoscope: "  # the live collection's name, as written to a package
DATA_KINDS = ("collections", "objects", "meshes", "materials", "images", "node_groups")
_DUPLICATE = re.compile(r"\.\d{3}$")  # Blender's suffix for a name already taken


def roots():
    """Every view-only board's parent Empty, in import order."""
    return sorted((obj for obj in bpy.data.objects if obj.get(ROOT_TAG)), key=lambda obj: obj[ROOT_TAG])


def collection_of(index):
    return next((c for c in bpy.data.collections if c.get("kls_view_only") == index), None)


# --- What a board collection uses -----------------------------------------------------------

def _modifier_ids(obj):
    """Materials and images handed to Geometry Nodes modifiers as inputs."""
    for modifier in obj.modifiers:
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        for item in modifier.node_group.interface.items_tree:
            if item.item_type == "SOCKET" and item.in_out == "INPUT" and \
                    item.socket_type in ("NodeSocketMaterial", "NodeSocketImage"):
                value = nodes.modifier_value(modifier, item.identifier)
                if value is not None:
                    yield value


def _owned_data(collection):
    """The materials and images the collection's objects use."""
    materials, images = set(), set()
    for obj in tuple(collection.all_objects):
        for value in (*(slot.material for slot in obj.material_slots), *_modifier_ids(obj)):
            if isinstance(value, bpy.types.Material):
                materials.add(value)
            elif isinstance(value, bpy.types.Image):
                images.add(value)
    for material in materials:
        if material.node_tree is not None:
            images |= {node.image for node in material.node_tree.nodes
                       if node.type == "TEX_IMAGE" and node.image is not None}
    return materials, images


def _outline(collection):
    """The board outline object, under its live or imported name."""
    return next((obj for obj in collection.all_objects
                 if obj.name == OUTLINE or obj.name.endswith(" " + OUTLINE)), None)


def _x_range(obj):
    xs = [(obj.matrix_world @ Vector(corner)).x for corner in obj.bound_box]
    return min(xs), max(xs)


def _right_edge(exclude):
    """The largest X of every other board shown, live or view-only."""
    bpy.context.view_layer.update()  # roots moved since the last redraw
    edges = []
    for collection in bpy.data.collections:
        if collection == exclude or not (collection.get("kls_view_only") or collection.get("kileido_owned")):
            continue
        outline = _outline(collection)
        if outline is not None and len(outline.data.vertices):
            edges.append(_x_range(outline)[1])
    return max(edges, default=None)


# --- Export ---------------------------------------------------------------------------------

def _packed_copy(image):
    """A packed copy of an image that only lives in memory (the hole mask is drawn, not loaded)."""
    width, height = image.size
    pixels = np.empty(len(image.pixels), np.float32)  # not a Python float per channel
    image.pixels.foreach_get(pixels)
    copy = bpy.data.images.new(image.name, width, height, alpha=True, float_buffer=image.is_float)
    copy.colorspace_settings.name = image.colorspace_settings.name
    copy.pixels.foreach_set(pixels)
    copy.pack()
    return copy


def _finished_materials():
    """(material, mask side) of the live copper whose covered part shows the mask colour."""
    found = []
    for key, sides in materials.FINISHED.items():
        material = board.materials.get(key)
        mix = material.node_tree.nodes.get("KLS finish mix") if material is not None else None
        if mix is not None and any(output.links for output in mix.outputs):
            found.append((material, sides[0]))
    return found


def _mask_sheets(collection):
    """The live board's solid mask sheets and their colours before the opacity: (material,
    {"mask": sRGBA, "core": sRGB}). None in PCB Editor colours (translucent sheets)."""
    if board.color_mode == "EDITOR":
        return []
    core = board.appearance.get("viewer", {}).get("core") or materials.FALLBACK_CORE
    found = []
    for obj in tuple(collection.all_objects):
        layer = obj.get("kls_cosmetic_layer", "")
        color = cosmetics._layer_color(layer) if layer.endswith(".Mask") else None
        if color and obj.type == "MESH" and obj.data.materials:
            found.append((obj.data.materials[0], {"mask": list(color), "core": list(core)}))
    return found


def export_board(filepath):
    """Write the live board to a .blend package for `import_board`: as it looks now,
    without KiCad's selection, and with its own Layers list."""
    collection = board.collection
    if collection is None or board.in_snapshot:
        raise RuntimeError("No complete board to export")
    selection = {"selected": board.highlight["selected"], "pair": board.highlight["pair"],
                 **board.highlight_components}
    highlight.apply_selection({})  # no highlight copies or X-ray fade in the package
    checked, board.return_path = board.return_path, {}
    return_path.refresh()  # nor return-path marks
    dc.hide_all()  # nor DC analysis results
    collection["kls_board_name"] = board.board_name
    collection["kls_thickness_m"] = board.thickness_m
    collection["kls_layers"] = json.dumps(layers.recorded())
    collection["kls_outline_warnings"] = json.dumps(outline_warnings())
    collection["kls_stackup"] = json.dumps({"heights": board.heights, "layer_thickness": board.layer_thickness,
                                           "color_mode": board.color_mode})
    finished = _finished_materials()
    for material, side in finished:  # a view-only mask eye recolours covered copper like the live one
        material["kls_covered"] = json.dumps({"side": side, "shown": materials.covered_state(side, True),
                                             "hidden": materials.covered_state(side, False),
                                             "mask": materials.mask_color(side, True)})
    sheets = _mask_sheets(collection)
    for material, colors in sheets:  # and the solder mask opacity recolours its mask
        material["kls_mask_colors"] = json.dumps(colors)
    for obj in tuple(collection.all_objects):  # hide_set is view-layer state, not saved with the object
        obj["kls_hidden"] = obj.hide_get()
        obj["kls_row"] = "Board" if obj.name == OUTLINE else layers.row_of(obj) or ""
    swapped = []
    try:
        for image in _owned_data(collection)[1]:
            if image.packed_file is None:
                copy = _packed_copy(image)
                image.user_remap(copy)
                swapped.append((image, copy))
        bpy.data.libraries.write(filepath, {collection}, path_remap="ABSOLUTE", compress=True)
    finally:
        for image, copy in swapped:
            copy.user_remap(image)
            bpy.data.images.remove(copy)
        for obj in tuple(collection.all_objects):
            obj.pop("kls_hidden", None)
            obj.pop("kls_row", None)
        for key in ("kls_board_name", "kls_thickness_m", "kls_layers", "kls_stackup", "kls_outline_warnings"):
            collection.pop(key, None)
        for material, _ in finished:
            material.pop("kls_covered", None)
        for material, _ in sheets:
            material.pop("kls_mask_colors", None)
        highlight.apply_selection({key: list(value) for key, value in selection.items()})
        board.return_path = checked
        return_path.refresh()
        dc.refresh()


# --- Import ---------------------------------------------------------------------------------

def _world_position_to_root(material, root):
    """Mask and hole plots are sampled at world XY; a moved board samples them in its
    root's space instead, which is the world it was exported in."""
    tree = material.node_tree
    if tree is None:
        return
    for node in [node for node in tree.nodes if node.type == "NEW_GEOMETRY"]:
        links = [link for link in tree.links if link.from_node == node and link.from_socket.name == "Position"]
        if not links:
            continue
        coordinates = tree.nodes.new("ShaderNodeTexCoord")
        coordinates.object = root
        coordinates.location = node.location
        for link in links:
            to_socket = link.to_socket
            tree.links.remove(link)
            tree.links.new(coordinates.outputs["Object"], to_socket)


def _frame(collection):
    """Fit the 3D views on a board (an import into a session without a live board)."""
    outline = _outline(collection)
    if bpy.app.background or outline is None or not len(outline.data.vertices):
        return
    bpy.context.view_layer.update()
    corners = [outline.matrix_world @ Vector(corner) for corner in outline.bound_box]
    low = Vector((min(c.x for c in corners), min(c.y for c in corners), 0))
    high = Vector((max(c.x for c in corners), max(c.y for c in corners), 0))
    for space in view3d_spaces():
        space.region_3d.view_location = (low + high) / 2
        space.region_3d.view_distance = max(high.x - low.x, high.y - low.y) * 1.5


def import_board(filepath):
    """Append a board package as a view-only board right of the others; its root Empty."""
    before = {kind: set(getattr(bpy.data, kind)) for kind in DATA_KINDS}
    with bpy.data.libraries.load(filepath, link=False) as (source, target):
        names = [name for name in source.collections if name.startswith(PACKAGE_PREFIX)]
        if len(names) != 1:
            raise ValueError("Not a KiLeidoscope board export")
        target.collections = names
    collection = target.collections[0]
    if collection is None:
        raise ValueError("The board export could not be read")

    index = max((obj[ROOT_TAG] for obj in roots()), default=0) + 1
    for kind in DATA_KINDS:
        for block in set(getattr(bpy.data, kind)) - before[kind]:
            if block.name.startswith("KLS"):
                block.name = f"KV{index} {_DUPLICATE.sub('', block.name)}"
    name = collection.get("kls_board_name") or collection.name.removeprefix(PACKAGE_PREFIX)
    collection.name = f"KiLeidoscope view-only: {name}"
    for group in collection.children:  # "Copper.001" beside the live board's "Copper"
        group.name = f"{name}: {_DUPLICATE.sub('', group.name)}"
    collection.pop("kileido_owned", None)
    collection.hide_select = False  # exported with the live board's selection lock
    collection["kls_view_only"] = index
    collection["kls_rows_off"] = (layer_list(index) or {}).get("off", [])
    scene = bpy.context.scene
    scene.collection.children.link(collection)

    root = bpy.data.objects.new(f"KiLeidoscope view-only: {name}", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 0.002
    root[ROOT_TAG] = index
    root["kls_board_name"] = name
    collection.objects.link(root)
    for obj in tuple(collection.all_objects):
        if obj != root and obj.parent is None:
            obj.parent = root
        if obj.pop("kls_hidden", False):
            hide(obj, True)
    for material in _owned_data(collection)[0]:
        _world_position_to_root(material, root)

    # What a live board sets up for itself, for a session without one.
    apply.see_through_holes(scene)
    render_depth.install()
    outline = _outline(collection)
    if outline is not None and len(outline.data.vertices):
        right = _right_edge(collection)
        if right is not None:
            root.location.x = right + GAP_M - _x_range(outline)[0]
    refresh_thickness(index)  # the panel's Thickness settings now, not the exporter's
    cosmetics.apply_silk_settings()  # and its silkscreen opacity
    lighting.ensure_studio_lights()  # over all boards, this one included
    if board.collection is None and len(roots()) == 1:
        _frame(collection)
    return root


# --- Managing view-only boards --------------------------------------------------------------

def remove(index):
    """Delete one view-only board and the data only it used."""
    collection = collection_of(index)
    if collection is None:
        return
    materials, images = _owned_data(collection)
    meshes = {obj.data for obj in collection.all_objects if obj.type == "MESH"}
    for obj in list(collection.all_objects):
        bpy.data.objects.remove(obj)
    for child in (*collection.children_recursive, collection):  # the groups, then the board
        bpy.data.collections.remove(child)
    for blocks, used in ((bpy.data.meshes, meshes), (bpy.data.materials, materials), (bpy.data.images, images)):
        for block in used:
            if block.users == 0:
                blocks.remove(block)
    lighting.fit_to_boards()
    prefix = f"KV{index} "
    for group in [group for group in bpy.data.node_groups if group.name.startswith(prefix)]:
        if group.users == 0:
            bpy.data.node_groups.remove(group)


def outline_warnings(index=None):
    """The bridge's board-outline warnings: the live board's, or a view-only board's as exported."""
    if index is None:
        return [warning for warning in board.warnings if warning.startswith(OUTLINE_PROBLEM)]
    collection = collection_of(index)
    return json.loads(collection.get("kls_outline_warnings", "[]")) if collection is not None else []


def layer_list(index):
    """A view-only board's Layers list as exported (layers.recorded), or None."""
    collection = collection_of(index)
    text = collection.get("kls_layers") if collection is not None else None
    return json.loads(text) if text else None


def row_shown(index, row):
    collection = collection_of(index)
    return collection is not None and row not in collection.get("kls_rows_off", ())


def set_row_visible(index, row, visible):
    """A view-only board's Layers eye: only that board's objects of that row."""
    collection = collection_of(index)
    if collection is None:
        return
    off = set(collection.get("kls_rows_off", ()))
    off = off - {row} if visible else off | {row}
    collection["kls_rows_off"] = sorted(off)
    for obj in tuple(collection.all_objects):
        if obj.get("kls_row") == row:
            layers.switch(obj, visible)
    if row.endswith((".SilkS", ".Mask")):
        _refresh_silk(collection)
        refresh_thickness(index)  # its ink walls show only while silkscreen thickness is on
    if row.endswith(".Mask"):  # covered copper: mask colour only while the mask is shown
        for material in _owned_data(collection)[0]:
            covered = json.loads(material.get("kls_covered", "null"))
            if covered is not None and covered["side"] == row[0]:
                materials.set_covered(material, _covered(covered, visible))


def _covered(covered, shown):
    """A view-only board's covered copper (its `kls_covered`), with the panel's mask opacity."""
    if shown and covered.get("mask"):
        return materials.covered_from(covered["mask"])
    return covered["shown" if shown else "hidden"]


def refresh_mask_opacity():
    """The panel's solder mask opacity on every view-only board (the live one recolours itself)."""
    for root in roots():
        index = root[ROOT_TAG]
        collection = collection_of(index)
        if collection is None:
            continue
        for material in _owned_data(collection)[0]:
            covered = json.loads(material.get("kls_covered", "null"))
            if covered is not None:
                materials.set_covered(material, _covered(covered, row_shown(index, f"{covered['side']}.Mask")))
            colors = json.loads(material.get("kls_mask_colors", "null"))
            if colors is not None:
                materials.paint(material, materials.mask_on_laminate(colors["mask"], colors["core"]))


def _refresh_silk(collection):
    """A view-only board's silkscreen, as cosmetics.refresh_silk for the live board:
    printed on the mask and copper while that side's mask is shown (and was plotted,
    in colours that print it), else the flat sheet; with its eye."""
    index = collection["kls_view_only"]
    mode = json.loads(collection.get("kls_stackup", "{}")).get("color_mode")
    owned = _owned_data(collection)[0]
    for side in "FB":
        sheet, mask = (next((obj for obj in collection.all_objects if obj.get("kls_cosmetic_layer") == f"{side}.{kind}"),
                            None) for kind in ("SilkS", "Mask"))
        if sheet is None or mask is None or not mask.data.materials:
            continue
        plot = mask.data.materials[0].node_tree.nodes.get(f"KLS silk plot {side}")
        active = (mode != "EDITOR" and plot is not None and plot.image is not None and
                  row_shown(index, f"{side}.Mask"))
        sheet["kls_silk_on_surface"] = active
        for material in owned:
            materials.set_silk_state(material, side, active, row_shown(index, f"{side}.SilkS"))
    cosmetics.apply_silk_settings()


# --- Thickness (3D) -------------------------------------------------------------------------

@contextmanager
def _as_board(collection):
    """Lend `board` a view-only board's stackup while the shared placement code runs
    on its objects; the live board's state comes back after."""
    saved = dict(vars(board))
    stackup = json.loads(collection.get("kls_stackup", "{}"))
    board.collection = collection
    board.name_prefix = f"KV{collection['kls_view_only']} "
    board.heights = stackup.get("heights", {})
    board.layer_thickness = stackup.get("layer_thickness", {})
    board.thickness_m = float(collection.get("kls_thickness_m", 0.0016))
    board.color_mode = stackup.get("color_mode", board.color_mode)
    try:
        yield
    finally:
        board.__dict__.clear()
        board.__dict__.update(saved)


def copper_thickness_of(index):
    """A view-only board's F.Cu stackup thickness (m), or None."""
    collection = collection_of(index)
    stackup = json.loads(collection.get("kls_stackup", "{}")) if collection is not None else {}
    return stackup.get("layer_thickness", {}).get("F.Cu")


def refresh_thickness(index=None):
    """Copper, silkscreen and solder paste heights of view-only boards (all, or one)
    from the panel's Thickness settings, as the live board has them."""
    solder = getattr(bpy.context.scene, "kileido_show_solder", False)
    for root in roots():
        collection = collection_of(root[ROOT_TAG])
        if collection is None or (index is not None and root[ROOT_TAG] != index):
            continue
        with _as_board(collection):
            for obj in tuple(collection.all_objects):
                if obj.get("kls_copper"):
                    z, thickness = copper_placement(*obj["kls_copper"])
                    obj.location.z = z
                    set_node_input(obj, "Thickness", thickness)
                elif obj.get("kls_paste_side"):
                    top = obj["kls_paste_side"] == "F"
                    obj.location.z = transform.copper_z("F.Cu" if top else "B.Cu", "drills", board.heights)
                    set_node_input(obj, "Thickness", stencil_thickness() * (1 if top else -1))
                    hide(obj, not solder)
                    obj.hide_render = not solder
                elif obj.get("kls_cosmetic_layer"):
                    cosmetics._place(obj)
            vias = find("KLS vias")
            if vias is not None:
                set_node_input(vias, "Top Thickness", copper_thickness("F.Cu"))
                set_node_input(vias, "Bottom Thickness", copper_thickness("B.Cu"))
            outline = _outline(collection)
            if outline is not None:  # as apply._place_board, keeping its own materials
                top, bottom = laminate_faces()
                clearance = min(BOARD_FACE_CLEARANCE_M, (top - bottom) / 4)
                outline.location.z = top - clearance
                set_node_input(outline, "Thickness", -(top - bottom - 2 * clearance))
            for walls in [obj for obj in collection.all_objects if obj.get("kls_overlay_walls")]:
                shown = walls.get("kls_walls_active", False) and row_shown(root[ROOT_TAG], walls["kls_overlay_walls"])
                hide(walls, not shown)
                walls.hide_render = not shown


# --- All boards at once ---------------------------------------------------------------------

def _row_order(row):
    """Stackup order for the combined list: top films, copper by layer, the core, bottom."""
    fixed = {"F.SilkS": 0, "F.Mask": 1, "F.Cu": 2, "Board": 900, "B.Cu": 901, "B.Mask": 902, "B.SilkS": 903}
    if row in fixed:
        return fixed[row]
    if row.startswith("In") and row.endswith(".Cu") and row[2:-3].isdigit():
        return 2 + int(row[2:-3])
    return 1000


def _board_rows():
    """(board, {row: entry}) for the live board and every view-only board. An entry is
    (section title, label, kind, colour); `board` is None for the live one."""
    found = []
    if board.collection is not None:
        found.append((None, {entry.row: (title, entry.label, entry.kind, layers.swatch_color(entry))
                             for title, entries in layers.sections() for entry in entries}))
    for root in roots():
        recorded = layer_list(root[ROOT_TAG])
        if recorded:
            found.append((root[ROOT_TAG], {item["row"]: (section["title"], item["label"], item["kind"], item["color"])
                                           for section in recorded["sections"] for item in section["entries"]}))
    return found


def all_boards_sections():
    """The Layers list for all boards together: every row any board has, once, in
    stackup order (all dielectrics as the one core row). [(title, [(row, label, kind, colour)])]"""
    merged = {}
    for _, rows in _board_rows():
        for row, (title, label, kind, color) in rows.items():
            if row not in merged:
                merged[row] = (title, "Dielectric" if row == "Board" else label, kind, color)
    sections = []
    for title in ("Stackup", "Objects", "Drawings"):
        entries = sorted((row for row, entry in merged.items() if entry[0] == title), key=_row_order)
        if entries:
            sections.append((title, [(row, *merged[row][1:]) for row in entries]))
    return sections


def shown_everywhere(row):
    """True when every board that has this row shows it."""
    return all(layers.shown(row) if index is None else row_shown(index, row)
               for index, rows in _board_rows() if row in rows)


def set_row_everywhere(row, visible):
    """An All-boards eye: this row on every board that has it."""
    for index, rows in _board_rows():
        if row not in rows:
            continue
        if index is None:
            setattr(bpy.context.scene, layers.property_name(row), visible)
        else:
            set_row_visible(index, row, visible)


def select(index):
    """Select a view-only board to move, rotate or scale it: its root active, all its
    shown objects selected (Blender moves children with their selected parent)."""
    root = next((obj for obj in roots() if obj[ROOT_TAG] == index), None)
    collection = collection_of(index)
    if root is None or collection is None or collection.hide_viewport:
        return False
    view_layer = bpy.context.view_layer
    for obj in tuple(view_layer.objects.selected):
        obj.select_set(False)
    for obj in collection.all_objects:
        if obj.visible_get(view_layer=view_layer):
            obj.select_set(True)
    root.select_set(True)
    view_layer.objects.active = root
    return True


def set_visible(index, visible):
    collection = collection_of(index)
    if collection is not None:
        collection.hide_viewport = not visible
        collection.hide_render = not visible
        lighting.fit_to_boards()
