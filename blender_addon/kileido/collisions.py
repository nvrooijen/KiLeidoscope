"""Collisions between boards: the live board and the view-only boards beside it.

Each board is its components and its board solid. A component is an oriented box
(its 3D model parts' bounds in the footprint frame, else the placeholder envelope);
a board solid is its outline polygon, cutouts included, between its faces. Only
different boards are tested: component against component (separating axes),
component against board (the box's outline in the board's plane against the board
polygon), and board against board. A red box marks each overlap.

Runs a moment after the boards stop changing (a moved view-only board, a live edit),
only while a view-only board exists and the panel's check is on. The same moment
refits the studio softboxes over all boards (lighting.fit_to_boards).
"""

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import lighting, packages
from .objects import OUTLINE, hide, read_coordinates, read_edges
from .placement import PLACEHOLDER_HEIGHT_M
from .state import board

COLLECTION = "KiLeidoscope collisions"
MATERIAL = "KiLeidoscope collision"
TOLERANCE_M = 20e-6  # parts touching (a connector seated on a board) are not a collision
DELAY_S = 0.3
MAX_BOXES = 200

_result = {"count": 0, "checked": False}


# --- What a board is made of ----------------------------------------------------------------

class Solid:
    """One board's components (oriented boxes) and its board solid (outline polygon)."""

    def __init__(self, collection):
        self.collection = collection
        centers, axes, halves = [], [], []
        for low, high, frame in _components(collection):
            center, rotation, scale = frame.decompose()
            matrix = rotation.to_matrix()
            local_center = (np.asarray(low) + np.asarray(high)) / 2
            half = np.abs(np.asarray(high) - np.asarray(low)) / 2 * np.abs(np.asarray(scale)) - TOLERANCE_M
            centers.append(tuple(frame @ Vector(tuple(local_center))))
            axes.append(np.array(matrix))  # columns: the box's x, y, z in world space
            halves.append(np.maximum(half, 0.0))
        self.centers = np.array(centers, np.float64).reshape(-1, 3)
        self.axes = np.array(axes, np.float64).reshape(-1, 3, 3)
        self.halves = np.array(halves, np.float64).reshape(-1, 3)
        corners = _box_corners(self.centers, self.axes, self.halves)
        self.low, self.high = corners.min(axis=1), corners.max(axis=1)

        self.outline = next((obj for obj in collection.all_objects
                             if (obj.name == OUTLINE or obj.name.endswith(" " + OUTLINE)) and not obj.hide_get()),
                            None)
        self.edges = None
        if self.outline is not None and len(self.outline.data.vertices):
            xy = read_coordinates(self.outline.data)[:, :2].astype(np.float64)
            self.edges = xy[read_edges(self.outline.data)]  # (e, 2 ends, xy) in the outline's frame
            depsgraph = bpy.context.evaluated_depsgraph_get()
            box = np.array([tuple(corner) for corner in self.outline.evaluated_get(depsgraph).bound_box])
            self.z_range = (box[:, 2].min() + TOLERANCE_M, box[:, 2].max() - TOLERANCE_M)
            self.frame = np.array(self.outline.matrix_world)
            self.to_local = np.linalg.inv(self.frame)
            local = np.array([(x, y, z) for x in box[[0, -1], 0] for y in box[[0, -1], 1] for z in self.z_range])
            world = local @ self.frame[:3, :3].T + self.frame[:3, 3]
            self.slab_low, self.slab_high = world.min(axis=0), world.max(axis=0)


def _components(collection):
    """(local low, local high, footprint matrix) of every shown component."""
    parts = {}
    for obj in collection.all_objects:
        if obj.get("kls_model_fp_id") is not None and obj.type == "MESH" and not obj.hide_get():
            parts.setdefault(obj["kls_model_fp_id"], []).append(obj)
    placeholders = {obj.get("kls_footprint_id"): obj for obj in collection.all_objects
                    if obj.get("kls_footprint_placeholder") == 1 and not obj.hide_get()}
    for footprint in collection.all_objects:
        if footprint.get("kls_footprint") != 1 or not footprint.get("kls_active", 1):
            continue
        footprint_id = footprint.get("kls_id")
        frame = footprint.matrix_world
        to_local = frame.inverted()
        corners = [to_local @ (part.matrix_world @ Vector(corner))
                   for part in parts.get(footprint_id, ()) for corner in part.bound_box]
        if corners:
            points = np.array([tuple(corner) for corner in corners])
            yield points.min(axis=0), points.max(axis=0), frame
            continue
        box = placeholders.get(footprint_id)
        if box is None or "kls_placeholder_width_m" not in box:
            continue
        center = np.array(tuple(to_local @ box.matrix_world.translation))
        half = np.array((box["kls_placeholder_width_m"] / 2, box["kls_placeholder_height_m"] / 2, 0.0))
        low, high = center - half, center + half
        low[2], high[2] = 0.0, PLACEHOLDER_HEIGHT_M  # footprint frame: +z points away from the board
        yield low, high, frame


def _box_corners(centers, axes, halves):
    """(n, 8, 3) world corners of oriented boxes."""
    if not len(centers):
        return np.empty((0, 8, 3))
    signs = np.array([(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], np.float64)
    return centers[:, None, :] + np.einsum("nij,nkj->nki", axes, signs[None] * halves[:, None, :])


# --- Geometry tests -------------------------------------------------------------------------

def _boxes_overlap(a, i, b, j):
    """Separating-axis test of oriented boxes a[i] and b[j] (index arrays of equal length)."""
    if not len(i):
        return np.zeros(0, bool)
    ra, rb = a.axes[i], b.axes[j]  # (m, 3, 3): columns are axes
    ha, hb = a.halves[i], b.halves[j]
    t = b.centers[j] - a.centers[i]
    candidates = [ra[:, :, k] for k in range(3)] + [rb[:, :, k] for k in range(3)]
    candidates += [np.cross(ra[:, :, k], rb[:, :, l]) for k in range(3) for l in range(3)]
    separated = np.zeros(len(i), bool)
    for axis in candidates:
        length = np.linalg.norm(axis, axis=1)
        usable = length > 1e-9
        axis = axis / np.where(usable, length, 1.0)[:, None]
        reach_a = np.abs(np.einsum("mij,mi->mj", ra, axis) * ha).sum(axis=1)
        reach_b = np.abs(np.einsum("mij,mi->mj", rb, axis) * hb).sum(axis=1)
        separated |= usable & (np.abs(np.einsum("mi,mi->m", t, axis)) > reach_a + reach_b)
    return ~separated


def _inside(points, edges):
    """Even-odd test: which points lie in the board (outline and cutouts together)."""
    if not len(points) or edges is None or not len(edges):
        return np.zeros(len(points), bool)
    (x1, y1), (x2, y2) = edges[:, 0].T, edges[:, 1].T
    px, py = points[:, 0:1], points[:, 1:2]
    straddles = (y1 > py) != (y2 > py)
    with np.errstate(divide="ignore", invalid="ignore"):
        crossing = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
    return ((straddles & (px < crossing)).sum(axis=1) % 2) == 1


def _segments_cross(a, b):
    """Does any segment of a (n, 2, 2) properly cross any of b (m, 2, 2)?"""
    if not len(a) or not len(b):
        return False
    p, r = a[:, None, 0], (a[:, 1] - a[:, 0])[:, None]
    q, s = b[None, :, 0], (b[:, 1] - b[:, 0])[None]
    denominator = r[..., 0] * s[..., 1] - r[..., 1] * s[..., 0]
    qp = q - p
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (qp[..., 0] * s[..., 1] - qp[..., 1] * s[..., 0]) / denominator
        u = (qp[..., 0] * r[..., 1] - qp[..., 1] * r[..., 0]) / denominator
    return bool(np.any((np.abs(denominator) > 1e-18) & (t > 0) & (t < 1) & (u > 0) & (u < 1)))


def _hull(points):
    """Convex hull (counter-clockwise) of 2D points."""
    points = sorted(set(map(tuple, np.round(points, 12))))
    if len(points) < 3:
        return np.array(points)

    def half(sequence):
        chain = []
        for point in sequence:
            while len(chain) >= 2 and ((chain[-1][0] - chain[-2][0]) * (point[1] - chain[-2][1]) -
                                       (chain[-1][1] - chain[-2][1]) * (point[0] - chain[-2][0])) <= 0:
                chain.pop()
            chain.append(point)
        return chain
    return np.array(half(points)[:-1] + half(reversed(points))[:-1])


def _region_hits_board(corners_world, slab):
    """Does a solid with these world corners overlap the board solid `slab`?"""
    local = corners_world @ slab.to_local[:3, :3].T + slab.to_local[:3, 3]
    if local[:, 2].max() <= slab.z_range[0] or local[:, 2].min() >= slab.z_range[1]:
        return False
    hull = _hull(local[:, :2])
    if len(hull) < 3:
        return False
    ring = np.stack([hull, np.roll(hull, -1, axis=0)], axis=1)
    if _inside(hull, slab.edges).any() or _segments_cross(ring, slab.edges):
        return True
    vertices = slab.edges[:, 0]  # a board edge inside the part's outline
    to_next = ring[:, 1] - ring[:, 0]
    relative = vertices[None] - ring[:, None, 0]
    return bool(np.all(to_next[:, None, 0] * relative[..., 1] - to_next[:, None, 1] * relative[..., 0] >= 0,
                       axis=0).any())


def _boards_overlap(a, b):
    """Board solid against board solid (b's solid as its outline's corners, sampled)."""
    corners = b.edges[:, 0]
    world = [np.column_stack([corners, np.full(len(corners), z), np.ones(len(corners))]) @ b.frame.T
             for z in b.z_range]
    points = np.vstack(world)[:, :3]
    local = points @ a.to_local[:3, :3].T + a.to_local[:3, 3]
    within = (local[:, 2] > a.z_range[0]) & (local[:, 2] < a.z_range[1])
    if _inside(local[within, :2], a.edges).any():
        return True
    parallel = abs(float(a.frame[:3, 2] @ b.frame[:3, 2]) /
                   (np.linalg.norm(a.frame[:3, 2]) * np.linalg.norm(b.frame[:3, 2]))) > 0.999
    if parallel:  # stacked boards: z overlap and crossing outlines
        z = local[:, 2]
        if z.max() <= a.z_range[0] or z.min() >= a.z_range[1]:
            return False
        ends = b.edges.reshape(-1, 2)
        ends_world = np.column_stack([ends, np.zeros(len(ends)), np.ones(len(ends))]) @ b.frame.T
        ends_local = (ends_world[:, :3] @ a.to_local[:3, :3].T + a.to_local[:3, 3])[:, :2]
        return _segments_cross(ends_local.reshape(-1, 2, 2), a.edges)
    return _region_hits_board(np.vstack(world)[:, :3], a)  # at an angle: b's corners' outline


def _aabb_pairs(a, b):
    """Index pairs of a's and b's components whose world bounds overlap."""
    if not len(a.low) or not len(b.low):
        return np.zeros(0, int), np.zeros(0, int)
    overlap = np.all((a.low[:, None] < b.high[None]) & (b.low[None] < a.high[:, None]), axis=2)
    return np.nonzero(overlap)


# --- Running and showing --------------------------------------------------------------------

def boards():
    """Every board shown: the live one and the visible view-only ones."""
    found = [board.collection] if board.collection is not None and not board.collection.hide_viewport else []
    for root in packages.roots():
        collection = packages.collection_of(root[packages.ROOT_TAG])
        if collection is not None and not collection.hide_viewport:
            found.append(collection)
    return found


def find():
    """World (low, high) of every overlap between two different boards."""
    solids = [Solid(collection) for collection in boards()]
    regions = []
    for n, a in enumerate(solids):
        for b in solids[n + 1:]:
            i, j = _aabb_pairs(a, b)
            hit = _boxes_overlap(a, i, b, j)
            regions += [(np.maximum(a.low[p], b.low[q]), np.minimum(a.high[p], b.high[q]))
                        for p, q in zip(i[hit], j[hit])]
            for parts, slab in ((a, b), (b, a)):
                if slab.edges is None or not len(parts.low):
                    continue
                near = np.nonzero(np.all((parts.low < slab.slab_high) & (slab.slab_low < parts.high), axis=1))[0]
                corners = _box_corners(parts.centers[near], parts.axes[near], parts.halves[near])
                for index, box in zip(near, corners):
                    if _region_hits_board(box, slab):
                        regions.append((np.maximum(parts.low[index], slab.slab_low),
                                        np.minimum(parts.high[index], slab.slab_high)))
            if a.edges is not None and b.edges is not None and \
                    np.all((a.slab_low < b.slab_high) & (b.slab_low < a.slab_high)) and _boards_overlap(a, b):
                regions.append((np.maximum(a.slab_low, b.slab_low), np.minimum(a.slab_high, b.slab_high)))
    return regions


def glow_material(name=MATERIAL, color=(1.0, 0.05, 0.02), alpha=0.35, base=None, glow=0.6):
    """A translucent, glowing marker material (also proximity.py's). A dark `base` with a
    stronger `glow` keeps a colour saturated under the studio lights."""
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        # By type: node names are translated with "Translate New Data" on.
        shader = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        shader.inputs["Base Color"].default_value = (*(base or color), 1.0)
        shader.inputs["Emission Color"].default_value = (*color, 1.0)
        shader.inputs["Emission Strength"].default_value = glow
        shader.inputs["Alpha"].default_value = alpha
        material.surface_render_method = "BLENDED"
        material.diffuse_color = (*color, alpha)
    return material


def unit_cube(name="KiLeidoscope collision box", material=None):
    """One shared unit cube, found by name (a module-level reference would outlive a file load)."""
    cube = bpy.data.meshes.get(name) or bpy.data.meshes.new(name)
    if not len(cube.vertices):
        corners = [(x, y, z) for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)]
        faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        cube.from_pydata(corners, [], faces)
        cube.materials.append(material or glow_material())
    return cube


def clear():
    collection = bpy.data.collections.get(COLLECTION)
    if collection is not None:
        for obj in list(collection.objects):
            bpy.data.objects.remove(obj)
    _result.update(count=0, checked=False)


def run():
    """Check now and show a box on every overlap."""
    scene = bpy.context.scene
    if not getattr(scene, "kileido_collisions", True) or not packages.roots():
        clear()
        return 0
    regions = find()
    collection = bpy.data.collections.get(COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION)
        collection["kileido_owned_collisions"] = 1
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    boxes = list(collection.objects)
    for index, (low, high) in enumerate(regions[:MAX_BOXES]):
        if index < len(boxes):
            obj = boxes[index]
        else:
            obj = bpy.data.objects.new(f"KiLeidoscope collision {index + 1}", unit_cube())
            obj.hide_select = True
            collection.objects.link(obj)
        size = np.maximum(high - low, 0.05e-3)  # a touch point still gets a visible box
        obj.matrix_world = Matrix.Translation(Vector(tuple((low + high) / 2))) @ Matrix.Diagonal((*size, 1.0))
        hide(obj, False)
    for obj in boxes[len(regions):]:
        bpy.data.objects.remove(obj)
    _result.update(count=len(regions), checked=True)
    return len(regions)


def status():
    """Panel text: None before the first check."""
    if not _result["checked"]:
        return None
    count = _result["count"]
    return "No collisions" if not count else f"{count} collision{'s' if count != 1 else ''}"


def _timer():
    try:
        lighting.fit_to_boards()  # a moved board: the softboxes cover it again
        run()
    except Exception as exc:  # never leave the timer failing silently
        print(f"KiLeidoscope collision check failed: {exc}")
    return None


def schedule():
    """Check a moment after the boards stop changing."""
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    bpy.app.timers.register(_timer, first_interval=DELAY_S)


def _on_depsgraph(scene, depsgraph):
    """A board object moved or changed: check again. Not for what the check moves
    itself, its boxes and the studio softboxes ("KLS Studio softbox"): each check
    then scheduled the next, re-rendering the viewport ~3 times a second while idle."""
    if not packages.roots():
        return
    for update in depsgraph.updates:
        block = update.id
        if isinstance(block, bpy.types.Object) and not block.name.startswith("KiLeidoscope collision") and \
                not block.get("kls_studio_side") and \
                (block.name.startswith(("KLS ", "KV")) or block.get(packages.ROOT_TAG)):
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
