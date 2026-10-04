"""Flex mode in Blender: the board's copy with thin, amber flex that folds at its bends.

The board objects are built by Geometry Nodes from flat outlines, tracks and points, and
their plots are projected from where they lie. So flex mode works on copies: each visible
board object is realized, cut along every bend's strip (so it can curve) and along the
flex's edges with rigid board, its flex squeezed thin, and tagged per point (foldmath.tags).
On the flex the board is polyimide (no solder mask: the coverlay takes its place), and
stiffeners stand under or over it. The copies carry the "KLS Fold" Geometry Nodes
modifier, whose inputs follow the bends' handles: turning one refolds the board at once,
without baking again. The flat originals hide while a flex board is shown; the copies'
shaders read their flat positions (kls_flat).

The copies are baked again after a live edit (`invalidate`), shortly after it settles.
"""

import math

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector, geometry
from mathutils.bvhtree import BVHTree

from . import foldmath, materials, nodes
from . import section as section_module
from .objects import hide, read_attribute, read_coordinates
from .state import board, log

COLLECTION = "KLS folded"
GRIPS = "KiLeidoscope bends"  # in the scene, not the board's collection: the handles are selectable
GRIP_REACH_PX = 8  # a click this close to a handle's circle selects it
GROUP = "KLS_Fold"
REALIZE = "KLS_Realize"
SETTLE_S = 0.4  # after a live edit, before the copies are baked again
LIMIT_MARGIN_M = 0.3e-3  # a cut limited to a dome's finger reaches this far past it
MOVED_TOLERANCE = 1e-5  # a folded part's matrix changed more than this: moved by a live edit

_baked = {"plan": None, "stale": False, "planes": [], "body": (0.0, 0.0)}
# keyed: the plan the fold animation was keyed for; render: the scene's frame settings before it was keyed
_state = {"busy": False, "angles": [], "keyed": None, "render": None}
_variants = {}  # material name -> its flex variant's name, made once per bake
_planned = {}  # the last fold plan, with the report and frame it was made for


def _forget():
    """Start over: the Blender data remembered here is gone (a file loaded, the add-on off)."""
    _baked.update(plan=None, stale=False, planes=[], body=(0.0, 0.0))
    _state.update(busy=False, angles=[], keyed=None, render=None)
    _variants.clear()
    _planned.clear()


# --- Entry points ---------------------------------------------------------------------------

def available() -> bool:
    return _plan() is not None


def foldable() -> bool:
    """The board has bends to fold (not only thin flex)."""
    plan = _plan()
    return plan is not None and bool(plan.bends)


def set_progress(progress):
    """The panel's Fold slider: 0 the flat board, 1 every bend at KiCad's angle, the steps
    one after another in between. It turns the bends' handles; they fold the board."""
    plan = _plan()
    if plan is None:
        return
    if keyed():  # the animation's keys turn the handles: move along the timeline instead
        scene = bpy.context.scene
        scene.frame_set(scene.frame_start + round(progress * (scene.frame_end - scene.frame_start)))
        return
    _turn_grips(plan, progress)
    refresh()


def _turn_grips(plan, progress):
    _ensure_grips(plan)
    for grip, angle in zip(_grips(), foldmath.handle_angles_at(plan, progress)):
        grip.rotation_euler.z = angle


def refresh():
    """Show the board as its handles say: flat, or its folded copies at the handles' angles
    (baked first when missing or out of date), with the parts and handles where they fold to."""
    plan = _plan()
    if plan is None or _state["busy"]:
        return
    _state["busy"] = True
    try:
        if _state["keyed"] is not None and _state["keyed"] is not plan:  # keyed for a board that has changed since
            clear_animation(refold=False)
            _turn_grips(plan, bpy.context.scene.kileido_fold)
        else:
            _ensure_grips(plan)
        handles = [grip.rotation_euler.z for grip in _grips()]
        angles = foldmath.bend_angles(plan, handles)
        folded = any(abs(angle) > foldmath.MIN_ANGLE for angle in angles)
        if _baked["plan"] is None or _baked["stale"] or not _alive():
            bake(plan)
        _show_folded()  # thin flex, folded or not
        _place_parts(plan if folded else None, angles)
        _place_grips(plan, angles)
        _state["angles"] = handles
    finally:
        _state["busy"] = False


def _alive() -> bool:
    """The copies baked for `_baked["plan"]` are still in the scene (an undo step may have
    taken them, or brought older ones back)."""
    collection = _folded_collection()
    return (collection is not None and bpy.data.node_groups.get(GROUP) is not None
            and collection.get("kls_plan_id") == _plan_id(_baked["plan"]))


def _plan_id(plan):
    return "|".join((str(len(plan.regions)), *(_bend_id(plan, k) for k in range(len(plan.handles)))))


def invalidate():
    """The board changed (a live edit, a new flex report): bake again once edits settle."""
    _baked["stale"] = True
    if bpy.app.timers.is_registered(_settled):
        bpy.app.timers.unregister(_settled)
    bpy.app.timers.register(_settled, first_interval=SETTLE_S)


def clear():
    """Remove the folded copies (before baking again, or flex mode gone)."""
    collection = _folded_collection()
    if collection is not None:
        for obj in tuple(collection.objects):
            mesh = obj.data
            bpy.data.objects.remove(obj)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        bpy.data.collections.remove(collection)
    _restore_originals()
    _baked["plan"] = None
    board.fold_findings = []


def _settled():
    if _plan() is None:
        clear()
        _remove_grips()
        _place_parts(None, None)
    else:
        refresh()
    return None


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    """A handle turned in the viewport: fold to its new angle. Copies out of date are baked
    from the timer instead: Blender's data is not remade from inside its own update."""
    if _state["busy"]:
        return
    grips = _grips()
    if not grips or [grip.rotation_euler.z for grip in grips] == _state["angles"]:
        return
    if _baked["plan"] is None or _baked["stale"]:
        invalidate()
        return
    refresh()


def _handlers():
    return ((bpy.app.handlers.render_init, _render_starts),
            (bpy.app.handlers.render_complete, _render_ends),
            (bpy.app.handlers.render_cancel, _render_ends),
            (bpy.app.handlers.load_post, _file_loaded),
            (bpy.app.handlers.undo_post, _undone),
            (bpy.app.handlers.redo_post, _undone))


def install():
    _resume()
    for handlers, function in _handlers():
        if function not in handlers:
            handlers.append(function)


def uninstall():
    """Flex mode off: the scene as without it (Blender may be quitting, its data gone)."""
    _pause()
    for handlers, function in _handlers():
        if function in handlers:
            handlers.remove(function)
    if bpy.app.timers.is_registered(_settled):
        bpy.app.timers.unregister(_settled)
    for step in (lambda: clear_animation(refold=False), clear, _remove_grips, lambda: _place_parts(None, None)):
        try:
            step()
        except (ReferenceError, RuntimeError, AttributeError):
            pass
    _forget()


@bpy.app.handlers.persistent
def _file_loaded(*_):
    _forget()


@bpy.app.handlers.persistent
def _undone(*_):
    """An undo or redo step: the handles may have turned back (fold to them again), and
    the keys may have gone."""
    _state.update(busy=False, angles=[])
    if _state["keyed"] is not None and not any(grip.animation_data for grip in _grips()):
        _state.update(keyed=None, render=None)


def _resume():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def _pause():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)


# Blender runs frame handlers on its render thread, and an object moved from there crashes
# it: while it renders, nothing here moves anything (an animation is all keys).
@bpy.app.handlers.persistent
def _render_starts(*_):
    _pause()


@bpy.app.handlers.persistent
def _render_ends(*_):
    _resume()


# --- The fold as an animation ---------------------------------------------------------------

def keyed() -> bool:
    """The fold is keyed on the timeline for the board as it is."""
    return _state["keyed"] is not None and _state["keyed"] is _plan()


def key_animation(frames=72, fps=24):
    """Key the fold for rendering: frame 1 flat, folded at `frames`, the steps one after
    another. Every handle is keyed per frame (the board folds by their drivers) and every
    part's place per frame, worked out here once, so a render moves nothing itself."""
    scene = bpy.context.scene
    plan = _plan()
    if plan is None or not plan.bends:
        return False
    clear_animation()
    _state["render"] = (scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end,
                        scene.render.use_lock_interface)
    scene.render.fps, scene.render.fps_base = fps, 1.0
    scene.frame_start, scene.frame_end = 1, frames
    scene.render.use_lock_interface = True  # no edits from the interface while it renders
    _ensure_grips(plan)
    grips, parts = _grips(), _parts()
    places = {obj.name: [] for obj in parts}
    for frame in range(1, frames + 1):
        for grip, angle in zip(grips, foldmath.handle_angles_at(plan, (frame - 1) / max(1, frames - 1))):
            grip.rotation_euler.z = angle
            grip.keyframe_insert("rotation_euler", index=2, frame=frame)
        bpy.context.view_layer.update()
        refresh()
        bpy.context.view_layer.update()
        for obj in parts:
            places[obj.name].append((frame, tuple(obj.location), tuple(obj.rotation_euler), tuple(obj.scale)))
    for obj in parts:
        _key_places(obj, places[obj.name])
    _state["keyed"] = plan
    scene.frame_set(1)
    return True


def clear_animation(refold=True):
    """Back to live folding: the keys gone, the scene's frame settings as before, the parts
    on their flat places, and (`refold`) the board at the slider's place."""
    was = _state["keyed"] is not None
    _state["keyed"] = None
    grips = _grips()
    for obj in (*grips, *_parts()):
        action = obj.animation_data.action if obj.animation_data else None
        if action is not None and action.name.startswith(("KLS fold", "KLS sweep")) or obj in grips:
            obj.animation_data_clear()
            if action is not None and action.users == 0:
                bpy.data.actions.remove(action)
    _reset_parts()
    if _state["render"] is not None:
        scene = bpy.context.scene
        (scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end,
         scene.render.use_lock_interface) = _state["render"]
        _state["render"] = None
    if was and refold and _plan() is not None:
        set_progress(bpy.context.scene.kileido_fold)


def _reset_parts():
    """Every part back on the flat place the fold recorded, the record dropped: once the
    keys are gone a part stands where its frame left it, not where the fold put it."""
    if board.collection is None:
        return
    for obj in _parts():
        flat = obj.get("kls_flat_matrix")
        if flat is not None and len(flat) == 16:
            obj.matrix_world = Matrix(np.reshape(flat, (4, 4)).tolist())
        for key in ("kls_flat_matrix", "kls_folded_matrix"):
            if key in obj:
                del obj[key]


def _key_places(obj, places):
    """Key a part's location, rotation and scale at every frame, in one go per channel."""
    obj.rotation_mode = "XYZ"
    action = bpy.data.actions.new(f"KLS fold {obj.name}")
    obj.animation_data_create().action = action
    for path, column in (("location", 1), ("rotation_euler", 2), ("scale", 3)):
        for index in range(3):
            curve = action.fcurve_ensure_for_datablock(obj, path, index=index)
            curve.keyframe_points.add(len(places))
            values = [value for place in places for value in (float(place[0]), place[column][index])]
            curve.keyframe_points.foreach_set("co", values)
            curve.keyframe_points.foreach_set("interpolation", [0] * len(places))  # CONSTANT: whole frames
            curve.update()


def _plan():
    """The fold plan for the board's flex report while flex mode is on (the Flex column's
    Enable), made again only when the report or the board's frame changes (each handle
    turn asks for it)."""
    if board.collection is None or not board.flex or not getattr(bpy.context.scene, "kileido_flex", False):
        return None
    key = (tuple(board.origin_nm), tuple(sorted(board.heights.items())), tuple(sorted(board.layer_thickness.items())))
    cached = _planned.get("plan")
    if _planned.get("report") is not board.flex or _planned.get("key") != key:
        cached = foldmath.plan(board.flex, board.origin_nm, board.heights, board.layer_thickness)
        _planned.update(report=board.flex, key=key, plan=cached)
    return cached


# --- Baking the copies ----------------------------------------------------------------------

def _folded_collection():
    if board.collection is None:
        return None
    return next((child for child in board.collection.children if child.get("kls_folded")), None)


def _originals():
    folded = _folded_collection()
    skip = set(folded.objects) if folded is not None else set()
    return [obj for obj in board.collection.all_objects
            if obj.type == "MESH" and obj not in skip and obj.get("kls_model_fp_id") is None
            and not obj.name.startswith("KLS footprint ")  # parts: placeholders and boxes fold whole
            and not obj.hide_get() and (not obj.hide_viewport or obj.get("kls_hidden_by_fold"))]


def _realize_group():
    group = bpy.data.node_groups.get(REALIZE)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(REALIZE, "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source, realize, out = (group.nodes.new(kind) for kind in
                            ("NodeGroupInput", "GeometryNodeRealizeInstances", "NodeGroupOutput"))
    group.links.new(source.outputs[0], realize.inputs[0])
    group.links.new(realize.outputs[0], out.inputs[0])
    return group


def _realized_mesh(obj):
    """The object's evaluated geometry, instances realized, as a new mesh in world space."""
    modifier = obj.modifiers.new("KLS realize", "NODES")
    modifier.node_group = _realize_group()
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
        mesh.transform(obj.matrix_world)  # board objects sit at their layer's height
        return mesh
    finally:
        obj.modifiers.remove(modifier)


def _body_range():
    outline = board.collection.all_objects.get("KLS outline")
    if outline is None:
        return 0.0, board.thickness_m
    depsgraph = bpy.context.evaluated_depsgraph_get()
    zs = [(outline.matrix_world @ Vector(corner)).z for corner in outline.evaluated_get(depsgraph).bound_box]
    return min(zs), max(zs)


def bake(plan):
    """Fresh folded copies of every visible board object for `plan`."""
    clear()
    _variants.clear()
    collection = bpy.data.collections.new(COLLECTION)
    collection["kls_folded"] = 1
    collection["kls_plan_id"] = _plan_id(plan)
    board.collection.children.link(collection)
    group = _fold_group(plan)
    planes = foldmath.cut_planes(plan)
    _baked.update(planes=planes, body=_body_range())
    count = sum(_bake_object(original, plan, collection) for original in _originals())
    for index, stiffener in enumerate(board.flex.get("stiffeners", ())):
        mesh = _stiffener_mesh(stiffener, plan)
        if mesh is not None:
            _cut(mesh, planes)
            _link(collection, group, _tagged(mesh, read_coordinates(mesh).astype(np.float64), plan),
                  f"KLS stiffener {index + 1}", "")
    _baked["plan"], _baked["stale"] = plan, False
    log(f"fold: {count} folded copies, {len(plan.bends)} bends, {len(planes)} cuts")
    check_steps(plan)


def check_steps(plan):
    """Fold the board body to the end of each step and find where it runs into itself:
    pieces that are not joined (foldmath.apart) and whose faces cross. Worked out with
    foldmath on the body's flat points, so nothing in the scene moves meanwhile."""
    board.fold_findings = []
    body = _folded_collection().objects.get("KLS folded outline") if _folded_collection() else None
    if body is None or not plan.bends:
        return
    mesh = body.data
    flat = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.attributes["kls_flat"].data.foreach_get("vector", flat)
    flat = flat.reshape(-1, 3).astype(np.float64)
    mask = _read_mask(mesh, plan)
    wrap = read_attribute(mesh, "kls_fold_wrap", np.float32).astype(np.float64)
    zone = read_attribute(mesh, "kls_fold_zone", np.float32).astype(np.int64)
    polygons = [tuple(polygon.vertices) for polygon in mesh.polygons]
    faces = foldmath.face_pieces(plan, flat, zone, polygons)
    sequence = foldmath.steps(plan)
    seen = set()
    for count, (number, _) in enumerate(sequence, start=1):
        angles = foldmath.angles_at(plan, count / len(sequence))
        folded = foldmath.fold(flat, mask, zone, angles, plan, wrap)
        tree = BVHTree.FromPolygons(folded.tolist(), polygons)
        for a, b in tree.overlap(tree):
            first, second = sorted((faces[a], faces[b]))
            if (first, second) in seen or not foldmath.apart(plan, first, second):
                continue
            seen.add((first, second))
            names = [foldmath.piece_name(plan, item) for item in (first, second)]
            said = f"Step {number}: " if len(sequence) > 1 else "Folded: "
            board.fold_findings.append(f"{said}{names[0]} runs into {names[1]}")


def _bake_object(original, plan, collection):
    """The folded copy of one board object (True), or nothing for an empty one (False)."""
    mesh = _realized_mesh(original)
    _cut(mesh, _baked["planes"])
    coverlay = board.flex.get("coverlay", "AMBER")  # from KiCad: a "Coverlay black" text on the Flex layer
    if original.get("kls_cosmetic_layer") in ("F.Mask", "B.Mask"):
        _on_flex(mesh, plan, lambda mask: materials.coverlay_variant(mask, coverlay))  # it takes the mask's place
    elif original.name == "KLS outline":
        _flex_faces(mesh, plan, board.materials["flex"])
    else:
        finished = {board.materials[key].name for key in materials.FINISHED if key in board.materials}
        _on_flex(mesh, plan, lambda copper: materials.flex_copper_variant(copper, coverlay), finished)
    if not len(mesh.vertices):
        bpy.data.meshes.remove(mesh)
        return False
    flat = foldmath.thin(read_coordinates(mesh).astype(np.float64), plan, _baked["body"])
    mesh.vertices.foreach_set("co", flat.astype(np.float32).ravel())
    copy = _link(collection, bpy.data.node_groups.get(GROUP), _tagged(mesh, flat, plan),
                 f"KLS folded {original.name.removeprefix('KLS ')}", original.name)
    for ray in ("camera", "diffuse", "glossy", "transmission", "volume_scatter", "shadow"):
        setattr(copy, f"visible_{ray}", getattr(original, f"visible_{ray}"))  # highlights: camera rays only
    copy["kls_zone_highlight"] = original.get("kls_zone_highlight", "")
    return True


def refresh_highlights():
    """KiCad's selection changed (highlight.refresh): the highlight copies and the zones that
    switched material are baked again on their own; the rest of the copy stays."""
    plan, collection = _baked["plan"], _folded_collection()
    if plan is None or collection is None or _state["busy"]:
        return
    if not _alive():  # an undo step took the copies' node group: baked again once things settle
        invalidate()
        return
    _state["busy"] = True
    try:
        _restore_highlight_originals()
        for copy in tuple(collection.objects):
            original = board.collection.all_objects.get(copy.get("kls_folded_from", ""))
            if original is not None and _stale_highlight(copy, original):
                _remove_copy(copy)
                _unhide(original)  # Blender evaluates no hidden object: baked hidden, a zone comes out bare
        baked = {copy.get("kls_folded_from") for copy in collection.objects}
        for original in _originals():
            if original.name not in baked and ("highlight" in original.name or original.get("kls_zone_highlight")
                                               is not None):
                _bake_object(original, plan, collection)
        angles = foldmath.bend_angles(plan, [grip.rotation_euler.z for grip in _grips()])
        _show_folded()
        _place_parts(plan if any(abs(a) > foldmath.MIN_ANGLE for a in angles) else None, angles)
    finally:
        _state["busy"] = False


def _stale_highlight(copy, original):
    if "highlight" in original.name:
        return True  # rebuilt from the copper at every selection change
    return original.get("kls_zone_highlight") is not None and \
        copy.get("kls_zone_highlight", "") != original.get("kls_zone_highlight", "")


def _restore_highlight_originals():
    """Highlight objects hidden for an earlier copy show again until baked anew."""
    for obj in tuple(board.collection.all_objects):  # a snapshot: hiding rebuilds Blender's list
        if "highlight" in obj.name:
            _unhide(obj)


def _unhide(original):
    """An original the fold hid behind its copy, shown again (until `_show_folded` hides it)."""
    if original.get("kls_hidden_by_fold"):
        del original["kls_hidden_by_fold"]
        original.hide_viewport = original.hide_render = False


def _remove_copy(copy):
    mesh = copy.data
    bpy.data.objects.remove(copy)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def flat_point(copy, depsgraph, location, face):
    """Where a point on a copy's face (a ray's hit) lies on the flat board, or None."""
    evaluated = copy.evaluated_get(depsgraph)
    mesh = evaluated.data
    if face >= len(mesh.polygons) or "kls_flat" not in mesh.attributes:
        return None
    flat = mesh.attributes["kls_flat"].data
    corners = list(mesh.polygons[face].vertices)
    world = evaluated.matrix_world
    for k in range(1, len(corners) - 1):  # the fan triangle holding the point
        a, b, c = (corners[0], corners[k], corners[k + 1])
        points = [world @ mesh.vertices[v].co for v in (a, b, c)]
        if geometry.intersect_point_tri(location, *points) is not None or k == len(corners) - 2:
            return geometry.barycentric_transform(location, *points, *(Vector(flat[v].vector) for v in (a, b, c)))
    return None


def section(rects, line):
    """The cut plane's section (section.cross_section's rectangles) as flex mode shows the
    board: along the stretches of the cut in the flex, the thin flex stack (its copper,
    plain polyimide from coverlay to coverlay); while folded, only where the board has
    not moved (the cut through a folded flap is no straight section)."""
    plan, collection = _baked["plan"], _folded_collection()
    if plan is None or collection is None or collection.hide_viewport:
        return rects
    flex = _crossed(line, plan.zones)
    out = []
    board_s, metal = [], []
    for s0, s1, z0, z1, color in rects:
        span = np.array([[s0, s1]])
        out += [(a, b, z0, z1, color) for a, b in section_module.subtract(span, flex)]
        for a, b in section_module.intersect(span, flex):
            board_s.append((a, b))
            if color in section_module.WOVEN or color in (section_module.POLYIMIDE, section_module.IMS_DIELECTRIC):
                continue  # laminate: the flex is polyimide all through
            low, high = foldmath.squeeze([z0, z1], plan, _baked["body"])
            if high - low > 1e-9:
                metal.append((a, b, float(low), float(high), color))
    out += _flex_stack(section_module.merge(board_s), metal, plan.z_range)
    angles = [grip.rotation_euler.z for grip in _grips()]
    if any(abs(angle) > foldmath.MIN_ANGLE for angle in angles):
        still = section_module.merge(np.vstack([section_module.EMPTY, *(
            _crossed(line, [region]) for region, parent in zip(plan.regions, _parents(plan)) if parent is None)]))
        strips = _crossed(line, [_strip_ring(bend) for bend in plan.bends])
        still = section_module.subtract(still, strips)
        out = [(a, b, z0, z1, color) for s0, s1, z0, z1, color in out
               for a, b in section_module.intersect(np.array([[s0, s1]]), still)]
    return out


def _flex_stack(board_s, metal, z_range):
    """Rectangles of the flex's section: its copper (and what else is not laminate) where
    the cut crosses it, polyimide around it from the bottom coverlay to the top one."""
    low, high = z_range
    cuts = sorted({low, high, *(z for _, _, z0, z1, _ in metal for z in (z0, z1) if low <= z <= high)})
    out = []
    for z0, z1 in zip(cuts, cuts[1:]):
        middle = (z0 + z1) / 2
        here = [(a, b, color) for a, b, m0, m1, color in metal if m0 <= middle < m1]
        taken = section_module.merge([(a, b) for a, b, _ in here])
        out += [(a, b, z0, z1, color) for a, b, color in here]
        out += [(a, b, z0, z1, section_module.POLYIMIDE) for a, b in section_module.subtract(board_s, taken)]
    out += [rect for rect in metal if rect[3] <= low or rect[2] >= high]  # outside the flex (films)
    return out


def _crossed(line, rings):
    """Where the cut runs inside any of `rings` (world xy), as section intervals."""
    if not rings:
        return section_module.EMPTY
    a = np.vstack([ring for ring in rings])
    b = np.vstack([np.roll(ring, -1, axis=0) for ring in rings])
    item = np.concatenate([np.full(len(ring), k) for k, ring in enumerate(rings)])
    return section_module.rings(line, a, b, item)


def _parents(plan):
    return [None if not any(bend.child == index for bend in plan.bends) else 0 for index in range(len(plan.regions))]


def _strip_ring(bend):
    if bend.area is not None:  # drawn as its area: that is where it curves
        return bend.area
    half, ends = bend.width / 2, bend.length / 2 + bend.width
    corners = [(u, v) for u, v in ((-ends, -half), (ends, -half), (ends, half), (-ends, half))]
    return np.array([bend.origin[:2] + u * bend.axis[:2] + v * bend.normal[:2] for u, v in corners])


def _tagged(mesh, flat, plan):
    """The mesh with what the fold reads per point: its flat place and its tags. Its flat
    faces in a bend's or twist's strip shade smooth: they curve once folded."""
    mask, zone, wrap = foldmath.tags(flat, plan)
    _smooth_strips(mesh, zone)
    _attribute(mesh, "kls_flat", "FLOAT_VECTOR", "vector", flat)
    _attribute(mesh, "kls_folded", "FLOAT", "value", np.ones(len(flat)))
    for chunk in range(_mask_chunks(plan)):  # float attributes hold 24 bits exactly
        _attribute(mesh, _mask_name(chunk), "FLOAT", "value", (mask >> (MASK_BITS * chunk)) & ((1 << MASK_BITS) - 1))
    _attribute(mesh, "kls_fold_wrap", "FLOAT", "value", wrap)
    _dome_attributes(mesh, plan, mask, zone)
    _attribute(mesh, "kls_fold_zone", "FLOAT", "value", zone)
    mesh.update()
    return mesh


MASK_BITS = 24


def _dome_attributes(mesh, plan, mask, zone):
    """Per dome, per point: the frame, width and share of the angle of the finger it curls
    with (in its strip, or past it), so one node chain folds every finger."""
    count = len(zone)
    for handle, members in enumerate(plan.handles):
        if not members or not plan.bends[members[0]].finger:
            continue
        values = {part: np.zeros((count, 3)) for part in ("origin", "axis", "normal")}
        values.update({part: np.zeros(count) for part in ("width", "ratio", "in", "past")})
        for index in members:
            bend = plan.bends[index]
            here = zone == index
            past = ((mask >> index) & 1 == 1) & ~here
            for part, value in (("origin", bend.origin), ("axis", bend.axis), ("normal", bend.normal),
                                ("width", bend.width), ("ratio", bend.ratio)):
                values[part][here | past] = value
            values["in"][here], values["past"][past] = 1.0, 1.0
        for part, value in values.items():
            kind, field = ("FLOAT_VECTOR", "vector") if value.ndim == 2 else ("FLOAT", "value")
            _attribute(mesh, f"kls_dome{handle + 1}_{part}", kind, field, value)


def _mask_name(chunk):
    return "kls_fold_mask" if chunk == 0 else f"kls_fold_mask{chunk + 1}"


def _mask_chunks(plan):
    return max(1, -(-len(plan.bends) // MASK_BITS))


def _read_mask(mesh, plan):
    kind = foldmath.mask_dtype(len(plan.bends))
    mask = np.zeros(len(mesh.vertices), dtype=kind)
    for chunk in range(_mask_chunks(plan)):
        mask |= read_attribute(mesh, _mask_name(chunk), np.float32).astype(np.int64).astype(kind) << (MASK_BITS * chunk)
    return mask


def _smooth_strips(mesh, zone):
    """Faces lying flat (top and bottom, not walls) with every corner in a strip (one, or
    two meeting, as a dome's finger on its wrap): smooth."""
    if not len(mesh.polygons):
        return
    count = len(mesh.polygons)
    normals, starts = np.empty(count * 3, np.float32), np.empty(count, np.int32)
    mesh.polygons.foreach_get("normal", normals)
    mesh.polygons.foreach_get("loop_start", starts)
    corners = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", corners)
    smooth = np.minimum.reduceat(zone[corners] >= 0, starts) if len(corners) else np.zeros(count, bool)
    smooth &= np.abs(normals.reshape(-1, 3)[:, 2]) > 0.7
    if smooth.any():
        current = np.empty(len(mesh.polygons), bool)
        mesh.polygons.foreach_get("use_smooth", current)
        mesh.polygons.foreach_set("use_smooth", current | smooth)


def _link(collection, group, mesh, name, original):
    copy = bpy.data.objects.new(name, mesh)
    copy["kls_folded_from"] = original  # "" for what only flex mode draws (stiffeners)
    modifier = copy.modifiers.new("KLS Fold", "NODES")
    modifier.node_group = group
    collection.objects.link(copy)
    _drive(modifier, group)
    return copy


def _face_flexness(mesh, plan):
    """Per face of the (cut) mesh: how far it lies in the flex (foldmath.flexness)."""
    centres = np.empty(len(mesh.polygons) * 3, np.float32)
    mesh.polygons.foreach_get("center", centres)
    return foldmath.flexness(centres.reshape(-1, 3)[:, :2].astype(np.float64), plan)


def _flex_faces(mesh, plan, material):
    """The board body's faces on the flex take the polyimide material."""
    if material.name not in [slot.name for slot in mesh.materials if slot is not None]:
        mesh.materials.append(material)
    slot = [entry.name if entry is not None else "" for entry in mesh.materials].index(material.name)
    indices = np.empty(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("material_index", indices)
    indices[_face_flexness(mesh, plan) > 0.5] = slot
    mesh.polygons.foreach_set("material_index", indices)


def _on_flex(mesh, plan, variant_of, only=None):
    """Faces on the flex take `variant_of(material)` in place of their material (only for
    materials named in `only`, if given): one variant per material, shared by the copies."""
    if not len(mesh.polygons):
        return
    slots = list(mesh.materials)
    on_flex = _face_flexness(mesh, plan) > 0.5
    indices = np.empty(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("material_index", indices)
    for slot, material in enumerate(slots):
        if material is None or (only is not None and material.name not in only):
            continue
        faces = on_flex & (indices == slot)
        if not faces.any():
            continue
        variant = bpy.data.materials.get(_variants.get(material.name, ""))
        if variant is None:
            variant = variant_of(material)
            _variants[material.name] = variant.name
        if variant.name not in [entry.name for entry in mesh.materials if entry is not None]:
            mesh.materials.append(variant)
        indices[faces] = [entry.name if entry is not None else "" for entry in mesh.materials].index(variant.name)
    mesh.polygons.foreach_set("material_index", indices)


def _stiffener_mesh(stiffener, plan):
    """A stiffener as a solid of its thickness against the flex's top or bottom, in a
    material for what it is made of (polyimide, metal, else FR4), with its holes (openings
    drawn in it, the board's drills and cutouts) cut through, walls and all."""
    thickness = (stiffener.get("thickness_nm") or 0) * 1e-9
    ring = foldmath.world_xy(stiffener["ring"], board.origin_nm)
    if thickness <= 0 or len(ring) < 3:
        return None
    rings = [ring] + [hole for hole in (foldmath.world_xy(points, board.origin_nm)
                                        for points in stiffener.get("holes", ())) if len(hole) >= 3]
    low, high = plan.z_range
    base = high if stiffener.get("side") == "top" else low - thickness
    work = bmesh.new()
    levels = []  # per ring: its bottom and top vertices
    for points in rings:
        levels.append([[work.verts.new((float(x), float(y), z)) for x, y in points] for z in (base, base + thickness)])
    bottom = [vert for floor, _ in levels for vert in floor]
    top = [vert for _, roof in levels for vert in roof]
    for triangle in geometry.tessellate_polygon([[vert.co for vert in floor] for floor, _ in levels]):
        work.faces.new([bottom[k] for k in triangle])
        work.faces.new([top[k] for k in reversed(triangle)])
    for floor, roof in levels:  # the outside wall, and each hole's
        for k in range(len(floor)):
            work.faces.new((floor[k - 1], floor[k], roof[k], roof[k - 1]))
    bmesh.ops.recalc_face_normals(work, faces=work.faces[:])
    mesh = bpy.data.meshes.new("KLS stiffener")
    work.to_mesh(mesh)
    work.free()
    look, _ = foldmath.stiffener_look(stiffener.get("material", ""))
    mesh.materials.append(board.materials[{"metal": "stiffener_metal", "polyimide": "flex"}.get(look, "board_core")])
    return mesh


def _cut(mesh, planes):
    """Cut the world-space `mesh` along every plane."""
    work = bmesh.new()
    work.from_mesh(mesh)
    for point, normal, *limit in planes:
        faces = work.faces[:]
        if limit:  # only the faces reaching into that ring (or a little past it), however large
            work.verts.index_update()
            corners = np.array([vert.co[:2] for vert in work.verts]).reshape(-1, 2)
            near = foldmath.inside(corners, limit[0]) | (foldmath.edge_distance(
                corners, np.stack((limit[0], np.roll(limit[0], -1, axis=0)), axis=1)) < LIMIT_MARGIN_M)
            faces = [face for face in faces if any(near[vert.index] for vert in face.verts)]
            geometry = list({item for face in faces for item in (*face.verts, *face.edges)}) + faces
        else:
            geometry = work.verts[:] + work.edges[:] + faces
        if geometry:
            bmesh.ops.bisect_plane(work, geom=geometry, dist=1e-9, plane_co=Vector(point), plane_no=Vector(normal))
    work.to_mesh(mesh)
    work.free()


def _attribute(mesh, name, kind, field, values):
    if name in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[name])
    attribute = mesh.attributes.new(name, kind, "POINT")
    attribute.data.foreach_set(field, np.asarray(values, dtype=np.float32).ravel())


# --- Showing it -----------------------------------------------------------------------------

def _show_folded():
    """The copies shown, their originals hidden behind them."""
    collection = _folded_collection()
    if collection is None:
        return
    collection.hide_viewport = collection.hide_render = False
    _follow_layer_eyes(collection)
    for obj in tuple(collection.objects):
        original = board.collection.all_objects.get(obj.get("kls_folded_from", ""))
        if original is not None and not original.get("kls_hidden_by_fold"):
            original["kls_hidden_by_fold"] = 1
            original.hide_viewport = original.hide_render = True


def follow_layers():
    """A row's eye changed in the Layers list (called inside its loop over the board's
    objects): the copies show as their originals would. Only the copies' own hiding
    changes here, as the Layers list's does."""
    collection = _folded_collection()
    if collection is not None and not collection.hide_viewport:
        _follow_layer_eyes(collection)


def _follow_layer_eyes(collection):
    for obj in tuple(collection.objects):
        original = board.collection.all_objects.get(obj.get("kls_folded_from", ""))
        hidden = bool(original is not None and original.get("kls_layer_hidden"))  # its row's eye
        if obj.hide_get() != hidden:
            hide(obj, hidden)
            obj.hide_render = hidden


def _restore_originals():
    if board.collection is None:
        return
    for obj in tuple(board.collection.all_objects):  # a snapshot: hiding rebuilds Blender's list
        if obj.get("kls_hidden_by_fold"):
            del obj["kls_hidden_by_fold"]
            obj.hide_viewport = obj.hide_render = False


def _drive(modifier, group):
    """Each "Angle n" input follows bend n's handle: its turn about its own z axis."""
    sockets = [item.identifier for item in group.interface.items_tree
               if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name.startswith("Angle")]
    for identifier, grip in zip(sockets, _grips()):
        owner, path = _angle_input(modifier, identifier)
        driver = owner.driver_add(path).driver
        driver.type = "AVERAGE"  # no Python expression: works without auto-run scripts
        variable = driver.variables.new()
        variable.type = "TRANSFORMS"
        target = variable.targets[0]
        target.id = grip
        target.transform_type = "ROT_Z"
        target.transform_space = "LOCAL_SPACE"


def _angle_input(modifier, identifier):
    """What holds a node group input's value, and the path to drive: on Blender 5.2 and later
    the modifier's own property for the socket, before that a custom property on the modifier
    named by the socket's identifier (made here when missing)."""
    slot = nodes._input_slot(modifier, identifier)
    if slot is not None:
        return slot, "value"
    if modifier.get(identifier) is None:
        modifier[identifier] = 0.0
    return modifier, f'["{identifier}"]'


def _parts():
    """Component models, placeholders and footprint frames: each folds as one piece."""
    return [obj for obj in board.collection.all_objects
            if obj.get("kls_model_fp_id") is not None or obj.get("kls_footprint_placeholder") == 1
            or obj.name.startswith("KLS footprint ")]  # frames, placeholders and their highlight boxes


def _place_parts(plan, angles):
    """Fold every part with the region it sits on (`plan` None: back to flat). A part moved
    by a live edit since it was folded takes its new place as its flat one."""
    if board.collection is None or keyed():  # keyed: the animation places them
        return
    placing = []
    for obj in _parts():
        flat = obj.get("kls_flat_matrix")
        folded = list(obj.get("kls_folded_matrix", ()))
        if flat is not None and (len(folded) != 16 or not np.allclose(_placed(obj), folded, atol=MOVED_TOLERANCE)):
            flat = None  # placed again since it was folded
        flat_matrix = Matrix(np.reshape(flat, (4, 4)).tolist()) if flat is not None else obj.matrix_world.copy()
        if plan is None:
            if flat is not None:
                obj.matrix_world = flat_matrix
            for key in ("kls_flat_matrix", "kls_folded_matrix"):
                if key in obj:
                    del obj[key]
            continue
        placing.append((obj, flat_matrix))
    if plan is None or not placing:
        return
    where = np.array([(matrix.translation.x, matrix.translation.y, plan.z_mid) for _, matrix in placing])
    for (obj, flat_matrix), fold_matrix in zip(placing, foldmath.point_matrices(where, angles, plan)):
        obj["kls_flat_matrix"] = _matrix_list(flat_matrix)
        obj.matrix_world = Matrix(fold_matrix.tolist()) @ flat_matrix
        obj["kls_folded_matrix"] = _placed(obj)


def unfold_parts():
    """Every folded part back on its flat place (until the next `refresh`): for what places
    things relative to them, such as models binding to their footprints."""
    if not _state["busy"]:
        _place_parts(None, None)


def refold_parts():
    """After `unfold_parts`: the parts folded again, if the board is shown folded."""
    if _baked["plan"] is not None:
        refresh()


def _placed(obj):
    """Where a part is, as Blender keeps it: from its location, rotation and scale (what a
    matrix set on it becomes once evaluated; near gimbal lock that differs from the matrix
    set by more than a live edit's tolerance would allow), or its world matrix if parented."""
    return _matrix_list(obj.matrix_basis if obj.parent is None else obj.matrix_world)


def _matrix_list(matrix):
    return [float(value) for row in matrix for value in row]


# --- The bends' handles ----------------------------------------------------------------------

def _grip_collection(create=False):
    collection = bpy.data.collections.get(GRIPS)
    if collection is None and create:
        collection = bpy.data.collections.new(GRIPS)
        collection["kileido_owned"] = 1
        bpy.context.scene.collection.children.link(collection)
    return collection


def _grips():
    """Bend n's handle at index n - 1."""
    collection = _grip_collection()
    if collection is None:
        return []
    grips = [obj for obj in collection.objects if obj.get("kls_bend") is not None]
    return sorted(grips, key=lambda obj: obj["kls_bend"])


def _ensure_grips(plan):
    """One handle per bend: a circle round the bend's chord, turning about it (its z axis)
    folds the bend. It hangs from a frame placed where the bend lies on the folded board."""
    grips = _grips()
    if len(grips) == len(plan.handles) and all(grip.get("kls_bend_id") == _bend_id(plan, k)
                                               for k, grip in enumerate(grips)):
        return
    _remove_grips()
    collection = _grip_collection(create=True)
    for index, members in enumerate(plan.handles):
        bend = plan.bends[members[len(members) // 2]]  # a dome's: its middle finger
        frame = bpy.data.objects.new(f"KLS bend {index + 1} frame", None)
        frame.empty_display_size = 0.0
        frame.hide_select = True
        grip = bpy.data.objects.new(f"KLS bend {index + 1}", None)
        grip.empty_display_type = "CIRCLE"
        grip.empty_display_size = max(1e-3, min(bend.length / 3, 4e-3))
        grip.parent = frame
        grip.lock_location = grip.lock_scale = (True, True, True)
        grip.lock_rotation = (True, True, False)
        grip.rotation_mode = "XYZ"
        grip["kls_bend"] = index
        grip["kls_bend_id"] = _bend_id(plan, index)
        for obj in (frame, grip):
            collection.objects.link(obj)
    _place_grips(plan, [0.0] * len(plan.bends))


def _bend_id(plan, index):
    """Handle `index`'s bends, to tell when they change."""
    return ";".join(plan.bends[k].kind + ",".join(f"{value:.9f}" for value in (*plan.bends[k].origin,
                                                                                *plan.bends[k].axis,
                                                                                plan.bends[k].target))
                    for k in plan.handles[index])


def _bend_frame(bend):
    """x across the chord towards the child, y up, z along the chord: a turn about z by a
    positive angle lifts the child, as a positive fold does. A twist's: z along the tail
    (its line), through the start of its strip; a turn about z turns the tail."""
    if bend.kind == "twist":
        matrix = np.eye(4)
        matrix[:3, 0], matrix[:3, 1], matrix[:3, 2] = np.cross(foldmath.UP, bend.normal), foldmath.UP, bend.normal
        matrix[:3, 3] = bend.pivot - bend.normal * bend.width / 2
        return matrix
    along = bend.axis if np.dot(np.cross(bend.axis, bend.normal), foldmath.UP) > 0 else -bend.axis
    matrix = np.eye(4)
    matrix[:3, 0], matrix[:3, 1], matrix[:3, 2] = bend.normal, np.cross(along, bend.normal), along
    matrix[:3, 3] = bend.origin - bend.normal * bend.width / 2  # where the strip starts curving
    return matrix


def _place_grips(plan, angles):
    """Each handle's frame where its bend lies: on its parent region, folded as it is."""
    for grip, members in zip(_grips(), plan.handles):
        index = members[len(members) // 2]
        parent = foldmath.hinge_matrix(index, angles, plan)
        grip.parent.matrix_world = Matrix((parent @ _bend_frame(plan.bends[index])).tolist())


def _remove_grips():
    collection = _grip_collection()
    if collection is None:
        return
    for obj in tuple(collection.objects):
        bpy.data.objects.remove(obj)
    bpy.data.collections.remove(collection)


def clicked(project, coordinate):
    """The handle drawn under a click at `coordinate` (region pixels), or None. `project`
    turns a world point into region pixels, or None behind the view."""
    mouse = Vector(coordinate)
    for grip in _grips():
        if grip.hide_get() or grip.hide_viewport:
            continue
        centre = project(grip.matrix_world.translation)
        rim = project(grip.matrix_world @ Vector((grip.empty_display_size, 0.0, 0.0)))
        if centre is None or rim is None:
            continue
        if (Vector(centre) - mouse).length <= (Vector(rim) - Vector(centre)).length + GRIP_REACH_PX:
            return grip
    return None


def select(grip, extend=False):
    """Select a bend's handle, ready to turn (R)."""
    if not extend:
        for obj in tuple(bpy.context.selected_objects):
            obj.select_set(False)
    grip.select_set(True)
    bpy.context.view_layer.objects.active = grip


# --- The Geometry Nodes group ---------------------------------------------------------------

def _floats(vector):
    return tuple(float(value) for value in vector)


def _field(value):
    """A node output as it is, else a constant vector."""
    return value if hasattr(value, "is_output") else _floats(value)


class _Nodes:
    """Shorthand for building a node tree: each call adds a node and returns its output."""

    def __init__(self, tree):
        self.tree = tree

    def _feed(self, socket, value):
        if hasattr(value, "is_output"):
            self.tree.links.new(value, socket)
        else:
            socket.default_value = value

    def math(self, operation, *values):
        node = self.tree.nodes.new("ShaderNodeMath")
        node.operation = operation
        for socket, value in zip(node.inputs, values):
            self._feed(socket, value)
        return node.outputs[0]

    def vector(self, operation, *values, scale=None):
        node = self.tree.nodes.new("ShaderNodeVectorMath")
        node.operation = operation
        for socket, value in zip(node.inputs, values):
            self._feed(socket, value)
        if scale is not None:
            self._feed(node.inputs["Scale"], scale)
        return node.outputs["Value" if operation in ("DOT_PRODUCT", "LENGTH") else "Vector"]

    def attribute(self, name, kind):
        node = self.tree.nodes.new("GeometryNodeInputNamedAttribute")
        node.data_type = kind
        node.inputs["Name"].default_value = name
        return node.outputs["Attribute"]

    def frame(self, origin, axis, normal, u, v, h):
        """origin + u * axis + v * normal + h * up (constants or per-point fields)."""
        point = self.vector("ADD", _field(origin), self.vector("SCALE", _field(axis), scale=u))
        point = self.vector("ADD", point, self.vector("SCALE", _field(normal), scale=v))
        return self.vector("ADD", point, self.vector("SCALE", (0.0, 0.0, 1.0), scale=h))

    def local(self, origin, axis, normal, point):
        relative = self.vector("SUBTRACT", point, _field(origin))
        return (self.vector("DOT_PRODUCT", relative, _field(axis)),
                self.vector("DOT_PRODUCT", relative, _field(normal)),
                self.vector("DOT_PRODUCT", relative, (0.0, 0.0, 1.0)))

    def blend(self, current, target, factor):
        """current + (target - current) * factor."""
        return self.vector("ADD", current, self.vector("SCALE", self.vector("SUBTRACT", target, current),
                                                       scale=factor))


def _fold_group(plan):
    """The fold as Geometry Nodes, built for this plan's bends (foldmath.fold's steps)."""
    old = bpy.data.node_groups.get(GROUP)
    if old is not None:
        bpy.data.node_groups.remove(old)
    group = bpy.data.node_groups.new(GROUP, "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    for index in range(len(plan.handles)):
        group.interface.new_socket(name=f"Angle {index + 1}", in_out="INPUT", socket_type="NodeSocketFloat")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source, out = group.nodes.new("NodeGroupInput"), group.nodes.new("NodeGroupOutput")
    build = _Nodes(group)
    current = group.nodes.new("GeometryNodeInputPosition").outputs["Position"]
    flat = build.attribute("kls_flat", "FLOAT_VECTOR")
    masks = [build.attribute(_mask_name(chunk), "FLOAT") for chunk in range(_mask_chunks(plan))]
    zone = build.attribute("kls_fold_zone", "FLOAT")
    wrap = build.attribute("kls_fold_wrap", "FLOAT")
    domes = set()
    ribbons = plan.ribbons or {}
    seconds = {ribbon.second for ribbon in ribbons.values()}
    for index in plan.order:
        bend = plan.bends[index]
        frame = (bend.origin, bend.axis, bend.normal)
        angle = source.outputs[bend.handle + 1]
        if index in seconds:  # folded with its ribbon's first
            continue
        if index in ribbons:
            ribbon = ribbons[index]
            current = _ribbon_nodes(build, plan, ribbon, *(source.outputs[plan.bends[member].handle + 1]
                                                          for member in (ribbon.bend, ribbon.twist)),
                                    current, flat, masks, zone)
            continue
        if bend.ratio != 1.0:
            angle = build.math("MULTIPLY", angle, bend.ratio)
        mask, bit = masks[index // MASK_BITS], index % MASK_BITS
        if bend.closed:
            current = _wrap_nodes(build, bend, bit, angle, current, mask, wrap)
            continue
        if bend.kind == "twist":
            current = _twist_nodes(build, bend, index, bit, angle, current, flat, mask, zone)
            continue
        if bend.kind == "cone":
            current = _cone_nodes(build, bend, index, bit, angle, current, flat, mask, zone)
            continue
        if bend.finger:  # a dome: all its fingers at once, each point with its finger's frame
            if bend.handle in domes:
                continue
            domes.add(bend.handle)
            name = f"kls_dome{bend.handle + 1}"
            frame = tuple(build.attribute(f"{name}_{part}", "FLOAT_VECTOR") for part in ("origin", "axis", "normal"))
            angle = build.math("MULTIPLY", source.outputs[bend.handle + 1], build.attribute(f"{name}_ratio", "FLOAT"))
            current = _bend_nodes(build, frame, build.attribute(f"{name}_width", "FLOAT"), angle,
                                  build.attribute(f"{name}_past", "FLOAT"), build.attribute(f"{name}_in", "FLOAT"),
                                  current, flat)
            continue
        hangs = build.math("FLOORED_MODULO", build.math("FLOOR", build.math("DIVIDE", mask, float(2 ** bit))), 2.0)
        current = _bend_nodes(build, frame, bend.width, angle, hangs, build.math("COMPARE", zone, float(index), 0.5),
                              current, flat)
    store = group.nodes.new("GeometryNodeStoreNamedAttribute")  # the flat normal, for the shaders
    store.data_type, store.domain = "FLOAT_VECTOR", "FACE"
    store.inputs["Name"].default_value = "kls_flat_normal"
    group.links.new(source.outputs[0], store.inputs["Geometry"])
    group.links.new(group.nodes.new("GeometryNodeInputNormal").outputs["Normal"], store.inputs["Value"])
    place = group.nodes.new("GeometryNodeSetPosition")
    group.links.new(store.outputs[0], place.inputs["Geometry"])
    group.links.new(current, place.inputs["Position"])
    group.links.new(place.outputs[0], out.inputs[0])
    return group


def _bend_nodes(build, frame, width, angle, hangs, in_strip, current, flat):
    """foldmath's bend as nodes: past the strip (`hangs`) turned with its far edge, in it
    (`in_strip`) rolled onto the cylinder; the frame and width constants or per-point fields."""
    turn = build.math("MAXIMUM", build.math("ABSOLUTE", angle), foldmath.MIN_ANGLE)
    sign = build.math("SIGN", angle)  # 0 when flat: the formulas then leave points in place
    radius = build.math("DIVIDE", width, turn)
    half = build.math("MULTIPLY", width, 0.5)
    less = build.math("MULTIPLY", width, -0.5)
    # Past the strip: turned with its far edge (foldmath.rigid).
    u, v, h = build.local(*frame, current)
    beyond = build.math("SUBTRACT", v, half)
    reach = build.math("SUBTRACT", radius, build.math("MULTIPLY", sign, h))
    across = build.math("ADD", less, build.math("ADD", build.math("MULTIPLY", reach, build.math("SINE", turn)),
                                                build.math("MULTIPLY", beyond, build.math("COSINE", turn))))
    lift = build.math("MULTIPLY", build.math("MULTIPLY", sign, radius),
                      build.math("SUBTRACT", 1.0, build.math("COSINE", turn)))
    up = build.math("ADD", lift, build.math("ADD", build.math("MULTIPLY", h, build.math("COSINE", turn)),
                                            build.math("MULTIPLY", build.math("MULTIPLY", sign, beyond),
                                                       build.math("SINE", turn))))
    turned = build.frame(*frame, u, across, up)
    current = build.blend(current, turned, hangs)
    # In the strip: rolled onto the cylinder from the flat position (foldmath.strip).
    u, v, h = build.local(*frame, flat)
    phi = build.math("DIVIDE", build.math("ADD", build.math("MINIMUM", build.math("MAXIMUM", v, less), half),
                                          half), radius)
    reach = build.math("SUBTRACT", radius, build.math("MULTIPLY", sign, h))
    across = build.math("ADD", less, build.math("MULTIPLY", reach, build.math("SINE", phi)))
    up = build.math("ADD", build.math("MULTIPLY", build.math("MULTIPLY", sign, radius),
                                      build.math("SUBTRACT", 1.0, build.math("COSINE", phi))),
                    build.math("MULTIPLY", h, build.math("COSINE", phi)))
    rolled = build.frame(*frame, u, across, up)
    return build.blend(current, rolled, in_strip)


def _ribbon_nodes(build, plan, ribbon, bend_angle, twist_angle, current, flat, masks, zone):
    """foldmath.ribbon as nodes: in its strips from the flat position at the point's own s;
    past both from where earlier folds put it, at the end; on the middle region at the join."""
    def bit(index):
        return build.math("FLOORED_MODULO", build.math("FLOOR", build.math(
            "DIVIDE", masks[index // MASK_BITS], float(2 ** (index % MASK_BITS)))), 2.0)

    along = _floats(ribbon.along)
    in_strip = build.math("COMPARE", zone, float(ribbon.first), 0.5)
    past = bit(ribbon.second)
    s_flat = build.math("MINIMUM", build.math("MAXIMUM", build.vector(
        "DOT_PRODUCT", build.vector("SUBTRACT", flat, _floats(ribbon.centre)), along), ribbon.start), ribbon.end)
    s_rest = build.math("ADD", build.math("MULTIPLY", past, ribbon.end),
                        build.math("MULTIPLY", build.math("SUBTRACT", 1.0, past), ribbon.join))
    s = build.math("ADD", build.math("MULTIPLY", in_strip, s_flat),
                   build.math("MULTIPLY", build.math("SUBTRACT", 1.0, in_strip), s_rest))
    point = build.blend(current, flat, in_strip)
    kappa = build.math("DIVIDE", bend_angle, plan.bends[ribbon.bend].width)
    tau = build.math("DIVIDE", twist_angle, plan.bends[ribbon.twist].width)
    across = _floats(np.cross(ribbon.along, foldmath.UP))
    for start, length, bending, twisting in reversed(ribbon.pieces):
        rates = build.vector("SCALE", along, scale=build.math("ADD", tau, foldmath.RIBBON_TURN) if twisting
                             else foldmath.RIBBON_TURN)
        if bending:
            rates = build.vector("ADD", rates, build.vector("SCALE", across, scale=kappa))
        speed = build.vector("LENGTH", rates)
        axis = build.vector("SCALE", rates, scale=build.math("DIVIDE", 1.0, speed))
        travel = build.math("MINIMUM", build.math("MAXIMUM", build.math("SUBTRACT", s, start), 0.0), length)
        theta = build.math("MULTIPLY", speed, travel)
        parallel = build.vector("SCALE", axis, scale=build.vector("DOT_PRODUCT", along, axis))
        square = build.vector("SUBTRACT", along, parallel)
        helix = build.vector("ADD", build.vector("SCALE", parallel, scale=travel), build.vector(
            "SCALE", square, scale=build.math("DIVIDE", build.math("SINE", theta), speed)))
        helix = build.vector("ADD", helix, build.vector("SCALE", build.vector("CROSS_PRODUCT", axis, square), scale=build.math(
            "DIVIDE", build.math("SUBTRACT", 1.0, build.math("COSINE", theta)), speed)))
        node = build.tree.nodes.new("ShaderNodeVectorRotate")
        node.rotation_type = "AXIS_ANGLE"
        build.tree.links.new(build.vector("SUBTRACT", point, build.vector("SCALE", along, scale=travel)),
                             node.inputs["Vector"])
        node.inputs["Center"].default_value = _floats(ribbon.centre + start * ribbon.along)
        build._feed(node.inputs["Axis"], axis)
        build._feed(node.inputs["Angle"], theta)
        point = build.vector("ADD", node.outputs["Vector"], helix)
    return build.blend(current, point, build.math("MAXIMUM", in_strip, bit(ribbon.first)))


def _twist_nodes(build, bend, index, bit, angle, current, flat, mask, zone):
    """foldmath's twist as nodes: past its strip, the whole angle about its line; in it,
    the share of the angle by how far along a point lies (from its flat position)."""
    def turn(point, amount):
        node = build.tree.nodes.new("ShaderNodeVectorRotate")
        node.rotation_type = "AXIS_ANGLE"
        build.tree.links.new(point, node.inputs["Vector"])
        node.inputs["Center"].default_value = _floats(bend.pivot)
        node.inputs["Axis"].default_value = _floats(bend.normal)
        build._feed(node.inputs["Angle"], amount)
        return node.outputs["Vector"]

    hangs = build.math("FLOORED_MODULO", build.math("FLOOR", build.math("DIVIDE", mask, float(2 ** bit))), 2.0)
    current = build.blend(current, turn(current, angle), hangs)
    _, along, _ = build.local(bend.origin, bend.axis, bend.normal, flat)
    share = build.math("MINIMUM", build.math("MAXIMUM", build.math("DIVIDE", build.math("ADD", along, bend.width / 2),
                                                                   bend.width), 0.0), 1.0)
    return build.blend(current, turn(flat, build.math("MULTIPLY", angle, share)),
                       build.math("COMPARE", zone, float(index), 0.5))


def _cone_nodes(build, bend, index, bit, angle, current, flat, mask, zone):
    """foldmath's cone as nodes (foldmath._cone, strip, rigid)."""
    def turn(point, axis, amount):
        node = build.tree.nodes.new("ShaderNodeVectorRotate")
        node.rotation_type = "AXIS_ANGLE"
        build.tree.links.new(point, node.inputs["Vector"])
        node.inputs["Center"].default_value = _floats(bend.pivot)
        build._feed(node.inputs["Axis"], axis)
        build._feed(node.inputs["Angle"], amount)
        return node.outputs["Vector"]

    towards = build.math("SUBTRACT", 1.0, build.math("MULTIPLY", 2.0, build.math("LESS_THAN", angle, 0.0)))  # +-1
    psi = build.math("ADD", bend.alpha, build.math("ABSOLUTE", angle))
    sine = build.math("MINIMUM", build.math("DIVIDE", bend.alpha, psi), 1.0)
    cosine = build.math("SQRT", build.math("MAXIMUM", build.math("SUBTRACT", 1.0, build.math("MULTIPLY", sine, sine)),
                                           0.0))
    up = build.tree.nodes.new("ShaderNodeCombineXYZ")
    build._feed(up.inputs["Z"], build.math("MULTIPLY", towards, sine))
    axis = build.vector("NORMALIZE", build.vector("ADD", build.vector("SCALE", _floats(bend.ray), scale=cosine),
                                                  up.outputs[0]))
    sign = build.math("MULTIPLY", towards, 1.0 if bend.spin[2] > 0 else -1.0)
    hangs = build.math("FLOORED_MODULO", build.math("FLOOR", build.math("DIVIDE", mask, float(2 ** bit))), 2.0)
    rigid = turn(turn(current, _floats(bend.spin), -bend.alpha), axis, build.math("MULTIPLY", sign, psi))
    current = build.blend(current, rigid, hangs)
    relative = build.vector("SUBTRACT", flat, _floats(bend.pivot))
    across = np.cross(bend.spin, bend.ray)
    gamma = build.math("ARCTAN2", build.vector("DOT_PRODUCT", relative, _floats(across)),
                       build.vector("DOT_PRODUCT", relative, _floats(bend.ray)))
    gamma = build.math("MINIMUM", build.math("MAXIMUM", gamma, 0.0), bend.alpha)
    back = turn(flat, _floats(bend.spin), build.math("MULTIPLY", gamma, -1.0))
    share = build.math("MULTIPLY", sign, build.math("MULTIPLY", gamma, build.math("DIVIDE", psi, bend.alpha)))
    return build.blend(current, turn(back, axis, share), build.math("COMPARE", zone, float(index), 0.5))


def _wrap_nodes(build, bend, bit, angle, current, mask, wrap):
    """foldmath.wrapped as nodes: each point at its own gamma (the `kls_fold_wrap` attribute)."""
    def turn(point, axis, amount):
        node = build.tree.nodes.new("ShaderNodeVectorRotate")
        node.rotation_type = "AXIS_ANGLE"
        build.tree.links.new(point, node.inputs["Vector"])
        node.inputs["Center"].default_value = _floats(bend.pivot)
        build._feed(node.inputs["Axis"], axis)
        build._feed(node.inputs["Angle"], amount)
        return node.outputs["Vector"]

    towards = build.math("SUBTRACT", 1.0, build.math("MULTIPLY", 2.0, build.math("LESS_THAN", angle, 0.0)))  # +-1
    psi = build.math("ADD", bend.alpha, build.math("ABSOLUTE", angle))
    sine = build.math("MINIMUM", build.math("DIVIDE", bend.alpha, psi), 1.0)
    cosine = build.math("SQRT", build.math("MAXIMUM", build.math("SUBTRACT", 1.0, build.math("MULTIPLY", sine, sine)),
                                           0.0))
    up = build.tree.nodes.new("ShaderNodeCombineXYZ")
    build._feed(up.inputs["Z"], build.math("MULTIPLY", towards, sine))
    axis = build.vector("NORMALIZE", build.vector("ADD", build.vector("SCALE", _floats(bend.ray), scale=cosine),
                                                  up.outputs[0]))
    sign = build.math("MULTIPLY", towards, 1.0 if bend.spin[2] > 0 else -1.0)
    flat = build.math("LESS_THAN", build.math("ABSOLUTE", angle), foldmath.MIN_ANGLE)  # 1: leave it
    round_ = build.math("MULTIPLY", wrap, build.math("DIVIDE", psi, bend.alpha))
    past = build.math("ADD", build.math("MINIMUM", build.math("MAXIMUM", build.math(
        "DIVIDE", build.math("SUBTRACT", round_, 2 * math.pi), foldmath.WRAP_RAMP), 0.0), 1.0),
        build.math("MINIMUM", build.math("MAXIMUM", build.math("DIVIDE", build.math("MULTIPLY", round_, -1.0),
                                                                foldmath.WRAP_RAMP), 0.0), 1.0))
    under = build.tree.nodes.new("ShaderNodeCombineXYZ")
    build._feed(under.inputs["Z"], build.math("MULTIPLY", past, -bend.width))
    moved = build.vector("ADD", current, under.outputs[0])
    turned = turn(turn(moved, _floats(bend.spin), build.math("MULTIPLY", wrap, -1.0)), axis,
                  build.math("MULTIPLY", sign, round_))
    hangs = build.math("FLOORED_MODULO", build.math("FLOOR", build.math("DIVIDE", mask, float(2 ** bit))), 2.0)
    return build.blend(current, turned, build.math("MULTIPLY", hangs, build.math("SUBTRACT", 1.0, flat)))


def angle_degrees(index):
    """A bend's angle now, in degrees (the panel's readout): its handle's."""
    grips = _grips()
    return math.degrees(grips[index].rotation_euler.z) if index < len(grips) else 0.0
