"""Headless Blender checks of the reflowed solder (solder.py) on the synthetic fixture board:
which metal counts, fillets up a lead, a dome where no metal is near, the solder each
aperture holds, through-hole joints, and building again only what changed.

The fixture's parts get stand-in models made here (boxes and a pin), coloured as KiCad's
library colours its metal, so no 3D model library is needed.

blender --background --factory-startup --python tests/blender/run_solder.py
"""

import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, palette, shading, solder, state  # noqa: E402
from kileido.objects import link_owned  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
J1, J3, J4 = "66666666-6666-4666-8666-666666666666", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", \
    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PIN_01 = (0.824, 0.820, 0.781)  # KiCad's library colour for tinned leads (sRGB)
BLACK = (0.148, 0.145, 0.145)  # its epoxy body


def frames():
    """The fixture with stencil apertures: J3's top pad and J4's bottom pad (0.6 x 1 mm)."""
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for pad in snapshot["pads"]:
        if pad["footprint_id"] == J3:
            pad["polygons"]["F.Paste"] = pad["polygons"]["F.Cu"]
        if pad["footprint_id"] == J4:
            pad["polygons"]["B.Paste"] = pad["polygons"]["B.Cu"]
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def material(name, srgb):
    made = bpy.data.materials.new(name)
    made.use_nodes = True
    principled = next(node for node in made.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    principled.inputs["Base Color"].default_value = (*shading.srgb_to_linear(srgb), 1.0)
    return made


def box(low, high):
    """A box's vertices and faces, from `low` to `high`."""
    (x0, y0, z0), (x1, y1, z1) = low, high
    vertices = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    return vertices, [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]


def part(name, footprint_id, side, low, high, made, role=None, sides=0, more=(), open_top=False, lean=None):
    """A stand-in model part of a footprint: a box from `low` to `high` (world metres), or a
    prism with `sides` sides about the box's vertical axis (a pin); `more`: further boxes
    (low, high) or (low, high, material) in the same mesh (a lid over a termination, a
    ceramic body). `open_top`: the box without its top face (fused into a body on it, as
    KiCad's STEP models come out). `lean`: the box's bottom moved this far (x, y) from its
    top (a snap-in leg slanting through a hole)."""
    if sides:
        centre = (np.asarray(low) + np.asarray(high)) / 2
        radius = (high[0] - low[0]) / 2
        ring = [(centre[0] + radius * math.cos(2 * math.pi * k / sides),
                 centre[1] + radius * math.sin(2 * math.pi * k / sides)) for k in range(sides)]
        vertices = [(x, y, low[2]) for x, y in ring] + [(x, y, high[2]) for x, y in ring]
        faces = [list(range(sides))[::-1], list(range(sides, 2 * sides))]
        faces += [[k, (k + 1) % sides, sides + (k + 1) % sides, sides + k] for k in range(sides)]
    else:
        vertices, faces = box(low, high)
        if open_top:
            faces = faces[:1] + faces[2:]
        if lean is not None:
            vertices = [(x + lean[0], y + lean[1], z) if index < 4 else (x, y, z)
                        for index, (x, y, z) in enumerate(vertices)]
    materials, slots = [made], [0] * len(faces)
    for more_low, more_high, *other in more:
        more_vertices, more_faces = box(more_low, more_high)
        faces = [*faces, *[[len(vertices) + index for index in face] for face in more_faces]]
        vertices = [*vertices, *more_vertices]
        if other and other[0] not in materials:
            materials.append(other[0])
        slots += [materials.index(other[0]) if other else 0] * len(more_faces)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    for material_ in materials:
        mesh.materials.append(material_)
    mesh.polygons.foreach_set("material_index", slots)
    obj = bpy.data.objects.new(name, mesh)
    link_owned(obj, "components")
    obj["kls_model_fp_id"] = footprint_id
    obj["kls_side"] = side
    if role is not None:
        obj["kls_role"] = role
    state.board.model_objects_by_fp.setdefault(footprint_id, []).append(obj.name)  # bound, as models.bind_root does
    state.board.model_bound.add(footprint_id)
    return obj


def coordinates(obj):
    co = np.empty(len(obj.data.vertices) * 3, np.float64)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3) @ np.array(obj.matrix_world)[:3, :3].T + np.array(obj.matrix_world)[:3, 3]


def signed_volume(obj):
    """The closed mesh's volume (m^3): positive when its faces turn outward."""
    co = coordinates(obj)
    obj.data.calc_loop_triangles()
    corners = np.empty(len(obj.data.loop_triangles) * 3, np.int64)
    obj.data.loop_triangles.foreach_get("vertices", corners)
    tris = co[corners.reshape(-1, 3)]
    return float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6)


def build(paste):
    reflowed = state.board.collection.all_objects[solder.NAME.format(layer=f"{paste['kls_paste_side']}.Paste")]
    solder.build(paste, reflowed)
    return reflowed, solder.last_build()


def main():
    assert bpy.app.background
    kileido.register()
    scene = bpy.context.scene
    apply.load_frames(frames())
    board = state.board
    objects = board.collection.all_objects
    top, bottom = objects["KLS F.Paste solder"], objects["KLS B.Paste solder"]

    # Metal: the library's metal colours; a parts pack's roles first
    tin, epoxy = material("stand-in tin", PIN_01), material("stand-in epoxy", BLACK)
    assert palette.is_metal(tin) and not palette.is_metal(epoxy)

    # Off: nothing is built
    solder.refresh()
    assert not any(obj.get("kls_reflowed_side") for obj in objects)
    # As printed: the paste, no reflowed solder
    scene.kileido_solder_reflowed = False
    scene.kileido_show_solder = True
    solder.refresh()
    assert top.visible_get() and not any(obj.get("kls_reflowed_side") for obj in objects)

    # Reflowed: the printed paste gives way (built at once: no timers in background mode)
    scene.kileido_solder_reflowed = True
    assert not solder.pending()
    assert not top.visible_get() and not bottom.visible_get()
    flowed_top = objects[solder.NAME.format(layer="F.Paste")]
    flowed_bottom = objects[solder.NAME.format(layer="B.Paste")]
    assert flowed_top.visible_get() and flowed_bottom.visible_get()
    assert flowed_top.data.materials[0] == board.materials["solder_reflowed"]

    # No part over either pad yet: a dome holding the aperture's solder
    _, stats = build(top)
    assert stats["apertures"] == 1 and stats["wetted"] == 0, stats
    pad = coordinates(top)[:, :2]
    low, high = pad.min(axis=0), pad.max(axis=0)
    area = float(np.prod(high - low))
    held = area * solder.stencil_thickness() * solder.SOLDER_FRACTION
    volume = signed_volume(flowed_top)
    assert abs(volume - held) < 0.03 * held, (volume, held)
    dome = coordinates(flowed_top)[:, 2] - top.location.z
    assert 0 < dome.max() <= solder.MAX_DOME_M

    # A termination over the pad's left half (0.3 mm high): solder climbs it, falling off
    # towards the pad's far edge, never above the metal
    seat = top.location.z - 3e-6  # the copper's face (the paste sits 3 um over it)
    middle = (low[0] + high[0]) / 2
    height = 0.3e-3
    end = part("J3 end", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat),
               (middle, high[1] + 0.1e-3, seat + height), tin)
    bpy.context.view_layer.update()
    _, stats = build(top)
    assert stats["wetted"] == 1, stats
    co = coordinates(flowed_top)
    rise = co[:, 2] - top.location.z
    free = co[:, 0] > middle + 0.02e-3
    assert rise.max() <= height + 1e-6, rise.max()
    near = free & (co[:, 0] < middle + 0.08e-3)
    far = free & (co[:, 0] > high[0] - 0.06e-3)
    assert rise[near].max() > 0.1e-3 and rise[near].max() > 2 * rise[far].max(), (rise[near].max(), rise[far].max())
    assert signed_volume(flowed_top) > 0

    # Roles: a pack's case is never metal, its contacts always are
    end["kls_role"] = "case"
    assert build(top)[1]["wetted"] == 0
    end["kls_role"] = "contacts"
    end.data.materials[0] = epoxy
    assert build(top)[1]["wetted"] == 1
    del end["kls_role"]
    end.data.materials[0] = tin
    assert build(top)[1]["revisited"] == 1  # the part changed again

    # Built again only where something changed, and the same as built from scratch
    stats = build(top)[1]
    assert stats["computed"] == 0 and stats["revisited"] == 0 and not stats["rewritten"], stats
    end.location.x += 0.05e-3  # the part alone: its solder is worked out again
    bpy.context.view_layer.update()
    stats = build(top)[1]
    assert stats["computed"] == 1 and stats["revisited"] == 1, stats
    edited = coordinates(flowed_top)
    solder.forget()
    build(top)
    assert np.allclose(coordinates(flowed_top), edited, atol=1e-9)
    # The footprint moved (part and pad together, as KiCad sends it): the same solder,
    # moved; only the points' positions are written
    co = np.empty(len(top.data.vertices) * 3, np.float32)
    top.data.vertices.foreach_get("co", co)
    top.data.vertices.foreach_set("co", (co.reshape(-1, 3) + (0.1e-3, 0.0, 0.0)).ravel())
    top.data.update()
    end.location.x += 0.1e-3
    bpy.context.view_layer.update()
    stats = build(top)[1]
    assert stats["computed"] == 0 and stats["revisited"] == 1 and not stats["rewritten"], stats
    assert np.allclose(coordinates(flowed_top), edited + (0.1e-3, 0.0, 0.0), atol=1e-7)

    # Underneath: metal further out than MAX_GAP_M (a toroid's winding) is not wetted;
    # a lead just under the pad is
    pad = coordinates(bottom)[:, :2]
    low, high = pad.min(axis=0), pad.max(axis=0)
    seat = bottom.location.z + 3e-6
    plate = part("J4 winding", J4, "bottom", (low[0], low[1], seat - 1.0e-3), (high[0], high[1], seat - 0.5e-3), tin)
    bpy.context.view_layer.update()
    reflowed, stats = build(bottom)
    assert stats["wetted"] == 0, stats
    assert signed_volume(reflowed) > 0  # faces outward underneath too
    assert (coordinates(reflowed)[:, 2] <= bottom.location.z + 1e-9).all()  # solder hangs down
    plate.location.z += 0.45e-3
    bpy.context.view_layer.update()
    assert build(bottom)[1]["wetted"] == 1

    # A crystal's termination (0.1 mm high, over the pad's left 30 %) under a metal lid over
    # the whole pad, in one mesh: the solder climbs the termination, not to the lid
    state.board.model_objects_by_fp[J3].remove(end.name)
    pad = coordinates(top)[:, :2]
    low, high = pad.min(axis=0), pad.max(axis=0)
    seat = top.location.z - 3e-6
    edge = low[0] + 0.3 * (high[0] - low[0])
    termination = 0.1e-3
    crystal = part("J3 crystal", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat), (edge, high[1] + 0.1e-3, seat + termination),
                   tin, more=[((low[0] - 0.3e-3, low[1] - 0.3e-3, seat + 0.4e-3), (high[0] + 0.3e-3, high[1] + 0.3e-3, seat + 0.45e-3))])
    bpy.context.view_layer.update()
    _, stats = build(top)
    rise = coordinates(flowed_top)[:, 2] - top.location.z
    assert stats["wetted"] == 1 and 0.5 * termination < rise.max() <= termination + 1e-6, (stats, rise.max())
    # A lower lid (0.25 mm up, in MAX_GAP_M's reach) on a ceramic body over the termination,
    # joined to it round the outside by a metal post (one piece of metal, as a crystal's
    # castellation joins pad and seal ring): out of the solder's reach through the ceramic
    state.board.model_objects_by_fp[J3].remove(crystal.name)
    wide = (low[0] - 0.3e-3, low[1] - 0.3e-3), (high[0] + 0.3e-3, high[1] + 0.3e-3)
    lid = [((*wide[0], seat + 0.25e-3), (*wide[1], seat + 0.3e-3)),
           ((*wide[0], seat), (low[0] - 0.2e-3, low[1] - 0.1e-3, seat + 0.25e-3))]  # the post: corners on both
    crystal = part("J3 crystal", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat), (edge, high[1] + 0.1e-3, seat + termination),
                   tin, more=[*lid, ((*wide[0], seat + termination), (*wide[1], seat + 0.25e-3), epoxy)])
    bpy.context.view_layer.update()
    _, stats = build(top)
    rise = coordinates(flowed_top)[:, 2] - top.location.z
    assert stats["wetted"] == 1 and 0.5 * termination < rise.max() <= termination + 1e-6, (stats, rise.max())
    # The termination's top fused into the body on it (no face there, as KiCad's STEP models
    # come out): it still reaches the top of that piece of metal
    state.board.model_objects_by_fp[J3].remove(crystal.name)
    crystal = part("J3 crystal", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat), (edge, high[1] + 0.1e-3, seat + termination),
                   tin, more=[((*wide[0], seat + termination), (*wide[1], seat + 0.25e-3), epoxy)], open_top=True)
    bpy.context.view_layer.update()
    _, stats = build(top)
    rise = coordinates(flowed_top)[:, 2] - top.location.z
    assert stats["wetted"] == 1 and 0.5 * termination < rise.max() <= termination + 1e-6, (stats, rise.max())
    # The same, its metal running on past the pad in one piece up to 0.4 mm (a crystal's pads,
    # castellations and seal ring; a QFN's lead frame): the termination's own walls are its
    # height there, not that piece's tallest part
    state.board.model_objects_by_fp[J3].remove(crystal.name)
    corner = (low[0] - 0.2e-3, low[1] - 0.1e-3, seat + termination)  # the termination's top corner: one piece
    crystal = part("J3 crystal", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat), (edge, high[1] + 0.1e-3, seat + termination),
                   tin, more=[(corner, (high[0] + 1e-3, low[1] - 0.05e-3, seat + 0.4e-3))], open_top=True)
    bpy.context.view_layer.update()
    _, stats = build(top)
    rise = coordinates(flowed_top)[:, 2] - top.location.z
    assert stats["wetted"] == 1 and 0.5 * termination < rise.max() <= termination + 1e-6, (stats, rise.max())
    # A termination over all but the pad's last 0.04 mm: the solder in front barely climbs
    # it, no wall (under the termination it is hidden)
    state.board.model_objects_by_fp[J3].remove(crystal.name)
    crystal = part("J3 crystal", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat),
                   (high[0] - 0.04e-3, high[1] + 0.1e-3, seat + termination), tin)
    bpy.context.view_layer.update()
    _, stats = build(top)
    co = coordinates(flowed_top)
    rise = (co[:, 2] - top.location.z)[co[:, 0] > high[0] - 0.04e-3 + 1e-6]
    assert stats["wetted"] == 1 and 0 < rise.max() <= solder.MAX_SLOPE * 0.04e-3 + 1e-6, (stats, rise.max())
    # A QFN's short, thin lead (0.05 mm high, over the pad's left 20 %): what a fillet up to
    # it cannot hold goes under the part, never bulging past the lead
    state.board.model_objects_by_fp[J3].remove(crystal.name)
    edge = low[0] + 0.2 * (high[0] - low[0])
    lead = 0.05e-3
    thin = part("J3 lead", J3, "top", (low[0] - 0.2e-3, low[1] - 0.1e-3, seat), (edge, high[1] + 0.1e-3, seat + lead), tin)
    bpy.context.view_layer.update()
    _, stats = build(top)
    rise = coordinates(flowed_top)[:, 2] - top.location.z
    assert stats["wetted"] == 1 and 0.5 * lead < rise.max() <= lead + 1e-6, (stats, rise.max())
    state.board.model_objects_by_fp[J3].remove(thin.name)

    def rise_with(*made):
        bpy.context.view_layer.update()
        solder.forget()
        stats = build(top)[1]
        co = coordinates(flowed_top)
        for obj in made:
            state.board.model_objects_by_fp[J3].remove(obj.name)
        assert np.isfinite(co).all() and stats["wetted"] == 1, stats
        return (co[:, 2] - top.location.z).max()

    edge = low[0] + 0.3 * (high[0] - low[0])
    termination_box = (low[0] - 0.2e-3, low[1] - 0.1e-3, seat), (edge, high[1] + 0.1e-3, seat + termination)
    # A fused termination 0.5 mm high (a terminal block), its mesh turned so its own up is y (a
    # model rotated in its footprint, or drawn y-up): the same fillet as unturned
    tall = 0.5e-3
    rises = []
    for turn in (None, Matrix.Rotation(math.pi / 2, 4, "X")):
        block = part("J3 block", J3, "top", termination_box[0], (edge, high[1] + 0.1e-3, seat + tall), tin,
                     more=[((*wide[0], seat + tall), (*wide[1], seat + 0.7e-3), epoxy)], open_top=True)
        if turn is not None:
            block.data.transform(turn.inverted())
            block.matrix_world = turn
        rises.append(rise_with(block))
    assert rises[0] > 0.1e-3 and abs(rises[1] - rises[0]) < 0.02 * rises[0], rises
    # A big board's coarse grid (few points per aperture): the fused termination's top still found
    budget, solder.POINT_BUDGET = solder.POINT_BUDGET, 1
    rise = rise_with(part("J3 crystal", J3, "top", *termination_box, tin,
                          more=[((*wide[0], seat + termination), (*wide[1], seat + 0.25e-3), epoxy)], open_top=True))
    solder.POINT_BUDGET = budget
    assert 0.25 * termination < rise <= termination + 1e-6, rise  # (a coarse grid: the first points out lower)
    # The lower lid's ceramic body a separate object of the footprint (a model in several
    # objects): still out of the solder's reach
    rise = rise_with(part("J3 crystal", J3, "top", *termination_box, tin, more=lid),
                     part("J3 body", J3, "top", (*wide[0], seat + termination), (*wide[1], seat + 0.25e-3), epoxy))
    assert 0.5 * termination < rise <= termination + 1e-6, rise
    # A ceramic body running 0.04 mm into a closed termination 0.3 mm high (unfused solids): the
    # termination's own top still counts, as without the body
    cap = (low[0] - 0.2e-3, low[1] - 0.1e-3, seat), (middle, high[1] + 0.1e-3, seat + height)
    bare = rise_with(part("J3 cap", J3, "top", *cap, tin))
    ceramic = ((low[0] - 1.0e-3, low[1] - 0.08e-3, seat + 0.04e-3), (middle - 0.02e-3, high[1] + 0.08e-3, seat + height - 0.04e-3),
               epoxy)
    rise = rise_with(part("J3 cap", J3, "top", *cap, tin, more=[ceramic]))
    assert rise > 0.9 * bare, (rise, bare)
    # One pad with two paste windows, the termination over the first: solder in both, finite
    pad_id, ring, _ = solder._rings(top)[0]
    span = np.ptp(ring[:, 0])

    def window(start, end):
        x0, x1 = ring[:, 0].min() + start * span, ring[:, 0].min() + end * span
        return np.array(((x0, ring[:, 1].min()), (x1, ring[:, 1].min()), (x1, ring[:, 1].max()), (x0, ring[:, 1].max()))), False

    cover = part("J3 cap", J3, "top", *cap, tin)
    bpy.context.view_layer.update()
    made = solder._aperture([window(0.0, 0.4), window(0.6, 1.0)], 1.0, np.array(solder.poses.in_root(top), dtype=np.float64),
                            solder._parts(), solder.stencil_thickness() * solder.SOLDER_FRACTION, 400)
    state.board.model_objects_by_fp[J3].remove(cover.name)
    assert made is not None and made[3] and np.isfinite(made[0]).all()
    # Along a metal's edge a tall piece beside a thin plate (a crystal's corner piece on its
    # bottom plate): the height the fillet climbs to ramps up from the plate no steeper than
    # MAX_SLOPE, no step (no spike in the notch between them)
    along = np.column_stack((np.arange(41) * 10e-6, np.zeros(41)))
    climbs = solder._local_heights(along, np.where(along[:, 0] < 0.2e-3, 0.05e-3, 0.3e-3), np.ones(41, bool),
                                   np.ones(41, bool), 0.1e-3)
    assert np.abs(np.diff(climbs)).max() <= solder.MAX_SLOPE * 10e-6 + 1e-9 and climbs[-1] > 0.2e-3, climbs
    # How far in from the pad's edge a fillet may rise (`_soft_edge`): beside one long edge the
    # plain distance, across a narrow pad a smooth arch (no ridge, no tent down its middle)
    strip = np.array(((0.0, 0.0), (4e-3, 0.0), (4e-3, 0.3e-3), (0.0, 0.3e-3)))
    across = np.column_stack((np.full(31, 2e-3), np.linspace(0.0, 0.3e-3, 31)))
    soft = solder._soft_edge(across, strip, np.roll(strip, -1, axis=0))
    assert abs(soft[1] - 0.01e-3) < 0.1e-6 and np.abs(np.diff(np.diff(soft) / 0.01e-3)).max() < 0.5, soft

    # Through-hole: J1's plated hole (2 x 1 mm slot) with a pin from its part on top: the
    # barrel filled, a fillet under the board up the pin, a thinner one on top
    drill = next(obj for obj in objects if obj.get("kls_drill") and obj.get("kls_id", "").startswith("5555"))
    x, y = drill.location[:2]
    z_top, z_bottom = flowed_top.location.z, flowed_bottom.location.z
    joint = solder.THT_NAME
    radius = 0.3e-3
    pin = part("J1 pin", J1, "top", (x - radius, y - radius, z_bottom - 2e-3), (x + radius, y + radius, z_top + 4e-3),
               tin, sides=24)
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    co = coordinates(objects[joint])
    below, above = z_bottom - co[:, 2].min(), co[:, 2].max() - z_top
    assert 0.2e-3 < below <= solder.MAX_THT_CLIMB_M + 1e-9 and 0 < above < below, (below, above)
    assert signed_volume(objects[joint]) > 0
    inside = np.hypot(co[:, 0] - x, co[:, 1] - y)
    assert inside.min() < radius and inside.min() > radius - 0.05e-3  # the joint's inner wall, just in the pin
    # Round about the pin on the 8 x 4 mm pad: the fillet reaches as far every way (to the
    # pad's narrowest, 2 mm), the corners past it only a thin film
    fillet = co[:, 2] < z_bottom - 2 * solder.WET_FILM_M
    assert np.ptp(inside[fillet]) < 2.0e-3 and inside[fillet].max() <= 2.0e-3 + 1e-6, inside[fillet].max()
    corners = (co[:, 2] < 0.5 * (z_top + z_bottom)) & (inside > 2.0e-3 + 1e-6)
    assert corners.any() and np.allclose(co[corners, 2], z_bottom, atol=1e-9)  # the film tapers to the pad's edge
    # A part no longer bound (its footprint left, or waiting for the next export) holds no solder
    state.board.model_objects_by_fp[J1].remove(pin.name)
    solder.refresh()
    assert solder.last_build()["joints"] == 0
    state.board.model_objects_by_fp[J1].append(pin.name)
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    pin.location.x += 5e-3  # out of the hole: an open hole gets no solder
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 0
    # A blade (1.7 x 0.4 mm) on that pad: its fillet an even band round it, the corners rounded
    # (not its outline blown up from the middle, the corners reaching further)
    band = part("J1 band", J1, "top", (x - 0.85e-3, y - 0.2e-3, z_bottom - 2e-3), (x + 0.85e-3, y + 0.2e-3, z_top + 4e-3), tin)
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    outer = coordinates(objects[joint]).reshape(solder.THT_ANGLES, -1, 3)[:, 3, :2] - (x, y)  # the fillet's edge
    away = np.hypot(np.maximum(np.abs(outer[:, 0]) - 0.85e-3, 0.0), np.maximum(np.abs(outer[:, 1]) - 0.2e-3, 0.0))
    assert np.ptp(away) < 0.05 * away.max(), (away.min(), away.max())  # (the pin found at THT_ANGLES points)
    state.board.model_objects_by_fp[J1].remove(band.name)

    # A blade pin (1.7 x 0.4 mm) in the slot on a narrow pad (2.4 x 1.2 mm): it reaches past
    # the pad's narrowest (0.6 mm), and the fillet still runs in towards it, never back out
    for name in ("KLS F.Cu pads", "KLS B.Cu pads"):
        pads = objects[name]
        item = list(pads["kls_ids"]).index(drill["kls_id"])
        items = np.empty(len(pads.data.vertices), np.int32)
        pads.data.attributes["item"].data.foreach_get("value", items)
        co = np.empty(len(pads.data.vertices) * 3, np.float32)
        pads.data.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        ring = np.flatnonzero(items == item)
        corners = [(-1.2e-3, -0.6e-3), (1.2e-3, -0.6e-3), (1.2e-3, 0.6e-3), (-1.2e-3, 0.6e-3)]
        co[ring, :2] = np.array(corners)[:len(ring)] + (x, y)
        pads.data.vertices.foreach_set("co", co.ravel())
        pads.data.update()
    blade = part("J1 blade", J1, "top", (x - 0.85e-3, y - 0.2e-3, z_bottom - 2e-3), (x + 0.85e-3, y + 0.2e-3, z_top + 4e-3), tin)
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    profiles = coordinates(objects[joint]).reshape(solder.THT_ANGLES, -1, 3)
    out = np.hypot(profiles[..., 0] - x, profiles[..., 1] - y)[:, 3:4 + solder.THT_STEPS]  # its fillet, edge to pin
    assert (np.diff(out, axis=1) <= 1e-9).all(), np.diff(out, axis=1).max()
    assert signed_volume(objects[joint]) > 0
    # The blade beside the hole's centre (a shield tab, a bent leg: 0.05 mm off it): still a
    # joint, its inner wall on the blade
    blade.location.y += 0.25e-3
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    inner = coordinates(objects[joint]).reshape(solder.THT_ANGLES, -1, 3)[:, 3 + solder.THT_STEPS:5 + solder.THT_STEPS, :2]
    assert (np.abs(inner[..., 0] - x) <= 0.85e-3 + 1e-6).all(), np.abs(inner[..., 0] - x).max()
    assert ((inner[..., 1] - y >= 0.05e-3 - 1e-6) & (inner[..., 1] - y <= 0.45e-3 + 1e-6)).all(), np.ptp(inner[..., 1])
    assert signed_volume(objects[joint]) > 0
    # A blade slanting through the hole (a snap-in leg: 0.35 mm across its length, 0.06 mm
    # through the board): each fillet hugs it where it leaves that face, and the solder
    # side's climbs it
    state.board.model_objects_by_fp[J1].remove(blade.name)
    reach = 1.5e-3
    part("J1 leg", J1, "top", (x - 0.85e-3, y - 0.2e-3, z_bottom - reach), (x + 0.85e-3, y + 0.2e-3, z_top + reach), tin,
         lean=(0.0, 0.35e-3))
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    profiles = coordinates(objects[joint]).reshape(solder.THT_ANGLES, -1, 3)
    shift = 0.35e-3 * (z_top + reach - z_bottom) / (z_top - z_bottom + 2 * reach)  # the leg's, at the solder side
    inner = profiles[:, 3 + solder.THT_STEPS, 1] - y  # the bottom fillet's inner edge, on the leg
    assert ((inner >= shift - 0.2e-3 - 1e-5) & (inner <= shift + 0.2e-3 + 1e-5)).all(), (inner.min(), inner.max(), shift)
    shift = 0.35e-3 * reach / (z_top - z_bottom + 2 * reach)  # the leg's, at the part's side
    inner = profiles[:, 4 + solder.THT_STEPS, 1] - y  # the part-side fillet's inner edge, on the leg too
    assert ((inner >= shift - 0.2e-3 - 1e-5) & (inner <= shift + 0.2e-3 + 1e-5)).all(), (inner.min(), inner.max(), shift)
    assert z_bottom - profiles[..., 2].min() > 0.05e-3, z_bottom - profiles[..., 2].min()  # a fillet, not a film
    assert signed_volume(objects[joint]) > 0

    # Not by colour: a numbered pad's hole holds its own footprint's pin, whatever colour its
    # model gives it (a vendor model's grey leads)
    assert drill["kls_fp_id"] == J1 and drill["kls_pad_number"] == "1"  # (the bridge says whose pad it is)
    state.board.model_objects_by_fp[J1].remove(objects["J1 leg"].name)
    grey = material("stand-in vendor grey", (0.8, 0.8, 0.8))
    assert not palette.is_metal(grey)
    vendor = part("J1 vendor pin", J1, "top", (x - 0.3e-3, y - 0.3e-3, z_bottom - 2e-3), (x + 0.3e-3, y + 0.3e-3, z_top + 4e-3),
                  grey, sides=24)
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    assert z_bottom - coordinates(objects[joint])[:, 2].min() > 0.05e-3  # a fillet, not a film
    # ... a hole without a pad number (a mounting or peg hole) only a metal one: a plastic peg stays bare
    drill["kls_pad_number"] = ""
    solder.refresh()
    assert solder.last_build()["joints"] == 0
    vendor.data.materials[0] = tin
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    drill["kls_pad_number"] = "1"
    vendor.data.materials[0] = grey
    # ... and a library part's object with a role only by that role: its case holds no solder
    vendor["kls_role"] = "case"
    solder.refresh()
    assert solder.last_build()["joints"] == 0
    state.board.model_objects_by_fp[J1].remove(vendor.name)

    # A pin ending inside the board (above mid-board): found just inside the face it leaves by
    part("J1 short pin", J1, "top", (x - 0.3e-3, y - 0.3e-3, z_top - 0.3e-3), (x + 0.3e-3, y + 0.3e-3, z_top + 4e-3),
         tin, sides=24)
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    assert signed_volume(objects[joint]) > 0

    # A model's pin wider than the footprint's hole (a blade through a smaller drill): it fills
    # the hole where it reaches past the wall
    state.board.model_objects_by_fp[J1].remove(objects["J1 short pin"].name)
    part("J1 wide blade", J1, "top", (x - 1.1e-3, y - 0.6e-3, z_bottom - 2e-3), (x + 1.1e-3, y + 0.6e-3, z_top + 4e-3), tin)
    bpy.context.view_layer.update()
    solder.refresh()
    assert solder.last_build()["joints"] == 1
    assert signed_volume(objects[joint]) > 0

    # No timers run in background mode: a switch builds the solder at once, and a script
    # that blocks Blender pumps it (studio.pump), settled only once it is built
    scene.kileido_solder_reflowed = False
    assert not solder.pending()
    scene.kileido_solder_reflowed = True  # built at once here
    assert not solder.pending() and objects[joint].visible_get()

    # Reflowed off: the printed paste is back, the reflowed solder hidden at once
    scene.kileido_solder_reflowed = False
    assert top.visible_get() and not flowed_top.visible_get() and not objects[joint].visible_get()
    kileido.unregister()
    print("KLS_SOLDER_OK")


if __name__ == "__main__":
    main()
