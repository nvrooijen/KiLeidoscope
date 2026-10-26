"""Reflowed solder: each stencil aperture's solder drawn as it flows up the metal above it.

One rule for every SMD package: the solder on a pad is a height map. A grid of points
over the aperture (KiCad's F.Paste/B.Paste pad shape) looks straight out from the copper:

- metal within MAX_GAP_M: the point is under a lead or termination; solder fills up to it.
- otherwise the height falls off with the flat distance r to that metal and d to the
  aperture's edge, h = climb * (d / (r + d))^2: `climb` against the lead, 0 at the edge,
  and never higher than the metal it climbs (what is left over bulges on the pad).

`climb` is set per aperture so the fillet holds the solder the stencil prints there:
aperture area x stencil thickness x SOLDER_FRACTION. An aperture with no known metal over
it (no part, an excluded DNP part, a model whose metal is not marked) gets a dome holding
the same solder. Apertures alike under the same model, placed alike, share one solder.

A plated hole with a pin in it gets a through-hole joint from the board instead (no paste):
the barrel filled, a concave fillet up the pin on the solder side, a thinner one on the
part's side (`build_through_hole`).

What counts as metal, best first:
- a parts-pack object marked `kls_role` "contacts" (any other role: none of it);
- faces of a KiCad library model in one of the library's metal colours. kicad-cli's GLB
  keeps a model's colours but neither its material names (they come in as mat_0, mat_1,
  ...) nor a metal flag (every material imports as metallic): measured 2026-10-19 on
  KiCad 10.0.6 exports in Blender 4.5 and 5.2. Vendor models use their own colours, so
  their metal is not known.
"""

import contextlib
import time

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import delaunay_2d_cdt

from . import components, fold, models, transform
from .objects import owned_name, owned_object, read_attribute, set_visible
from .placement import stencil_thickness
from .state import board, boards, each_loaded, using

# The KiCad library's metal materials (VRML names; sRGB as its .wrl files give them).
METAL_SRGB = (
    (0.824, 0.820, 0.781),  # PIN-01: tinned leads, terminations and pins
    (0.859, 0.738, 0.496),  # PIN-02: gold
    (0.298, 0.298, 0.298),  # MET-01
    (0.508, 0.508, 0.508),  # MET-SILVER
    (0.704, 0.270, 0.083),  # MET-COPPER
    (0.372, 0.372, 0.373),  # MET-ALUMINUM
    (0.714, 0.428, 0.181),  # MET-BRONZE
)
COLOUR_TOLERANCE = 0.004  # sRGB: the GLB's colours match the library's to 3 decimals
CONTACTS = "contacts"  # a parts pack's terminations and leads (kls_role)
MAX_GAP_M = 0.3e-3  # metal further out than this is not wetted (a toroid's winding, a shield can)
RAY_START_M = 20e-6  # rays start this far under the paste's plane (3 um over the copper), inside the
                     # copper: a lead seated on the copper is hit from below, not from inside
TARGET_POINTS = 400  # grid points per aperture: spacing from MIN_GRID_M to GRID_M, coarser past
GRID_M = 0.05e-3     # MAX_POINTS_FACTOR times the target (a large pad)
MIN_GRID_M = 0.025e-3
MAX_POINTS_FACTOR = 4
POINT_BUDGET = 400_000  # a board with many apertures gets fewer points each, down to MIN_TARGET_POINTS
MIN_TARGET_POINTS = 48
TARGET_STEP = 8  # the target in steps: a pad more or less seldom changes it
KEY_RESOLUTION_M = 1e-7  # apertures and parts placed alike to this share one solder
ROW_CLEARANCE = 0.9  # grid points nearer the aperture's edge than this many spacings give way to a
                     # row of points along the edge, half a spacing in
CAP_REACH = 3  # a fillet climbs as high as the metal this many spacings past its nearest point
SMOOTH_ROUNDS = 6  # of the finished surface
TOP_INSET_M = 20e-6  # under a lead, the surface stays this far under the metal's top (hidden)
SOLDER_FRACTION = 0.5  # of the printed paste's volume, solder after reflow (SAC paste: ~88 % metal
                       # by weight, about half by volume; the flux leaves)
MAX_CLIMB_M = 0.6e-3
MAX_DOME_M = 0.35e-3
SETTLE_S = 0.4  # after a change, before the solder is built again
NAME = "KLS {layer} solder reflowed"
THT_NAME = "KLS THT solder reflowed"
THT_ANGLES = 48  # profiles around a through-hole joint
THT_STEPS = 10  # points along each of its fillets
SOLDER_SIDE_CLIMB = 0.9  # up the pin, as a share of the annular ring's width: a concave fillet
PART_SIDE_CLIMB = 0.35  # through the barrel to the part's side: a thinner one
MAX_THT_CLIMB_M = 1.0e-3
PIN_HITS = 0.75  # of the rays across a hole that must meet metal for a pin to be in it
PIN_SLACK_M = 0.05e-3  # past the hole's wall, a ray may still meet the pin (a model's pin a bit large)
PIN_REACH_M = 10e-3  # how far along a pin its end is looked for
PIN_INSET_M = 20e-6
WALL_GAP_M = 10e-6  # the barrel's solder stops this far inside the drill (the plated wall's face)
WET_FILM_M = 15e-6  # solder on a through-hole pad's corners, past the round fillet

THT = "THT"  # the through-hole joints' entry in `_s().sides`
MIN_FILLET_M = 0.05e-3  # a through-hole fillet starts at least this far out from the pin

# Keyed by Blender pointers (and names): `forget` drops them whenever Blender may have
# freed and reused those addresses (a file loaded, an undo, new model meshes imported).
_trees = {}  # (mesh pointer and name, sizes, metal faces) -> BVHTree of a model part's metal, or None
_waiting = set()  # boards (slots) whose build the `_settled` timer runs


class _Builds:
    """One board's solder builds (`board.solder`, made by `_s`)."""

    def __init__(self):
        self.built = {}  # side (outward) -> {what an aperture's solder depends on: its solder}, from the last build
        self.known = {}  # model part (pointer, name) -> (what it was when last looked at, its `_parts` entry or None)
        self.moves = []  # (generation, box min, box max): where parts changed, appeared or went
        self.generation = 0
        self.sides = {}  # side (outward) or THT -> its last build: per pad its solder, and the mesh's layout
        self.stats = {}  # side ("F", "B") or THT -> its last build's numbers; "latest": which built last
        self.due = None  # when a change asked for a build (time.monotonic()), or None


def _s():
    """The board's solder builds."""
    if board.solder is None:
        board.solder = _Builds()
    return board.solder


def reflowed():
    scene = bpy.context.scene
    return bool(getattr(scene, "kileido_show_solder", False) and getattr(scene, "kileido_solder_reflowed", True))


def show():
    """A switch or the stencil thickness changed: reflowed solder switched off hides at
    once; shown, it is built once changes settle (dragging the thickness builds once)."""
    if board.collection is not None and not reflowed():
        for obj in tuple(board.collection.all_objects):
            if obj.get("kls_reflowed_side"):
                set_visible(obj, False)
    invalidate()


def invalidate():
    """Something the solder rests on changed (paste, parts, stencil): build it again once
    changes settle, if reflowed solder is shown. Off, nothing is built: switched on, parts
    and pads are compared with the last build then.

    Blender runs no timers in background mode, nor while a script runs: in background mode
    the solder is built at once (after a snapshot, at its end: `flush`), and a script that
    keeps Blender busy pumps it (studio.pump)."""
    if not reflowed():
        return
    _s().due = time.monotonic() + SETTLE_S
    if bpy.app.background:
        if not board.in_snapshot:
            flush()
        return
    _waiting.add(board.slot)
    if bpy.app.timers.is_registered(_settled):
        bpy.app.timers.unregister(_settled)
    bpy.app.timers.register(_settled, first_interval=SETTLE_S)


def pending():
    """A build was asked for, on some board, and has not run yet."""
    return any(state.solder is not None and state.solder.due is not None for state in boards.values())


def flush(now=True):
    """Run the builds changes asked for, on every board: now, or (`now` False) once due."""
    for slot, state in boards.items():
        due = state.solder.due if state.solder is not None else None
        if due is not None and (now or time.monotonic() >= due):
            with using(slot):
                refresh()


def _settled():
    waiting = sorted(_waiting)
    _waiting.clear()
    for slot in waiting:
        with using(slot):
            if _s().due is not None:  # not already built by `flush`
                try:
                    refresh()
                except ReferenceError:  # the board went away meanwhile
                    pass
    return None


def forget(*_args):
    """Blender freed IDs (a file loaded, an undo or redo, new model meshes imported): drop
    everything kept by their addresses; the next build starts over."""
    _trees.clear()
    for state in boards.values():
        if state.solder is not None:
            due = state.solder.due
            state.solder = _Builds()
            state.solder.due = due


_HANDLERS = ("load_post", "undo_post", "redo_post")


def install():
    for name in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if _on_reload not in handlers:
            handlers.append(_on_reload)


def uninstall():
    for name in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if _on_reload in handlers:
            handlers.remove(_on_reload)
    if bpy.app.timers.is_registered(_settled):
        bpy.app.timers.unregister(_settled)


@bpy.app.handlers.persistent
def _on_reload(*_args):
    forget()
    for _ in each_loaded():
        invalidate()  # an undo may bring back older solder: build it as the board is now


def refresh():
    """Show the paste as printed or reflowed, building the reflowed solder when shown."""
    _s().due = None
    if board.collection is None:
        return
    show = reflowed()
    with _flat(show):
        # one pass over the board's objects for all three: a name lookup in a big board's
        # collection takes milliseconds
        named = {obj.name: obj for obj in board.collection.all_objects}
        parts = _parts(named.values()) if show else []
        for paste in [obj for obj in named.values() if obj.get("kls_paste_side")]:
            name = NAME.format(layer=f"{paste['kls_paste_side']}.Paste")
            obj = named.get(owned_name(name)) or (owned_object(name) if show else None)
            if obj is None:
                continue
            obj["kls_reflowed_side"] = paste["kls_paste_side"]
            if show:
                _build(paste, obj, parts)
            set_visible(obj, show)
        obj = named.get(owned_name(THT_NAME)) or (owned_object(THT_NAME) if show else None)
        if obj is not None:
            obj["kls_reflowed_side"] = THT
            if show:
                _build_through_hole(obj, parts, named)
            set_visible(obj, show)


@contextlib.contextmanager
def _flat(needed=True):
    """While solder is built: the parts on their flat places (a folded board folds again
    after, and bakes its copies again) and their world matrices up to date (once for all)."""
    unfold = needed and fold.folded()
    if unfold:
        fold.unfold_parts()
    if needed:
        bpy.context.view_layer.update()  # parts placed since the last redraw
    try:
        yield
    finally:
        if unfold:
            fold.refold_parts()
            fold.invalidate()


def build(paste, obj):
    """The reflowed solder of one side's apertures (`paste`, the printed paste) into `obj`."""
    with _flat():
        _build(paste, obj, _parts())


def build_through_hole(obj):
    """The through-hole joints into `obj` (`_build_through_hole`)."""
    with _flat():
        _build_through_hole(obj, _parts(), {obj.name: obj for obj in board.collection.all_objects})


def _build(paste, obj, parts):
    """One side's apertures. A live edit (a footprint moved: its part and its pads'
    outlines) builds again only the apertures whose outline changed or that a changed part's
    box reaches; the others keep their solder, and the mesh keeps its faces when they are the
    same."""
    started = time.perf_counter()
    outward = 1.0 if paste["kls_paste_side"] == "F" else -1.0
    obj.location = paste.location
    obj.rotation_euler = paste.rotation_euler
    matrix = np.array(paste.matrix_world, dtype=np.float64)
    lows = np.array([part[3] for part in parts]).reshape(-1, 3)
    highs = np.array([part[4] for part in parts]).reshape(-1, 3)
    apertures = _apertures(paste)
    target = int(np.clip(POINT_BUDGET / max(len(apertures), 1), MIN_TARGET_POINTS, TARGET_POINTS))
    target -= target % TARGET_STEP  # a pad more or less rarely changes every aperture's grid
    volume_per_area = stencil_thickness() * SOLDER_FRACTION
    settings = (outward, volume_per_area, target)
    last = _s().sides.get(outward)
    moved = _moved(outward)
    if last is not None and last["settings"] != (settings, matrix.round(9).tobytes()):
        last = None  # thickness or stencil changed: every aperture
    outlines = [b"".join(points.tobytes() + bytes([hole]) for points, hole in rings) for _, rings in apertures]
    previous = [last["entries"].get(pad_id) if last is not None else None for pad_id, _ in apertures]
    keep = _unchanged(previous, outlines, moved)
    cache, built = _s().built.get(outward, {}), {}
    entries = {}
    computed = revisited = 0
    for (pad_id, rings), outline, entry, kept_as_is in zip(apertures, outlines, previous, keep):
        if kept_as_is:
            built.setdefault(entry["key"], entry["kept"])
            entries[pad_id] = entry
            continue
        revisited += 1
        low, high = _column(rings, outward, matrix)
        over = [parts[index] for index in np.flatnonzero(np.all(highs >= low, axis=1) & np.all(lows <= high, axis=1))]
        # An aperture's solder depends on its shape and the metal over it, both as seen
        # from one of those parts: every 0402 under the same model shares one solder.
        frame = _frame(rings, matrix, over)
        key = _key(rings, frame, over, settings)
        kept = built[key] if key in built else cache.get(key)
        if kept is None and key not in built:
            computed += 1
            kept = _aperture(rings, outward, matrix, over, volume_per_area, target)
            if kept is not None:
                kept = (kept[0] @ frame[:3, :3].T + frame[:3, 3], *kept[1:])
        built[key] = kept
        back = np.linalg.inv(frame)
        entries[pad_id] = {"outline": outline, "low": low, "high": high, "key": key, "kept": kept,
                           "vertices": None if kept is None else kept[0] @ back[:3, :3].T + back[:3, 3]}
    _s().built[outward] = built
    layout = _place(obj.data, last, entries)
    _s().sides[outward] = {"settings": (settings, matrix.round(9).tobytes()), "entries": entries,
                       "generation": _s().generation, **layout}
    _use_solder_material(obj)
    obj["kls_stencil_m"] = stencil_thickness()  # what it was built for (packages.refresh_thickness)
    obj.scale = (1.0, 1.0, 1.0)
    shown = [entry["kept"] for entry in entries.values() if entry["kept"] is not None]
    _note(paste["kls_paste_side"], ms=(time.perf_counter() - started) * 1000, apertures=len(shown),
          wetted=sum(kept[3] for kept in shown), points=sum(kept[4] for kept in shown),
          computed=computed, revisited=revisited, rewritten=layout["rewritten"])


def _unchanged(previous, outlines, moved):
    """Per aperture: its last solder still holds (same outline, no changed part's box
    reaching its column). `previous`: its last entry or None."""
    keep = np.array([entry is not None and entry["outline"] == outline for entry, outline in zip(previous, outlines)],
                    dtype=bool)
    if keep.any() and len(moved):
        index = np.flatnonzero(keep)
        keep[index[_reached(np.array([previous[i]["low"] for i in index]),
                            np.array([previous[i]["high"] for i in index]), moved)]] = False
    return keep


def _reached(lows, highs, moved):
    """Per box lows..highs (n, 3): whether one of the boxes `moved` (k, 2, 3) reaches into it."""
    reached = np.zeros(len(lows), dtype=bool)
    for box_low, box_high in moved:  # a few boxes per edit
        reached |= np.all(highs >= box_low, axis=1) & np.all(lows <= box_high, axis=1)
    return reached


def _use_solder_material(obj):
    if not obj.data.materials or obj.data.materials[0] != board.materials["solder_reflowed"]:
        obj.data.materials.clear()
        obj.data.materials.append(board.materials["solder_reflowed"])


def _place(mesh, last, entries):
    """Write the solder of `entries` (pad id -> its solder) into `mesh`: only the points'
    positions when the faces are those of the last build (`last`), else everything.
    Returns what the next build compares against."""
    order = [pad_id for pad_id, entry in entries.items() if entry["kept"] is not None]
    faces = [entries[pad_id]["kept"][1] for pad_id in order]
    sizes = [len(entries[pad_id]["vertices"]) for pad_id in order]
    # the mesh as the last build left it (an undo can bring back another: see `forget`), and
    # every pad with as many points and the same faces
    same = (last is not None and last["order"] == order and last["sizes"] == sizes and
            len(mesh.vertices) == last["count"] and len(mesh.polygons) == last["polygons"] and
            all(a is b or (a.shape == b.shape and np.array_equal(a, b)) for a, b in zip(faces, last["faces"])))
    if same:
        co = last["co"]
        for pad_id in order:
            entry = entries[pad_id]
            if entry is not last["entries"].get(pad_id):
                start = last["starts"][pad_id]
                co[start:start + len(entry["vertices"])] = entry["vertices"]
        mesh.vertices.foreach_set("co", co.astype(np.float32).ravel())
        mesh.update()
        return {"order": order, "faces": faces, "sizes": sizes, "co": co, "starts": last["starts"], "count": len(co),
                "polygons": len(mesh.polygons), "rewritten": False}
    vertices = [entries[pad_id]["vertices"] for pad_id in order]
    starts, count = {}, 0
    triangles = []
    for pad_id, verts, tris in zip(order, vertices, faces):
        starts[pad_id] = count
        triangles.append(tris + count)
        count += len(verts)
    _write(mesh, vertices, triangles, [entries[pad_id]["kept"][2] for pad_id in order])
    co = np.vstack(vertices) if vertices else np.empty((0, 3))
    return {"order": order, "faces": faces, "sizes": sizes, "co": co, "starts": starts, "count": len(co),
            "polygons": len(mesh.polygons), "rewritten": True}


def _column(rings, outward, matrix):
    """The world box over an aperture, as far out as solder could climb."""
    every = np.concatenate([points for points, _ in rings])
    low, high = every.min(axis=0), every.max(axis=0)
    corners = np.array([(x, y, z, 1.0) for x in (low[0], high[0]) for y in (low[1], high[1])
                        for z in (-outward * RAY_START_M, outward * (MAX_GAP_M + MAX_CLIMB_M))])
    world = (corners @ matrix.T)[:, :3]
    return world.min(axis=0), world.max(axis=0)


def _frame(rings, matrix, over):
    """From the paste's coordinates into those the aperture's solder is kept in: one of the
    parts over it (the same model's part in every copy), else the aperture's own centre."""
    if over:
        first = min(over, key=lambda part: (part[5][0], part[5][1]))
        return first[2] @ matrix
    every = np.concatenate([points for points, _ in rings])
    frame = np.eye(4)
    frame[:2, 3] = -every.mean(axis=0)
    return frame


def _key(rings, frame, over, settings):
    """What an aperture's solder depends on, in its frame (`_frame`): the rings, the parts
    over it, where they are, and the settings."""
    shape = b"".join(np.round((np.column_stack((points, np.zeros(len(points)))) @ frame[:3, :3].T + frame[:3, 3])
                              / KEY_RESOLUTION_M).astype(np.int64).tobytes() + bytes([hole]) for points, hole in rings)
    if over:
        first = min(over, key=lambda part: (part[5][0], part[5][1]))
        placed = tuple(sorted((part[5][0], np.round((first[2] @ part[1]) / KEY_RESOLUTION_M).astype(np.int64)
                               .tobytes()) for part in over))
    else:
        placed = ()
    return shape, placed, settings


def last_build(side=None):
    """A build's numbers: one side's ("F", "B": time in ms, apertures, those with metal over
    them, grid points, how many were worked out or revisited, whether the mesh was written
    anew) or the through-hole joints' (THT); by default whichever was built last."""
    return dict(_s().stats.get(side or _s().stats.get("latest"), {}))


def _note(side, **numbers):
    _s().stats[side] = numbers
    _s().stats["latest"] = side


# --- Metal --------------------------------------------------------------------------------

def _to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _base_colour(material):
    if material is None:
        return None
    if not material.use_nodes or material.node_tree is None:
        return tuple(material.diffuse_color[:3])
    principled = next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if principled is None or principled.inputs["Base Color"].is_linked:  # a texture: colour unknown
        return None
    return tuple(principled.inputs["Base Color"].default_value[:3])


def is_metal(material):
    """True for a material in one of the KiCad library's metal colours."""
    colour = _base_colour(material)
    if colour is None:
        return False
    srgb = [_to_srgb(c) for c in colour]
    return any(all(abs(a - b) <= COLOUR_TOLERANCE for a, b in zip(srgb, metal)) for metal in METAL_SRGB)


def _metal_tree(obj):
    """A BVH of one model part's metal faces, in its mesh's own coordinates (None without
    any), and its cache key. Parts sharing a mesh and materials share it."""
    role = obj.get("kls_role")
    if role is not None and role != CONTACTS:
        return None, None
    mesh = obj.data
    flags = (True,) if role == CONTACTS else tuple(is_metal(slot.material) for slot in obj.material_slots)
    key = (mesh.as_pointer(), mesh.name, len(mesh.vertices), len(mesh.polygons), flags)
    if key in _trees:
        return _trees[key], key
    tree = None
    if any(flags):
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        corners = np.empty(count * 3, np.int32)
        mesh.loop_triangles.foreach_get("vertices", corners)
        corners = corners.reshape(-1, 3)
        if role != CONTACTS:
            slots = np.empty(count, np.int32)
            mesh.loop_triangles.foreach_get("material_index", slots)
            metal = np.array(flags, bool)
            corners = corners[metal[np.clip(slots, 0, len(metal) - 1)]] if len(metal) else corners[:0]
        if len(corners):
            co = np.empty(len(mesh.vertices) * 3, np.float64)
            mesh.vertices.foreach_get("co", co)
            tree = BVHTree.FromPolygons(co.reshape(-1, 3).tolist(), corners.tolist(), all_triangles=True)
    _trees[key] = tree
    return tree, key


def _parts(objects=None):
    """Model parts on the board (shown by KiCad, not an excluded DNP part) that have metal:
    (tree, world matrix, its inverse, world box min, max, what an aperture's solder depends
    on: the tree and where it is, the board side the part is on). `objects`: the board's,
    if already at hand.

    Each part is looked at again only when it changed (moved, shown or hidden, other mesh or
    materials); the boxes where parts changed, appeared or went are logged in `_s().moves`."""
    found, changed, seen = [], [], set()
    # Bound now: a part left behind (its footprint gone, its model changed, waiting for the
    # next export) stays in the scene, hidden, and must not hold solder meanwhile
    bound = {name for names in board.model_objects_by_fp.values() for name in names}
    for obj in tuple(board.collection.all_objects) if objects is None else objects:
        footprint_id = obj.get("kls_model_fp_id")
        if footprint_id is None or obj.type != "MESH":
            continue
        pointer = (obj.as_pointer(), obj.name)
        seen.add(pointer)
        frame = obj.parent  # its footprint (components.attach), else looked up
        if frame is None or frame.get("kls_footprint") != 1:
            frame = components.find(components.FRAME, footprint_id)
        matrix = np.array(obj.matrix_world, dtype=np.float64)
        now = (obj.name in bound and models.model_is_visible(frame, obj), obj.data.as_pointer(), matrix.tobytes(),
               tuple(slot.material.as_pointer() if slot.material else 0 for slot in obj.material_slots),
               obj.get("kls_role"))
        known = _s().known.get(pointer)
        if known is not None and known[0] == now:
            if known[1] is not None:
                found.append(known[1])
            continue
        part = None
        if now[0]:
            tree, key = _metal_tree(obj)
            if tree is not None:
                corners = np.array([tuple(corner) for corner in obj.bound_box], dtype=np.float64)
                world = corners @ matrix[:3, :3].T + matrix[:3, 3]
                part = (tree, matrix, np.linalg.inv(matrix), world.min(axis=0), world.max(axis=0),
                        (key, matrix.round(9).tobytes()), obj.get("kls_side", "top"))
        changed += [(entry[3], entry[4]) for entry in (known[1] if known else None, part) if entry is not None]
        _s().known[pointer] = (now, part)
        if part is not None:
            found.append(part)
    for pointer in [pointer for pointer in _s().known if pointer not in seen]:
        gone = _s().known.pop(pointer)[1]
        if gone is not None:
            changed.append((gone[3], gone[4]))
    if changed:
        _s().generation += 1
        _s().moves.extend((_s().generation, low, high) for low, high in changed)
    return found


def _moved(consumer):
    """The boxes (k, 2, 3) where parts changed since `consumer` (a side, or "THT") last
    built; log entries every consumer has seen are dropped."""
    since = _s().sides[consumer]["generation"] if consumer in _s().sides else _s().generation
    boxes = [(low, high) for generation, low, high in _s().moves if generation > since]
    oldest = min((side["generation"] for side in _s().sides.values()), default=_s().generation)
    _s().moves[:] = [move for move in _s().moves if move[0] > min(oldest, since)]
    return np.array(boxes, dtype=np.float64).reshape(-1, 2, 3)


# --- Apertures ------------------------------------------------------------------------------

def _rings(obj):
    """The rings of a contour object (paste or copper pads: apply._ring_edges, edge i runs
    from point i to the next point of its ring): [(pad id, points (n, 2) in metres, is_hole)]."""
    mesh = obj.data
    count = len(mesh.vertices)
    if not count or mesh.attributes.get("item") is None:
        return []
    co = np.empty(count * 3, np.float64)
    mesh.vertices.foreach_get("co", co)
    xy = co.reshape(-1, 3)[:, :2]
    edges = np.empty(len(mesh.edges) * 2, np.int64)
    mesh.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)
    following = np.arange(count) + 1
    following[edges[:, 0]] = edges[:, 1]
    ends = np.flatnonzero(following != np.arange(count) + 1)
    starts = np.concatenate(([0], ends[:-1] + 1))
    items = read_attribute(mesh, "item", np.int32)
    holes = read_attribute(mesh, "hole", np.int32)
    ids = list(obj.get("kls_ids", ()))
    return [(ids[items[start]] if items[start] < len(ids) else int(items[start]), xy[start:end + 1], bool(holes[start]))
            for start, end in zip(starts, ends) if end - start >= 2]


def _apertures(paste):
    """Each aperture's pad id and rings, [(pad id, [(points (n, 2) in metres, is_hole)])]."""
    apertures = {}
    for pad_id, points, hole in _rings(paste):
        apertures.setdefault(pad_id, []).append((points, hole))
    return list(apertures.items())


def _segments(rings):
    starts = np.concatenate([points for points, _ in rings])
    ends = np.concatenate([np.roll(points, -1, axis=0) for points, _ in rings])
    return starts, ends


def _inside(points, starts, ends):
    """Even-odd: inside the aperture (outer rings less holes)."""
    x, y = points[:, 0:1], points[:, 1:2]
    ax, ay, bx, by = starts[:, 0], starts[:, 1], ends[:, 0], ends[:, 1]
    crosses = (ay > y) != (by > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        at = ax + (y - ay) * (bx - ax) / (by - ay)
    return np.count_nonzero(crosses & (x < at), axis=1) % 2 == 1


def _edge_distance(points, starts, ends):
    """Distance from each point to the aperture's edge."""
    d = ends - starts
    length2 = np.maximum((d * d).sum(axis=1), 1e-30)
    rel = points[:, None, :] - starts[None, :, :]
    t = np.clip((rel * d[None]).sum(axis=2) / length2, 0.0, 1.0)
    nearest = starts[None] + t[..., None] * d[None]
    return np.sqrt(((points[:, None, :] - nearest) ** 2).sum(axis=2)).min(axis=1)


def _area(points):
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _grid(rings, area, starts, ends, target):
    """Points on a square grid inside the aperture, clear of its edge; and their spacing."""
    every = np.concatenate([points for points, _ in rings])
    low, high = every.min(axis=0), every.max(axis=0)
    coarsest = GRID_M * (TARGET_POINTS / target) ** 0.5  # coarser too on a board over the budget
    spacing = max(min(coarsest, max((area / target) ** 0.5, MIN_GRID_M)), (area / (MAX_POINTS_FACTOR * target)) ** 0.5)
    xs = np.arange(low[0] + spacing / 2, high[0], spacing)
    ys = np.arange(low[1] + spacing / 2, high[1], spacing)
    if not len(xs) or not len(ys):
        return np.empty((0, 2)), np.empty(0), spacing
    grid = np.stack(np.meshgrid(xs, ys), axis=-1).reshape(-1, 2)
    grid = grid[_inside(grid, starts, ends)]
    edge = _edge_distance(grid, starts, ends) if len(grid) else np.empty(0)
    keep = edge > ROW_CLEARANCE * spacing
    grid, edge = grid[keep], edge[keep]
    # A row along the edge, half a spacing in, where the grid is left out: the square grid's
    # first row lies at uneven distances from a rounded or slanted edge, and the rim rippled
    rows = []
    for points, hole in rings:
        dense = _dense(points, spacing)
        side = np.roll(dense, -1, axis=0) - dense
        normal = np.column_stack((-side[:, 1], side[:, 0]))  # left of each side
        normal /= np.maximum(np.hypot(normal[:, 0], normal[:, 1]), 1e-30)[:, None]
        normal = normal + np.roll(normal, 1, axis=0)  # at each point: its two sides' average
        normal /= np.maximum(np.hypot(normal[:, 0], normal[:, 1]), 1e-30)[:, None]
        inward = 1.0 if (_area(points) > 0) != hole else -1.0
        rows.append(dense + inward * normal * (0.5 * spacing))
    row = np.concatenate(rows)
    row_edge = _edge_distance(row, starts, ends)
    good = _inside(row, starts, ends) & (row_edge > 0.3 * spacing) & (row_edge < 0.7 * spacing)
    if len(grid):
        good &= np.sqrt(((row[:, None, :] - grid[None, :, :]) ** 2).sum(axis=2)).min(axis=1) > 0.3 * spacing
    return np.concatenate((grid, row[good])), np.concatenate((edge, row_edge[good])), spacing


def _metal(grid, outward, matrix, parts):
    """Per point, how far out from the copper the nearest metal is (inf: none within
    MAX_GAP_M), and how high that metal reaches (its top, looking back from MAX_CLIMB_M
    further out): solder climbs a lead no higher than the lead. `parts`: those over the
    aperture (`_over`)."""
    gaps = np.full(len(grid), np.inf)
    tops = np.zeros(len(grid))
    if not len(grid) or not parts:
        return gaps, tops
    local = np.column_stack((grid, np.full(len(grid), -outward * RAY_START_M), np.ones(len(grid))))
    origins = (local @ matrix.T)[:, :3]
    direction = matrix[:3, :3] @ np.array((0.0, 0.0, outward))
    direction /= np.linalg.norm(direction)
    reach = RAY_START_M + MAX_GAP_M
    above = reach + MAX_CLIMB_M
    for tree, part_matrix, inverse, *_ in parts:
        part_origins = origins @ inverse[:3, :3].T + inverse[:3, 3]
        part_direction = inverse[:3, :3] @ direction
        scale = np.linalg.norm(part_direction)
        part_direction /= scale
        up, down = Vector(part_direction), Vector(-part_direction)
        cast = tree.ray_cast
        hits = [(index, cast(Vector(origin), up, reach * scale)[0])
                for index, origin in enumerate(part_origins.tolist())]
        hits = [(index, tuple(hit)) for index, hit in hits if hit is not None]
        if not hits:
            continue
        index = np.array([row[0] for row in hits])
        world = np.array([row[1] for row in hits]) @ part_matrix[:3, :3].T + part_matrix[:3, 3]
        gap = np.maximum((world - origins[index]) @ direction - RAY_START_M, 0.0)
        nearer = gap < gaps[index]
        index, gap = index[nearer], gap[nearer]
        gaps[index] = gap
        starts = part_origins[index] + part_direction * (above * scale)
        found = [cast(Vector(start), down, above * scale)[0] for start in starts.tolist()]
        top = gap.copy()
        hit = np.array([value is not None for value in found], bool)
        if hit.any():
            world = np.array([tuple(value) for value in found if value is not None]) @ part_matrix[:3, :3].T + \
                part_matrix[:3, 3]
            top[hit] = np.maximum((world - origins[index[hit]]) @ direction - RAY_START_M, gap[hit])
        tops[index] = top
    return gaps, tops


def _smooth(values, triangles, free, valid, rounds=3):
    """Average each free vertex with its valid neighbours, a few times; the others stay."""
    pairs = np.vstack((triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]))
    pairs = np.vstack((pairs, pairs[:, ::-1]))
    pairs = pairs[valid[pairs[:, 1]]]
    count = np.bincount(pairs[:, 0], minlength=len(values))
    for _ in range(rounds):
        mean = np.bincount(pairs[:, 0], weights=values[pairs[:, 1]], minlength=len(values)) / np.maximum(count, 1)
        values = np.where(free & (count > 0), 0.5 * values + 0.5 * mean, values)
    return values


def _dense(points, spacing):
    """A ring with points added along its sides, no further apart than `spacing`: a straight
    pad side has only its corners, and slivers from them to the grid ripple the rim."""
    following = np.roll(points, -1, axis=0)
    steps = np.maximum(np.ceil(np.hypot(*(following - points).T) / spacing).astype(int), 1)
    return np.concatenate([a + (b - a) * (np.arange(n)[:, None] / n) for a, b, n in zip(points, following, steps)])


def _triangulate(rings, grid, spacing):
    """Constrained Delaunay over the rings and the grid points: (xy, triangles, grid index
    per vertex or -1 for a ring vertex, the flat bottom's triangles over the ring vertices)."""
    edges, faces, offset = [], [], 0
    rings = [(_dense(points, spacing), hole) for points, hole in rings]
    for points, hole in rings:
        n = len(points)
        edges.extend((offset + i, offset + (i + 1) % n) for i in range(n))
        if not hole:
            faces.append(list(range(offset, offset + n)))
        offset += n
    ring_count = offset
    ring_coords = np.concatenate([points for points, _ in rings])
    coords = np.concatenate((ring_coords, grid.reshape(-1, 2))).tolist()
    out_coords, _, out_faces, orig_verts, _, _ = delaunay_2d_cdt(coords, edges, faces, 1, 1e-9)
    xy = np.array([tuple(v) for v in out_coords], dtype=np.float64).reshape(-1, 2)
    source = np.array([max(orig) if orig else -1 for orig in orig_verts], dtype=np.int64)
    source = np.where(source >= ring_count, source - ring_count, -1)
    triangles = np.array([face for face in out_faces if len(face) == 3], dtype=np.int64).reshape(-1, 3)
    # The bottom needs no grid: the rings alone, on the same vertices
    ring_vertex = np.zeros(ring_count, np.int64)
    for vertex, orig in enumerate(orig_verts):
        for index in orig:
            if index < ring_count:
                ring_vertex[index] = vertex
    _, _, flat_faces, flat_orig, _, _ = delaunay_2d_cdt(ring_coords.tolist(), edges, faces, 1, 1e-9)
    flat_vertex = np.array([ring_vertex[orig[0]] if orig else 0 for orig in flat_orig], dtype=np.int64)
    bottom = flat_vertex[np.array([face for face in flat_faces if len(face) == 3], dtype=np.int64).reshape(-1, 3)]
    holes = [points for points, hole in rings if hole]
    if holes:
        starts, ends = _segments([(points, True) for points in holes])
        if len(triangles):
            triangles = triangles[~_inside(xy[triangles].mean(axis=1), starts, ends)]
        if len(bottom):
            bottom = bottom[~_inside(xy[bottom].mean(axis=1), starts, ends)]
    return xy, triangles, source, bottom


def _aperture(rings, outward, matrix, parts, volume_per_area, target):
    """One aperture's solder: (vertices (n, 3), triangles, smooth flag per triangle, metal
    found, grid points), or None for an empty aperture. `target`: grid points to aim for."""
    area = sum(-abs(_area(points)) if hole else abs(_area(points)) for points, hole in rings)
    if area <= 0:
        return None
    starts, ends = _segments(rings)
    grid, edge, spacing = _grid(rings, area, starts, ends, target)
    gaps, tops = _metal(grid, outward, matrix, parts)
    under = gaps <= MAX_GAP_M
    wetted = bool(under.any())
    shape = np.zeros(len(grid))  # the fillet's shape, 0..1 (the dome's without metal)
    cap = np.full(len(grid), MAX_DOME_M)  # no higher than the metal it climbs
    if wetted:
        free = ~under
        if free.any():
            distance = np.sqrt(((grid[free][:, None, :] - grid[under][None, :, :]) ** 2).sum(axis=2))
            near = distance.min(axis=1)
            reach = np.maximum(near - spacing / 2, 0.0)
            shape[free] = (edge[free] / (reach + edge[free])) ** 2
            # the highest metal close to the nearest: a lead's rounded or chamfered edge
            # does not notch the fillet in front of it
            close = distance <= near[:, None] + CAP_REACH * spacing
            cap[free] = np.minimum(np.where(close, tops[under][None, :], 0.0).max(axis=1), MAX_CLIMB_M)
        shape[under] = 1.0  # at the metal, for smoothing; its height is its gap
        cap[under] = tops[under]
    elif len(grid):
        shape = 1.0 - (1.0 - edge / edge.max()) ** 2
    xy, triangles, source, flat = _triangulate(rings, grid, spacing)
    if not len(triangles):
        return None
    inner = source >= 0
    at = np.maximum(source, 0)

    def per_vertex(values, default):
        return np.where(inner, values[at], default) if len(grid) else np.full(len(xy), default)

    under_v = per_vertex(under, False)
    free_v = inner & ~under_v
    held = per_vertex(np.where(under, gaps, 0.0), 0.0)  # under a lead: solder up to it
    shape_v = np.where(under_v, 0.0, _smooth(per_vertex(shape, 0.0), triangles, free_v, np.ones(len(xy), bool)))
    cap_v = _smooth(per_vertex(cap, 0.0), triangles, free_v, inner)
    corners = xy[triangles]
    signed = 0.5 * ((corners[:, 1, 0] - corners[:, 0, 0]) * (corners[:, 2, 1] - corners[:, 0, 1]) -
                    (corners[:, 2, 0] - corners[:, 0, 0]) * (corners[:, 1, 1] - corners[:, 0, 1]))
    areas = np.abs(signed)

    def volume(heights):
        return float((areas * heights[triangles].mean(axis=1)).sum())

    target = area * volume_per_area
    if wetted:
        def fillet(climb):
            return held + np.minimum(climb * shape_v, cap_v)

        low, high = 0.0, 8 * MAX_CLIMB_M
        if volume(fillet(high)) > target:
            for _ in range(30):
                middle = 0.5 * (low + high)
                low, high = (middle, high) if volume(fillet(middle)) < target else (low, middle)
        height = fillet(high)
        rest = target - volume(height)  # every fillet as high as its metal: the rest bulges on the pad
        if rest > 0 and free_v.any():
            edge_v = per_vertex(edge, 0.0)
            bulge = np.where(free_v, 1.0 - (1.0 - edge_v / max(edge_v[free_v].max(), 1e-12)) ** 2, 0.0)
            per_unit = volume(bulge)
            if per_unit > 0:
                height = height + min(rest / per_unit, MAX_DOME_M) * bulge
    else:
        per_unit = volume(shape_v)
        height = min(target / per_unit, MAX_DOME_M) * shape_v if per_unit > 0 else np.zeros(len(xy))
    # The grid's steps (distances to the nearest metal point) show on a shiny surface:
    # smooth the free surface, then give back the solder smoothing moved.
    free_before = volume(np.where(free_v, height, 0.0))
    smoothed = _smooth(height, triangles, free_v, ~under_v, SMOOTH_ROUNDS)
    free_after = volume(np.where(free_v, smoothed, 0.0))
    scale = free_before / free_after if free_after > 0 else 1.0
    height = np.where(free_v, smoothed * scale, held)
    # Along a lead's edge the surface runs on level into the metal (hidden there) instead of
    # dropping to its underside in a steep wall (the volume counted solder to the underside).
    pairs = np.vstack((triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]))
    pairs = np.vstack((pairs, pairs[:, ::-1]))
    pairs = pairs[under_v[pairs[:, 0]] & free_v[pairs[:, 1]]]
    level = held.copy()
    np.maximum.at(level, pairs[:, 0], height[pairs[:, 1]])
    height = np.where(under_v, np.maximum(held, np.minimum(level, per_vertex(tops, 0.0) - TOP_INSET_M)), height)
    # Top faces turn outward, the flat bottom on the copper (the edge vertices, height 0) inward.
    top = np.where(((signed < 0) != (outward < 0))[:, None], triangles[:, ::-1], triangles)
    corners = xy[flat]
    flat_signed = ((corners[:, 1, 0] - corners[:, 0, 0]) * (corners[:, 2, 1] - corners[:, 0, 1]) -
                   (corners[:, 2, 0] - corners[:, 0, 0]) * (corners[:, 1, 1] - corners[:, 0, 1]))
    bottom = np.where(((flat_signed > 0) == (outward > 0))[:, None], flat[:, ::-1], flat)
    vertices = np.column_stack((xy, outward * height))
    tris = np.vstack((top, bottom))
    smooth = np.concatenate((np.ones(len(top), bool), np.zeros(len(bottom), bool)))
    return vertices, tris, smooth, wetted, len(grid)


# --- Through-hole ---------------------------------------------------------------------------

def _build_through_hole(obj, parts, named):
    """Each plated hole with a part's pin in it, from the board: solder filling the barrel
    between pin and wall, a concave fillet up the pin on the solder side and a thinner one on
    the part's side (wave or hand soldering wicks it through). Into `obj`, in board
    coordinates. A hole without a pin (no part, no model, a mounting hole) stays open. As
    for the apertures (`_build`), an edit builds only the holes it changed or reached.
    `named`: the board's objects by name."""
    started = time.perf_counter()
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    lows = np.array([part[3] for part in parts]).reshape(-1, 3)
    highs = np.array([part[4] for part in parts]).reshape(-1, 3)
    top, bottom = _pad_rings(named.get(owned_name("KLS F.Cu pads"))), _pad_rings(named.get(owned_name("KLS B.Cu pads")))
    z_top = transform.copper_z("F.Cu", "drills", board.heights)
    z_bottom = transform.copper_z("B.Cu", "drills", board.heights)
    last = _s().sides.get(THT)
    moved = _moved(THT)
    if last is not None and last["settings"] != (z_top, z_bottom):
        last = None
    entries = {}
    revisited = 0
    for drill in named.values():
        if not (drill.get("kls_drill") and drill.get("kls_plated") and drill.get("kls_drill_size")
                and len(drill.data.vertices)):
            continue
        pad_id = drill.get("kls_id")
        centre = np.array(drill.location[:2], dtype=np.float64)
        size = tuple(drill["kls_drill_size"])
        rings = (top.get(pad_id), bottom.get(pad_id))
        shape = (centre.tobytes(), size, bool(drill.get("kls_drill_oval", False)), round(drill.rotation_euler.z, 9),
                 *(b"" if ring is None else ring.tobytes() for ring in rings))
        half = 0.5 * max(size)
        low = np.array((centre[0] - half, centre[1] - half, z_bottom - PIN_REACH_M))
        high = np.array((centre[0] + half, centre[1] + half, z_top + PIN_REACH_M))
        entry = last["entries"].get(pad_id) if last is not None else None
        if entry is not None and entry["shape"] == shape and not _reached(entry["low"][None], entry["high"][None], moved)[0]:
            entries[pad_id] = entry
            continue
        revisited += 1
        joint = _hole(drill, centre, rings, z_top, z_bottom, parts, lows, highs)
        entries[pad_id] = {"shape": shape, "low": low, "high": high, "kept": joint,
                           "vertices": None if joint is None else joint[0]}
    layout = _place(obj.data, last, entries)
    _s().sides[THT] = {"settings": (z_top, z_bottom), "entries": entries, "generation": _s().generation, **layout}
    _use_solder_material(obj)
    _note(THT, ms=(time.perf_counter() - started) * 1000,
          joints=sum(entry["kept"] is not None for entry in entries.values()), revisited=revisited,
          rewritten=layout["rewritten"])


def _hole(drill, centre, rings, z_top, z_bottom, parts, lows, highs):
    """One plated hole's joint (vertices, triangles, smooth flags), or None without a pin."""
    angles = np.linspace(0.0, 2 * np.pi, THT_ANGLES, endpoint=False)
    unit = np.column_stack((np.cos(angles), np.sin(angles)))
    wall = _radial(centre, _drill_outline(centre, *drill["kls_drill_size"], drill.rotation_euler.z,
                                          drill.get("kls_drill_oval", False)), unit) - WALL_GAP_M
    if not np.all(np.isfinite(wall)):
        return None
    mid = 0.5 * (z_top + z_bottom)
    column = (centre[0], centre[1], mid)
    near = np.flatnonzero(np.all(highs >= column, axis=1) & np.all(lows <= column, axis=1))
    pin = _pin(np.array(column), unit, wall, [parts[index] for index in near])
    if pin is None:
        return None
    radius, up, down, side = pin
    # the solder's inner wall a little inside the pin: on its face, the two flickered
    radius = np.clip(radius - PIN_INSET_M, 1e-6, wall - 1e-6)
    pads = {"top": _radial(centre, rings[0], unit), "bottom": _radial(centre, rings[1], unit)}
    reach = {"top": up - (z_top - mid), "bottom": down - (mid - z_bottom)}  # pin past each face
    climbs, rounds = {}, {}
    for face, edge in pads.items():
        edge = np.where(np.isfinite(edge) & (edge > wall), edge, wall)
        pads[face] = edge
        # The fillet is round about the pin, out to where the pad is narrowest; a square
        # pad's corners past that are only wetted (a thin film), as on a real joint. Where a
        # blade pin in a slot reaches past that, it starts a little out from the pin instead
        rounds[face] = np.minimum(np.maximum(edge.min(), radius + MIN_FILLET_M), edge)
        width = float(np.median(rounds[face] - radius))
        share = PART_SIDE_CLIMB if face == side else SOLDER_SIDE_CLIMB
        climbs[face] = max(WET_FILM_M, min(share * width, 0.8 * reach[face], MAX_THT_CLIMB_M))
    return _joint(centre, unit, wall, radius, pads, rounds, z_top, z_bottom, climbs)


def _pad_rings(obj):
    """Each pad's outline on one copper layer (`obj`, its pads): {pad id: its largest outer ring}."""
    found = {}
    for pad_id, ring, hole in _rings(obj) if obj is not None else ():
        if not hole and (pad_id not in found or abs(_area(ring)) > abs(_area(found[pad_id]))):
            found[pad_id] = ring
    return found


def _drill_outline(centre, width, height, angle, oval):
    """A drill's outline: a circle, or a slot (two half circles joined)."""
    radius = 0.5 * min(width, height)
    half = 0.5 * (max(width, height) - min(width, height)) if oval else 0.0
    turn = np.linspace(-np.pi / 2, np.pi / 2, 32)
    arc = np.column_stack((np.cos(turn), np.sin(turn))) * radius
    local = np.concatenate((arc + (half, 0.0), -arc + (-half, 0.0)))
    if oval and height > width:
        local = local[:, ::-1] * (1, -1)
    rotate = np.array(((np.cos(angle), -np.sin(angle)), (np.sin(angle), np.cos(angle))))
    return local @ rotate.T + centre


def _radial(centre, ring, unit):
    """How far from `centre` along each direction `unit` the ring's edge is (inf: none)."""
    if ring is None or len(ring) < 3:
        return np.full(len(unit), np.inf)
    a = ring - centre
    b = np.roll(ring, -1, axis=0) - centre
    side = b - a
    cross = unit[:, 0:1] * side[None, :, 1] - unit[:, 1:2] * side[None, :, 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (a[None, :, 0] * side[None, :, 1] - a[None, :, 1] * side[None, :, 0]) / cross
        s = (a[None, :, 0] * unit[:, 1:2] - a[None, :, 1] * unit[:, 0:1]) / cross
    good = (np.abs(cross) > 1e-30) & (t > 0) & (s >= 0) & (s <= 1)
    return np.where(good, t, np.inf).min(axis=1)


def _pin(origin, unit, wall, parts):
    """The pin in a hole: rays across the hole at mid-board, from its centre, meet a pin's
    wall from inside. (radius per direction, how far the pin reaches up and down from
    mid-board, the side its part is on), or None without one (PIN_HITS of the rays)."""
    best = None
    for tree, matrix, inverse, *_, side in parts:
        start = Vector(inverse[:3, :3] @ origin + inverse[:3, 3])
        radius = np.full(len(unit), np.nan)
        for index, (direction, reach) in enumerate(zip(unit, wall)):
            local = inverse[:3, :3] @ np.array((direction[0], direction[1], 0.0))
            scale = np.linalg.norm(local)
            distance = tree.ray_cast(start, Vector(local / scale), (reach + PIN_SLACK_M) * scale)[3]
            if distance is not None:
                radius[index] = distance / scale
        hits = int(np.isfinite(radius).sum())
        if hits >= PIN_HITS * len(unit) and (best is None or hits > best[0]):
            ends = []
            for sign in (1.0, -1.0):
                local = inverse[:3, :3] @ np.array((0.0, 0.0, sign))
                scale = np.linalg.norm(local)
                distance = tree.ray_cast(start, Vector(local / scale), PIN_REACH_M * scale)[3]
                ends.append(distance / scale if distance is not None else PIN_REACH_M)
            radius = np.where(np.isfinite(radius), radius, np.nanmedian(radius))
            best = (hits, radius, ends[0], ends[1], side)
    return None if best is None else best[1:]


def _joint(centre, unit, wall, radius, pads, rounds, z_top, z_bottom, climbs):
    """A through-hole joint as one closed solid: per direction a profile (distance from the
    hole's centre, height) up the barrel's wall, out over the solder-side pad, a thin film
    in to where the fillet starts (`rounds`: the same distance every way the pin allows), the
    fillet up to the pin, along the pin, the part-side fillet and film, back over that pad;
    the profiles joined around the hole."""
    steps = np.arange(THT_STEPS + 1) / THT_STEPS
    count = len(unit)
    columns = [(wall, np.full(count, z_top)), (wall, np.full(count, z_bottom)),
               (pads["bottom"], np.full(count, z_bottom))]
    for k in steps:  # the bottom fillet: from its round edge in to the pin, rising (down)
        columns.append((rounds["bottom"] + (radius - rounds["bottom"]) * k,
                        np.full(count, z_bottom - (WET_FILM_M + (climbs["bottom"] - WET_FILM_M) * k * k))))
    for k in steps:  # the top fillet: from the pin out to its round edge
        columns.append((radius + (rounds["top"] - radius) * k,
                        np.full(count, z_top + WET_FILM_M + (climbs["top"] - WET_FILM_M) * (1 - k) ** 2)))
    columns.append((pads["top"], np.full(count, z_top)))
    distance = np.stack([np.broadcast_to(column[0], (count,)) for column in columns], axis=1)  # (directions, points)
    height = np.stack([column[1] for column in columns], axis=1)
    directions, points = distance.shape
    xy = centre + distance[..., None] * unit[:, None, :]
    vertices = np.concatenate((xy, height[..., None]), axis=2).reshape(-1, 3)
    j, k = np.meshgrid(np.arange(directions), np.arange(points), indexing="ij")
    a = j * points + k
    b = j * points + (k + 1) % points
    c = ((j + 1) % directions) * points + (k + 1) % points
    d = ((j + 1) % directions) * points + k
    triangles = np.concatenate((np.stack((a, b, c), axis=-1).reshape(-1, 3), np.stack((a, c, d), axis=-1).reshape(-1, 3)))
    segment = np.concatenate((k.ravel(), k.ravel()))
    # smooth: the fillets and the films on the pads' corners (flat, their quads facet)
    fillet = (((segment >= 2) & (segment < 3 + THT_STEPS)) |
              ((segment >= 4 + THT_STEPS) & (segment < 5 + 2 * THT_STEPS)))
    corners = vertices[triangles]
    volume = np.einsum("ij,ij->i", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])).sum()
    if volume < 0:  # faces turn out of the solid
        triangles = triangles[:, ::-1]
    return vertices, triangles, fillet


def _write(mesh, vertices, triangles, smooth):
    mesh.clear_geometry()
    if not vertices:
        mesh.update()
        return
    co = np.vstack(vertices).astype(np.float32)
    tris = np.vstack(triangles).astype(np.int32)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.polygons.foreach_set("loop_total", np.full(len(tris), 3, np.int32))
    mesh.polygons.foreach_set("use_smooth", np.concatenate(smooth))
    mesh.update(calc_edges=True)
