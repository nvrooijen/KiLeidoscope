"""Small helpers for the Blender objects, meshes and modifiers KiLeidoscope owns."""

import re

import bpy
import numpy as np
from mathutils import Matrix

from . import nodes
from .state import board

OUTLINE = "KLS outline"
ROOT = "KLS root"  # the board's root Empty: everything it owns hangs from it (poses.py)
VIEW_CLIP_START_M = 0.001  # 3D views' near clip, set once per board: close enough to look into a cut via
_BOARD_PREFIX = re.compile(r"^(?:B\d+|KV\d+) (?=KLS)")  # a live board's (state.slot_prefix) or a view-only one's


def owned_name(name):
    """This board's Blender name for a KiLeidoscope name ("KLS ..."): with its prefix
    ("B2 KLS outline" for board 2, "KV1 KLS outline" while a view-only board is placed),
    as Blender names are global. Other names (a component's "R12", one already
    prefixed) are kept."""
    return board.name_prefix + name if board.name_prefix and name.startswith("KLS ") else name


def role(name):
    """A KiLeidoscope name without its board's prefix: what name parsers read."""
    return _BOARD_PREFIX.sub("", name, count=1)


def set_visible(obj, visible):
    """Viewport and render visibility together. A row switched off in the Layers list
    keeps its objects hidden (and remembers to bring them back)."""
    from . import layers
    held = visible and layers.hidden(obj)
    if held or obj.get("kls_layer_hidden"):
        obj["kls_layer_hidden"] = held
    visible = visible and not held
    hide(obj, not visible)
    obj.hide_render = not visible


def hide(obj, hidden):
    """`obj.hide_set`, which raises for an object outside the current view layer (its
    collection excluded in the Outliner, another scene shown); there it has nothing to show."""
    try:
        obj.hide_set(hidden)
    except RuntimeError:
        pass


def camera_rays_only(obj):
    """Overlapping copper (a track's end cap and body, tracks at a junction,
    overlapping pads) shares exact heights; never merged, by design. Cycles
    only excludes the triangle a ray leaves, so shadow and bounce rays from
    one face hit its coplanar twin: black wedges on the reference board.
    So copper is seen by camera rays only: 35 um copper casts no visible
    shadow, and the only loss is copper missing from reflections in solder
    and parts (reflection rays also hit the twin: faint discs, measured).
    """
    obj.visible_shadow = False
    obj.visible_diffuse = False
    obj.visible_glossy = False
    obj.visible_transmission = False


# The board collection's children in Outliner order: (key, label). Every KiLeidoscope object
# lives in exactly one, picked from its name (`group_of`); code finds objects through
# `board.collection.all_objects`, whichever child holds them.
GROUPS = (
    ("board", "Board"),
    ("copper", "Copper"),
    ("pours", "Copper pours"),
    ("vias", "Vias"),
    ("drills", "Drills"),
    ("paste", "Solder paste"),
    ("mask", "Solder mask"),
    ("silkscreen", "Silkscreen"),
    ("drawings", "Drawings"),
    ("components", "Components"),
    ("highlights", "Highlights"),
    ("phase", "Dynamic phase"),
    ("findings", "Findings"),  # the shown finding's drawing (findings_draw)
)
_COPPER_KINDS = {"tracks": "copper", "pads": "copper", "graphics": "copper", "zone": "pours", "drill": "drills",
                 "solder": "paste"}


def group_of(name):
    """The GROUPS key for a KiLeidoscope object name ("KLS F.Cu zone <id>" -> "pours")."""
    name = role(name)
    if name.startswith("KLS finding "):
        return "findings"
    if "highlight" in name:
        return "highlights"
    if name.startswith("KLS phase "):
        return "phase"
    if name.startswith("KLS overlay "):  # "KLS overlay F.SilkS", "KLS overlay F.SilkS walls"
        side_kind = name.split(" ")[2].partition(".")[2]
        return {"SilkS": "silkscreen", "Mask": "mask", "Paste": "paste"}.get(side_kind, "drawings")
    if name.startswith(("KLS footprint", "KLS model ")):
        return "components"
    if name.startswith("KLS vias"):
        return "vias"
    parts = name.split(" ")  # "KLS <layer> <kind> ..."
    return _COPPER_KINDS.get(parts[2] if len(parts) > 2 else "", "board")


def ensure_groups():
    """Create the board collection's children (all of them, so the order never changes)."""
    children = {child.get("kls_group") for child in board.collection.children}
    for key, label in GROUPS:
        if key not in children:
            child = bpy.data.collections.new(board.name_prefix + label)  # "Copper", board 2's "B2 Copper"
            child["kls_group"] = key  # no "kileido_owned": that tag marks a whole board
            board.collection.children.link(child)


def link_owned(obj, key=None):
    """Link a new KiLeidoscope object into its group of the board collection (`key`,
    else picked from its name)."""
    key = key or group_of(obj.name)
    group = next((child for child in board.collection.children if child.get("kls_group") == key),
                 board.collection)
    group.objects.link(obj)
    _index()[obj.name] = obj
    lock_in_place(obj)
    adopt(obj)


def root():
    """The board's root Empty (poses.py), or None before it has one."""
    return lookup(ROOT) if board.collection is not None else None


def adopt(obj, parent=None):
    """A board object that hangs from nothing hangs from the board's root (`parent`, else
    looked up), with an identity parent inverse: what it was placed at is in the root's space."""
    parent = parent or root()
    if parent is not None and obj.parent is None and obj != parent:
        obj.parent = parent
        obj.matrix_parent_inverse = Matrix.Identity(4)


def lock_in_place(obj):
    """The live board is where KiCad puts it: Blender's move, rotate and scale tools
    leave its objects alone (the add-on still places them)."""
    obj.lock_location = obj.lock_rotation = obj.lock_scale = (True, True, True)


def _index():
    """`board.object_index` for the board collection shown now (a view-only board lent to
    `board` gets its own)."""
    pointer = board.collection.as_pointer()
    if board.object_index_of != pointer:
        board.object_index, board.object_index_of = {}, pointer
    return board.object_index


def lookup(name):
    """The board collection's object `name` (a "KLS ..." name under the board's prefix,
    `owned_name`), or None. A collection's `all_objects.get`
    walks every object: 0.2 ms each on a 1,500-object board while an edit is applied
    (measured, MedusaV5), and one edit makes thousands. So objects found are remembered,
    and checked by name on the way out (removed, undone or renamed since: looked up again)."""
    name = owned_name(name)
    index = _index()
    obj = index.get(name)
    if obj is not None:
        try:
            if obj.name == name:
                return obj
        except ReferenceError:
            pass
    obj = board.collection.all_objects.get(name)
    if obj is None:
        index.pop(name, None)
    else:
        index[name] = obj
    return obj


def find(name):
    """The board's object `name` ("KLS ..."), under its prefix if it has one."""
    return lookup(name)


def owned_object(name, kind="MESH", group=None):
    """The board collection's object `name`, created (with empty mesh data) if missing."""
    obj = lookup(name)
    if obj is not None:
        return obj
    return new_owned(name, kind, group)


def new_owned(name, kind="MESH", group=None):
    """A new object in the board collection (in `group`, else the one its name picks)."""
    name = owned_name(name)
    obj = bpy.data.objects.new(name, bpy.data.meshes.new(name) if kind == "MESH" else None)
    obj["kileido_owned"] = 1
    link_owned(obj, group)
    return obj


def single_point(mesh):
    """One vertex at the object origin: the anchor for instanced geometry."""
    if len(mesh.vertices) != 1:
        mesh.clear_geometry()
        mesh.vertices.add(1)
        mesh.vertices.foreach_set("co", np.zeros(3, dtype=np.float32))
        mesh.update()


def write_attribute(mesh, name, data_type, values):
    attribute = mesh.attributes.get(name)
    if attribute is None:
        attribute = mesh.attributes.new(name, data_type, "POINT")
    attribute.data.foreach_set("value", values)


def read_attribute(mesh, name, dtype):
    values = np.empty(len(mesh.vertices), dtype)
    if len(values):
        mesh.attributes[name].data.foreach_get("value", values)
    return values


def read_coordinates(mesh):
    """Vertex positions as an (n, 3) float32 array."""
    coordinates = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", coordinates)
    return coordinates.reshape(-1, 3)


def read_edges(mesh):
    edges = np.empty(len(mesh.edges) * 2, np.int32)
    mesh.edges.foreach_get("vertices", edges)
    return edges.reshape(-1, 2)


def node_modifier(obj):
    """The object's Geometry Nodes modifier, or None."""
    return next((m for m in obj.modifiers if m.type == "NODES" and m.node_group is not None), None)


def set_node_input(obj, name, value):
    """Change one input of the object's Geometry Nodes modifier, if it has one."""
    modifier = node_modifier(obj)
    if modifier is not None:
        nodes.modifier_input(modifier, modifier.node_group, name, value)
        obj.update_tag()


def set_modifier(obj, group, material, inputs=None):
    """Drive `obj` with one KiLeidoscope Geometry Nodes group.

    `material` is a Material or a key of `board.materials`.
    """
    if group.name.startswith(("KLS_Tracks", "KLS_Fill_", "KLS_FillSingle")) and obj.get("kls_copper"):
        camera_rays_only(obj)
    modifier = obj.modifiers.get(group.name)
    if modifier is None:
        modifier = obj.modifiers.new(group.name, "NODES")
    modifier.node_group = group
    if not isinstance(material, bpy.types.Material):
        material = board.materials[material]
    nodes.modifier_input(modifier, group, "Material", material)
    for name, value in (inputs or {}).items():
        nodes.modifier_input(modifier, group, name, value)


def view3d_spaces():
    """The active space of every 3D View in every screen."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                yield area.spaces.active


def shown_view_areas():
    """Every 3D View area in an open window: the screens on show, not every workspace's
    screen in the file (those are views nobody is looking through)."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                yield area


def shown_views():
    """(area pointer, region_3d) of every 3D view in an open window (`shown_view_areas`)."""
    for area in shown_view_areas():
        if area.spaces.active.region_3d is not None:
            yield area.as_pointer(), area.spaces.active.region_3d


def outline_bounds():
    """(xmin, ymin, xmax, ymax) of the board outline in metres, in the board root's space, or None."""
    if board.collection is None:
        return None
    outline = lookup(OUTLINE)
    if outline is None or not len(outline.data.vertices):
        return None
    xy = read_coordinates(outline.data)[:, :2]
    (xmin, ymin), (xmax, ymax) = xy.min(axis=0), xy.max(axis=0)
    return float(xmin), float(ymin), float(xmax), float(ymax)
