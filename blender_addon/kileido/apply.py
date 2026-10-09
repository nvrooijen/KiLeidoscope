"""Apply decoded protocol frames to the objects of the KiLeidoscope board collection.

Frames and their order are defined by the bridge (kileido_bridge/protocol.py): a
snapshot is `snapshot_begin`, `board`, layer data, `footprints`, `stackup`,
`snapshot_end`; live edits send single layer-data, footprint or selection frames.
"""

import hashlib
import time

import bpy
import numpy as np

from . import (cosmetics, cut, edge_plating, focus, fold, footprints, highlight, holes, ims, laminate, layers,
               lighting, materials, models, nodes, protection, render_depth, transform)
from .client import FrameDecoder
from .objects import (OUTLINE, ensure_groups, hide, owned_object, read_attribute, read_coordinates, read_edges,
                      set_modifier, set_node_input, set_visible, single_point, view3d_spaces, write_attribute,
                      outline_bounds)
from .placement import (BOARD_FACE_CLEARANCE_M, CAP_PLATING_M, SOLDER_TOP_SCALE, copper_placement, copper_thickness,
                        laminate_faces, outward, stencil_thickness, via_plating)
from . import state
from .state import board

MIN_TRANSPARENT_BOUNCES = 32
VIEW_CLIP_START_M = 0.001  # 3D views' near clip, set once per board: close enough to look into a cut via
SECTION_INPUTS = ("board", "layer_data", "appearance", "stackup")  # frames the cut plane's section reads
PLATED_WALL_INSET_M = 5e-6  # a plated drill wall stands this far inside its drill: clear of a board edge
                           # that follows the drill (a castellation drawn as a notch)


def load_frames(data: bytes) -> float:
    """Synchronous apply of a complete frame stream (headless tests and tools); ms taken."""
    started = time.perf_counter()
    for header, arrays in FrameDecoder().feed(data):
        apply_frame(header, arrays)
    return (time.perf_counter() - started) * 1000


def apply_frame(header, arrays):
    message_type = header["type"]
    if message_type == "snapshot_begin":
        _begin_snapshot()
    elif message_type == "board":
        apply_board(header)
        if board.in_snapshot:
            _hide_collection(True)
    elif message_type == "layer_data":
        apply_layer_data(header, arrays)
    elif message_type == "appearance":
        # Saved-board or theme colours changed (after a KiCad save): recolour in place.
        board.appearance = header.get("appearance", {})
        set_color_mode(board.color_mode)
        refresh_protection()  # the board's via rules come with it
    elif message_type == "footprints":
        footprints.apply(header)
        highlight.refresh_components()
    elif message_type == "stackup":
        board.warnings = list(dict.fromkeys([*board.warnings, *header.get("warnings", [])]))
        board.layer_names = dict(header.get("display_names", {}))  # the Layers list uses KiCad's names
        board.stackup = list(header.get("layers", []))  # the Layers list draws it, dielectrics included
    elif message_type == "snapshot_end":
        _end_snapshot()
    elif message_type == "selection":
        highlight.apply_selection(header)
    elif message_type == "flex":
        board.flex = header.get("flex") or {}  # the panel's flex box: stack, bends, checks
    elif message_type == "status":
        pass  # KiCad's link state: live.LiveLink reads it
    else:
        raise ValueError(f"unknown message type: {message_type}")
    if message_type == "layer_data" and not board.in_snapshot:  # a live edit: what shorts may have changed
        refresh_ims_warnings()
    if message_type in ("layer_data", "flex", "board") and not board.in_snapshot:
        fold.invalidate()  # the folded copies follow the edit once it settles
    if message_type in SECTION_INPUTS:
        cut.invalidate()
        laminate.update_bands()
        edge_plating.invalidate()


def apply_layer_data(header, arrays):
    kind = header["kind"]
    if kind == "tracks":
        _apply_tracks(header, arrays)
    elif kind == "pads":
        _apply_pads(header, arrays)
    elif kind == "paste":
        _apply_solder(header, arrays)
    elif kind == "zones":
        _apply_zones(header, arrays)
    elif kind == "graphics":
        _apply_graphics(header, arrays)
    elif kind == "vias":
        _apply_vias(header, arrays)
    elif kind == "outline":
        _apply_outline(arrays)
    else:
        raise ValueError(f"unknown layer kind: {kind}")


# --- Snapshots ------------------------------------------------------------------------------

def _hide_collection(hidden):
    board.collection.hide_viewport = hidden
    board.collection.hide_render = hidden


def _begin_snapshot():
    """Hide the board while a full snapshot rebuilds it (no half-drawn frames)."""
    # A collection freed since the last frame (undo, file load) is noticed here, before anything
    # touches it: the state starts over and this snapshot builds the board anew.
    board.drop_if_freed("snapshot begin", resync=False)
    board.in_snapshot = True
    board.footprint_state.clear()
    board.touched.clear()
    if board.collection is not None:
        _hide_collection(True)


def _end_snapshot():
    """Clear what the snapshot did not mention, show the board, start the file watchers."""
    for obj in tuple(board.collection.all_objects):
        if obj.name in board.touched:
            continue
        if obj.get("kls_model_fp_id") is not None:
            set_visible(obj, False)
        elif obj.get("kls_cosmetic_layer") is not None:
            if obj.get("kls_board_path") != board.board_path:
                set_visible(obj, False)
        elif obj.type == "MESH":
            obj.data.clear_geometry()
        else:
            hide(obj, True)
    _hide_collection(False)
    board.in_snapshot = False
    board.status = f"Loaded {board.board_name}"
    highlight.refresh()  # skipped per frame during the snapshot
    holes.rebuild()  # likewise: one redraw for the snapshot's pads, vias and outline
    for row, _ in layers.rows():  # rows switched off (or under the IMS base) stay off for the new objects too
        if not layers.shown(row) or layers.under_base(row):
            layers.refresh(row)
    lighting.ensure_studio_lights()
    refresh_ims_warnings()
    focus.refresh()
    cut.refresh()
    fold.invalidate()
    edge_plating.refresh()
    frame_board()
    models.follow_board(board.board_path, board.export)
    cosmetics.follow_board(board.board_path, board.export)


def _collection(board_name):
    name = f"KiLeidoscope: {board_name}"
    collection = bpy.data.collections.get(name)
    if collection is not None and collection.get("kileido_owned") != 1:
        raise RuntimeError(f"Collection {name!r} already exists and is not owned by KiLeidoscope")
    if collection is None:
        collection = bpy.data.collections.new(name)
        collection["kileido_owned"] = 1
    if collection.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(collection)
    return collection


def apply_board(header):
    """Board identity, stackup heights and appearance; always the first snapshot frame."""
    name = header["board_name"]
    board.drop_if_freed("board header")
    board_changed = board.board_name != name
    if not board.groups:
        board.groups = nodes.ensure_all()
        materials.create_all()
    if board.collection is None:
        board.collection = _collection(name)
    elif board_changed:
        existing = bpy.data.collections.get(f"KiLeidoscope: {name}")
        if existing is not None and existing != board.collection:
            raise RuntimeError(f"Collection {existing.name!r} already exists")
        board.collection.name = f"KiLeidoscope: {name}"
    ensure_groups()
    lock_selection(board.collection)
    if board_changed:
        board.origin_nm = tuple(header["origin_nm"])
        if board.model_bound:
            state.log(f"board changed to {name!r}; {len(board.model_bound)} bound models dropped")
        board.model_bound.clear()
        board.model_objects_by_fp.clear()
        board.mask_images.clear()  # the previous board's mask plots no longer apply
        board.relief_images.clear()
    board.board_name = name
    board.heights = {entry["name"]: float(entry["z_m"]) for entry in header["layers"]}
    board.thickness_m = float(header["board_thickness_m"])
    board.layer_thickness = dict(header.get("layer_thickness_m") or {})
    _shape_ims()
    see_through_holes(bpy.context.scene)
    render_depth.install()  # EEVEE renders: 35 um copper under the default clip range
    board.appearance = header.get("appearance", {})
    board.board_path = header.get("board_path", "")
    board.export = header.get("export") or {}
    set_color_mode(getattr(bpy.context.scene, "kileido_color_mode", "FAB"))
    board.warnings = list(header.get("warnings", []))
    board.status = f"Loaded {name}"
    for space in view3d_spaces():
        space.overlay.show_relationship_lines = False


def _shape_ims():
    """IMS mode on a 2-layer board: the base takes B.Cu's place, so every height read from
    here on (copper, vias, drills, components) is the IMS stack's. The bottom layers hide
    under the base, or come back when IMS mode ends."""
    was = board.ims is not None
    scene = bpy.context.scene
    board.ims = None
    if getattr(scene, "kileido_ims", False) and ims.eligible(board.heights)[0]:
        board.ims = ims.stack(board.heights, board.layer_thickness, scene.kileido_ims_epoxy_um * 1e-6)
        board.heights = board.ims.heights
        board.layer_thickness = board.ims.layer_thickness
        board.thickness_m = board.ims.thickness_m
    if was != (board.ims is not None):  # what the base hides, or shows again: every row (layers.hidden)
        for row, _ in layers.rows():
            layers.refresh(row)


def lock_selection(collection):
    """The live board follows KiCad, so nothing in it can be selected (and so moved) in
    Blender; the lock covers its child collections too. Clicks still pick KiCad items
    (pick.py casts its own ray). Exported, then imported boards are selectable again
    (packages.import_board)."""
    collection.hide_select = True


def see_through_holes(scene):
    """A ray down an open hole passes a transparent land, zone copper on each layer,
    both board faces, ... Cycles' default cap of 8 transparent bounces turned open
    holes black with the mask hidden (measured); 32 clears the reference board."""
    cycles = getattr(scene, "cycles", None)
    if cycles is not None and cycles.transparent_max_bounces < MIN_TRANSPARENT_BOUNCES:
        cycles.transparent_max_bounces = MIN_TRANSPARENT_BOUNCES


def frame_board(force=False):
    """Centre every 3D view on the board and fit it (like Shift+C), once per board."""
    if bpy.app.background or board.collection is None:
        return
    if not force and board.framed_board == board.board_name:
        return
    bounds = outline_bounds()
    if bounds is None:
        return
    xmin, ymin, xmax, ymax = bounds
    extent = max(xmax - xmin, ymax - ymin)
    for space in view3d_spaces():
        view = space.region_3d
        view.view_location = ((xmin + xmax) / 2, (ymin + ymax) / 2, board.thickness_m / 2)
        view.view_distance = extent * 1.5  # the whole board fits a 50 mm lens view
        space.clip_start = VIEW_CLIP_START_M
    board.framed_board = board.board_name


# --- Layer data -----------------------------------------------------------------------------

def _ring_next(starts, count):
    """Each ring point's next point: the following one, and each ring's last its first."""
    following = np.arange(1, count + 1, dtype=np.int32)
    following[starts[1:] - 1] = starts[:-1]
    return following


def _ring_edges(mesh, arrays):
    """Input contours only: Geometry Nodes creates the fill surfaces."""
    points = arrays["points"]
    starts = arrays["ring_start"]
    count = len(points)
    mesh.clear_geometry()
    mesh.vertices.add(count)
    mesh.edges.add(count)
    if count:
        coords = np.zeros((count, 3), dtype=np.float32)
        coords[:, :2] = transform.xy_m(points, board.origin_nm)
        edges = np.column_stack((np.arange(count, dtype=np.int32), _ring_next(starts, count)))
        mesh.vertices.foreach_set("co", coords.ravel())
        mesh.edges.foreach_set("vertices", edges.ravel())
    ring_sizes = np.diff(starts)
    write_attribute(mesh, "item", "INT", np.repeat(arrays["ring_item"], ring_sizes))
    write_attribute(mesh, "hole", "INT", np.repeat(arrays["ring_hole"], ring_sizes).astype(np.int32))
    mesh.update()


def _place_copper(obj, header, layer, kind, group):
    """Height, ids and the copper modifier of one layer's copper object."""
    z, thickness = copper_placement(layer, kind)
    obj.location.z = z
    obj["kls_ids"] = header["ids"]
    obj["kls_copper"] = (layer, kind)
    set_modifier(obj, board.groups[group], materials.layer_material(layer),
                 {"Thickness": thickness, "Up": outward(layer)})
    board.touched.add(obj.name)


def _segment_edges(mesh, seg, item):
    """Segments (x1, y1, x2, y2, width in nm) as edges with width and item attributes,
    for the KLS_Tracks group."""
    count = len(seg)
    mesh.clear_geometry()
    mesh.vertices.add(count * 2)
    mesh.edges.add(count)
    if count:
        coords = np.zeros((count * 2, 3), dtype=np.float32)
        ends = np.empty((count * 2, 2), dtype=np.int32)
        ends[0::2] = seg[:, :2]
        ends[1::2] = seg[:, 2:4]
        coords[:, :2] = transform.xy_m(ends, board.origin_nm)
        mesh.vertices.foreach_set("co", coords.ravel())
        mesh.edges.foreach_set("vertices", np.arange(count * 2, dtype=np.int32))
    write_attribute(mesh, "width", "FLOAT", np.repeat(seg[:, 4].astype(np.float64) * 1e-9, 2).astype(np.float32))
    write_attribute(mesh, "item", "INT", np.repeat(item, 2))
    mesh.update()


def _apply_tracks(header, arrays):
    """Straight segments (arcs arrive sampled) as edges with a width attribute."""
    layer = header["layer"]
    obj = owned_object(f"KLS {layer} tracks")
    _segment_edges(obj.data, arrays["seg"], arrays["item"])
    _place_copper(obj, header, layer, "tracks", "tracks")
    highlight.refresh(layer, "tracks")


def _apply_pads(header, arrays):
    layer = header["layer"]
    obj = owned_object(f"KLS {layer} pads")
    _ring_edges(obj.data, arrays)
    _place_copper(obj, header, layer, "pads", "fill")
    _apply_drills(header, arrays)
    highlight.refresh(layer, "pads")  # pads on a highlighted net follow edits


def _apply_graphics(header, arrays):
    """Copper drawn as graphics (net-tie bridges, rings around holes): filled like pads."""
    layer = header["layer"]
    obj = owned_object(f"KLS {layer} graphics")
    _ring_edges(obj.data, arrays)
    _place_copper(obj, header, layer, "pads", "fill")


def _apply_drills(header, arrays):
    """Pad drills: one wall per hole through the whole board, built from the F.Cu
    frame (every drilled pad, plated or not, rides on F.Cu; other layers' frames
    repeat the same holes). The hole itself is see-through (holes.py)."""
    if header["layer"] != "F.Cu":
        return
    active = set()
    hole_rows = []
    for row, index, angle, oval, plated in zip(arrays["drill"], arrays["drill_item"], arrays["drill_angle"],
                                               arrays["drill_oval"], arrays["drill_plated"]):
        pad_id = header["ids"][int(index)]
        obj = owned_object(f"KLS F.Cu drill {pad_id}")
        obj["kls_drill"] = 1
        single_point(obj.data)
        x, y = (float(v) for v in transform.xy_m(np.asarray(row[:2], dtype=np.int32).reshape(1, 2),
                                                  board.origin_nm)[0])
        obj.location = (x, y, 0.0)
        obj.rotation_euler.z = float(angle)
        width, height = (float(value) * 1e-9 for value in row[2:4])
        if not oval:
            width = height = min(width, height)
        # Plated holes: copper wall with the finish; np_thru_hole: bare laminate, or on an IMS
        # board the metal base (all but its thin epoxy).
        bare = "ims_wall" if board.ims is not None else "board_core"
        inset = 2 * PLATED_WALL_INSET_M if plated else 0.0  # off a board edge that follows the drill
        set_modifier(obj, board.groups["drills"], "plating" if plated else bare,
                     {"Width": width - inset, "Height": height - inset, "Depth": board.thickness_m})
        _smooth_walls(obj, not plated and board.ims is not None)
        obj["kls_plated"] = bool(plated)
        hole_rows.append((x, y, width, height, float(angle), bool(oval)))
        obj["kls_id"] = pad_id
        set_visible(obj, True)
        active.add(obj.name)
        board.touched.add(obj.name)
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_drill") and obj.name not in active:
            obj.data.clear_geometry()
            set_visible(obj, False)
    rows = np.asarray(hole_rows, dtype=np.float64).reshape(-1, 6)
    holes.set_pads(rows[:, :2], rows[:, 2:4], rows[:, 4], rows[:, 5])


def _apply_solder(header, arrays):
    """Stencil apertures (KiCad's pad shape on F.Paste/B.Paste) as solder deposits."""
    layer = header["layer"]
    obj = owned_object(f"KLS {layer} solder")
    _ring_edges(obj.data, arrays)
    obj["kls_ids"] = header["ids"]
    obj["kls_paste_side"] = layer[0]
    _place_solder(obj)
    board.touched.add(obj.name)


def _place_solder(obj):
    """Deposits start on the outer copper surface of their side's pads."""
    top = obj["kls_paste_side"] == "F"
    obj.location.z = transform.copper_z("F.Cu" if top else "B.Cu", "drills", board.heights)  # above pads
    set_modifier(obj, board.groups["solder"], "solder",
                 {"Thickness": stencil_thickness() * (1 if top else -1), "Top Scale": SOLDER_TOP_SCALE})
    set_visible(obj, getattr(bpy.context.scene, "kileido_show_solder", False))


def _zone_arrays(arrays, index):
    """The rings of zone `index` alone, re-based to start at 0."""
    starts = arrays["ring_start"]
    ring_indices = np.flatnonzero(arrays["ring_item"] == index)
    if not len(ring_indices):
        return {"points": np.empty((0, 2), dtype=np.int32), "ring_start": np.array([0], dtype=np.int32),
                "ring_item": np.empty(0, dtype=np.int32), "ring_hole": np.empty(0, dtype=np.uint8)}
    first, last = int(ring_indices[0]), int(ring_indices[-1])
    if last - first + 1 != len(ring_indices):
        raise ValueError("zone rings for one item must be consecutive")
    point_start, point_end = int(starts[first]), int(starts[last + 1])
    return {"points": arrays["points"][point_start:point_end],
            "ring_start": starts[first:last + 2] - point_start,
            "ring_item": np.zeros(len(ring_indices), dtype=np.int32),
            "ring_hole": arrays["ring_hole"][first:last + 1]}


def _apply_zones(header, arrays):
    """One object per zone, so an edited zone rebuilds alone (zones are the heaviest geometry)."""
    layer = header["layer"]
    active = set()
    for index, item_id in enumerate(header["ids"]):
        obj = owned_object(f"KLS {layer} zone {item_id}")
        obj["kls_zone_layer"] = layer
        item_arrays = _zone_arrays(arrays, index)
        fingerprint = hashlib.blake2b(digest_size=16)
        fingerprint.update(np.asarray(board.origin_nm, dtype=np.int64).tobytes())
        for key in ("points", "ring_start", "ring_hole"):
            fingerprint.update(item_arrays[key].tobytes())
        digest = fingerprint.hexdigest()
        if obj.get("kls_geometry_hash") != digest:
            _ring_edges(obj.data, item_arrays)
            obj["kls_geometry_hash"] = digest
        _place_copper(obj, {"ids": [item_id]}, layer, "zones", "fill_single")
        obj["kls_zone_highlight"] = ""  # copper again; highlight.refresh re-applies it
        set_visible(obj, True)
        active.add(obj.name)
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_zone_layer") == layer and obj.name not in active:
            obj.data.clear_geometry()
            set_visible(obj, False)
            obj["kls_geometry_hash"] = ""
    highlight.refresh(layer, "zones")


def _apply_vias(header, arrays):
    obj = owned_object("KLS vias")
    via = arrays["via"]
    count = len(via)
    coords = np.zeros((count, 3), dtype=np.float32)
    if count:
        coords[:, :2] = transform.xy_m(via[:, :2], board.origin_nm)
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(count)
    if count:
        mesh.vertices.foreach_set("co", coords.ravel())
    write_attribute(mesh, "diameter", "FLOAT", (via[:, 2].astype(np.float64) * 1e-9).astype(np.float32))
    write_attribute(mesh, "drill", "FLOAT", (via[:, 3].astype(np.float64) * 1e-9).astype(np.float32))
    names = header["layers"]
    layer_z = np.asarray([board.heights[name] for name in names], dtype=np.float32)
    spans = layer_z[arrays["span"]] if count else np.empty((0, 2), dtype=np.float32)
    # Via lands sit 3 um outside their copper surface, above pads (2 um) and tracks:
    # a land exactly level with the zone it connects to renders as coplanar noise.
    land = transform.copper_z("F.Cu", "drills", {"F.Cu": 0.0})  # above pads: via-in-pad
    write_attribute(mesh, "z_top", "FLOAT", spans.max(axis=1) + land if count else np.empty(0, np.float32))
    write_attribute(mesh, "z_bottom", "FLOAT", spans.min(axis=1) - land if count else np.empty(0, np.float32))
    write_attribute(mesh, "item", "INT", np.arange(count, dtype=np.int32))
    for flag, layer in (("outer_top", "F.Cu"), ("outer_bottom", "B.Cu")):  # its outer ends: tents, plugs, caps
        write_attribute(mesh, flag, "FLOAT", np.array([layer in (names[a], names[b]) for a, b in arrays["span"]],
                                                      dtype=np.float32) if count else np.empty(0, np.float32))
    protect = arrays.get("protect")  # KiCad's protection features (a bridge from before them: the board's rules)
    if protect is None or len(protect) != count:
        protect = np.full((count, len(protection.FIELDS)), protection.FROM_RULES, np.uint8)
    write_attribute(mesh, "protection", "INT", protection.pack(protect))
    # Its annular rings (kileido_bridge.via_rings): bit i on header["copper"][i]. A bridge from
    # before them: every layer. The cut draws them all; the outer lands only where ringed.
    copper = list(header.get("copper") or names)
    ringed = arrays.get("ringed")
    ringed = (ringed.view(np.uint32).astype(np.int64) if ringed is not None and len(ringed) == count
              else np.full(count, (1 << len(copper)) - 1, np.int64))
    write_attribute(mesh, "ringed", "INT", ringed.astype(np.uint32).view(np.int32))
    obj["kls_ring_layers"] = copper
    for flag, layer in (("ring_top", "F.Cu"), ("ring_bottom", "B.Cu")):
        bit = 1 << copper.index(layer) if layer in copper else 0
        write_attribute(mesh, flag, "FLOAT", ((ringed & bit) > 0).astype(np.float32) *
                        read_attribute(mesh, "outer_" + flag.split("_")[1], np.float32))
    mesh.update()
    obj.location.z = 0
    obj["kls_ids"] = header["ids"]
    set_modifier(obj, board.groups["vias"], "vias", {"Top Thickness": copper_thickness("F.Cu"),
                                                     "Bottom Thickness": copper_thickness("B.Cu"),
                                                     "Plating": via_plating(), "Land Lift": land,
                                                     **materials.via_inputs()})
    refresh_protection(highlights=False)
    board.touched.add(obj.name)
    _apply_via_rings(coords, via[:, 2].astype(np.float64) * 1e-9, [(names[a], names[b]) for a, b in arrays["span"]],
                     copper, ringed)
    highlight.refresh(kind="vias")


VIA_RINGS = "KLS vias rings"


def _apply_via_rings(xy, diameter, ends, copper, ringed):
    """A via's annular rings on the inner layers between its ends (the outer ones are its
    lands, nodes.vias): one point per ring, at that copper, drawn as flat disks
    (nodes.via_rings) that show in X-ray mode and inside a see-through board."""
    order = {name: i for i, name in enumerate(copper)}
    points, sizes, owners = [], [], []
    for index, (top, bottom) in enumerate(ends):
        if top not in order or bottom not in order:
            continue
        first, last = sorted((order[top], order[bottom]))
        for i in range(first + 1, last):
            if ringed[index] >> i & 1 and copper[i] in board.heights:
                points.append((*xy[index, :2], transform.copper_z(copper[i], "drills", board.heights)))
                sizes.append(diameter[index])
                owners.append(index)
    obj = owned_object(VIA_RINGS)
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(len(points))
    if points:
        mesh.vertices.foreach_set("co", np.asarray(points, np.float32).ravel())
    write_attribute(mesh, "diameter", "FLOAT", np.asarray(sizes, np.float32))
    write_attribute(mesh, "via", "INT", np.asarray(owners, np.int32))
    mesh.update()
    obj.location.z = 0
    set_modifier(obj, board.groups["via_rings"], "via_rings", {})  # inner copper: bare, never the finish
    board.touched.add(obj.name)


def _apply_outline(arrays):
    obj = owned_object(OUTLINE)
    _ring_edges(obj.data, arrays)
    _place_board(obj)
    if len(arrays["points"]):
        xy = transform.xy_m(arrays["points"], board.origin_nm)
        holes.set_bounds((*xy.min(axis=0), *xy.max(axis=0)), (xy, xy[_ring_next(arrays["ring_start"], len(xy))]))
    else:
        holes.clear_outline()  # no closed outline: nothing to clip copper to
    set_board_visible(getattr(bpy.context.scene, "kileido_show_board", True))
    board.touched.add(obj.name)
    cosmetics.refresh_geometry()
    _apply_outline_problem(arrays)


OUTLINE_PROBLEM = "KLS outline problem"
PROBLEM_LINE_NM = 300_000  # the Edge.Cuts lines, drawn red
PROBLEM_DOT_NM = 1_500_000  # a dot on each problem location
PROBLEM_MARGIN_M = 50e-6  # the red wall stands out of both board faces


def _apply_outline_problem(arrays):
    """A malformed outline: every Edge.Cuts line as a bright red wall through the
    board, a red dot where it goes wrong. X-ray mode is ticked so the rest fades
    (focus.refresh), and unticked again once the outline is fixed if it was off."""
    strokes = arrays.get("stroke", np.empty((0, 4), np.int32))
    problems = arrays.get("problem", np.empty((0, 2), np.int32))
    board.outline_problem = bool(len(strokes) or len(problems))
    obj = owned_object(OUTLINE_PROBLEM)
    board.touched.add(obj.name)
    scene = bpy.context.scene
    if not board.outline_problem:
        obj.data.clear_geometry()
        set_visible(obj, False)
        if board.xray_for_outline:  # ticked for the fault: untick now it is fixed
            board.xray_for_outline = False
            scene.kileido_focus = False
        focus.refresh()
        return
    if not getattr(scene, "kileido_focus", True):  # the red outline, with everything else faded
        board.xray_for_outline = True
        scene.kileido_focus = True
    seg = np.vstack([np.column_stack([strokes, np.full(len(strokes), PROBLEM_LINE_NM)]),
                     np.column_stack([problems, problems, np.full(len(problems), PROBLEM_DOT_NM)])]).astype(np.int32)
    _segment_edges(obj.data, seg, np.zeros(len(seg), np.int32))
    obj.location.z = -PROBLEM_MARGIN_M
    set_modifier(obj, board.groups["tracks"], "highlight_outline",
                 {"Thickness": board.thickness_m + 2 * PROBLEM_MARGIN_M, "Up": 1.0})
    set_visible(obj, True)
    focus.refresh()


def _place_board(obj):
    """The dielectric between the outer copper layers' inner faces."""
    top, bottom = laminate_faces()
    clearance = min(BOARD_FACE_CLEARANCE_M, (top - bottom) / 4)
    obj.location.z = top - clearance
    set_modifier(obj, board.groups["board"], "board",
                 {"Thickness": -(top - bottom - 2 * clearance),
                  "Bottom Material": board.materials["board_bottom"],
                  "Core Material": board.materials["board_edge"]})
    obj.update_tag()
    _place_ims_base(obj)


IMS_BASE = "KLS IMS base"


def _smooth_walls(obj, on):
    """Smooth shading after an object's own modifier (on), or none: flat facets streak a
    reflective metal wall, the IMS base's and its bare drill walls."""
    smooth = board.groups["smooth_walls"]
    modifier = obj.modifiers.get(smooth.name)
    if on and modifier is None:
        obj.modifiers.new(smooth.name, "NODES").node_group = smooth
    elif not on and modifier is not None:
        obj.modifiers.remove(modifier)


def _place_ims_base(outline):
    """The IMS metal base: the outline's fill (sharing its mesh), from the board's bottom
    up to the dielectric. Gone again when IMS mode is off."""
    existing = board.collection.all_objects.get(IMS_BASE)
    if board.ims is None:
        if existing is not None:
            bpy.data.objects.remove(existing)
        return
    obj = existing or owned_object(IMS_BASE)
    if obj.data != outline.data:
        own = obj.data
        obj.data = outline.data
        if own.users == 0:
            bpy.data.meshes.remove(own)
    z0, z1 = board.ims.base
    clearance = min(BOARD_FACE_CLEARANCE_M, (z1 - z0) / 4)  # its top just under the dielectric's bottom face
    obj.location.z = z1 - clearance
    metal = board.materials["ims_base"]
    set_modifier(obj, board.groups["board"], metal,
                 {"Thickness": -(z1 - z0 - clearance), "Bottom Material": metal, "Core Material": metal})
    _smooth_walls(obj, True)  # a round cutout's wall without facets
    set_visible(obj, getattr(bpy.context.scene, "kileido_show_board", True))
    board.touched.add(obj.name)
    obj.update_tag()


# --- Panel settings -------------------------------------------------------------------------

def set_color_mode(mode):
    """Recolour board materials and overlays for a colour mode ("FAB", "EDITOR", "REALISTIC")."""
    materials.set_color_mode(mode)
    if board.materials:
        cosmetics.recolor()
    lighting.apply_settings()  # the softboxes dim for a light mask colour


def set_board_visible(visible):
    if board.collection is None:
        return
    for name in (OUTLINE, IMS_BASE):
        obj = board.collection.all_objects.get(name)
        if obj is not None:
            set_visible(obj, visible)
    cut.invalidate()  # the section has no laminate while the board solid is hidden
    edge_plating.refresh()  # the plated edge goes with the board solid


def _fill_area(obj):
    """Area (m2) an object's rings enclose (zones, the outline)."""
    mesh = obj.data
    if not len(mesh.vertices) or "hole" not in mesh.attributes:
        return 0.0
    following = np.empty(len(mesh.vertices), np.int64)
    edges = read_edges(mesh)
    following[edges[:, 0]] = edges[:, 1]
    return ims.fill_area(read_coordinates(mesh)[:, :2], following, read_attribute(mesh, "hole", np.int32) > 0)


def refresh_ims_warnings():
    """What an IMS board cannot have, for the panel: vias and plated holes (they would
    reach the metal base) and copper of B.Cu's own."""
    board.ims_warnings = []
    if board.ims is None or board.collection is None:
        return
    objects = board.collection.all_objects
    vias = objects.get("KLS vias")
    plated = {obj["kls_id"] for obj in objects
              if obj.get("kls_drill") and obj.get("kls_plated") and len(obj.data.vertices)}
    routed = 0
    for name in ("KLS B.Cu tracks", "KLS B.Cu pads", "KLS B.Cu graphics"):
        obj = objects.get(name)
        if obj is not None and len(obj.data.vertices):
            # Tracks by segment; pads and graphics by item, a through-hole pad's B.Cu land not counted.
            routed += len(obj.data.edges) if name.endswith("tracks") else len(set(obj.get("kls_ids", ())) - plated)
    zones = sum(_fill_area(obj) for obj in objects if obj.get("kls_zone_layer") == "B.Cu")
    outline = objects.get(OUTLINE)
    bottom = ims.bottom_copper(routed, zones, _fill_area(outline) if outline is not None else 0.0)
    board.ims_warnings = ims.warnings(len(vias.data.vertices) if vias is not None else 0, len(plated), bottom)


def refresh_ims_metal():
    """The IMS base's metal changed: its colour in 3D and in the cut."""
    materials.paint_ims_base()
    laminate.update_bands()
    cut.invalidate()


def refresh_ims_finish():
    """The IMS base's finish changed: its two materials only (the cut is always polished)."""
    materials.paint_ims_base()


def refresh_protection(highlights=True):
    """Resolve each via's protection (KiCad's, with the board's rules and the panel's Via
    fill, Max tent hole and Via wall; protection.py) into the point attributes nodes.vias
    draws (plugs, fills, caps, tents, bare or finished barrels), and draw the vias that are
    holes into the hole mask, on the sides they open on."""
    if board.collection is None:
        return
    vias = board.collection.all_objects.get("KLS vias")
    if vias is None or "protection" not in vias.data.attributes:
        return
    mesh = vias.data
    scene = bpy.context.scene
    drill = read_attribute(mesh, "drill", np.float32).astype(np.float64)
    bore = np.maximum(drill - 2 * via_plating(), 0.2 * drill)  # the finished hole, as nodes.vias draws it
    found = protection.resolve(read_attribute(mesh, "protection", np.int32), board.appearance.get("via_rules"),
                               bore, read_attribute(mesh, "outer_top", np.float32) > 0.5,
                               read_attribute(mesh, "outer_bottom", np.float32) > 0.5,
                               float(getattr(scene, "kileido_max_tent_mm", 0.3)) * 1e-3)
    copper_fill = getattr(scene, "kileido_via_fill_material", "RESIN") == "COPPER"
    for name, values in (("core_top", found["core_top"]), ("core_bottom", found["core_bottom"]),
                         ("fill_copper", found["filled"] & copper_fill), ("plug_ink", found["plugged"]),
                         ("cap", found["capped"] * CAP_PLATING_M), ("drilled", found["drilled"]),
                         ("tent_top", found["tent_top"]), ("tent_bottom", found["tent_bottom"]),
                         ("bare_barrel", ~found["finished"])):
        write_attribute(mesh, name, "FLOAT", np.asarray(values, np.float32))
    mesh.update()
    board.via_too_big = int(found["too_big"].sum())
    xy = read_coordinates(mesh)[:, :2] if len(mesh.vertices) else np.empty((0, 2))
    drilled = found["drilled"]  # rings; tents and cores close them
    holes.set_vias(xy[drilled], bore[drilled], found["side"][drilled])
    if highlights:
        highlight.refresh(kind="vias")
    cut.invalidate()


def refresh_plating():
    """The panel's Via wall: the barrels, the holes inside them, and the cut."""
    if board.collection is None:
        return
    vias = board.collection.all_objects.get("KLS vias")
    if vias is not None and vias.modifiers:
        set_node_input(vias, "Plating", via_plating())
    refresh_protection()


def refresh_solder():
    """Tick box or stencil thickness changed: no geometry is resent."""
    if board.collection is None:
        return
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_paste_side"):
            _place_solder(obj)


def refresh_thickness():
    """Copper-thickness toggle: re-place copper, board and mask without new data."""
    if board.collection is None:
        return
    for obj in tuple(board.collection.all_objects):
        placement = obj.get("kls_copper")
        if placement:
            z, thickness = copper_placement(*placement)
            obj.location.z = z
            set_node_input(obj, "Thickness", thickness)
            obj.update_tag()
    vias = board.collection.all_objects.get("KLS vias")
    if vias is not None and vias.modifiers:
        set_node_input(vias, "Top Thickness", copper_thickness("F.Cu"))
        set_node_input(vias, "Bottom Thickness", copper_thickness("B.Cu"))
    outline = board.collection.all_objects.get(OUTLINE)
    if outline is not None:
        _place_board(outline)
    holes.refresh_sides()  # which heights are the board's top and bottom side
    highlight.refresh()
    cosmetics.recolor()
    cut.invalidate()  # the section's lands stand out of the copper as far as the 3D pads
