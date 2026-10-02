"""Headless checks of the silkscreen printed on the mask and copper (it follows the copper,
so traces show through a silkscreen fill), the flat sheet it falls back to, and the
panel's silkscreen and solder mask opacity. Needs kicad-cli for the mask and silkscreen plots.

blender --background --factory-startup --python tests/blender/run_silk.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, cosmetics, materials, packages, state  # noqa: E402
from kileido.objects import outline_bounds  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
BOARD_FILE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_pcb"


def node_value(material, name):
    return material.node_tree.nodes[name].inputs[1].default_value


def printed(material, side="F"):
    """The side's ink is drawn on this surface (switched on, eye on, mix linked)."""
    nodes = material.node_tree.nodes
    return (node_value(material, f"KLS silk active {side}") == 1 and node_value(material, f"KLS silk shown {side}") == 1
            and any(link.to_node.type in ("EMISSION", "BSDF_PRINCIPLED")
                    for output in nodes["KLS silk mix"].outputs for link in output.links))


def sheet_opacity():
    return node_value(bpy.data.materials[cosmetics.material_name("F.SilkS")], materials.SHEET_OPACITY)


def covered_color(material):
    """Linear RGB of copper under the mask."""
    return tuple(material.node_tree.nodes["KLS finish mix"].inputs["A"].default_value[:3])


def mask_color(material):
    """Linear RGB of the mask sheet (under the ink)."""
    return tuple(material.node_tree.nodes[materials.BASE_COLOR].outputs[0].default_value[:3])


def distance(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


def walls_active():
    walls = state.board.collection.all_objects.get(cosmetics.walls_name("F.SilkS"))
    return walls is not None and walls.get("kls_walls_active", False)


def main():
    kileido.register()
    scene = bpy.context.scene
    package = Path(tempfile.mkdtemp()) / "silk.blend"
    try:
        snapshot = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
        apply.load_frames(b"".join(snapshot_frames(snapshot, board_path=str(BOARD_FILE))))
        deadline = time.monotonic() + 90
        while not state.board.mask_images and time.monotonic() < deadline:
            cosmetics.drain()
            time.sleep(0.2)
        assert state.board.mask_images, "no mask plot: " + cosmetics.status()
        viewer = state.board.appearance.setdefault("viewer", {})  # KiCad's 3D colours, as the bridge sends them
        viewer.update(soldermask_top=(0.55, 0.7, 0.9, 0.83), soldermask_bottom=(0.55, 0.7, 0.9, 0.83),
                      silkscreen_top=(0.95, 0.95, 0.95, 1.0), core=(0.33, 0.29, 0.17))
        apply.set_color_mode("FAB")
        copper = state.board.materials["copper:F.Cu"]
        mask = bpy.data.materials[cosmetics.material_name("F.Mask")]
        sheet = state.board.collection.all_objects[cosmetics.object_name("F.SilkS")]

        # Board stackup colours, mask shown: the ink is on the mask and copper, the sheet clear.
        assert state.board.silk["F"]["active"] and sheet["kls_silk_on_surface"]
        assert printed(copper) and printed(mask) and printed(state.board.materials["vias"], "B")
        assert sheet_opacity() == 0 and not walls_active(), "the flat sheet still shows over the printed ink"

        # Realistic: lit, and the ink thickness is a bump along its edges.
        scene.kileido_color_mode = "REALISTIC"
        assert printed(copper) and printed(mask)
        principled = next(node for node in copper.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        assert principled.inputs["Normal"].links[0].from_node.name == "KLS silk bump"
        assert abs(copper.node_tree.nodes["KLS silk bump"].inputs["Distance"].default_value - 15e-6) < 1e-9
        scene.kileido_silk_3d = False
        assert copper.node_tree.nodes["KLS silk bump"].inputs["Distance"].default_value == 0
        scene.kileido_silk_3d = True

        # The opacity slider reaches every printed surface; the sheet stays clear.
        scene.kileido_silk_opacity = 0.4
        assert all(abs(node_value(material, materials.SILK_OPACITY) - 0.4) < 1e-6 for material in (copper, mask))
        assert sheet_opacity() == 0

        # Mask hidden: nothing for the ink to lie on, so it is the flat sheet again (with walls).
        scene.kileido_show_F_Mask = False
        assert not state.board.silk["F"]["active"] and not printed(copper) and not printed(mask)
        assert abs(sheet_opacity() - 0.4) < 1e-6 and walls_active()
        assert state.board.silk["B"]["active"], "the bottom side followed the top mask's eye"
        scene.kileido_show_F_Mask = True
        assert printed(copper) and sheet_opacity() == 0 and not walls_active()

        # The silkscreen's eye switches the printed ink too.
        scene.kileido_show_F_SilkS = False
        assert not printed(copper) and not printed(mask)
        scene.kileido_show_F_SilkS = True
        assert printed(copper) and printed(mask)

        # Mask over copper is lighter than mask over the laminate, in every channel.
        covered_full, mask_full = covered_color(copper), mask_color(mask)
        assert all(c > m + 0.01 for c, m in zip(covered_full, mask_full)), (covered_full, mask_full)
        plain = materials.shading.srgb_to_linear(materials.seen_through(viewer["soldermask_top"], viewer["core"]))
        assert all(m < p - 0.01 for m, p in zip(mask_full, plain)), "the mask over the laminate is not darkened"
        scene.kileido_mask_opacity = 0.0  # no mask: the plain laminate
        assert distance(mask_color(mask), materials.shading.srgb_to_linear(viewer["core"])) < 1e-6

        # Solder mask opacity: copper under the mask, and the laminate, show through it.
        scene.kileido_mask_opacity = 0.3
        covered_thin = covered_color(copper)
        bare = materials.shading.srgb_to_linear(materials.COPPER_UNDER_MASK)
        assert distance(covered_thin, bare) < distance(covered_full, bare), "the copper does not show through"
        assert distance(mask_color(mask), mask_full) > 0.01, "the mask over the laminate kept its colour"
        scene.kileido_mask_opacity = 1.0
        assert distance(covered_color(copper), covered_full) < 1e-6
        scene.kileido_mask_opacity = 0.3

        # PCB Editor colours: the translucent sheet above everything, as in KiCad's editor.
        scene.kileido_color_mode = "EDITOR"
        assert not state.board.silk["F"]["active"] and abs(sheet_opacity() - 0.4) < 1e-6
        assert not any(output.links for output in copper.node_tree.nodes["KLS silk mix"].outputs)
        scene.kileido_color_mode = "REALISTIC"
        assert printed(copper)

        # From inside the board (cut open, hidden or see-through), outer copper is closed on its
        # laminate side: copper shows where there is copper, the mask sheet only beside it, with
        # its inner face farther out than the copper's. On both sides.
        rows = ("kileido_show_board", "kileido_show_vias", "kileido_show_In1_Cu", "kileido_show_In2_Cu")
        for row in rows:
            setattr(scene, row, False)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        xmin, ymin, _xmax, _ymax = outline_bounds()

        def from_inside(x, y, inward):
            """(object, z, normal z) first met from mid-board on the way out through one side."""
            hit, location, normal, _index, obj, _matrix = scene.ray_cast(
                depsgraph, Vector((x, y, state.board.thickness_m / 2)), Vector((0, 0, -inward)))
            assert hit, (x, y, inward)
            return obj, location.z, normal.z

        for layer, inward in (("F.Cu", -1.0), ("B.Cu", 1.0)):  # the way its laminate side faces
            tracks = state.board.collection.all_objects[f"KLS {layer} tracks"]
            ends = [tracks.matrix_world @ vertex.co for vertex in tracks.data.vertices]
            first, second = tracks.data.edges[0].vertices
            on_track = (ends[first] + ends[second]) / 2
            met, copper_z, facing = from_inside(on_track.x, on_track.y, inward)
            assert met.get("kls_copper") and met["kls_copper"][0] == layer, (layer, met.name)
            assert facing * inward > 0.99, (layer, facing)  # its own inner face, facing into the board
            met, mask_z, _ = from_inside(xmin + 0.001, ymin + 0.001, inward)  # a corner without copper
            assert met.get("kls_cosmetic_layer") == f"{layer[0]}.Mask", (layer, met.name)
            assert (mask_z - copper_z) * inward < -1e-6, (layer, mask_z, copper_z)
        for row in rows:
            setattr(scene, row, True)

        # A view-only board keeps its printed ink; its own mask eye brings its sheet back.
        packages.export_board(str(package))
        root = packages.import_board(str(package))
        index = root[packages.ROOT_TAG]
        collection = packages.collection_of(index)
        imported = next(obj for obj in collection.all_objects if obj.get("kls_cosmetic_layer") == "F.SilkS")
        own = [material for material in packages._owned_data(collection)[0]  # top-side ink
               if "KLS silk active F" in material.node_tree.nodes]
        assert own and imported["kls_silk_on_surface"] and all(printed(material) for material in own)
        packages.set_row_visible(index, "F.Mask", False)
        assert not imported["kls_silk_on_surface"] and not any(printed(material) for material in own)
        assert printed(copper), "a view-only eye switched the live board"
        packages.set_row_visible(index, "F.Mask", True)
        assert all(printed(material) for material in own)
        covered_own = next(material for material in own if "KLS finish mix" in material.node_tree.nodes)
        assert distance(covered_color(covered_own), covered_color(copper)) < 1e-6  # exported at 0.3
        scene.kileido_mask_opacity = 1.0  # the mask slider sets every board
        assert distance(covered_color(covered_own), covered_full) < 1e-6
        scene.kileido_silk_opacity = 0.7  # the slider sets every board
        assert all(abs(node_value(material, materials.SILK_OPACITY) - 0.7) < 1e-6 for material in own)
    finally:
        kileido.unregister()
    print("KLS_SILK_OK")


if __name__ == "__main__":
    main()
