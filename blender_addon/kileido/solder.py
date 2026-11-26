"""Reflowed solder: each stencil aperture's solder drawn as it flows up the metal above it.

One rule for every SMD package: the solder on a pad is a height map. A grid of points
over the aperture (KiCad's F.Paste/B.Paste pad shape) looks straight out from the copper:

- metal within MAX_GAP_M: the point is under a lead or termination; solder fills a
  standoff up to it (GAP_FILL_M).
- otherwise one smooth meniscus: h = share * climb * u^2, u the smoothest surface
  (harmonic) from 1 at the metal to 0 where the pad ends far ahead of it (`_far_edge`),
  `climb` how high the metal typically stands along its edge (the median, a little under
  its top): falling off away from it, round its corners too; and up from the pad's edge no
  steeper than MAX_SLOPE.

`share` (0..1) is set per aperture so the fillet holds the solder the stencil prints there
(aperture area x stencil thickness x SOLDER_FRACTION); what it cannot hold goes under the
part (hidden), as the part floats on it. A metal's top is that piece's own (`_pieces`: not a
lid or seal ring over it), not behind the footprint's body (`_Body`), and where it is fused
into the body as high as that piece stands right round it (`_local_top`). An aperture with
no known metal over it (no part, an excluded DNP part, a model whose metal is not marked)
gets a dome holding the same solder. Apertures alike under the same model, placed alike,
share one solder.

A plated hole with a pin in it (anywhere in it) gets a through-hole joint from the board instead (no paste):
the barrel filled, a concave fillet up the pin on the solder side, a thinner one on the
part's side (`build_through_hole`).

What counts as metal, best first:
- a library part's object marked `kls_role` "contacts" (any other role: none of it);
- faces whose material KiCad's palette calls metal (`palette.is_metal`: PIN-01, PIN-02,
  MET-*), by the material's palette tag or else its colour. Vendor models use their own
  colours, so their metal is not known;
- in a numbered pad's plated hole where no such metal is found: any face of that pad's own
  footprint's model (without a role): only its pin goes through there, whatever its colour.
"""

import contextlib
import time
from collections import namedtuple

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import delaunay_2d_cdt

from . import components, fold, models, palette, poses, transform
from .objects import owned_name, owned_object, read_attribute, set_visible
from .placement import stencil_thickness
from .state import board, boards, each_loaded, using

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
SMOOTH_ROUNDS = 6  # of the finished surface
TOP_INSET_M = 5e-6  # the solder stays this far under its metal's top: never level with its face
SOLDER_FRACTION = 0.5  # of the printed paste's volume, solder after reflow (SAC paste: ~88 % metal
                       # by weight, about half by volume; the flux leaves)
MAX_CLIMB_M = 0.6e-3
MAX_DOME_M = 0.35e-3
MAX_SLOPE = 1.0  # a fillet rises from the pad's edge no steeper than this (45 degrees)
HARMONIC_ROUNDS = 200  # conjugate-gradient steps at most, for a meniscus's shape
HARMONIC_TOLERANCE = 1e-3  # done when what is left over is this share of the start
OUTLINE_HALVINGS = 5  # finding where the metal ends between a point under it and a free one
EDGE_ROUNDS = 3  # smoothing the metal's height along its edge (a chamfer, a grazing ray)
GAP_FILL_M = 0.05e-3  # under a lead, solder fills a gap (a standoff) up to this; metal further up stays clear
LOCAL_REACH_M = 0.1e-3  # how far along the metal's edge its typical height is taken
FAR_EDGE = 0.5  # of the farthest edge vertex's distance from the metal: from there on the meniscus ends
SOFT_MIN = 4  # the rounding of a soft minimum (higher: sharper)
WELD = 1e-6  # of a part's size: corners nearer than this are one (a model's faces each have their own)
MAX_LAYERS = 16  # pieces of other metal (a lid, a seal ring) passed coming down to a lead's top
NUDGE_M = 1e-7  # past a face, before a ray goes on
BODY_SLACK_M = 25e-6  # a part's body face this near a lead's face is level with it (a QFN's flush leads)
SAME_FACE_M = 1e-6  # coming down, a face this near where the ray came in is that face
LOCAL_TOP_M = 0.05e-3  # a fused termination's top: the highest of its metal this near
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
FOLLOW_STEPS = 8  # from mid-board to a face, following a slanting pin (a step well under its width)
FACE_PROBE_M = 50e-6  # each face's fillet hugs the pin where it is this far inside the board
FACE_HITS = 0.25  # of the rays from inside a pin near a face (boxed in by it: `_pin_middle`) that must meet it;
                  # the others meet the drill's wall first (a leg pressed into it)
WALL_GAP_M = 10e-6  # the barrel's solder stops this far inside the drill (the plated wall's face)
WET_FILM_M = 15e-6  # solder on a through-hole pad's corners, past the round fillet

THT = "THT"  # the through-hole joints' entry in `_s().sides`
MIN_FILLET_M = 0.05e-3  # a through-hole fillet starts at least this far out from the pin

# Keyed by Blender pointers (and names): `forget` drops them whenever Blender may have
# freed and reused those addresses (a file loaded, an undo, new model meshes imported).
_trees = {}  # (mesh pointer and name, sizes, metal faces) -> (BVHTree of a model part's metal, or None,
             # its pieces, its body or None)
_waiting = set()  # boards (slots) whose build the `_settled` timer runs
_seen_along = {}  # (a piece's triangles, piece, up) -> them seen along `up` (`_local_top`)
_faces = {}  # a model part's mesh, sizes and materials -> its faces that are not metal, or None


class _Builds:
    """One board's solder builds (`board.solder`, made by `_s`)."""

    def __init__(self):
        self.built = {}  # side (outward) -> {what an aperture's solder depends on: its solder}, from the last build
        self.known = {}  # model part (pointer, name) -> (what it was when last looked at, its `_parts` entry or None)
        self.moves = []  # (generation, box min, box max): where parts changed, appeared or went
        self.generation = 0
        self.sides = {}  # side (outward) or THT -> its last build: per pad its solder, and the mesh's layout
        self.stats = {}  # side ("F", "B") or THT -> its last build's numbers; "latest": which built last
        self.bodies = {}  # footprint -> its body (`_Body`), while its objects stay as they are
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
    _seen_along.clear()
    _faces.clear()
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
    matrix = np.array(poses.in_root(paste), dtype=np.float64)  # the board root's space, as the parts
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
        placed = tuple(sorted((part[5][0], part[5][2], np.round((first[2] @ part[1]) / KEY_RESOLUTION_M)
                               .astype(np.int64).tobytes()) for part in over))
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

Pieces = namedtuple("Pieces", "face low high triangles")  # `_pieces`


def _pieces(co, corners):
    """Per triangle, which piece of metal it is part of, numbered from 0 (`face`): triangles
    meeting at a corner are one piece. A model's faces each have their own vertices, so
    corners at the same point count as one (to WELD of the part's size). With each piece's
    box (`low`, `high` corners) and the triangles' corners (`triangles`), in the mesh's
    coordinates."""
    if not len(corners):
        return Pieces(np.zeros(0, np.int64), np.zeros((0, 3)), np.zeros((0, 3)), np.zeros((0, 3, 3)))
    used = np.unique(corners)
    extent = max(float(np.ptp(co[used], axis=0).max()), 1e-12)
    cells = np.round(co[used] / (extent * WELD)).astype(np.int64)
    cells -= cells.min(axis=0)
    _, welded = np.unique(np.ravel_multi_index(cells.T, cells.max(axis=0) + 1), return_inverse=True)
    welded = welded.ravel()[np.searchsorted(used, corners)]
    parent = np.arange(int(welded.max()) + 1)
    a, b = np.concatenate((welded[:, 0], welded[:, 1])), np.concatenate((welded[:, 1], welded[:, 2]))
    while True:  # each piece's corners point at its lowest one
        root_a, root_b = parent[a], parent[b]
        if np.array_equal(root_a, root_b):
            break
        lowest = np.minimum(root_a, root_b)
        np.minimum.at(parent, root_a, lowest)
        np.minimum.at(parent, root_b, lowest)
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    _, piece = np.unique(parent[welded[:, 0]], return_inverse=True)
    piece = piece.ravel()
    points = co[corners]
    lows, highs = np.full((piece.max() + 1, 3), np.inf), np.full((piece.max() + 1, 3), -np.inf)
    np.minimum.at(lows, piece, points.min(axis=1))
    np.maximum.at(highs, piece, points.max(axis=1))
    return Pieces(piece, lows, highs, points)


class _Body:
    """A footprint's faces that are not metal, of all its model's objects (an assembly comes
    as several: "Body", "PinK", ...), in the board root's space: metal behind them is out of
    the solder's reach. Their BVH is built when first cast at. `signature`: what it was made
    from (`_parts`)."""

    def __init__(self, members, signature):
        self._members = members  # [(vertices, faces that are not metal, matrix into the root)]
        self._tree = None
        self.signature = signature

    def ray_cast(self, origin, direction, distance):
        """Where a ray from `origin` along `direction` (board root's space) first meets the body
        within `distance`: BVHTree.ray_cast's (location, normal, index, distance)."""
        if self._tree is None:
            vertices, faces, offset = [], [], 0
            for co, corners, matrix in self._members:
                vertices.append(co @ matrix[:3, :3].T + matrix[:3, 3])
                faces.append(corners + offset)
                offset += len(co)
            self._tree = BVHTree.FromPolygons(np.concatenate(vertices).tolist(), np.concatenate(faces).tolist(),
                                              all_triangles=True)
            self._members = None
        return self._tree.ray_cast(origin, direction, distance)


class _BodyView:
    """A footprint's body (`_Body`) seen from one of its parts: rays in that part's own
    coordinates (`matrix`: from them into the root's)."""

    def __init__(self, body, matrix):
        self.body, self.matrix = body, matrix

    def ray_cast(self, origin, direction, distance):
        along = self.matrix[:3, :3] @ np.array(direction)
        stretch = float(np.linalg.norm(along))
        hit = self.body.ray_cast(Vector(self.matrix[:3, :3] @ np.array(origin) + self.matrix[:3, 3]),
                                 Vector(along / stretch), distance * stretch)
        return hit if hit[0] is None else (hit[0], hit[1], hit[2], hit[3] / stretch)


SHAPED = {"MESH", "CURVES", "CURVE", "SURFACE", "META", "FONT"}  # object types with faces to solder to


def _mesh_arrays(mesh):
    """A mesh's vertices (n, 3), triangles' corners (k, 3) and material per triangle (k,)."""
    mesh.calc_loop_triangles()
    count = len(mesh.loop_triangles)
    corners = np.empty(count * 3, np.int32)
    mesh.loop_triangles.foreach_get("vertices", corners)
    slots = np.empty(count, np.int32)
    mesh.loop_triangles.foreach_get("material_index", slots)
    co = np.empty(len(mesh.vertices) * 3, np.float64)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3), corners.reshape(-1, 3), slots


def _shape(obj):
    """A model part's geometry in its own coordinates: (its materials, a key for what its
    triangles depend on, a function giving them: `_mesh_arrays`), or None. A mesh as it is
    (read only when asked); another object (a library part's leads drawn as Curves by
    Geometry Nodes) as Blender evaluates it, with that geometry's materials."""
    if obj.type == "MESH":
        mesh = obj.data
        return ([slot.material for slot in obj.material_slots],
                (mesh.as_pointer(), mesh.name, len(mesh.vertices), len(mesh.polygons)), lambda: _mesh_arrays(mesh))
    if obj.type not in SHAPED:
        return None
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    # the mesh its modifiers make (Curves: to_mesh refuses them), else Blender's conversion
    geometry = evaluated.evaluated_geometry() if hasattr(evaluated, "evaluated_geometry") else None
    mesh = geometry.mesh if geometry is not None else None  # (alive while `geometry` is)
    if mesh is not None:
        arrays, materials = _mesh_arrays(mesh), list(mesh.materials)
    else:
        try:
            mesh = evaluated.to_mesh()
        except RuntimeError:  # no geometry to make a mesh of
            mesh = None
        try:
            if mesh is None:
                return None
            arrays, materials = _mesh_arrays(mesh), list(mesh.materials)
        finally:
            evaluated.to_mesh_clear()
    key = (obj.data.as_pointer(), obj.data.name, hash(arrays[0].tobytes()), len(arrays[1]))
    return materials, key, lambda: arrays


def _body_faces(obj):
    """One model part's faces that are not metal, in its mesh's own coordinates: (vertices,
    triangles), or None. A parts pack's case is all body, its contacts none."""
    role = obj.get("kls_role")
    if role == CONTACTS:
        return None
    shape = _shape(obj)
    if shape is None:
        return None
    materials, base, arrays = shape
    flags = (False,) if role is not None else tuple(palette.is_metal(material) for material in materials)
    key = (*base, flags)
    if key not in _faces:
        co, corners, slots = arrays()
        if role is None and flags:
            corners = corners[~np.array(flags, bool)[np.clip(slots, 0, len(flags) - 1)]]
        _faces[key] = (co, corners) if len(corners) else None
    return _faces[key]


def _metal_tree(obj, whole=False):
    """A BVH of one model part's metal faces, in its mesh's own coordinates (None without
    any), the piece of metal each of its faces is part of (`_pieces`) and their cache key.
    Parts sharing a mesh and materials share them. `whole`: every face, whatever its colour
    (a numbered pad's own pin, `_own_parts`)."""
    role = obj.get("kls_role")
    if role is not None and role != CONTACTS:
        return None, None, None
    shape = _shape(obj)
    if shape is None:
        return None, None, None
    materials, base, arrays = shape
    every = whole or role == CONTACTS
    flags = (True,) if every else tuple(palette.is_metal(material) for material in materials)
    key = (*base, flags, whole)
    if key in _trees:
        return (*_trees[key], key)
    tree = pieces = None
    if any(flags):
        co, corners, slots = arrays()
        if not every:
            corners = corners[np.array(flags, bool)[np.clip(slots, 0, len(flags) - 1)]]
        if len(corners):
            tree = BVHTree.FromPolygons(co.tolist(), corners.tolist(), all_triangles=True)
            pieces = None if whole else _pieces(co, corners)  # (only the SMD solder uses them)
    _trees[key] = (tree, pieces)
    return tree, pieces, key


def _parts(objects=None):
    """Model parts on the board (shown by KiCad, not an excluded DNP part) that have metal:
    (tree, matrix, its inverse, box min, max, what an aperture's solder depends on: the tree,
    where it is and its footprint's body, its pieces of metal (`_pieces`), its footprint's
    body (`_BodyView`, or None), the board side the part is on), in the board root's space:
    wherever the board stands, its solder is the same. `objects`: the board's, if already at
    hand.

    Each part is looked at again only when it changed (moved, shown or hidden, other mesh or
    materials); the boxes where parts changed, appeared or went are logged in `_s().moves`,
    those of a footprint's parts without metal too (its body)."""
    found, changed, seen = [], [], set()
    members = {}  # footprint -> [(what each shown object of its model is now, the object)]
    # Bound now: a part left behind (its footprint gone, its model changed, waiting for the
    # next export) stays in the scene, hidden, and must not hold solder meanwhile
    bound = {name for names in board.model_objects_by_fp.values() for name in names}
    world_to_root = poses.to_root()
    for obj in tuple(board.collection.all_objects) if objects is None else objects:
        footprint_id = obj.get("kls_model_fp_id")
        if footprint_id is None or obj.type not in SHAPED:
            continue
        pointer = (obj.as_pointer(), obj.name)
        seen.add(pointer)
        frame = obj.parent  # its footprint (components.attach), else looked up
        if frame is None or frame.get("kls_footprint") != 1:
            frame = components.find(components.FRAME, footprint_id)
        matrix = np.array(poses.in_root(obj, world_to_root), dtype=np.float64)
        # a model scaled to nothing holds no solder (and has no inverse)
        visible = (obj.name in bound and models.model_is_visible(frame, obj)
                   and abs(np.linalg.det(matrix[:3, :3])) > 1e-30)
        now = (visible, obj.data.as_pointer(), matrix.tobytes(),
               tuple(slot.material.as_pointer() if slot.material else 0 for slot in obj.material_slots),
               obj.get("kls_role"))
        if now[0]:
            members.setdefault(footprint_id, []).append((now, obj, matrix))
        known = _s().known.get(pointer)
        if known is not None and known[0] == now:
            if known[1] is not None:
                found.append((footprint_id, known[1]))
            continue
        part = box = None
        if now[0]:
            corners = np.array([tuple(corner) for corner in obj.bound_box], dtype=np.float64)
            world = corners @ matrix[:3, :3].T + matrix[:3, 3]
            box = (world.min(axis=0), world.max(axis=0))
            tree, pieces, key = _metal_tree(obj)
            if tree is not None:
                part = (tree, matrix, np.linalg.inv(matrix), box[0], box[1], (key, matrix.round(9).tobytes()), pieces,
                        None, obj.get("kls_side", "top"))
        changed += [entry for entry in (known[2] if known else None, box) if entry is not None]
        _s().known[pointer] = (now, part, box)
        if part is not None:
            found.append((footprint_id, part))
    for pointer in [pointer for pointer in _s().known if pointer not in seen]:
        gone = _s().known.pop(pointer)[2]
        if gone is not None:
            changed.append(gone)
    if changed:
        _s().generation += 1
        _s().moves.extend((_s().generation, low, high) for low, high in changed)
    bodies = {}
    for footprint_id, shown in members.items():
        signature = hash(tuple(now for now, _, _ in shown))
        last = _s().bodies.get(footprint_id)
        if last is None or last.signature != signature:
            made = [(*faces, matrix) for faces, matrix in ((_body_faces(obj), matrix) for _, obj, matrix in shown)
                    if faces is not None]
            last = _Body(made, signature)
            last.empty = not made
            # what an aperture's solder depends on: the body's objects as placed against its
            # first, not where the footprint stands (moved, it keeps its solder)
            first = np.linalg.inv(shown[0][2])
            last.shape = hash(tuple((now[1], now[3], now[4], np.round((first @ matrix) / KEY_RESOLUTION_M)
                                     .astype(np.int64).tobytes()) for now, _, matrix in shown))
            _s().bodies[footprint_id] = last
        bodies[footprint_id] = last
    for footprint_id in [footprint_id for footprint_id in _s().bodies if footprint_id not in members]:
        del _s().bodies[footprint_id]
    parts = []
    for footprint_id, part in found:
        body = bodies.get(footprint_id)
        view = None if body is None or body.empty else _BodyView(body, part[1])
        parts.append((*part[:5], (*part[5][:2], body.shape if body is not None else 0), part[6], view, part[8]))
    return parts


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


def _harmonic(count, triangles, fixed, values, rounds=HARMONIC_ROUNDS):
    """Per vertex of a mesh (`count` of them, `triangles`), `values` where `fixed`, and
    between them the smoothest blend: each other vertex the mean of its neighbours (a
    membrane pinned at the fixed ones), solved by conjugate gradients. A stretch reaching no
    fixed vertex stays 0."""
    pairs = np.sort(np.vstack((triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]])), axis=1)
    edges = np.unique(pairs[:, 0] * count + pairs[:, 1])
    a, b = edges // count, edges % count
    loose = ~fixed
    degree = (np.bincount(a, minlength=count) + np.bincount(b, minlength=count)).astype(float)
    scale = np.where(loose & (degree > 0), 1.0 / np.maximum(degree, 1.0), 0.0)  # Jacobi

    def neighbours(x):
        return np.bincount(a, weights=x[b], minlength=count) + np.bincount(b, weights=x[a], minlength=count)

    residual = np.where(loose, neighbours(np.where(fixed, values, 0.0)), 0.0)  # x = 0 to start
    x = np.zeros(count)
    tilted = scale * residual
    direction = tilted.copy()
    size = float(residual @ tilted)
    enough = size * HARMONIC_TOLERANCE ** 2
    for _ in range(rounds):
        if size <= enough or size <= 0:
            break
        product = np.where(loose, degree * direction - neighbours(direction), 0.0)
        step = size / float(direction @ product)
        x += step * direction
        residual -= step * product
        tilted = scale * residual
        previous, size = size, float(residual @ tilted)
        direction = tilted + (size / previous) * direction
    if fixed.any():
        x = np.clip(x, values[fixed].min(), values[fixed].max())
    return np.where(fixed, values, x)





def _edge_distance(points, starts, ends):
    """Distance from each point to the aperture's edge."""
    d = ends - starts
    length2 = np.maximum((d * d).sum(axis=1), 1e-30)
    rel = points[:, None, :] - starts[None, :, :]
    t = np.clip((rel * d[None]).sum(axis=2) / length2, 0.0, 1.0)
    nearest = starts[None] + t[..., None] * d[None]
    return np.sqrt(((points[:, None, :] - nearest) ** 2).sum(axis=2)).min(axis=1)


def _soft_edge(points, starts, ends):
    """Distance from each point to the aperture's edge, rounded where two stretches of edge
    are about as near (no ridge down a narrow pad's middle, no crease into a corner): every
    bit of the edge counts, the nearer the more (the inverse fourth power, integrated along
    each segment), scaled to the plain distance beside one long straight edge."""
    d = ends - starts
    length = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-30)
    unit = d / length[:, None]
    rel = points[:, None, :] - starts[None, :, :]
    s = (rel * unit[None]).sum(axis=2)  # the foot of the perpendicular, along the segment
    # at least 10 nm: on an edge's line part()'s difference is rounding noise, over h³
    h = np.maximum(np.abs(rel[..., 0] * unit[None, :, 1] - rel[..., 1] * unit[None, :, 0]), 1e-8)

    def part(t):  # h³ times the integral of (h² + t²)^-2 up to t
        return 0.5 * (t * h / (h * h + t * t) + np.arctan(t / h))

    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        total = np.maximum((part(length[None] - s) - part(-s)) / h ** 3, 0.0).sum(axis=1)
        return np.nan_to_num((2.0 / np.pi * total) ** (-1.0 / 3.0), posinf=0.0)


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


def _metal(grid, outward, matrix, parts, heights=True, spacing=0.0):
    """Per point, how far out from the copper the nearest metal is (inf: none within
    MAX_GAP_M), and how high that metal reaches (its top, looking back from MAX_CLIMB_M
    further out): solder climbs a lead no higher than the lead. Only the same piece of metal
    counts (`_pieces`): a crystal's lid or seal ring over its terminations is not their top.
    Metal behind the part's body (`_Body`: a crystal's lid over its ceramic) is out of reach,
    for both. `parts`: those over the aperture (`_over`); `heights` False: the gaps alone
    (the tops all 0); `spacing`: the grid's (how far round a fused top `_local_top` looks)."""
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
    radius = max(LOCAL_TOP_M, 1.5 * spacing)
    for tree, part_matrix, inverse, *_, pieces, body, _side in parts:
        part_origins = origins @ inverse[:3, :3].T + inverse[:3, 3]
        part_direction = inverse[:3, :3] @ direction
        scale = np.linalg.norm(part_direction)
        part_direction /= scale
        up, down = Vector(part_direction), Vector(-part_direction)
        cast = tree.ray_cast
        hits = [(index, cast(Vector(origin), up, reach * scale)) for index, origin in enumerate(part_origins.tolist())]
        hits = [(index, hit[0], hit[2], hit[3] / scale) for index, hit in hits if hit[0] is not None]
        if body is not None:  # nor through it
            hits = [row for row in hits if not _behind(body, Vector(part_origins[row[0]]), up, row[3], scale)]
        if not hits:
            continue
        index = np.array([row[0] for row in hits])
        piece = pieces.face[np.array([row[2] for row in hits])]
        gap = np.maximum(np.array([row[3] for row in hits]) - RAY_START_M, 0.0)
        nearer = gap < gaps[index]
        index, gap, piece = index[nearer], gap[nearer], piece[nearer]
        met = [row[1] for row, near in zip(hits, nearer) if near]
        gaps[index] = gap
        starts = part_origins[index] + part_direction * (above * scale)
        if not heights:
            continue
        found = [_down_to(cast, Vector(start), down, above * scale, scale, pieces, which, body, below, radius * scale)
                 for start, which, below in zip(starts.tolist(), piece.tolist(), met)]
        top = gap.copy()
        hit = np.array([value is not None for value in found], bool)
        if hit.any():
            world = np.array([tuple(value) for value in found if value is not None]) @ part_matrix[:3, :3].T + \
                part_matrix[:3, 3]
            top[hit] = np.maximum((world - origins[index[hit]]) @ direction - RAY_START_M, gap[hit])
        tops[index] = top
    return gaps, tops


def _behind(body, start, up, distance, scale, tree=None):
    """True when the part's body lies between `start` and the metal `distance` (metres) up
    from it, its faces level with either end (within BODY_SLACK_M) not counted. `tree`: the
    metal's, when `start` is where the ray came into it: a body face inside the metal (a
    ceramic box running into an end cap) does not count either. In the part's coordinates,
    `scale` of them to a metre."""
    travelled, end = BODY_SLACK_M, distance - BODY_SLACK_M
    for _ in range(MAX_LAYERS):
        if travelled >= end:
            return False
        hit = body.ray_cast(start + up * (travelled * scale), up, (end - travelled) * scale)
        if hit[0] is None:
            return False
        at = travelled + hit[3] / scale
        if tree is None or not _in_metal(tree, start, up, at, scale):
            return True
        travelled = at + NUDGE_M
    return True


def _in_metal(tree, start, up, at, scale):
    """True when the point `at` (metres) up from `start`, where the ray came into the metal,
    is still inside it: an even number of its faces crossed on the way."""
    crossed, travelled = 0, NUDGE_M
    while travelled < at and crossed <= MAX_LAYERS:
        hit = tree.ray_cast(start + up * (travelled * scale), up, (at - travelled) * scale)
        if hit[0] is None:
            break
        crossed += 1
        travelled += hit[3] / scale + NUDGE_M
    return crossed % 2 == 0


def _down_to(cast, start, down, distance, scale, pieces, piece, body, below, radius):
    """Coming down from `start`, where the ray first meets a face of the metal `piece` with
    no body between it and `below` (where the ray up met that metal), past MAX_LAYERS other
    faces at most (a lid, a seal ring, an end cap's inside over its ceramic), or None. When
    that is the face the ray came in by (no face above it: a termination's top fused into
    the part's body): as high as that piece stands within `radius` (`_local_top`: an
    inductor's terminal block its walls, a QFN's lead frame the lead's end, a crystal's thin
    pad plate its own thickness, not the corner piece it is one with), unless the body lies
    between (a seal ring over a crystal's ceramic): then that face. In the part's
    coordinates, `scale` of them to a metre."""
    nudge = NUDGE_M * scale
    for _ in range(MAX_LAYERS):
        location, _, face, travelled = cast(start, down, distance)
        if location is None:
            return None
        if pieces.face[face] == piece:
            if (location - below).length < SAME_FACE_M * scale:
                top = _local_top(pieces, piece, below, -down, radius)
                if body is not None and _behind(body, below, -down, (top - below).length / scale, scale, cast.__self__):
                    return location
                return top
            if body is None or not _behind(body, below, -down, (location - below).length / scale, scale,
                                           cast.__self__):
                return location
        start = location + down * nudge
        distance -= travelled + nudge
        if distance <= 0:
            return None
    return None


def _local_top(pieces, piece, below, up, radius):
    """Straight `up` from `below`, as high as the metal `piece` reaches within `radius` of
    that line: its triangles whose box, seen along `up`, comes that near."""
    up = np.array(up)
    key = (id(pieces.triangles), piece, tuple(np.round(up, 6)))
    if key not in _seen_along:
        triangles = pieces.triangles[pieces.face == piece]
        across = np.linalg.svd(up[None, :])[2][1:]  # two directions square to `up`
        flat = triangles @ across.T  # (triangles, 3, 2)
        _seen_along[key] = (across, flat.min(axis=1), flat.max(axis=1), (triangles @ up).max(axis=1), pieces.triangles)
    across, low, high, tops, _kept = _seen_along[key]
    at = np.array(below) @ across.T
    near = np.all((low - radius <= at) & (high + radius >= at), axis=1)
    if not near.any():
        return below
    rise = float(tops[near].max() - np.dot(below, up))
    return below + Vector(up) * max(rise, 0.0)


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


def _outline(grid, edge, gaps, tops, under, spacing, starts, ends, outward, matrix, parts):
    """The grid with points added on the metal's real outline, between each point under the
    metal at its edge and the free point nearest it (OUTLINE_HALVINGS): the solder meets the
    metal along that line, not along the grid's staircase round it. (grid, edge, gaps, tops,
    under), the new points under the metal, with their inside neighbour's top."""
    free = ~under
    distance = np.sqrt(((grid[free][:, None, :] - grid[under][None, :, :]) ** 2).sum(axis=2))
    rim = distance.min(axis=0) <= 1.5 * spacing  # of the points under the metal
    if not rim.any():
        return grid, edge, gaps, tops, under
    inside = grid[under][rim]
    outside = grid[free][distance[:, rim].argmin(axis=0)]
    low, high = np.zeros(len(inside)), np.ones(len(inside))
    met = gaps[under][rim].copy()
    for _ in range(OUTLINE_HALVINGS):
        middle = 0.5 * (low + high)
        probe = _metal(inside + (outside - inside) * middle[:, None], outward, matrix, parts, False)[0]
        metal = probe <= MAX_GAP_M
        low, high = np.where(metal, middle, low), np.where(metal, high, middle)
        met = np.where(metal, probe, met)
    outline = inside + (outside - inside) * low[:, None]
    keep = (np.sqrt(((outline[:, None, :] - grid[None, :, :]) ** 2).sum(axis=2)).min(axis=1) > 0.3 * spacing) \
        & _inside(outline, starts, ends)
    for index in range(len(outline)):  # one point per spot
        if keep[index]:
            keep[index + 1:] &= np.hypot(*(outline[index + 1:] - outline[index]).T) >= 0.3 * spacing
    if not keep.any():
        return grid, edge, gaps, tops, under
    outline = outline[keep]
    return (np.concatenate((grid, outline)), np.concatenate((edge, _edge_distance(outline, starts, ends))),
            np.concatenate((gaps, met[keep])), np.concatenate((tops, tops[under][rim][keep])),
            np.concatenate((under, np.ones(len(outline), bool))))


def _local_heights(xy, heights, metal, edge, reach):
    """`heights` under the metal, each along its edge (`edge`) set to the median of the edge's
    within `reach` of it (a grazing ray's too high a top does not count), no higher than its
    own, and rising along the edge from a lower one no steeper than MAX_SLOPE (beside a
    crystal's thin bottom plate, in its tall corner piece's notch: the fillet ramps up from
    it, no step and no spike); each further in to that of the edge point nearest it."""
    found = heights.copy()
    rim = np.flatnonzero(edge)
    if not len(rim):
        return found
    apart = np.sqrt(((xy[rim][:, None, :] - xy[rim][None, :, :]) ** 2).sum(axis=2))
    typical = np.minimum([np.median(heights[rim][row]) for row in apart <= reach], heights[rim])
    found[rim] = (typical[None, :] + MAX_SLOPE * apart).min(axis=1)
    inner = np.flatnonzero(metal & ~edge)
    if len(inner):
        nearest = np.einsum("ijk,ijk->ij", *(2 * [xy[inner][:, None, :] - xy[rim][None, :, :]])).argmin(axis=1)
        found[inner] = found[rim][nearest]
    return found


def _far_edge(xy, edge, metal, fall):
    """Of the aperture's edge vertices (`edge`), those where the pad ends far ahead of the
    metal: at least FAR_EDGE of the farthest any is from it, or further than the fillet
    there falls (`fall` per vertex: a gull-wing's heel, the pad running on past the bend).
    Along the sides near the metal the meniscus runs on instead."""
    found = np.zeros(len(xy), bool)
    if not edge.any() or not metal.any():
        return found
    apart = xy[edge][:, None, :] - xy[metal][None, :, :]
    distance = np.sqrt(np.einsum("ijk,ijk->ij", apart, apart).min(axis=1))
    found[np.flatnonzero(edge)] = (distance >= FAR_EDGE * distance.max()) | (distance >= fall[edge])
    return found


def _soft_min(a, b):
    """The smaller of `a` and `b` (>= 0), rounded where they meet."""
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return np.nan_to_num((np.maximum(a, 1e-30) ** -SOFT_MIN + np.maximum(b, 1e-30) ** -SOFT_MIN) ** (-1.0 / SOFT_MIN))


def _aperture(rings, outward, matrix, parts, volume_per_area, target):
    """One aperture's solder: (vertices (n, 3), triangles, smooth flag per triangle, metal
    found, grid points), or None for an empty aperture. `target`: grid points to aim for."""
    area = sum(-abs(_area(points)) if hole else abs(_area(points)) for points, hole in rings)
    if area <= 0:
        return None
    starts, ends = _segments(rings)
    grid, edge, spacing = _grid(rings, area, starts, ends, target)
    gaps, tops = _metal(grid, outward, matrix, parts, spacing=spacing)
    under = gaps <= MAX_GAP_M
    wetted = bool(under.any())
    if wetted and not under.all():
        grid, edge, gaps, tops, under = _outline(grid, edge, gaps, tops, under, spacing, starts, ends, outward,
                                                 matrix, parts)
    xy, triangles, source, flat = _triangulate(rings, grid, spacing)
    if not len(triangles):
        return None
    inner = source >= 0
    at = np.maximum(source, 0)

    def per_vertex(values, default):
        return np.where(inner, values[at], default) if len(grid) else np.full(len(xy), default)

    under_v = per_vertex(under, False)
    free_v = inner & ~under_v
    pairs = np.vstack((triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]))
    pairs = np.vstack((pairs, pairs[:, ::-1]))
    edge_v = np.zeros(len(xy), bool)  # the metal's edge: under it, next to the free pad
    edge_v[pairs[under_v[pairs[:, 0]] & free_v[pairs[:, 1]], 0]] = True
    corners = xy[triangles]
    signed = 0.5 * ((corners[:, 1, 0] - corners[:, 0, 0]) * (corners[:, 2, 1] - corners[:, 0, 1]) -
                    (corners[:, 2, 0] - corners[:, 0, 0]) * (corners[:, 1, 1] - corners[:, 0, 1]))
    areas = np.abs(signed)

    def volume(heights):
        return float((areas * heights[triangles].mean(axis=1)).sum())

    target = area * volume_per_area
    if wetted:
        # under a lead: solder up to it across a standoff, not a column up to metal far over the pad
        held = per_vertex(np.where(under, np.minimum(gaps, GAP_FILL_M), 0.0), 0.0)
        # How high the metal stands along its edge, a little under its top (TOP_INSET_M:
        # the metal hides where they meet): round each point there the typical height (the
        # median within LOCAL_REACH_M: a thin wall or a grazing ray alone does not count, a
        # QFN lead's high outer end and low inner end both do), and over the pad the
        # smoothest blend of that. The fillet climbs no higher.
        seated = per_vertex(np.where(under, gaps, np.inf), np.inf) <= GAP_FILL_M
        # (beside metal further up, a gull-wing's heel, only the standoff's solder: no sheet
        # standing up beside it, no fold where it meets the solder under it)
        stands = np.where(seated, per_vertex(tops, 0.0) - TOP_INSET_M, held)
        stands = np.clip(stands, WET_FILM_M, MAX_CLIMB_M)
        climbs = _local_heights(xy, stands, under_v, edge_v, max(LOCAL_REACH_M, 3 * spacing))
        climbs = _harmonic(len(xy), triangles, under_v, climbs)
        # one smooth meniscus, sagging (squared): from the metal (1) to where the pad ends
        # far ahead of it or further than the fillet falls at MAX_SLOPE (0, `_far_edge`);
        # along a pad's sides it runs on (a QFN's narrow toe all the way out, a chip's fillet
        # the same across its width), rounded off there
        pinned = under_v | (~inner & _far_edge(xy, ~inner, under_v, climbs / MAX_SLOPE))
        shape_v = np.where(free_v, climbs * _harmonic(len(xy), triangles, pinned, under_v.astype(float)) ** 2, 0.0)
        # and up from the pad's edge no steeper than MAX_SLOPE, rounded (a soft minimum, no
        # ridge where the two meet; `_soft_edge`, none down a narrow pad's middle or into a
        # corner): where the pad barely shows past a part, or beside a termination, a low
        # fillet, no wall, fold or point
        cap_v = np.where(free_v, MAX_SLOPE * _soft_edge(xy, starts, ends), np.inf)

        def fillet(share):
            return held + _soft_min(share * shape_v, cap_v)

        low, high = 0.0, 1.0
        if volume(fillet(high)) > target:
            for _ in range(30):
                middle = 0.5 * (low + high)
                low, high = (middle, high) if volume(fillet(middle)) < target else (low, middle)
        # As high as the metal and solder still left (a QFN's short, thin lead on a long
        # pad): it goes under the part, which floats on it, hidden. It never bulges.
        height = fillet(high)
        # Along the edge of metal sitting on the pad, as high as the solder beside it (no step
        # along the grid's staircase round it) and no higher than the metal (no sheet out of a
        # thin wall). Further in, and under metal further up (a gull-wing's heel), only the
        # standoff's solder, hidden under the metal: none stands up in a slot or hole in it.
        beside = pairs[edge_v[pairs[:, 0]] & free_v[pairs[:, 1]]]
        level = np.bincount(beside[:, 0], weights=height[beside[:, 1]], minlength=len(xy)) / \
            np.maximum(np.bincount(beside[:, 0], minlength=len(xy)), 1)
        top = np.maximum(held, np.minimum(high * climbs, per_vertex(tops, 0.0) - TOP_INSET_M))
        height = np.where(edge_v & seated, np.clip(level, held, top), np.where(under_v, held, height))
    else:
        held = np.zeros(len(xy))
        dome = 1.0 - (1.0 - edge / edge.max()) ** 2 if len(grid) else np.zeros(0)
        shape_v = _smooth(per_vertex(dome, 0.0), triangles, free_v, np.ones(len(xy), bool))
        cap_v = np.full(len(xy), MAX_DOME_M)
        per_unit = volume(shape_v)
        height = min(target / per_unit, MAX_DOME_M) * shape_v if per_unit > 0 else np.zeros(len(xy))
    # The grid's steps (distances to the nearest metal point) show on a shiny surface:
    # smooth the free surface, then give back the solder smoothing moved, no higher than
    # allowed. The metal's edge rides along too (no step along the grid's staircase beside a
    # termination), never higher than it was set (under its metal) nor under its gap; the
    # hidden solder further under the metal does not pull it down.
    free_before = volume(np.where(free_v, height, 0.0))
    smoothed = _smooth(height, triangles, free_v | edge_v, ~under_v | edge_v, SMOOTH_ROUNDS)
    free_after = volume(np.where(free_v, smoothed, 0.0))
    scale = free_before / free_after if free_after > 0 else 1.0
    height = np.where(free_v, np.minimum(smoothed * scale, cap_v),
                      np.where(edge_v, np.clip(smoothed, held, height), height))
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
    owned = {}  # footprint -> its model parts, every face (`_own_parts`)
    for drill in named.values():
        if not (drill.get("kls_drill") and drill.get("kls_plated") and drill.get("kls_drill_size")
                and len(drill.data.vertices)):
            continue
        pad_id = drill.get("kls_id")
        centre = np.array(drill.location[:2], dtype=np.float64)
        size = tuple(drill["kls_drill_size"])
        rings = (top.get(pad_id), bottom.get(pad_id))
        # a numbered pad's footprint: its model has a pin in the hole (an older bridge: not known)
        owner = drill.get("kls_fp_id") if drill.get("kls_pad_number") else None
        shape = (centre.tobytes(), size, bool(drill.get("kls_drill_oval", False)), round(drill.rotation_euler.z, 9),
                 owner, *(b"" if ring is None else ring.tobytes() for ring in rings))
        half = 0.5 * max(size)
        low = np.array((centre[0] - half, centre[1] - half, z_bottom - PIN_REACH_M))
        high = np.array((centre[0] + half, centre[1] + half, z_top + PIN_REACH_M))
        entry = last["entries"].get(pad_id) if last is not None else None
        if entry is not None and entry["shape"] == shape and not _reached(entry["low"][None], entry["high"][None], moved)[0]:
            entries[pad_id] = entry
            continue
        revisited += 1
        joint = _hole(drill, centre, rings, z_top, z_bottom, parts, lows, highs,
                      (lambda owner=owner: _own_parts(owner, named, owned)) if owner else None)
        entries[pad_id] = {"shape": shape, "low": low, "high": high, "kept": joint,
                           "vertices": None if joint is None else joint[0]}
    layout = _place(obj.data, last, entries)
    _s().sides[THT] = {"settings": (z_top, z_bottom), "entries": entries, "generation": _s().generation, **layout}
    _use_solder_material(obj)
    _note(THT, ms=(time.perf_counter() - started) * 1000,
          joints=sum(entry["kept"] is not None for entry in entries.values()), revisited=revisited,
          rewritten=layout["rewritten"])


def _hole(drill, centre, rings, z_top, z_bottom, parts, lows, highs, own=None):
    """One plated hole's joint (vertices, triangles, smooth flags), or None without a pin.
    `own`: for a numbered pad, what gives its own model parts, every face (`_own_parts`),
    looked at where no metal is found."""
    angles = np.linspace(0.0, 2 * np.pi, THT_ANGLES, endpoint=False)
    unit = np.column_stack((np.cos(angles), np.sin(angles)))
    outline = _drill_outline(centre, *drill["kls_drill_size"], drill.rotation_euler.z,
                             drill.get("kls_drill_oval", False))
    half = 0.5 * max(drill["kls_drill_size"])
    # The pin at mid-board, else just inside either face (a pin ending inside the board)
    for mid in (0.5 * (z_top + z_bottom), z_top - FACE_PROBE_M, z_bottom + FACE_PROBE_M):
        near = np.flatnonzero(np.all(highs[:, :2] >= centre - half, axis=1) & np.all(lows[:, :2] <= centre + half, axis=1)
                              & (highs[:, 2] >= mid) & (lows[:, 2] <= mid))  # parts reaching into the hole there
        pin = _pin(np.array((centre[0], centre[1], mid)), outline, unit, [parts[index] for index in near])
        if pin is None and own is not None:
            pin = _pin(np.array((centre[0], centre[1], mid)), outline, unit, own())
        if pin is not None:
            break
    if pin is None:
        return None
    middle, wall, radius, up, down, side, (tree, matrix, inverse) = pin
    limit = 0.5 * float(np.ptp(outline, axis=0).max())
    # Each face's fillet about the pin where it leaves that face (a snap-in leg slants
    # through the hole, pressed against its wall); as at mid-board where it is not found there
    centres, walls, radii, reach = {}, {}, {}, {}
    for face, z, end in (("top", z_top - FACE_PROBE_M, 4), ("bottom", z_bottom + FACE_PROBE_M, 5)):
        found = _pin_face(tree, matrix, inverse, middle, mid, centre, z, outline, unit, limit)
        if found is None:
            centres[face], walls[face], radii[face] = middle, wall, radius
            reach[face] = up - (z_top - mid) if face == "top" else down - (mid - z_bottom)
        else:
            centres[face], walls[face], radii[face] = found[1:4]
            reach[face] = found[end] - FACE_PROBE_M  # the pin past the face
        # the solder's inner wall a little inside the pin: on its face, the two flickered
        most = np.maximum(walls[face] - 1e-6, 0.5 * walls[face])  # inside the wall, even a pin nearly on it
        radii[face] = np.clip(radii[face] - PIN_INSET_M, np.minimum(1e-6, most), most)
    pads = {"top": _radial(centres["top"], rings[0], unit), "bottom": _radial(centres["bottom"], rings[1], unit)}
    climbs, fillets = {}, {}
    steps = np.arange(THT_STEPS + 1) / THT_STEPS
    for face, edge in pads.items():
        edge = np.where(np.isfinite(edge) & (edge > walls[face]), edge, walls[face])
        pads[face] = edge
        # The fillet: an even band round the pin's outline, its corners rounded (as solder
        # wets a round pin, a blade or a bent leg alike), as wide as the pad allows all
        # round; a square pad's corners past it are only wetted (a thin film), as on a
        # real joint. Per direction, its edge (k = 0) in to the pin (k = 1)
        width = max(float(np.min(edge - radii[face])), MIN_FILLET_M)
        fillets[face] = np.minimum(_grown(unit, radii[face], (1.0 - steps) * width), edge[:, None])
        share = PART_SIDE_CLIMB if face == side else SOLDER_SIDE_CLIMB
        climbs[face] = max(WET_FILM_M, min(share * width, 0.8 * reach[face], MAX_THT_CLIMB_M))
    return _joint(centres, unit, walls, radii, pads, fillets, z_top, z_bottom, climbs)


def _own_parts(footprint_id, named, owned):
    """A footprint's shown model parts without a role, every face of them (`_metal_tree`'s
    `whole`), as `_pin` takes parts: in a numbered pad's hole only its pin goes through,
    whatever colour the model gives it. `owned`: these, per footprint, for this build."""
    if footprint_id not in owned:
        found = []
        world_to_root = poses.to_root()
        for name in board.model_objects_by_fp.get(footprint_id, ()):
            obj = named.get(name)
            if obj is None or obj.type not in SHAPED or obj.get("kls_role") is not None:
                continue
            frame = obj.parent
            if frame is None or frame.get("kls_footprint") != 1:
                frame = components.find(components.FRAME, footprint_id)
            matrix = np.array(poses.in_root(obj, world_to_root), dtype=np.float64)
            if not models.model_is_visible(frame, obj) or abs(np.linalg.det(matrix[:3, :3])) <= 1e-30:
                continue
            tree = _metal_tree(obj, whole=True)[0]
            if tree is not None:
                found.append((tree, matrix, np.linalg.inv(matrix), obj.get("kls_side", "top")))
        owned[footprint_id] = found
    return owned[footprint_id]


def _grown(unit, radius, widths):
    """Per direction (`unit`, from the pin's middle) and width, how far out the pin's outline
    (`radius` per direction) grown by that width reaches: (directions, widths). An even
    band round the pin, its corners rounded: where each ray last leaves a band round one of
    the outline's sides or a disc round one of its corners."""
    corners = radius[:, None] * unit
    side = np.roll(corners, -1, axis=0) - corners
    length = np.maximum(np.linalg.norm(side, axis=1), 1e-30)
    along = side / length[:, None]
    normal = np.column_stack((along[:, 1], -along[:, 0]))
    normal *= np.where((normal * corners).sum(axis=1) < 0, -1.0, 1.0)[:, None]  # outward
    width = widths[None, None, :]
    toward = (unit @ corners.T)[:, :, None]  # (directions, corners, 1)
    reach = toward ** 2 - (corners ** 2).sum(axis=1)[None, :, None] + width ** 2
    discs = np.where(reach >= 0, toward + np.sqrt(np.maximum(reach, 0.0)), -np.inf)
    facing = (unit @ normal.T)[:, :, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = ((normal * corners).sum(axis=1)[None, :, None] + width) / facing
        at = (out * (unit @ along.T)[:, :, None] - (corners * along).sum(axis=1)[None, :, None]) / length[None, :, None]
    bands = np.where((facing > 1e-12) & (at >= 0) & (at <= 1), out, -np.inf)
    return np.maximum(np.maximum(discs.max(axis=1), bands.max(axis=1)), radius[:, None])


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


def _cast(tree, inverse, origin, direction, limit):
    """How far from `origin` along `direction` (board root's space) a ray meets the part's
    metal (its tree, `inverse` into the part), within `limit`; or None."""
    local = inverse[:3, :3] @ direction
    scale = np.linalg.norm(local)
    distance = tree.ray_cast(Vector(inverse[:3, :3] @ origin + inverse[:3, 3]), Vector(local / scale), limit * scale)[3]
    return None if distance is None else distance / scale


def _pin_middle(tree, matrix, inverse, centre, limit):
    """A point in the middle of the metal nearest the hole's centre at mid-board (a blade or
    a bent leg beside the centre), or None: across that metal towards it, then across that."""
    local = inverse[:3, :3] @ centre + inverse[:3, 3]
    scale = np.linalg.norm(inverse[:3, :3], axis=0).max()
    nearest = tree.find_nearest(Vector(local), limit * scale)[0]
    if nearest is None:
        return None
    toward = (matrix[:3, :3] @ np.array(nearest) + matrix[:3, 3] - centre)[:2]
    if np.hypot(*toward) < NUDGE_M:
        return None
    toward = np.array((*(toward / np.hypot(*toward)), 0.0))
    near = _cast(tree, inverse, centre, toward, limit)
    if near is None:
        return None
    far = _cast(tree, inverse, centre + toward * (near + NUDGE_M), toward, 2 * limit)
    if far is None:
        return None
    middle = centre + toward * (near + 0.5 * (NUDGE_M + far))
    across = np.array((-toward[1], toward[0], 0.0))
    ahead, behind = _cast(tree, inverse, middle, across, 2 * limit), _cast(tree, inverse, middle, -across, 2 * limit)
    if ahead is None or behind is None:
        return None
    return middle + across * 0.5 * (ahead - behind)


def _pin(centre, outline, unit, parts):
    """The pin in a hole: rays across the hole at mid-board meet a pin's wall from inside.
    They start at the hole's centre, else in the middle of the metal nearest it
    (`_pin_middle`). (where they started, how far the drill's wall less WALL_GAP_M is from
    there per direction, the pin's radius per direction, how far the pin reaches up and down
    from mid-board, the side its part is on, the part: its tree, matrix and inverse), or None
    without one (PIN_HITS of the rays)."""
    best = None
    limit = 0.5 * float(np.ptp(outline, axis=0).max())
    for tree, matrix, inverse, *_, side in parts:
        found = _pin_from(tree, inverse, centre, outline, unit)
        if found is None:
            middle = _pin_middle(tree, matrix, inverse, centre, limit)
            found = None if middle is None else _pin_from(tree, inverse, middle, outline, unit)
        if found is not None and (best is None or found[0] > best[0]):
            best = (*found, side, (tree, matrix, inverse))
    return None if best is None else best[1:]


def _pin_face(tree, matrix, inverse, middle, mid, centre, z, outline, unit, limit):
    """The pin at height `z`, just inside a face: followed there from its middle at mid-board
    (`middle`, height `mid`: `_follow`), else about the middle of the metal nearest the
    hole's centre (`centre`) there. `_pin_from`'s, or None."""
    for inner in (_follow(tree, inverse, middle, mid, z, limit),
                  _pin_middle(tree, matrix, inverse, np.array((centre[0], centre[1], z)), limit)):
        found = None if inner is None else _pin_from(tree, inverse, inner, outline, unit, inside=True)
        if found is not None:
            return found
    return None


def _follow(tree, inverse, middle, start, end, limit):
    """A pin's middle followed from (`middle`, height `start`) to height `end` in
    FOLLOW_STEPS, centred across it (along x, then y) at each: a snap-in leg slants
    through the hole. None when lost (a ray across it meets nothing)."""
    point = np.array((middle[0], middle[1], start))
    for z in np.linspace(start, end, FOLLOW_STEPS + 1)[1:]:
        point[2] = z
        for axis in (np.array((1.0, 0.0, 0.0)), np.array((0.0, 1.0, 0.0))):
            ahead, behind = _cast(tree, inverse, point, axis, 2 * limit), _cast(tree, inverse, point, -axis, 2 * limit)
            if ahead is None or behind is None:
                return None
            point = point + axis * 0.5 * (ahead - behind)
    return point


def _pin_from(tree, inverse, origin, outline, unit, inside=False):
    """`_pin`'s rays from `origin`, for one part: (hits, origin's xy, wall, radius, up, down),
    or None when fewer than PIN_HITS of them meet its metal. `inside`: `origin` is in the
    pin (`_pin_middle`): FACE_HITS of them must, and one that does not meets the drill's
    wall first (a leg pressed against it). The same where the rays turn out to start inside
    the pin (PIN_HITS of them meet its faces from behind) and it reaches past the drill's
    wall: a model's blade wider than the footprint's hole fills the hole there."""
    wall = _radial(origin[:2], outline, unit) - WALL_GAP_M
    if not np.all(np.isfinite(wall)):
        return None
    start = Vector(inverse[:3, :3] @ origin + inverse[:3, 3])  # in the part: once for all the rays
    local = np.column_stack((unit, np.zeros(len(unit)))) @ inverse[:3, :3].T
    scale = np.linalg.norm(local, axis=1)
    radius = np.array([tree.ray_cast(start, Vector(along / stretch), (reach + PIN_SLACK_M) * stretch)[3]
                       for along, stretch, reach in zip(local, scale, wall)], dtype=float) / scale
    hits = int(np.isfinite(radius).sum())
    if not inside and hits < PIN_HITS * len(unit):
        # further out: a face turned away is where the ray left the pin it started in
        behind = 0
        for along, stretch in zip(local, scale):
            _, normal, _, _ = tree.ray_cast(start, Vector(along / stretch), PIN_REACH_M * stretch)
            behind += normal is not None and normal.dot(Vector(along)) > 0
        if behind >= PIN_HITS * len(unit):
            inside, hits = True, len(unit)
    if hits < (FACE_HITS if inside else PIN_HITS) * len(unit):
        return None
    ends = [_cast(tree, inverse, origin, np.array((0.0, 0.0, sign)), PIN_REACH_M) for sign in (1.0, -1.0)]
    ends = [PIN_REACH_M if end is None else end for end in ends]
    radius = np.where(np.isfinite(radius), radius, wall if inside else np.nanmedian(radius))
    return hits, origin[:2], wall, radius, ends[0], ends[1]


def _joint(centres, unit, walls, radii, pads, fillets, z_top, z_bottom, climbs):
    """A through-hole joint as one closed solid: per direction a profile (distance, height)
    up the barrel's wall, out over the solder-side pad, a thin film in to where the fillet
    starts, the fillet up to the pin (`fillets`: per direction its distances, edge to pin),
    along the pin, the part-side fillet and film, back over that pad; the profiles joined
    around the hole. Each face's distances are from its own centre (`centres`: where the pin
    leaves that face), as are its `walls` (the drill's), `radii` (the pin's) and pads."""
    steps = np.arange(THT_STEPS + 1) / THT_STEPS
    count = len(unit)
    columns = [("top", walls["top"], z_top), ("bottom", walls["bottom"], z_bottom),
               ("bottom", pads["bottom"], z_bottom)]
    for index, k in enumerate(steps):  # the bottom fillet: from its edge in to the pin, rising (down)
        columns.append(("bottom", fillets["bottom"][:, index],
                        z_bottom - (WET_FILM_M + (climbs["bottom"] - WET_FILM_M) * k * k)))
    for index, k in enumerate(steps):  # the top fillet: from the pin out to its edge
        columns.append(("top", fillets["top"][:, THT_STEPS - index],
                        z_top + WET_FILM_M + (climbs["top"] - WET_FILM_M) * (1 - k) ** 2))
    columns.append(("top", pads["top"], z_top))
    distance = np.stack([np.broadcast_to(column[1], (count,)) for column in columns], axis=1)  # (directions, points)
    height = np.stack([np.full(count, column[2]) for column in columns], axis=1)
    origin = np.stack([np.broadcast_to(centres[column[0]], (count, 2)) for column in columns], axis=1)
    directions, points = distance.shape
    xy = origin + distance[..., None] * unit[:, None, :]
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
