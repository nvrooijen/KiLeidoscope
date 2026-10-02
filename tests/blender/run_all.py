"""Headless Blender checks of the add-on on the synthetic fixture board, and a render of it."""

import json
import math
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import (apply, dump, focus, footprints, highlight, holes, materials, nodes,  # noqa: E402
                     placement, state, transform)
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402


def frames_of(snapshot: dict) -> bytes:
    """The bridge's own frame builders, exactly as `python -m kileido_bridge dump x.kls` writes them."""
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot)))


def world_xy(obj, local_mm):
    """Where a footprint-library point (KiCad mm, Y down) lands in Blender."""
    from mathutils import Vector
    point = obj.matrix_world @ Vector((local_mm[0] * 1e-3, -local_mm[1] * 1e-3, 0))
    return float(point.x), float(point.y)


def evaluated_mesh(obj):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)
    return evaluated, mesh


def surface_area(obj):
    """Area facing one way along Z (copper outlines): thick copper is closed, its outer and
    inner sides the same outline; extruded side walls are excluded."""
    evaluated, mesh = evaluated_mesh(obj)
    try:
        return max(sum(face.area for face in mesh.polygons if sign * face.normal.z > 0.5) for sign in (1, -1))
    finally:
        bpy.data.meshes.remove(mesh)


def world_z_range(obj):
    evaluated, mesh = evaluated_mesh(obj)
    try:
        coords = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", coords)
        z = coords.reshape(-1, 3)[:, 2].astype(np.float64) * obj.matrix_world[2][2] + obj.location.z
        return float(z.min()), float(z.max())
    finally:
        bpy.data.meshes.remove(mesh)


def main():
    assert bpy.app.background, "run with Blender --background"
    fixture_path = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"  # KiCad output
    snapshot = json.loads(fixture_path.read_text(encoding="utf-8"))
    groups = nodes.ensure_all()
    assert groups == nodes.ensure_all()
    milliseconds = apply.load_frames(frames_of(snapshot))
    collection = bpy.data.collections["KiLeidoscope: synthetic_rf_geometry.kicad_pcb"]
    assert collection.get("kileido_owned") == 1
    assert collection.hide_select, "the live board follows KiCad and must not be selectable"
    assert not collection.hide_viewport
    assert all(not area.spaces.active.overlay.show_relationship_lines
               for screen in bpy.data.screens for area in screen.areas if area.type == "VIEW_3D")
    assert all(not area.spaces.active.overlay.show_extras  # no softbox outlines over the board
               for screen in bpy.data.screens for area in screen.areas if area.type == "VIEW_3D")
    studio = next(child for child in bpy.context.scene.collection.children if child.get("kls_studio_lights"))
    assert studio.hide_select and all(obj.type == "LIGHT" and obj.visible_get() for obj in studio.objects)
    assert all(name in bpy.data.node_groups for name in ("KLS_Tracks_v4", "KLS_Fill_v4", "KLS_FillSingle_v4",
                                                      "KLS_Drills_v2", "KLS_Vias_v14", "KLS_ViaRings_v1", "KLS_Board_v3",
                                                      "KLS_Solder"))

    top = collection.all_objects["KLS F.Cu tracks"]
    bottom = collection.all_objects["KLS B.Cu tracks"]
    groups = {child["kls_group"]: child for child in collection.children}
    assert [child["kls_group"] for child in collection.children] == [key for key, _ in kileido.objects.GROUPS]
    assert not collection.objects  # every object sits in a group
    assert top.name in groups["copper"].objects and "KLS outline" in groups["board"].objects
    assert "KLS vias" in groups["vias"].objects
    assert all(obj.name.startswith(("KLS footprint", "KLS model ")) for obj in groups["components"].objects)
    assert len(top.data.edges) > 3  # three straight segments and sampled arc segments
    assert len(bottom.data.edges) == 3
    assert math.isclose(top.data.vertices[0].co.x, -0.015, abs_tol=1e-8)
    assert math.isclose(top.data.vertices[0].co.y, 0.010, abs_tol=1e-8)
    # Outer copper is 35 um thick (fixture stackup): each object sits on the laminate
    # side and extrudes outward, so the outer copper surface stays where KiCad puts it.
    copper = 35e-6
    assert math.isclose(top.location.z, 0.001541 - copper, abs_tol=1e-9)
    assert math.isclose(bottom.location.z, -0.000001 + copper, abs_tol=1e-9)
    low, high = world_z_range(top)
    assert math.isclose(high, 0.001541, abs_tol=1e-8) and math.isclose(low, 0.001541 - copper, abs_tol=1e-8)
    low, high = world_z_range(bottom)
    assert math.isclose(low, -0.000001, abs_tol=1e-8) and math.isclose(high, -0.000001 + copper, abs_tol=1e-8)
    assert len(top["kls_ids"]) == 4
    assert top["kls_ids"][-1] == "33333333-3333-4333-8333-333333333333"
    track_coords = np.empty(len(top.data.vertices) * 3, dtype=np.float32)
    top.data.vertices.foreach_get("co", track_coords)
    arc_coords = track_coords.reshape(-1, 3)[6:, :2]
    assert np.allclose(arc_coords[0], (-0.005, 0.010), atol=1e-8)
    assert np.allclose(arc_coords[-1], (-0.005, 0.0), atol=1e-8)

    zone = collection.all_objects["KLS In1.Cu zone 88888888-8888-4888-8888-888888888888"]
    pad = collection.all_objects["KLS F.Cu pads"]
    assert math.isclose(zone.location.z, 0.001305, abs_tol=1e-9)  # inner copper stays a flat sheet
    assert math.isclose(state.board.heights["In2.Cu"], 0.000870, abs_tol=1e-9)
    assert math.isclose(pad.location.z, 0.001542 - copper, abs_tol=1e-9)
    assert math.isclose(world_z_range(pad)[1], 0.001542, abs_tol=1e-8)
    # KiCad sends the fill as ONE fractured ring (holes joined by slits). Blender's fill
    # must reproduce that ring's exact shoelace area, computed here independently.
    (fill_ring,) = snapshot["zones"][0]["polygons"][0]
    shoelace = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2)
                       in zip(fill_ring, fill_ring[1:] + fill_ring[:1]))) / 2 * 1e-18
    assert 780e-6 < shoelace < 864e-6
    assert math.isclose(surface_area(zone), shoelace, rel_tol=1e-4), (surface_area(zone), shoelace)
    pad_area = surface_area(pad)
    # KiCad pad polygons carry no drill ring: J1 8 x 4 mm + J3 1 x 0.6 mm.
    assert math.isclose(pad_area, 32.6e-6, rel_tol=1e-4), pad_area
    assert len(pad.data.edges) == 8
    assert len(pad.data.attributes["item"].data) == 8
    drill = collection.all_objects["KLS F.Cu drill 55555555-5555-4555-8555-555555555555"]
    # The drill is a wall through the whole board (the opening is see-through, holes.py).
    assert math.isclose(drill.location.z, 0.0, abs_tol=1e-12)
    assert np.allclose(drill.location[:2], transform.xy_m([[12_000_000, 23_000_000]],
                                                          state.board.origin_nm)[0], atol=1e-8)
    low, high = world_z_range(drill)
    assert math.isclose(low, 0.0, abs_tol=1e-9) and math.isclose(high, 0.00154, abs_tol=1e-9)
    evaluated, mesh = evaluated_mesh(drill)
    try:
        wall_area = sum(face.area for face in mesh.polygons)
    finally:
        bpy.data.meshes.remove(mesh)
    expected_wall = (2 * (2 - 1) + math.pi * 1) * 1e-3 * 0.00154  # 2 x 1 mm stadium perimeter x depth
    assert math.isclose(wall_area, expected_wall, rel_tol=0.02), (wall_area, expected_wall)
    assert not any(collection.all_objects.get(f"KLS {layer} drill 55555555-5555-4555-8555-555555555555")
                   for layer in ("In1.Cu", "In2.Cu", "B.Cu"))
    # The hole mask: opaque board outside the drill, see-through at its centre.
    mask = bpy.data.images["KLS holes"]
    alpha = np.empty(mask.size[0] * mask.size[1] * 4, np.float32)
    mask.pixels.foreach_get(alpha)
    alpha = alpha.reshape(mask.size[1], mask.size[0], 4)[..., 3]
    xmin, ymin = holes._bounds[:2]
    pixel = max(holes._bounds[2] - xmin, holes._bounds[3] - ymin) / holes.RESOLUTION
    column, row = int((drill.location.x - xmin) / pixel), int((drill.location.y - ymin) / pixel)
    assert alpha[row, column] > 0.99 and alpha[0, 0] == 0.0
    assert "KLS holes mix" in state.board.materials["copper:F.Cu"].node_tree.nodes

    outline = collection.all_objects["KLS outline"]
    assert outline.hide_render is False
    assert outline.modifiers.get("KLS_Board_v3") is not None
    evaluated, mesh = evaluated_mesh(outline)
    try:
        # Walls only along the outline's edge loops, none inside the board (v2 walled
        # every fill triangle, seen as wedges through the see-through board).
        walls = sum(abs(face.normal.z) < 0.5 for face in mesh.polygons)
        assert walls == len(outline.data.edges), (walls, len(outline.data.edges))
        coords = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", coords)
        z_values = coords.reshape(-1, 3)[:, 2] + outline.location.z
        # The dielectric lies between the outer copper layers' inner faces.
        assert math.isclose(z_values.max(), 0.00154 - copper - placement.BOARD_FACE_CLEARANCE_M, abs_tol=1e-8)
        assert math.isclose(z_values.min(), copper + placement.BOARD_FACE_CLEARANCE_M, abs_tol=1e-8)
        assert z_values.max() < state.board.heights["F.Cu"]
        assert z_values.min() > state.board.heights["B.Cu"]
        material_by_face = {}
        for face in mesh.polygons:
            mean_z = sum(z_values[index] for index in face.vertices) / len(face.vertices)
            if abs(mean_z - z_values.max()) < 1e-8:
                key = "top"
            elif abs(mean_z - z_values.min()) < 1e-8:
                key = "bottom"
            else:
                key = "side"
            material_by_face.setdefault(key, set()).add(mesh.materials[face.material_index].name)
        assert material_by_face == {
            "top": {"KLS Board mask top"}, "bottom": {"KLS Board mask bottom"},
            "side": {"KLS Board FR4 core"}}
    finally:
        bpy.data.meshes.remove(mesh)
    apply.set_board_visible(False)
    assert outline.hide_get() and outline.hide_render
    apply.set_board_visible(True)
    assert not outline.hide_get() and not outline.hide_render
    state.board.appearance = {
        "saved_colors": {"F.Mask": [75/255, 124/255, 182/255, 1]},
        "viewer": {"core": [109/255, 116/255, 75/255, 1],
                   "copper": [179/255, 156/255, 0, 1]},
        "editor_copper": {"F.Cu": [200/255, 52/255, 52/255, 1]},
        "editor_mask_top": [216/255, 100/255, 1, 1]}
    apply.set_color_mode("FAB")
    top_material = state.board.materials["board"]
    fab_color = tuple(top_material.node_tree.nodes.get("Emission").inputs["Color"].default_value)
    apply.set_color_mode("EDITOR")
    editor_color = tuple(top_material.node_tree.nodes.get("Emission").inputs["Color"].default_value)
    assert fab_color != editor_color
    assert tuple(state.board.materials["copper:F.Cu"].node_tree.nodes.get("Emission").inputs["Color"].default_value)[:3] != tuple(state.board.materials["copper:B.Cu"].node_tree.nodes.get("Emission").inputs["Color"].default_value)[:3]
    apply.set_color_mode("FAB")

    back_fp = collection.all_objects["KLS footprint 77777777-7777-4777-8777-777777777777"]
    assert back_fp["kls_side"] == "bottom"
    assert back_fp.hide_get() and back_fp.hide_render
    assert back_fp.empty_display_size < 1.01e-4
    assert math.isclose(back_fp.location.z, 0, abs_tol=1e-10)
    assert tuple(back_fp.scale) == (1, -1, -1)  # mirrored in local Y, facing down
    assert math.isclose(back_fp.matrix_world[0][0], 1, abs_tol=1e-6)
    assert math.isclose(back_fp.matrix_world[1][1], -1, abs_tol=1e-6)
    assert math.isclose(back_fp.matrix_world[2][2], -1, abs_tol=1e-6)

    # Rotated footprints: library pad (-2, -1) mm must land on KiCad's own pad position
    # (29, 27) mm top and (35, 18) mm bottom, as `kicad-cli pcb export ipcd356` reports.
    origin = state.board.origin_nm
    for footprint_id, pad_mm in (("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", (29, 27)),
                                 ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", (35, 18))):
        empty = collection.all_objects[f"KLS footprint {footprint_id}"]
        expected = transform.xy_m([[pad_mm[0] * 1_000_000, pad_mm[1] * 1_000_000]], origin)[0]
        assert np.allclose(world_xy(empty, (-2, -1)), expected, atol=1e-7), (footprint_id, world_xy(empty, (-2, -1)), expected)

    # KiCad supplies world-axis bounds. The placeholder uses a rotated local
    # rectangle whose world bounds agree for recoverable angles.
    records = []
    for fp in snapshot["footprints"]:
        x, y = fp["pos"]
        records.append({"id": fp["id"], "ref": fp["reference"], "x": x, "y": y,
                        "rot": fp["rotation_rad"], "side": fp["side"],
                        "model_paths": fp["model_paths"],
                        "bbox_nm": [x - 1_000_000, y - 2_000_000, 2_000_000, 4_000_000]
                        if fp["rotation_rad"] else None})
    footprints.apply({"footprints": records})
    bpy.context.view_layer.update()
    from mathutils import Vector
    for record in records:
        if record["bbox_nm"] is None:
            continue
        box = collection.all_objects[f"KLS footprint placeholder {record['id']}"]
        width = box["kls_placeholder_width_m"]
        height = box["kls_placeholder_height_m"]
        world_corners = [box.matrix_world @ Vector((x * width / 2, y * height / 2, 0))
                         for x in (-1, 1) for y in (-1, 1)]
        expected = transform.xy_m([[record["bbox_nm"][0], record["bbox_nm"][1]],
                                   [record["bbox_nm"][0] + record["bbox_nm"][2],
                                    record["bbox_nm"][1] + record["bbox_nm"][3]]], origin)
        assert math.isclose(min(p.x for p in world_corners), min(expected[:, 0]), abs_tol=1e-8)
        assert math.isclose(max(p.x for p in world_corners), max(expected[:, 0]), abs_tol=1e-8)
        assert math.isclose(min(p.y for p in world_corners), min(expected[:, 1]), abs_tol=1e-8)
        assert math.isclose(max(p.y for p in world_corners), max(expected[:, 1]), abs_tol=1e-8)
    # A 45-degree AABB has no unique inverse aspect ratio; the square fallback
    # is visibly rotated and stays inside its KiCad-supplied world bounds.
    forty_five = math.pi / 4
    width, height = footprints.oriented_placeholder_size((0, 0, 4_000_000, 4_000_000), forty_five)
    assert math.isclose(width, 4e-3 / math.sqrt(2), abs_tol=1e-8)
    assert math.isclose(height, width, abs_tol=1e-8)

    via = collection.all_objects["KLS vias"]
    assert len(via.data.vertices) == 1
    assert via["kls_ids"][0] == "44444444-4444-4444-8444-444444444444"
    # Via lands sit 3 um outside the copper surfaces (above pads; coplanar faces render as noise).
    assert math.isclose(via.data.attributes["z_top"].data[0].value, 0.00154 + 3e-6, abs_tol=1e-8)
    assert math.isclose(via.data.attributes["z_bottom"].data[0].value, -3e-6, abs_tol=1e-8)

    # Reapply the same snapshot: all data blocks and objects must be reused.
    ids_before = {obj.name: obj.as_pointer() for obj in collection.all_objects}
    data_before = {obj.name: obj.data.as_pointer() for obj in collection.all_objects if obj.data}
    apply.load_frames(frames_of(snapshot))
    assert ids_before == {obj.name: obj.as_pointer() for obj in collection.all_objects}
    assert data_before == {obj.name: obj.data.as_pointer() for obj in collection.all_objects if obj.data}

    overlap_points = np.array([[20, 1], [24, 1], [24, 5], [20, 5]] * 2, dtype=np.int32) * 1_000_000
    apply.apply_layer_data({"layer": "In2.Cu", "kind": "zones", "ids": ["overlap-a", "overlap-b"]},
                           {"points": overlap_points, "ring_start": np.array([0, 4, 8]),
                            "ring_item": np.array([0, 1]), "ring_hole": np.array([0, 0], np.uint8)})
    overlap_objects = [collection.all_objects[f"KLS In2.Cu zone {item_id}"]
                       for item_id in ("overlap-a", "overlap-b")]
    overlap_area = sum(surface_area(obj) for obj in overlap_objects)
    assert math.isclose(overlap_area, 32e-6, rel_tol=1e-4), overlap_area
    apply.apply_layer_data({"layer": "In2.Cu", "kind": "zones", "ids": ["overlap-a"]},
                           {"points": overlap_points[:4], "ring_start": np.array([0, 4]),
                            "ring_item": np.array([0]), "ring_hole": np.array([0], np.uint8)})
    assert len(overlap_objects[1].data.vertices) == 0
    assert math.isclose(surface_area(overlap_objects[0]), 16e-6, rel_tol=1e-4)
    for obj in overlap_objects:
        obj.hide_render = True

    # Shareable screenshot of synthetic geometry only.
    cube = bpy.data.objects.get("Cube")
    if cube is not None:
        cube.hide_render = True
    camera_data = bpy.data.cameras.new("KiLeidoscope fixture camera")
    camera = bpy.data.objects.new("KiLeidoscope fixture camera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (0, 0, 0.15)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = 0.05
    bpy.context.scene.camera = camera
    bpy.context.scene.world.use_nodes = True
    bpy.context.scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.015, 0.025, 0.04, 1)
    bpy.context.scene.render.engine = "CYCLES"
    bpy.context.scene.cycles.samples = 16
    bpy.context.scene.render.resolution_x = 1400
    bpy.context.scene.render.resolution_y = 1050
    bpy.context.scene.render.resolution_percentage = 100
    # A picture to look at, not compared; tests/blender/phase2_fixture.png is a kept copy.
    bpy.context.scene.render.filepath = str(Path(tempfile.gettempdir()) / "kileido_fixture_cycles.png")
    bpy.ops.render.render(write_still=True)

    # EEVEE final render with the default 0.1..1000 m clip range: its depth buffer
    # then resolves ~60 um, and at this camera height the board face hid all copper
    # (with mask overlays, board and copper both vanished). render_depth fits the
    # clip range for the frame and restores it afterwards.
    scene = bpy.context.scene
    user_clip = (camera_data.clip_start, camera_data.clip_end)
    assert math.isclose(user_clip[0], 0.1, rel_tol=1e-6) and user_clip[1] == 1000
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = 1
    scene.render.resolution_x, scene.render.resolution_y = 200, 150
    eevee_png = Path(tempfile.gettempdir()) / "kileido_fixture_eevee.png"
    scene.render.filepath = str(eevee_png)
    bpy.ops.render.render(write_still=True)
    assert (camera_data.clip_start, camera_data.clip_end) == user_clip
    rendered = bpy.data.images.load(str(eevee_png), check_existing=False)
    rgb = np.array(rendered.pixels[:], np.float32).reshape(-1, 4)[:, :3]
    bpy.data.images.remove(rendered)
    copper_pixels = int((rgb[:, 0] > rgb[:, 2] + 0.2).sum())  # F.Cu tracks, pads, via land
    board_pixels = int((rgb[:, 2] > rgb[:, 0] + 0.1).sum())
    assert copper_pixels > 600 and board_pixels > 15000, (copper_pixels, board_pixels)
    scene.render.engine = "CYCLES"

    second_board = dict(snapshot)
    second_board["board_name"] = "synthetic_second.kicad_pcb"
    apply.load_frames(frames_of(second_board))
    assert collection.name == "KiLeidoscope: synthetic_second.kicad_pcb"
    assert ids_before == {obj.name: obj.as_pointer() for obj in collection.all_objects if obj.name in ids_before}
    assert data_before == {obj.name: obj.data.as_pointer() for obj in collection.all_objects
                          if obj.name in data_before and obj.data}

    kbf = Path(tempfile.gettempdir()) / "kileido_fixture_test.kls"
    kbf.write_bytes(frames_of(snapshot))
    kileido.register()
    try:
        assert bpy.ops.kileido.load_dump(filepath=str(kbf)) == {"FINISHED"}
        dump.wait(timeout=5)
        assert not dump._worker.is_alive()
        assert state.board.status == "Loaded synthetic_rf_geometry.kicad_pcb"
        assert collection.name == "KiLeidoscope: synthetic_rf_geometry.kicad_pcb"
    finally:
        kileido.unregister()

    # Board finish only in solder-mask openings (user report: ENIG covered all copper).
    state.board.appearance["copper_finish"] = "ENIG"
    mask = bpy.data.images.new("KLS test mask", 2, 2, alpha=True)
    materials.set_mask_image("F", mask, (-0.02, -0.015, 0.02, 0.015))
    materials.set_mask_image("B", mask, (-0.02, -0.015, 0.02, 0.015))
    apply.set_color_mode("FAB")
    finish_mix = state.board.materials["copper:F.Cu"].node_tree.nodes["KLS finish mix"]
    assert finish_mix.inputs["Factor"].links and finish_mix.outputs["Result"].links
    assert state.board.materials["vias"].node_tree.nodes["KLS finish mix"].outputs["Result"].links
    bare = tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in materials.BARE_COPPER)
    inner = state.board.materials["copper:In1.Cu"].diffuse_color[:3]
    assert np.allclose(inner, bare, atol=1e-6), inner  # inner copper never gets the finish
    apply.set_color_mode("EDITOR")  # theme colours: no finish logic
    assert not finish_mix.outputs["Result"].links
    state.board.appearance["copper_finish"] = None
    apply.set_color_mode("FAB")

    # Stencil deposits: KiCad's F.Paste/B.Paste pad shapes, hidden until ticked, sitting
    # on the pad copper and extruded outward by the stencil thickness.
    pasted = json.loads(json.dumps(snapshot))
    for pad in pasted["pads"]:
        if pad["drill"] is None:
            side = next(iter(pad["polygons"]))[0]
            pad["polygons"][f"{side}.Paste"] = pad["polygons"][f"{side}.Cu"]
    kileido.register()
    try:
        apply.load_frames(frames_of(pasted))
        solder_top, solder_bottom = (collection.all_objects[f"KLS {side}.Paste solder"] for side in "FB")
        assert solder_top.hide_get() and solder_bottom.hide_get()
        bpy.context.scene.kileido_show_solder = True
        assert not solder_top.hide_get() and not solder_bottom.hide_get()
        low, high = world_z_range(solder_top)
        assert math.isclose(low, 0.00154 + 3e-6, abs_tol=1e-8), low
        assert math.isclose(high - low, 0.12e-3, rel_tol=1e-3), high - low
        low, high = world_z_range(solder_bottom)
        assert math.isclose(high, -3e-6, abs_tol=1e-8) and math.isclose(high - low, 0.12e-3, rel_tol=1e-3)
        bpy.context.scene.kileido_stencil_mm = 0.1
        low, high = world_z_range(solder_top)
        assert math.isclose(high - low, 0.1e-3, rel_tol=1e-3)
        bpy.context.scene.kileido_copper_3d = False  # flat copper: the dielectric reaches the copper surface
        evaluated, mesh = evaluated_mesh(collection.all_objects["KLS outline"])
        try:
            z_top = max(v.co.z for v in mesh.vertices) + collection.all_objects["KLS outline"].location.z
        finally:
            bpy.data.meshes.remove(mesh)
        assert math.isclose(z_top, 0.00154 - placement.BOARD_FACE_CLEARANCE_M, abs_tol=1e-8)
        assert math.isclose(world_z_range(collection.all_objects["KLS F.Cu tracks"])[0], 0.001541, abs_tol=1e-8)
        bpy.context.scene.kileido_copper_3d = True

        # Via protection from KiCad: only an unprotected through via is see-through. The
        # fixture's vias follow its rules; a 0.35 mm drill is too large to tent at 0.30 mm.
        via_obj = collection.all_objects["KLS vias"]
        via_xy = np.array(via_obj.data.vertices[0].co[:2])

        def via_alpha():
            mask = bpy.data.images["KLS holes"]
            pixels = np.empty(mask.size[0] * mask.size[1] * 4, np.float32)
            mask.pixels.foreach_get(pixels)
            pixels = pixels.reshape(mask.size[1], mask.size[0], 4)[..., 3]
            xmin, ymin = holes._bounds[:2]
            pixel = max(holes._bounds[2] - xmin, holes._bounds[3] - ymin) / holes.RESOLUTION
            return pixels[int((via_xy[1] - ymin) / pixel), int((via_xy[0] - xmin) / pixel)]

        scene = bpy.context.scene
        assert "via_rules" not in state.board.appearance  # KiCad's defaults: tented both sides
        tents = via_obj.data.attributes["tent_top"]
        assert via_alpha() > 0.99 and tents.data[0].value == 1.0  # a hole, the tent closing it
        assert state.board.via_too_big == 0  # 0.35 mm drill, 25 um wall: a 0.30 mm hole
        scene.kileido_via_plating_um = 10.0  # a 0.33 mm hole: too large to tent
        assert via_alpha() > 0.99 and tents.data[0].value == 0.0
        assert state.board.via_too_big == len(via_obj.data.vertices)
        scene.kileido_max_tent_mm = 0.4
        assert tents.data[0].value == 1.0 and state.board.via_too_big == 0
        scene.kileido_via_plating_um = 25.0
        state.board.appearance["via_rules"] = [0] * 8  # a board whose vias are unprotected
        apply.refresh_protection()
        assert via_alpha() > 0.99 and state.board.via_too_big == 0 and tents.data[0].value == 0.0
        state.board.appearance["via_rules"] = [0, 0, 0, 0, 0, 0, 1, 1]  # filled and capped (type VII)
        apply.refresh_protection()
        assert via_alpha() == 0.0
        del state.board.appearance["via_rules"]
        scene.kileido_max_tent_mm = 0.3
        apply.refresh_protection()

        # KiCad selection: selected track red, diff-pair partner blue, over the copper.
        f_tracks = collection.all_objects["KLS F.Cu tracks"]
        chosen, partner = f_tracks["kls_ids"][0], f_tracks["kls_ids"][1]
        highlight.apply_selection({"selected": [chosen], "pair": [partner]})
        red = collection.all_objects["KLS F.Cu highlight selected"]
        blue = collection.all_objects["KLS F.Cu highlight pair"]
        assert len(red.data.edges) == 1 and len(blue.data.edges) == 1
        assert not red.hide_get() and red.data.materials or red.modifiers
        assert nodes.modifier_value(red.modifiers[0], next(i.identifier for i in red.modifiers[0].node_group.interface.items_tree
                                                           if getattr(i, "name", "") == "Material")).name == "KLS Highlight selected"
        assert world_z_range(red)[1] > world_z_range(f_tracks)[1]  # on top of the copper
        # A highlighted net's power plane: the zone itself turns red-orange.
        zone_obj = collection.all_objects["KLS In1.Cu zone 88888888-8888-4888-8888-888888888888"]

        def zone_material():
            modifier = zone_obj.modifiers[0]
            return nodes.modifier_value(modifier, next(i.identifier for i in modifier.node_group.interface.items_tree
                                                       if getattr(i, "name", "") == "Material")).name
        highlight.apply_selection({"selected": [chosen, "88888888-8888-4888-8888-888888888888"], "pair": []})
        assert zone_material() == "KLS Highlight selected"
        highlight.apply_selection({"selected": [], "pair": []})
        assert red.hide_get() and blue.hide_get() and not red.data.vertices
        assert zone_material() == "KLS In1.Cu copper"

        # A selected component: its pads get a red-orange sheet, the part a glow box.
        smd = next(pad for pad in snapshot["pads"] if pad["drill"] is None and "F.Cu" in pad["polygons"])
        highlight.apply_selection({"selected": [], "pair": [], "footprints": [smd["footprint_id"]],
                               "pads": [smd["id"]]})
        sheet = collection.all_objects["KLS F.Cu highlight pads"]
        assert not sheet.hide_get() and len(sheet.data.edges) == 4
        assert math.isclose(surface_area(sheet), 0.6e-6, rel_tol=1e-3)  # the 1 x 0.6 mm pad
        box = collection.all_objects[f"KLS footprint highlight {smd['footprint_id']}"]
        assert not box.hide_get()
        # Moved in KiCad (an edit or an undo) with the selection unchanged: the box follows.
        def footprint_frame(shift_nm):  # bounds as the bridge sends them; the box wraps the envelope
            return {"type": "footprints", "footprints": [
                {"id": fp["id"], "ref": fp["reference"], "x": fp["pos"][0] + shift_nm, "y": fp["pos"][1],
                 "rot": fp["rotation_rad"], "side": fp["side"], "model_paths": fp["model_paths"],
                 "bbox_nm": [fp["pos"][0] + shift_nm - 1_000_000, fp["pos"][1] - 2_000_000, 2_000_000, 4_000_000]}
                for fp in snapshot["footprints"]]}
        apply.apply_frame(footprint_frame(0), {})
        before = box.matrix_world.translation.x
        apply.apply_frame(footprint_frame(5_000_000), {})
        assert math.isclose(box.matrix_world.translation.x - before, 0.005, abs_tol=1e-9), box.matrix_world
        apply.apply_frame(footprint_frame(0), {})
        highlight.apply_selection({"selected": [], "pair": [], "footprints": [], "pads": []})
        assert sheet.hide_get() and box.hide_get()

        # Click picking: a ray from above finds the KiCad item under it.
        from mathutils import Vector
        from kileido import pick
        depsgraph = bpy.context.evaluated_depsgraph_get()

        def picked(x, y):
            return pick.item_at(bpy.context.scene, depsgraph, Vector((x, y, 0.05)), Vector((0, 0, -1)))

        first = [f_tracks.data.vertices[i].co for i in (0, 1)]
        assert picked((first[0].x + first[1].x) / 2, (first[0].y + first[1].y) / 2) == f_tracks["kls_ids"][0]
        via_point = collection.all_objects["KLS vias"].data.vertices[0].co
        assert picked(via_point.x, via_point.y) == collection.all_objects["KLS vias"]["kls_ids"][0]
        pads = collection.all_objects["KLS F.Cu pads"]
        item = np.empty(len(pads.data.vertices), np.int32)
        pads.data.attributes["item"].data.foreach_get("value", item)
        index = pads["kls_ids"].index(smd["id"])
        ring = np.array([tuple(v.co) for v, i in zip(pads.data.vertices, item) if i == index])
        assert picked(*ring[:, :2].mean(axis=0)) == smd["id"]
        assert picked(0.5, 0.5) is None  # off the board

        # Focus mode: with a selection, everything but the highlights fades.
        amount = bpy.data.node_groups[focus.GROUP].nodes["Amount"].outputs[0]
        # Faded = unlit grey: lit faded layers made focus mode 2.5x slower per frame.
        assert not any(node.type.startswith("BSDF") and node.type != "BSDF_TRANSPARENT"
                       for node in bpy.data.node_groups[focus.GROUP].nodes)
        bpy.context.scene.kileido_focus = True
        assert amount.default_value == 0.0  # nothing selected yet
        highlight.apply_selection({"selected": [chosen], "pair": [partner]})
        assert amount.default_value == 1.0
        assert "KLS focus" in state.board.materials["copper:F.Cu"].node_tree.nodes
        assert "KLS focus" in state.board.materials["board"].node_tree.nodes
        assert "KLS focus" not in state.board.materials["highlight_selected"].node_tree.nodes
        holes_mix = state.board.materials["copper:F.Cu"].node_tree.nodes["KLS holes mix"]
        assert holes_mix.outputs[0].links[0].to_node.name == "KLS focus"  # holes stay before focus
        apply.set_color_mode("REALISTIC")  # colour-mode switches keep the focus stage
        focus_node = state.board.materials["copper:F.Cu"].node_tree.nodes["KLS focus"]
        assert focus_node.outputs[0].links[0].to_node.type == "OUTPUT_MATERIAL"
        # Faded materials blend in EEVEE (dithered 15 % opacity was grainy); highlights stay dithered.
        faded = [state.board.materials[key] for key in ("copper:F.Cu", "board", "vias")]
        assert all(m.surface_render_method == "BLENDED" and m.use_transparency_overlap for m in faded)
        assert state.board.materials["highlight_selected"].surface_render_method == "DITHERED"
        highlight.apply_selection({"selected": [], "pair": []})
        assert amount.default_value == 0.0
        assert all(m.surface_render_method == "DITHERED" for m in faded)
        bpy.context.scene.kileido_focus = False
    finally:
        kileido.unregister()

    print("KLS_PHASE2_FIXTURE_OK=" + json.dumps({
        "blender": bpy.app.version_string,
        "load_ms": milliseconds,
        "objects": len(collection.all_objects),
        "f_cu_segments": len(top.data.edges),
        "zone_area_m2": surface_area(zone),
        "pad_area_m2": pad_area,
    }))


if __name__ == "__main__":
    main()
