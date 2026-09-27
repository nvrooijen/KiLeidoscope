"""Proximity warnings: 3D objects close above (or below) outer-layer high-speed traces.

A microstrip's field reaches out of the board over its trace, so a part close to it
(a shield can, a heatsink, a connector housing, another board stacked above) can shift
its impedance. Checked are the F.Cu and B.Cu tracks of every differential pair
(KiCad's naming rule, `diff_pair_partner`) and of the nets listed in the panel. Inner
layers sit between planes and are not checked. The limit is a multiple of h, the
dielectric between the outer layer and its nearest other copper layer (the stackup).

Obstacles are the live board's component models, except those on the trace's own net
(the parts it connects), and each visible view-only board: its component models and its
board solid. Only geometry on the trace's outward side counts: a bottom-side part is no
obstacle for an F.Cu trace. Placeholder envelopes (models not loaded yet) are skipped.
GLB models do not say whether a part is metal or plastic, so this is a warning to look
closer, never an impedance value.

Each checked track is sampled along its centre and both edges; each sample asks a BVH of
an obstacle's triangles for the nearest point within the limit. The flagged samples
become orange spans over the trace, and the obstacle gets an orange box. The live board
lies where KiCad puts it, so its frame is the world's. Runs a moment after the tracks,
models or boards stop changing, like collisions.py.
"""

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from . import collisions, live, packages
from .objects import OUTLINE, hide, lock_in_place, read_attribute, read_coordinates, set_modifier, write_attribute
from .state import board

COLLECTION = "KiLeidoscope proximity"
COLOR = (1.0, 0.22, 0.0)  # deep orange: apart from gold copper and the collision check's red
BASE = (0.02, 0.005, 0.0)  # dark under its glow: lit, a bright base washes out to pale peach
DEFAULT_FACTOR = 5.0
DELAY_S = 0.3
MAX_WARNINGS = 100
MAX_SAMPLES = 100_000  # along every checked track together; the step grows beyond it
SAMPLES_PER_LIMIT = 4  # sample spacing: a quarter of the limit
SIDE_EPSILON_M = 1e-6  # geometry this far out of the copper surface is on its outward side
SPAN_LIFT_M = 60e-6  # span strips float above the mask and silkscreen
SPAN_GROW_M = 40e-6
BOX_MARGIN_M = 0.1e-3
VIEW_ONLY_PREFIX = "KiLeidoscope view-only: "  # packages.import_board's collection name

_result = {"rows": [], "checked": False, "nets": 0}


# --- What is checked ------------------------------------------------------------------------

def diff_pair_partner(net, nets):
    """kileido_bridge.selection.diff_pair_partner (the add-on never imports the bridge):
    a final "+"/"-" or "P"/"N" pairs with the other suffix, if a net of that name exists."""
    if not net:
        return None
    swap = {"+": "-", "-": "+", "P": "N", "N": "P"}.get(net[-1])
    if swap is None:
        return None
    partner = net[:-1] + swap
    return partner if partner in nets and partner != net else None


def listed_nets(scene):
    """The panel's extra nets, comma-separated."""
    return [net.strip() for net in getattr(scene, "kileido_proximity_nets", "").split(",") if net.strip()]


def _track_objects():
    """The live board's track objects by layer."""
    return {obj["kls_copper"][0]: obj for obj in board.collection.all_objects
            if (obj.get("kls_copper") or ("", ""))[1] == "tracks"}


def checked_nets(scene):
    """Every net of a differential pair on the live board, and the listed nets."""
    nets = {net for obj in _track_objects().values() for net in obj.get("kls_nets", ())}
    nets |= {net for obj in board.collection.all_objects if obj.get("kls_footprint") == 1
             for net in obj.get("kls_nets", ())}
    nets.discard("")
    return {net for net in nets if diff_pair_partner(net, nets)} | set(listed_nets(scene))


def selected_nets():
    """Nets of the tracks KiCad's selection highlights (not their pair partners)."""
    chosen = board.highlight["selected"]
    if board.collection is None or not chosen:
        return set()
    return {net for obj in _track_objects().values()
            for item_id, net in zip(obj.get("kls_ids", ()), obj.get("kls_nets", ())) if item_id in chosen and net}


def _top(name):
    """Upper face of a copper layer: `board.heights` holds each layer's top, but B.Cu's bottom (0)."""
    return board.heights[name] + (float(board.layer_thickness.get(name) or 0.0) if name == "B.Cu" else 0.0)


def _bottom(name):
    return board.heights[name] - (0.0 if name == "B.Cu" else float(board.layer_thickness.get(name) or 0.0))


def dielectric_heights():
    """h (m) per outer layer: from its inner face to the nearest other copper layer (on a
    two-layer board the other outer layer), from the stackup heights and copper thicknesses."""
    found = {}
    for layer in ("F.Cu", "B.Cu"):
        others = [name for name in board.heights if name != layer]
        if layer not in board.heights or not others:
            continue
        if layer == "F.Cu":
            h = _bottom(layer) - max(_top(name) for name in others)
        else:
            h = min(_bottom(name) for name in others) - _top(layer)
        if h > 0:
            found[layer] = h
    return found


def limits(scene):
    """The distance limit (m) per outer layer: the panel's multiple of h."""
    factor = float(getattr(scene, "kileido_proximity_factor", DEFAULT_FACTOR))
    return {layer: factor * h for layer, h in dielectric_heights().items()}


def _segments(obj, nets):
    """Checked segments of one track object: starts, ends (n, 2) world xy, widths, item ids, nets."""
    mesh = obj.data
    ids, item_nets = list(obj.get("kls_ids", ())), list(obj.get("kls_nets", ()))
    empty = np.empty((0, 2))
    if len(mesh.vertices) < 2 or len(item_nets) != len(ids):
        return empty, empty, np.empty(0), [], np.empty(0, object)
    matrix = np.array(obj.matrix_world)
    xy = (read_coordinates(mesh).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3])[:, :2]
    items = read_attribute(mesh, "item", np.int32)[0::2]
    net = np.array(item_nets, object)[items]
    keep = np.isin(net, list(nets))
    widths = read_attribute(mesh, "width", np.float32)[0::2].astype(np.float64)
    return (xy[0::2][keep], xy[1::2][keep], widths[keep], [ids[item] for item in items[keep]], net[keep])


def _samples(a, b, width, step):
    """Points along each segment's centre line and both edges, about `step` apart:
    (points (m, 2), segment (m,), sample index (m,), samples per segment (n,))."""
    direction = b - a
    length = np.hypot(direction[:, 0], direction[:, 1])
    count = np.maximum(2, np.ceil(length / step).astype(np.int64) + 1)
    segment = np.repeat(np.arange(len(a)), count)
    index = np.arange(len(segment)) - np.repeat(np.cumsum(count) - count, count)
    along = a[segment] + (index / (count[segment] - 1))[:, None] * direction[segment]
    unit = direction / np.where(length > 0, length, 1.0)[:, None]
    across = np.column_stack([-unit[:, 1], unit[:, 0]])[segment] * width[segment, None]
    points = np.vstack([along, along - across / 2, along + across / 2])
    return points, np.tile(segment, 3), np.tile(index, 3), count


# --- Obstacles ------------------------------------------------------------------------------

class Obstacle:
    """A component (its visible model parts) or a view-only board's solid."""

    def __init__(self, label, parts, footprint=None, live_part=False, solid=False):
        self.label = label
        self.parts = parts
        self.footprint = footprint
        self.footprint_id = footprint.get("kls_id") if footprint is not None and live_part else None
        self.nets = set(footprint.get("kls_nets", ())) if footprint is not None and live_part else set()
        self.solid = solid
        self._triangles = None
        self._trees = {}
        depsgraph = bpy.context.evaluated_depsgraph_get()  # the outline mesh is flat, its solid is not
        corners = np.array([tuple(part.matrix_world @ Vector(corner)) for part in parts
                            for corner in (part.evaluated_get(depsgraph) if solid else part).bound_box])
        self.low, self.high = corners.min(axis=0), corners.max(axis=0)

    def triangles(self, meshes=None):
        """World vertices (n, 3) and triangles (t, 3) of all parts."""
        if self._triangles is None:
            vertices, triangles, offset = [], [], 0
            for part in self.parts:
                local, faces = _evaluated_triangles(part) if self.solid else \
                    _mesh_triangles(part.data, {} if meshes is None else meshes)
                matrix = np.array(part.matrix_world)
                vertices.append(local @ matrix[:3, :3].T + matrix[:3, 3])
                triangles.append(faces + offset)
                offset += len(local)
            self._triangles = (np.vstack(vertices) if vertices else np.empty((0, 3)),
                               np.vstack(triangles) if triangles else np.empty((0, 3), np.int32))
        return self._triangles

    def tree(self, layer, surface, up, meshes):
        """(BVH, low, high) of the triangles on the outward side of `surface`, or None."""
        if layer not in self._trees:
            vertices, triangles = self.triangles(meshes)
            outward = ((vertices[:, 2][triangles] - surface) * up).max(axis=1) > SIDE_EPSILON_M if len(triangles) \
                else np.zeros(0, bool)
            kept = triangles[outward]
            if not len(kept):
                self._trees[layer] = None
            else:
                used = vertices[np.unique(kept)]
                self._trees[layer] = (BVHTree.FromPolygons(vertices.tolist(), kept.tolist(), all_triangles=True),
                                      used.min(axis=0), used.max(axis=0))
        return self._trees[layer]

    def box(self, hits, limit):
        """The matrix of a unit cube around the component, or around a board's flagged region."""
        if self.footprint is not None:
            frame = self.footprint.matrix_world
            to_local = frame.inverted()
            points = np.array([tuple(to_local @ (part.matrix_world @ Vector(corner)))
                               for part in self.parts for corner in part.bound_box])
            low, high = points.min(axis=0) - BOX_MARGIN_M, points.max(axis=0) + BOX_MARGIN_M
            return frame @ Matrix.Translation(Vector(tuple((low + high) / 2))) @ \
                Matrix.Diagonal((*np.abs(high - low), 1.0))
        points = np.array(hits)
        grow = np.array((limit, limit, BOX_MARGIN_M))
        low, high = points.min(axis=0) - grow, points.max(axis=0) + grow
        return Matrix.Translation(Vector(tuple((low + high) / 2))) @ Matrix.Diagonal((*(high - low), 1.0))


def _mesh_triangles(mesh, meshes):
    """Local vertices and triangles of a mesh, once per mesh (model parts share them)."""
    key = mesh.as_pointer()
    if key not in meshes:
        triangles = np.empty(len(mesh.loop_triangles) * 3, np.int32)
        mesh.loop_triangles.foreach_get("vertices", triangles)
        meshes[key] = (read_coordinates(mesh).astype(np.float64), triangles.reshape(-1, 3))
    return meshes[key]


def _evaluated_triangles(obj):
    """A board solid is built by Geometry Nodes: its evaluated mesh."""
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return _mesh_triangles(mesh, {})
    finally:
        evaluated.to_mesh_clear()


def obstacles():
    """The live board's components, then each visible view-only board's components and solid."""
    found = []
    for collection in collisions.boards():
        live_board = collection == board.collection
        name = collection.name.removeprefix(VIEW_ONLY_PREFIX)
        footprints = {obj.get("kls_id"): obj for obj in collection.all_objects if obj.get("kls_footprint") == 1}
        parts = {}
        for obj in collection.all_objects:
            if obj.get("kls_model_fp_id") is not None and obj.type == "MESH" and not obj.hide_get() and \
                    len(obj.data.vertices):
                parts.setdefault(obj["kls_model_fp_id"], []).append(obj)
        for footprint_id, objects in parts.items():
            footprint = footprints.get(footprint_id)
            if footprint is None or not footprint.get("kls_active", 1):
                continue
            reference = footprint.get("kls_reference", "?")
            found.append(Obstacle(reference if live_board else f"{reference} ({name})", objects, footprint,
                                  live_part=live_board))
        solid = None if live_board else next(
            (obj for obj in collection.all_objects
             if obj.name.endswith(" " + OUTLINE) and not obj.hide_get() and len(obj.data.vertices)), None)
        if solid is not None:
            found.append(Obstacle(f"{name} (board)", [solid], solid=True))
    return found


# --- Running --------------------------------------------------------------------------------

def find(scene):
    """Warnings, closest first: dicts of label, net, layer, distance_m, spans ((x1, y1, x2, y2,
    width) world m) and their z, ids (tracks), footprint_id (live parts), obstacle (its index)
    and box_matrix (around it)."""
    nets = checked_nets(scene)
    _result["nets"] = len(nets)
    tracks = _track_objects()
    found = {}  # (obstacle index, net, layer) -> warning
    candidates = obstacles() if nets else []
    meshes = {}
    for layer, limit in limits(scene).items():
        if layer not in tracks or not candidates:
            continue
        a, b, width, ids, net = _segments(tracks[layer], nets)
        if not len(a):
            continue
        up = 1.0 if layer == "F.Cu" else -1.0
        surface = _top(layer) if layer == "F.Cu" else _bottom(layer)
        total = float(np.hypot(*(b - a).T).sum()) * 3
        step = max(limit / SAMPLES_PER_LIMIT, total / MAX_SAMPLES)
        points, segment, index, count = _samples(a, b, width, step)
        points = np.column_stack([points, np.full(len(points), surface)])
        for number, obstacle in enumerate(candidates):
            # Samples near its bounds first: a BVH only for the few obstacles near a checked track.
            near = np.all((points > obstacle.low - limit) & (points < obstacle.high + limit), axis=1)
            if obstacle.nets:
                near &= ~np.isin(net[segment], list(obstacle.nets))
            tree = obstacle.tree(layer, surface, up, meshes) if near.any() else None
            if tree is None:
                continue
            bvh, low, high = tree
            near &= np.all((points > low - limit) & (points < high + limit), axis=1)
            for point in np.flatnonzero(near):
                location, _normal, _face, distance = bvh.find_nearest(Vector(points[point]), limit)
                if location is None:
                    continue
                key = (number, net[segment[point]], layer)
                warning = found.setdefault(key, {"obstacle": obstacle, "layer": layer, "limit": limit,
                                                 "surface": surface, "up": up, "distance_m": distance,
                                                 "flagged": {}, "hits": [], "a": a, "b": b, "width": width,
                                                 "count": count, "ids": ids})
                warning["distance_m"] = min(warning["distance_m"], distance)
                warning["flagged"].setdefault(int(segment[point]), set()).add(int(index[point]))
                warning["hits"].append(tuple(location))
    warnings = []
    for (number, net_name, _layer), warning in found.items():
        obstacle = warning["obstacle"]
        spans = []
        for segment, flagged in warning["flagged"].items():
            spans += _spans(warning["a"][segment], warning["b"][segment], warning["width"][segment],
                            sorted(flagged), int(warning["count"][segment]))
        warnings.append({"label": obstacle.label, "net": net_name, "layer": warning["layer"],
                         "distance_m": float(warning["distance_m"]), "spans": spans,
                         "z": warning["surface"] + warning["up"] * SPAN_LIFT_M, "up": warning["up"],
                         "ids": sorted({warning["ids"][segment] for segment in warning["flagged"]}),
                         "footprint_id": obstacle.footprint_id, "obstacle": number,
                         "box_matrix": obstacle.box(warning["hits"], warning["limit"])})
    warnings.sort(key=lambda warning: (warning["distance_m"], warning["label"], warning["net"]))
    return warnings


def _spans(a, b, width, flagged, count):
    """Runs of consecutive flagged samples as (x1, y1, x2, y2, width), each reaching half a
    sample spacing past its outer samples."""
    runs, start = [], flagged[0]
    for previous, current in zip(flagged, flagged[1:] + [None]):
        if current != previous + 1:
            runs.append((start, previous))
            start = current
    half = 0.5 / (count - 1)
    return [(*(a + (b - a) * max(0.0, first / (count - 1) - half)),
             *(a + (b - a) * min(1.0, last / (count - 1) + half)), width) for first, last in runs]


def _collection(scene):
    collection = bpy.data.collections.get(COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION)
        collection["kileido_owned_proximity"] = 1
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    return collection


def _marker(collection, name, data=None):
    """The marker object `name`; a new one gets `data`, else a mesh of its own."""
    obj = collection.objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, data or bpy.data.meshes.new(name))
        lock_in_place(obj)  # rebuilt by each check: nothing to move
        collection.objects.link(obj)
    hide(obj, False)
    return obj


def _draw_span(obj, warning):
    """The flagged spans as a strip over the trace (KiLeidoscope's track nodes)."""
    spans = np.array(warning["spans"], np.float64).reshape(-1, 5)
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(len(spans) * 2)
    mesh.edges.add(len(spans))
    coordinates = np.zeros((len(spans) * 2, 3), np.float32)
    coordinates[0::2, :2], coordinates[1::2, :2] = spans[:, 0:2], spans[:, 2:4]
    mesh.vertices.foreach_set("co", coordinates.ravel())
    mesh.edges.foreach_set("vertices", np.arange(len(spans) * 2, dtype=np.int32))
    write_attribute(mesh, "width", "FLOAT", np.repeat(spans[:, 4] + SPAN_GROW_M, 2).astype(np.float32))
    mesh.update()
    obj.location = (0.0, 0.0, warning["z"])
    material = collisions.glow_material("KiLeidoscope proximity span", COLOR, 0.9, BASE, 1.5)
    set_modifier(obj, board.groups["tracks"], material, {"Thickness": 0.0, "Up": warning["up"]})


def run():
    """Check now; show a span strip over each flagged trace and a box on each obstacle."""
    scene = bpy.context.scene
    if not getattr(scene, "kileido_proximity", True) or board.collection is None or not board.groups:
        clear()
        return 0
    if board.in_snapshot:
        schedule()
        return len(_result["rows"])
    warnings = find(scene)[:MAX_WARNINGS]
    collection = _collection(scene)
    cube = collisions.unit_cube("KiLeidoscope proximity box",
                                collisions.glow_material("KiLeidoscope proximity", COLOR, 0.35, BASE, 1.5))
    boxes = {}  # obstacle index -> box name: one box per obstacle, however many nets pass it
    active = set()
    for number, warning in enumerate(warnings):
        span_name = f"KiLeidoscope proximity span {number + 1}"
        _draw_span(_marker(collection, span_name), warning)
        warning["span"] = span_name
        if warning["obstacle"] not in boxes:
            box = _marker(collection, f"KiLeidoscope proximity box {len(boxes) + 1}", cube)
            box.matrix_world = warning["box_matrix"]
            boxes[warning["obstacle"]] = box.name
        warning["box"] = boxes[warning["obstacle"]]
        active |= {span_name, warning["box"]}
    for obj in list(collection.objects):
        if obj.name not in active:
            data = obj.data
            bpy.data.objects.remove(obj)
            if data is not cube and data is not None and data.users == 0:
                bpy.data.meshes.remove(data)
    _result.update(rows=warnings, checked=True)
    return len(warnings)


def clear():
    collection = bpy.data.collections.get(COLLECTION)
    if collection is not None:
        for obj in list(collection.objects):
            bpy.data.objects.remove(obj)
    _result.update(rows=[], checked=False, nets=0)


def rows():
    return _result["rows"]


def status():
    """Panel text: None before the first check."""
    if not _result["checked"]:
        return None
    count, nets = len(_result["rows"]), _result["nets"]
    if not nets:
        return "No differential pairs or listed nets"
    checked = f"{nets} net{'s' if nets != 1 else ''} checked"
    return f"Nothing within the limit ({checked})" if not count else \
        f"{count} warning{'s' if count != 1 else ''} ({checked})"


def select(index):
    """Select a warning's span and box; while live, also its tracks and component in KiCad."""
    if not 0 <= index < len(_result["rows"]):
        return False
    warning = _result["rows"][index]
    view_layer = bpy.context.view_layer
    for obj in tuple(view_layer.objects.selected):
        obj.select_set(False)
    for name in (warning["span"], warning["box"]):
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.name in view_layer.objects:
            obj.select_set(True)
            view_layer.objects.active = obj
    if live.linked():
        live.request_select(warning["ids"] + ([warning["footprint_id"]] if warning["footprint_id"] else []))
    return True


def _timer():
    try:
        run()
    except Exception as exc:  # never leave the timer failing silently
        print(f"KiLeidoscope proximity check failed: {exc}")
    return None


def schedule():
    """Check a moment after the tracks, models and boards stop changing."""
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    bpy.app.timers.register(_timer, first_interval=DELAY_S)


def _relevant(block):
    """Tracks, components, model parts, board solids and view-only roots; never the markers."""
    return isinstance(block, bpy.types.Object) and (
        block.get("kls_model_fp_id") is not None or block.get("kls_footprint") == 1 or
        (block.get("kls_copper") or ("", ""))[1] == "tracks" or block.name.endswith(OUTLINE) or
        bool(block.get(packages.ROOT_TAG)))


def _on_depsgraph(scene, depsgraph):
    if board.collection is None or not getattr(scene, "kileido_proximity", True):
        return
    for update in depsgraph.updates:
        if (update.is_updated_geometry or update.is_updated_transform) and _relevant(update.id):
            schedule()
            return


def install():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def uninstall():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)


# --- Panel ----------------------------------------------------------------------------------
# Kept here, not in __init__.py beside the other panels, so the sidebar gains one entry.

MAX_ROWS_SHOWN = 12


class KILEIDO_OT_proximity_select(bpy.types.Operator):
    bl_idname = "kileido.proximity_select"
    bl_label = "Select proximity warning"
    bl_description = ("Select the flagged trace span and the object near it (in KiCad too, while live). "
                      "Whether it disturbs the impedance depends on its material, which the model does not say")
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        return {"FINISHED"} if select(self.index) else {"CANCELLED"}


class KILEIDO_OT_proximity_add_selection(bpy.types.Operator):
    bl_idname = "kileido.proximity_add_selection"
    bl_label = "Add KiCad selection"
    bl_description = "Add the nets of the tracks selected in KiCad to the nets checked"

    def execute(self, context):
        nets = listed_nets(context.scene)
        added = sorted(selected_nets() - set(nets))
        if not added:
            self.report({"WARNING"}, "KiLeidoscope: select tracks in KiCad first")
            return {"CANCELLED"}
        context.scene.kileido_proximity_nets = ", ".join(nets + added)
        return {"FINISHED"}


class KILEIDO_PT_proximity(bpy.types.Panel):
    """Objects close over the outer-layer tracks of differential pairs and listed nets."""
    bl_label = "Proximity"
    bl_idname = "KILEIDO_PT_proximity"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "KiLeidoscope"
    bl_parent_id = "KILEIDO_PT_panel"

    def draw_header(self, context):
        self.layout.prop(context.scene, "kileido_proximity", text="")

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        layout.active = scene.kileido_proximity
        row = layout.row(align=True)
        row.prop(scene, "kileido_proximity_factor", text="Limit (× h)")
        found = limits(scene)
        if found:
            right = row.row(align=True)
            right.alignment = "RIGHT"
            right.active = False
            right.label(text=" · ".join(f"{layer[0]} {value * 1e3:.2f} mm" for layer, value in found.items()))
        row = layout.row(align=True)
        row.prop(scene, "kileido_proximity_nets", text="Nets")
        row.operator(KILEIDO_OT_proximity_add_selection.bl_idname, text="", icon="EYEDROPPER")
        text = status() if scene.kileido_proximity else None
        if text:
            layout.label(text=text, icon="ERROR" if rows() else "CHECKMARK")
        column = layout.column(align=True)
        for index, warning in enumerate(rows()[:MAX_ROWS_SHOWN]):
            line = column.row(align=True)
            line.alignment = "LEFT"
            button = line.operator(KILEIDO_OT_proximity_select.bl_idname, emboss=False, icon="RESTRICT_SELECT_OFF",
                                     text=f"{warning['label']} · {warning['net']} · "
                                          f"{warning['distance_m'] * 1e3:.2f} mm")
            button.index = index
        if len(rows()) > MAX_ROWS_SHOWN:
            column.label(text=f"… {len(rows()) - MAX_ROWS_SHOWN} more")


CLASSES = (KILEIDO_OT_proximity_select, KILEIDO_OT_proximity_add_selection, KILEIDO_PT_proximity)


def scene_properties():
    return {
        "kileido_proximity": bpy.props.BoolProperty(
            name="Proximity warnings", default=True,
            description="Mark 3D objects close over the outer-layer tracks of differential pairs and listed "
                        "nets, where they can disturb the impedance",
            update=lambda self, context: schedule()),
        "kileido_proximity_factor": bpy.props.FloatProperty(
            name="Proximity limit", default=DEFAULT_FACTOR, min=0.5, soft_max=20.0, step=50, precision=1,
            description="Warn within this many times h, the dielectric between the outer layer and its "
                        "nearest copper layer (from the stackup)",
            update=lambda self, context: schedule()),
        "kileido_proximity_nets": bpy.props.StringProperty(
            name="Nets", default="",
            description="More nets to check, comma-separated (differential pairs are always checked)",
            update=lambda self, context: schedule()),
    }
