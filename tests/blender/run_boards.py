"""Headless checks of board packages: export the live board, import it view-only elsewhere.

blender --background --factory-startup --python tests/blender/run_boards.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, cosmetics, highlight, holes, layers, lighting, packages, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


BOARD_FILE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_pcb"


def frames(name, two_layer=False, board_path=""):
    """The fixture (four copper layers) under another name, or cut down to two layers."""
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["board_name"] = name
    if two_layer:
        stack = snapshot["stackup"]["layers"]
        core = dict(stack[1], thickness_nm=sum(layer["thickness_nm"] for layer in stack[1:-1]))
        snapshot["stackup"]["layers"] = [stack[0], core, stack[-1]]
        snapshot["zones"] = [zone for zone in snapshot["zones"] if zone["layer"] in ("F.Cu", "B.Cu")]
        for pad in snapshot["pads"]:  # through-hole pads list every copper layer
            pad["polygons"] = {layer: shape for layer, shape in pad["polygons"].items() if not layer.startswith("In")}
    return b"".join(snapshot_frames(snapshot_from_jsonable(snapshot), board_path=board_path))


def copper_rows(recorded):
    return [entry["row"] for section in recorded["sections"] for entry in section["entries"]
            if entry["kind"] == "copper"]


def _covered(material):
    """Covered copper as the material shows it: colour, metallic, roughness."""
    nodes = material.node_tree.nodes
    if "KLS finish mix" not in nodes:
        return None
    return (tuple(round(v, 5) for v in nodes["KLS finish mix"].inputs["A"].default_value),
            round(nodes["KLS metal amount"].inputs["To Min"].default_value, 5),
            round(nodes["KLS metal roughness"].inputs["To Min"].default_value, 5))


SETTINGS = {"default": {"kileido_copper_3d": True, "kileido_silk_3d": True, "kileido_silk_um": 15.0,
                        "kileido_show_solder": False, "kileido_stencil_mm": 0.12},
            "changed": {"kileido_copper_3d": False, "kileido_silk_3d": True, "kileido_silk_um": 40.0,
                        "kileido_show_solder": True, "kileido_stencil_mm": 0.2}}


def heights(collection, prefix=""):
    """Every placed object's z, Geometry Nodes thickness inputs and visibility, by live name."""
    from kileido.objects import node_modifier
    found = {}
    for obj in collection.all_objects:
        if not (obj.get("kls_copper") or obj.get("kls_paste_side") or obj.get("kls_cosmetic_layer")
                or obj.get("kls_overlay_walls") or obj.name.endswith(("KLS outline", "KLS vias"))):
            continue
        modifier = node_modifier(obj)
        inputs = {} if modifier is None else {
            item.name: round(float(modifier[item.identifier]), 9) for item in modifier.node_group.interface.items_tree
            if item.item_type == "SOCKET" and item.in_out == "INPUT" and "Thickness" in item.name}
        found[obj.name.removeprefix(prefix)] = (round(obj.location.z, 9), inputs, obj.hide_get())
    return found


def apply_settings(name):
    for key, value in SETTINGS[name].items():
        setattr(bpy.context.scene, key, value)


def live_objects():
    return {obj.name: obj.as_pointer() for obj in state.board.collection.all_objects}


def x_range(collection):
    bpy.context.view_layer.update()
    return packages._x_range(packages._outline(collection))


def fresh_session():
    """A new Blender session: empty file, add-on state as after a restart."""
    kileido.unregister()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    state.board.__init__()
    holes._sources = {key: value[:0] for key, value in holes._sources.items()}
    holes._bounds = holes._digest = holes._drawn = None
    kileido.register()


def main():
    kileido.register()
    directory = Path(tempfile.mkdtemp())
    package = directory / "alpha.blend"
    try:
        # Session 1: board alpha live, a track selected in KiCad and In2.Cu switched off; exported.
        apply.load_frames(frames("alpha", board_path=str(BOARD_FILE)))
        deadline = time.monotonic() + 90  # kicad-cli plots the mask, so copper can show it
        while not state.board.mask_images and time.monotonic() < deadline:
            cosmetics.drain()
            time.sleep(0.2)
        has_mask = bool(state.board.mask_images)
        covered_live = {"shown": _covered(state.board.materials["copper:F.Cu"])}
        bpy.context.scene.kileido_show_F_Mask = False
        covered_live["hidden"] = _covered(state.board.materials["copper:F.Cu"])
        bpy.context.scene.kileido_show_F_Mask = True
        track = json.loads(FIXTURE.read_text(encoding="utf-8"))["tracks"][0]["id"]
        highlight.apply_selection({"selected": [track], "pair": []})
        bpy.context.scene.kileido_show_In2_Cu = False
        highlighted = sorted(obj.name for obj in state.board.collection.all_objects
                             if "highlight" in obj.name and not obj.hide_get())
        assert highlighted
        expected = {}
        for name in ("changed", "default"):  # what the live board shows under each setting
            apply_settings(name)
            expected[name] = heights(state.board.collection)
        changes = {kind for name in expected["default"] if expected["default"][name] != expected["changed"][name]
                   for kind in ("tracks", "pads", "overlay", "outline", "vias") if kind in name}
        assert {"tracks", "pads", "overlay", "outline", "vias"} <= changes, changes  # the settings move these
        apply_settings("changed")  # exported like this; the importing session decides
        packages.export_board(str(package))
        apply_settings("default")
        assert package.is_file()
        assert sorted(obj.name for obj in state.board.collection.all_objects
                      if "highlight" in obj.name and not obj.hide_get()) == highlighted, "selection not restored"
        assert not any(key in obj for obj in state.board.collection.all_objects for key in ("kls_hidden", "kls_row"))
        assert all(image.users for image in bpy.data.images), "export left a temporary image behind"
        highlight.apply_selection({})
        hidden = sorted(obj.name for obj in state.board.collection.all_objects if obj.hide_get())

        # Session 2: board beta (two copper layers) live; alpha imported beside it, twice.
        fresh_session()
        apply.load_frames(frames("beta", two_layer=True))
        live_before = live_objects()
        live_materials = {material.name for material in state.board.materials.values()}
        first = packages.import_board(str(package))
        second = packages.import_board(str(package))
        assert live_objects() == live_before, "the import touched the live board"
        assert state.board.collection.hide_select, "the live board can be selected (and moved)"
        assert not packages.collection_of(first[packages.ROOT_TAG]).hide_select, "a view-only board is locked"
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.object.select_all(action="SELECT")
        assert not any(obj.select_get() for obj in state.board.collection.all_objects)
        assert first.select_get(), "a view-only board's root cannot be selected"
        bpy.ops.object.select_all(action="DESELECT")
        assert {material.name for material in state.board.materials.values()} == live_materials

        one, two = (packages.collection_of(root[packages.ROOT_TAG]) for root in (first, second))
        assert one.name == "KiLeidoscope view-only: alpha" and "kileido_owned" not in one
        assert one.children and all(group.name.startswith("alpha: ") and group.name[-4:-3] != "."
                                    for group in one.children), [group.name for group in one.children]
        assert all(obj.name.startswith("KV1 ") or obj == first for obj in one.all_objects)
        assert all(obj.name.startswith("KV2 ") or obj == second for obj in two.all_objects)
        assert sorted(obj.name.removeprefix("KV1 ") for obj in one.all_objects if obj.hide_get()) == hidden
        assert not packages._owned_data(one)[0] & packages._owned_data(state.board.collection)[0]
        assert not any(block.name.startswith("KLS") and block.name[-4:-3] == "."
                       for kind in packages.DATA_KINDS for block in getattr(bpy.data, kind)), \
            "a live-board name was taken by imported data"
        assert x_range(state.board.collection)[1] <= x_range(one)[0] < x_range(one)[1] <= x_range(two)[0]
        remapped = [node for material in packages._owned_data(one)[0] if material.node_tree
                    for node in material.node_tree.nodes if node.type == "TEX_COORD" and node.object == first]
        assert remapped, "hole and mask plots still sampled at world XY"

        assert not any("highlight" in obj.name and not obj.hide_get() for obj in one.all_objects),             "KiCad's selection was exported"

        # The pure board is exported: no lights; one pair of studio softboxes spans every board.
        with bpy.data.libraries.load(str(package)) as (source, _):
            assert not source.lights, source.lights
            assert not any("softbox" in name.lower() for name in source.objects)
        assert not any(obj.type == "LIGHT" for obj in (*one.all_objects, *two.all_objects))
        studio = [obj for obj in bpy.data.objects if obj.type == "LIGHT" and obj.get("kls_studio_side")]
        assert len(studio) == 2 and all(light.light_linking.receiver_collection is None for light in studio)
        span = (x_range(state.board.collection)[0], x_range(two)[1])
        top = next(light for light in studio if light["kls_studio_side"] == "top")
        assert abs(top.location.x - sum(span) / 2) < 1e-6, (top.location.x, span)
        assert top.data.size >= (span[1] - span[0]) * lighting.COVER_FRACTION - 1e-9
        size, energy = top.data.size, top.data.energy
        second.location.x += 0.2  # far away: the softboxes grow over it, power by area
        lighting.fit_to_boards()
        assert top.data.size > size and abs(top.data.energy / energy - (top.data.size / size) ** 2) < 1e-6
        second.location.x -= 0.2
        lighting.fit_to_boards()
        assert abs(top.data.size - size) < 1e-6, (top.data.size, size)  # float32 light data

        # The panel's Thickness settings, not the exporter's, apply to view-only boards too.
        assert heights(one, "KV1 ") == expected["default"], "the import kept the exported thickness"
        apply_settings("changed")
        assert heights(one, "KV1 ") == expected["changed"]
        apply_settings("default")
        assert heights(one, "KV1 ") == expected["default"]

        # Each board has its own Layers list and eyes.
        index = first[packages.ROOT_TAG]
        assert copper_rows(packages.layer_list(index)) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        assert copper_rows(layers.recorded()) == ["F.Cu", "B.Cu"]
        assert not packages.row_shown(index, "In2.Cu") and packages.row_shown(index, "F.Cu")
        top = [obj for obj in one.all_objects if obj.get("kls_row") == "F.Cu" and not obj.hide_get()]
        assert top
        packages.set_row_visible(index, "F.Cu", False)
        assert all(obj.hide_get() for obj in top)
        assert not any(obj.hide_get() for obj in two.all_objects if obj.get("kls_row") == "F.Cu" and
                       not obj.get("kls_layer_hidden") and obj.name.removeprefix("KV2 ") not in hidden)
        assert bpy.context.scene.kileido_show_F_Cu, "a view-only eye switched the live board"
        if has_mask:  # a view-only mask eye recolours its covered copper exactly like the live one
            copper = next(m for m in packages._owned_data(one)[0] if m.name == "KV1 KLS F.Cu copper")
            assert _covered(copper) == covered_live["shown"] != covered_live["hidden"]
            packages.set_row_visible(index, "F.Mask", False)
            assert _covered(copper) == covered_live["hidden"]
            packages.set_row_visible(index, "F.Mask", True)
            assert _covered(copper) == covered_live["shown"]
        else:
            print("KLS_BOARDS_SKIPPED=mask colours (kicad-cli plotted no mask)")
        packages.set_row_visible(index, "F.Cu", True)
        assert not any(obj.hide_get() for obj in top)
        bpy.context.scene.kileido_layers_board = str(index)  # the panel's board choice

        # All boards: every row once, in stackup order; an eye switches every board that has it.
        bpy.context.scene.kileido_layers_board = "ALL"
        stack = [row for title, entries in packages.all_boards_sections() if title == "Stackup"
                 for row, *_ in entries]
        assert stack == ["F.SilkS", "F.Mask", "F.Cu", "In1.Cu", "In2.Cu", "Board", "B.Cu", "B.Mask", "B.SilkS"], stack
        assert not packages.shown_everywhere("In2.Cu")  # off on alpha since its export
        packages.set_row_everywhere("In2.Cu", True)  # the live board has no In2.Cu: view-only only
        assert packages.row_shown(index, "In2.Cu") and packages.shown_everywhere("In2.Cu")
        packages.set_row_everywhere("F.Cu", False)
        assert not bpy.context.scene.kileido_show_F_Cu and not packages.row_shown(index, "F.Cu")
        assert not packages.row_shown(second[packages.ROOT_TAG], "F.Cu")
        packages.set_row_everywhere("F.Cu", True)
        assert packages.shown_everywhere("F.Cu") and bpy.context.scene.kileido_show_F_Cu

        # Select: a view-only board moves as one; the live board is locked in place.
        assert all(obj.lock_location[:] == (True,) * 3 and obj.lock_rotation[:] == (True,) * 3 and
                   obj.lock_scale[:] == (True,) * 3 for obj in state.board.collection.all_objects)
        assert packages.select(index)
        view_layer = bpy.context.view_layer
        assert view_layer.objects.active == first and not any(first.lock_location)
        assert set(view_layer.objects.selected) == {obj for obj in one.all_objects if obj.visible_get()}
        track = next(obj for obj in one.all_objects if obj.get("kls_copper"))
        before = track.matrix_world.translation.copy()
        first.location.y += 0.01
        view_layer.update()
        assert abs((track.matrix_world.translation - before).y - 0.01) < 1e-9, "a part did not follow its board"
        first.location.y -= 0.01
        view_layer.update()

        # Live edits still reach the live board only.
        apply.load_frames(frames("beta", two_layer=True))
        assert live_objects() == live_before

        packages.set_visible(second[packages.ROOT_TAG], False)
        assert two.hide_viewport
        packages.remove(second[packages.ROOT_TAG])
        assert [root[packages.ROOT_TAG] for root in packages.roots()] == [1]
        assert not any(block.name.startswith("KV2 ") for kind in packages.DATA_KINDS
                       for block in getattr(bpy.data, kind))

        # Session 3: nothing live; the import alone sets the scene up.
        fresh_session()
        packages.import_board(str(package))
        assert any(child.get("kls_studio_lights") == 1 for child in bpy.context.scene.collection.children)
    finally:
        kileido.unregister()
    print("KLS_BOARDS_OK=" + json.dumps({"package_kb": package.stat().st_size // 1024}))


if __name__ == "__main__":
    main()
