"""Headless checks of part libraries (parts.py): KiCad's models by default; a library
made the Default puts its parts on the footprints they match, where they follow, are told
the footprint, and come off; a footprint name's Model from picks a library or KiCad; a model
saved to My parts is picked for every footprint of its name, and can be removed again.

blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_parts.py
"""

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, focus, highlight, kicad_cli, parts, settings  # noqa: E402
from kileido.state import board  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import footprints_message, snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
MM = 0.001
LIBRARY = "Test library"
placed_with = []  # what the test library's `place` was handed: (reference, pins, part)
told_footprint = {}  # reference -> the last `footprint` the test library's `place` was handed


def make_library(folder):
    """A library of two parts and its index: a resistor (a 1.6 x 0.8 x 0.45 mm block, an
    inside and field lines) and a bead that is only for references starting "FB" (and has a
    clear cover over its insides)."""
    made = []
    for name, size in (("Test_R_0603", (1.6, 0.8, 0.45)), ("Test_Bead_0603", (1.6, 0.8, 0.8))):
        collection = bpy.data.collections.new(name)
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        body = bpy.context.active_object
        body.name = f"{name}: body"
        body.scale = Vector(size) * MM
        body.location = (0.0, 0.0, size[2] / 2 * MM)
        body["kls_role"] = "case"
        body["kls_time"] = 0.0  # the part's own clock, a driver from the frame: it must survive being placed
        body.driver_add('["kls_time"]').driver.expression = "frame / 24"
        body.data.materials.append(bpy.data.materials.new(f"{name}: material"))
        body.data.materials[0].use_nodes = True
        for users in tuple(body.users_collection):
            users.objects.unlink(body)
        collection.objects.link(body)
        core = bpy.data.objects.new(f"{name}: core", bpy.data.meshes.new(f"{name}: core"))
        core["kls_role"] = "insides"
        collection.objects.link(core)
        field = bpy.data.objects.new(f"{name}: field", bpy.data.meshes.new(f"{name}: field"))
        field["kls_role"] = "field"
        collection.objects.link(field)
        if name == "Test_Bead_0603":  # a clear cover over its insides, as an LED's lens
            lens = bpy.data.objects.new(f"{name}: lens", bpy.data.meshes.new(f"{name}: lens"))
            lens["kls_role"] = "cover"
            collection.objects.link(lens)
        made.append(collection)
    index = bpy.data.texts.new(parts.INDEX)
    index.write(json.dumps({
        "R_0603_1608Metric": [{"part": "Test_R_0603", "family": "R_CHIP"}],
        "L_0603_1608Metric": [{"part": "Test_Bead_0603", "reference_prefixes": ["FB"]}],
    }))
    path = folder / "test_library.blend"
    bpy.data.libraries.write(str(path), {*made, index}, fake_user=True)
    bpy.data.texts.remove(index)
    for collection in made:  # the board's file starts without them
        for obj in tuple(collection.objects):
            bpy.data.objects.remove(obj)
        bpy.data.collections.remove(collection)
    return path


def place(objects, footprint, part):
    """The test library's `place`: every object carries the value and the part's own index
    data, and the insides get their own mesh."""
    placed_with.append((footprint["reference"], footprint["pins"], part))
    told_footprint[footprint["reference"]] = footprint
    for obj in objects:
        obj["kls_test_value"] = footprint["value"]
        obj["kls_test_family"] = part.get("family", "")
        if obj.get("kls_role") == "insides":
            obj.data = obj.data.copy()
            obj.data["kls_dressed"] = 1
            obj.data["kls_test_value"] = footprint["value"]


def draw(layout, context):
    """The test library's own switches, as its add-on would draw them in the Parts column."""
    layout.label(text="Test switch")


def dressed_meshes():
    return [mesh for mesh in bpy.data.meshes if mesh.get("kls_dressed")]


def snapshot():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    identity = {"J1": ("Resistor_SMD:R_0603_1608Metric", "4k7"),  # a library part, no KiCad model
                "J2": ("Resistor_SMD:R_0603_1608Metric", "10k"),  # the same on the bottom
                "J3": ("Inductor_SMD:L_0603_1608Metric", "600R"),  # a bead's footprint, not a bead's reference
                "J4": ("Imported:MAKER-PART-123", "")}  # named by its field
    # J4 names the bead part itself (whatever its reference), turned a quarter
    fields = {"J1": [["Color", "Red"]], "J4": [["KiLeidoscope Model", "Test_Bead_0603 @90"]]}
    for footprint in data["footprints"]:
        footprint["lib_id"], footprint["value"] = identity[footprint["reference"]]
        footprint["fields"] = fields.get(footprint["reference"], [])
        footprint["pins"] = [["1", "A", "passive"], ["2", "K", "passive"]] if footprint["reference"] == "J1" else []
    return snapshot_from_jsonable(data)


def insides_of(reference):
    return next(obj for obj in objects_of(reference)[1] if obj.get("kls_role") == "insides")


def objects_of(reference):
    footprint = next(obj for obj in board.collection.all_objects
                     if obj.get("kls_footprint") == 1 and obj.get("kls_reference") == reference)
    return footprint, [board.collection.all_objects[name] for name in
                       board.model_objects_by_fp.get(footprint["kls_id"], ())]


def case_of(reference):
    return next(obj for obj in objects_of(reference)[1] if obj.get("kls_role") == "case")


def part_objects():
    return [obj for obj in bpy.data.objects if obj.get(parts.PART) == 1]


class Layout:
    """Records what the Parts column draws: (kind, text) per label, prop or operator."""

    def __init__(self, drawn=None):
        self.drawn = [] if drawn is None else drawn
        self.alignment, self.active, self.enabled, self.alert = "EXPAND", True, True, False

    def _child(self, *args, **kwargs):
        return type(self)(self.drawn)

    box = split = row = column = _child

    def label(self, text="", icon="NONE"):
        self.drawn.append(("label", text))

    def prop(self, data, name, **kwargs):
        self.drawn.append(("prop", name))

    def operator(self, idname, text="", icon="NONE", emboss=True):
        self.drawn.append(("operator", idname))
        return type("Properties", (), {})()

    def menu(self, menu, text="", icon="NONE"):
        self.drawn.append(("menu", text))


def column():
    layout = Layout()
    kileido._draw_parts(layout, bpy.context.scene)
    return layout.drawn


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    folder = Path(tempfile.mkdtemp(prefix="kls_parts_"))
    kicad_cli.cache_root = lambda: folder / "cache" / "exports"  # never the user's settings or My parts
    kileido.register()
    check(sys.modules.get(parts.MODULE_ALIAS) is parts and parts.API_VERSION >= 1, "other add-ons find the module")
    check(parts.my_parts_folder().parts[-2:] == ("KiLeidoscope", "My parts") and
          Path.home() in parts.my_parts_folder().parents, f"My parts start out with the user's files")
    parts.set_my_parts_folder(folder / "my-parts")
    check([row["key"] for row in parts.listing()] == ["my"], "at first: My parts alone")
    library = make_library(folder)
    check(not parts.has_library(LIBRARY), "not offered yet")
    sys.modules[parts.MODULE_ALIAS].add_library(LIBRARY, str(library), place=place, draw=draw)  # as an add-on does
    check(parts.has_library(LIBRARY), "offered")
    check([(row["name"], row["kind"], row["on"]) for row in parts.listing()] ==
          [("My parts", "my", False), (LIBRARY, "addon", False)], f"the libraries, none the Default: {parts.listing()}")
    first = snapshot()
    apply.load_frames(b"".join(snapshot_frames(first)))
    bpy.context.view_layer.update()
    check(not board.swapped and not part_objects(), "KiCad's models by default: no library part placed")
    check([key for key, _ in parts.options(objects_of("J1")[0])] == [f"addon:{LIBRARY}"],
          "but Model from offers the library that has J1's part")
    check(parts.default() == parts.KICAD, "KiCad is the Default to start with")
    parts.set_default(f"addon:{LIBRARY}")
    bpy.context.view_layer.update()

    # J1 and J2 carry the resistor, nothing else; its field lines placed, hidden
    footprint, placed = objects_of("J1")
    check([obj.get(parts.PART) for obj in placed] == [1, 1, 1], f"J1: {[obj.name for obj in placed]}")
    check(all("(part)" in obj.name and obj.parent == footprint for obj in placed), "named and held as its parts")
    lines = next(obj for obj in placed if obj.get("kls_role") == "field")
    check(lines.hide_get() and lines.hide_render and not lines.hide_viewport, "J1's field lines placed, hidden")
    part, core = sorted((obj for obj in placed if obj is not lines), key=lambda obj: obj.get("kls_role") != "case")
    check(part["kls_test_value"] == "4k7" and part["kls_test_family"] == "R_CHIP", "place was told value and part")
    told = next(row for row in placed_with if row[0] == "J1")
    check(told[1] == {"1": ("A", "passive"), "2": ("K", "passive")}, f"and J1's pins: {told[1]}")
    check(told[2] == {"part": "Test_R_0603", "family": "R_CHIP"}, f"and the index entry: {told[2]}")
    check(dict(footprint["kls_fields"]) == {"Color": "Red"}, "J1's fields")
    check(told_footprint["J1"]["id"] == footprint["kls_id"] == placed[0]["kls_model_fp_id"]
          and told_footprint["J1"]["label"] == "J1", f"place is told J1's id and label: {told_footprint['J1']}")
    check(all(key in obj for obj in placed for key in parts.PLACED_PROPERTIES), "placed objects carry what is promised")
    check(not part.hide_get() and not part.hide_render, "J1's part is shown")
    centre = part.matrix_world.translation
    check((centre.xy - footprint.matrix_world.translation.xy).length < 1e-9, "J1's part on its footprint")
    check(abs(centre.z - (board.thickness_m + 0.225 * MM)) < 1e-9, f"J1's part on the top face: {centre.z}")
    under = case_of("J2")
    check(abs(under.matrix_world.translation.z + 0.225 * MM) < 1e-9, "J2's part hangs under the board")
    check(not objects_of("J3")[1], "the bead is not for a reference J3")
    check(part.data == under.data, "the parts share their mesh")
    # its insides closed in its opaque case: not drawn, viewport or render, until something opens it
    check(core.hide_get() and core.hide_render, "J1's insides hidden inside its case")
    check(core.data.get("kls_dressed") == 1 and core.data["kls_test_value"] == "4k7" and core.data.users == 1,
          "place gave J1's insides their own mesh")
    check(part.material_slots[0].material in focus.shown_materials(), "X-ray reaches the part's material")
    drivers = part.animation_data.drivers if part.animation_data else []
    check([driver.data_path for driver in drivers] == ['["kls_time"]'], "the placed part keeps its own driver")
    bpy.context.scene.frame_set(48)
    clock = part.evaluated_get(bpy.context.evaluated_depsgraph_get())["kls_time"]
    check(abs(clock - 2.0) < 1e-6, f"and its clock runs with the frame: {clock}")
    bpy.context.scene.frame_set(1)
    j4, named = objects_of("J4")
    named = [obj for obj in named if obj.get("kls_role") == "case"]
    check(len(named) == 1 and named[0].get(parts.PART) == 1 and named[0].dimensions.z > 0.7 * MM,
          "J4's KiLeidoscope Model field names the bead part")
    long_axis = named[0].matrix_world.to_3x3() @ Vector((1.0, 0.0, 0.0))
    across = j4.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))
    check(abs(abs(long_axis.normalized().dot(across.normalized())) - 1.0) < 1e-6, "J4's part turned 90 degrees")
    check(parts.status() == "3 footprints use a library's part", parts.status())
    check(parts.source_of(footprint) == LIBRARY and parts.source_of(objects_of("J3")[0]) == "", "where models come from")

    # the Parts column: the Default and the libraries, the selected footprint, Transparent cases
    # (J1's part has a case), then the add-on's library's own box; nothing to save without a selection
    drawn = column()
    check(drawn[1:3] == [("label", "Default library"), ("menu", LIBRARY)] and ("label", "My parts") in drawn and ("label", LIBRARY) in drawn
          and ("label", "3 footprints use a library's part") in drawn, f"the Parts column: {drawn}")
    # an add-on's library is not a row in the list: its own box below, its name, its switches
    name = drawn.index(("label", LIBRARY)) if ("label", LIBRARY) in drawn else -1
    adding, saving = drawn.index(("operator", "kileido.part_library_add")), drawn.index(("operator", "kileido.save_part"))
    check(drawn.count(("label", LIBRARY)) == 1 and adding < saving < drawn.index(("prop", "kileido_see_through")) < name
          and drawn[name + 1] == ("label", "Test switch"),
          f"the Default menu names it; the footprint, Transparent cases, then its own box: {drawn}")
    check(LIBRARY in parts.libraries_in_use() and parts.has_cases(), "its parts placed, with cases")
    check([kind for kind, text in drawn].count("menu") == 1, "no footprint selected: the Default menu only, no Model from")
    check(not kileido.KILEIDO_OT_save_part.poll(bpy.context), "nothing to save yet")

    # X-ray with J1 selected: its insides stay, its case fades with the rest, no box over it
    bpy.context.scene.kileido_focus = True
    highlight.apply_selection({"selected": [], "pair": [], "pads": [], "footprints": [footprint["kls_id"]]})
    check(parts.selected() == footprint, "the Parts tab's footprint is KiCad's selection")
    drawn = column()
    check(("label", "J1: R_0603_1608Metric") in drawn and ("menu", LIBRARY) in drawn,
          f"the column says where J1's model comes from: {drawn}")
    # its part comes from a library whose `place` keeps it up to date: not saved to My parts
    check(parts.kept_up_by(footprint) == LIBRARY and not kileido.KILEIDO_OT_save_part.poll(bpy.context),
          "an add-on's kept-up part cannot be saved")
    try:
        parts.save_to_my_parts(footprint)
        check(False, "an add-on's kept-up part was saved")
    except ValueError as error:
        check(LIBRARY in str(error), f"and says why: {error}")
    check(not parts.my_parts_folder().exists() or not any(parts.my_parts_folder().iterdir()), "nothing written")
    check(core.get(focus.KEEP) == 1.0 and not part.get(focus.KEEP) and not under.get(focus.KEEP),
          "X-ray opens the selected part only")
    check(not core.hide_get() and not core.hide_render and insides_of("J2").hide_get(),
          "X-ray draws the opened part's insides only")
    check(tuple(core[focus.FOCUS]) == (1.0, 0.0, 0.0) and tuple(part[focus.FOCUS]) == (0.0, 0.0, 0.0),
          "the steady stage's one attribute says the same")
    box = board.collection.all_objects.get(f"KLS footprint highlight {footprint['kls_id']}")
    check(box is None or box.hide_get(), "no selection box over an opened part")
    bpy.context.scene.kileido_focus = False
    check(not core.get(focus.KEEP), "X-ray off: nothing kept")
    check(core.hide_get() and core.hide_render, "X-ray off: J1's insides closed in again")
    highlight.apply_selection({"selected": [], "pair": [], "pads": [], "footprints": []})
    check(parts.selected() is None, "nothing selected")

    # the cut plane opens every part: their insides drawn while it is on
    bpy.context.scene.kileido_cut = True
    check(not core.hide_get() and not insides_of("J2").hide_get(), "the cut plane draws every part's insides")
    bpy.context.scene.kileido_cut = False
    check(core.hide_get() and insides_of("J2").hide_get(), "cut plane off: closed in again")
    # a part a finding draws lifted (findings_draw): its insides stay hidden under the copy
    core["kls_lift_hidden"] = 1
    bpy.context.scene.kileido_cut = True
    check(core.hide_get() and core.hide_render, "a lifted part's insides stay hidden when the cut opens parts")
    bpy.context.scene.kileido_cut = False
    del core["kls_lift_hidden"]

    # Transparent cases: every case fades, without X-ray or a selection; insides stay
    bpy.context.scene.kileido_see_through = True
    check(part.get(focus.SEE_THROUGH) == 1.0 and under.get(focus.SEE_THROUGH) == 1.0
          and not core.get(focus.SEE_THROUGH), "transparent cases: the cases only")
    check(not core.hide_get() and not core.hide_render and not insides_of("J2").hide_get(),
          "transparent cases: every part's insides drawn")
    case_material = part.material_slots[0].material
    check(case_material.use_backface_culling and case_material.get(focus.OWN_CULLING) == 0,
          "transparent cases: the case's back faces culled, its own setting kept")
    # The slider reaches the case's transparency through maxima only (no x0.95 for X-ray's faint
    # grey on the way): at 1, nothing of the case is left
    stage = case_material.node_tree.nodes[focus.STEADY_NODE].node_tree
    clear = next(node for node in stage.nodes if node.type == "MIX_SHADER" and node.inputs[2].is_linked
                 and node.inputs[2].links[0].from_node.type == "BSDF_TRANSPARENT"
                 and any(link.to_node.type == "GROUP_OUTPUT" for link in node.outputs[0].links))
    slider = next(node for node in stage.nodes if node.type == "ATTRIBUTE" and node.attribute_type == "VIEW_LAYER")

    def through_maxima(node):
        """Whether `node`'s output reaches the clear mix's factor through MAXIMUM nodes only."""
        for link in node.outputs[0].links:
            if link.to_node == clear and link.to_socket == clear.inputs[0]:
                return True
            if link.to_node.type == "MATH" and link.to_node.operation == "MAXIMUM" and through_maxima(link.to_node):
                return True
        return False

    see = next(link.to_node for link in slider.outputs["Fac"].links if link.to_node.type == "MATH")
    check(see.operation == "MULTIPLY" and through_maxima(see),
          "the slider sets the case's transparency itself (at 1: nothing left of it)")
    check(tuple(part[focus.FOCUS]) == (0.0, 1.0, 0.0), "and in the steady stage's attribute")
    stage = part.material_slots[0].material.node_tree.nodes[focus.STEADY_NODE]
    check(stage.node_tree.name == focus.STEADY_GROUP, "the part's material has the current steady stage")
    # The Transparency slider is read by the stage from the scene, not copied onto objects: keyframed,
    # it animates in the viewport and in renders with nothing run per frame
    slider = [node for node in stage.node_tree.nodes if node.type == "ATTRIBUTE" and node.attribute_type == "VIEW_LAYER"]
    check([node.attribute_name for node in slider] == [focus.SEE_THROUGH_AMOUNT], "the stage reads the slider")
    bpy.context.scene.kileido_see_through_amount = 0.3
    check(part.get(focus.SEE_THROUGH) == 1.0, "moving the slider leaves the objects as they are")
    bpy.context.scene.kileido_see_through_amount = 0.85
    check(all(node.name.startswith(parts.STAGE_PREFIX) for node in part.material_slots[0].material.node_tree.nodes
              if node.type == "GROUP" and node.node_tree.name.startswith("KLS")),
          "KiLeidoscope's stages are named as STAGE_PREFIX promises")
    bpy.context.scene.kileido_see_through = False
    check(not part.get(focus.SEE_THROUGH), "transparent cases off")
    check(core.hide_get() and core.hide_render, "transparent cases off: insides closed in again")
    check(not case_material.use_backface_culling and focus.OWN_CULLING not in case_material,
          "transparent cases off: the case's own culling back")
    # the bead's part has a clear cover: its insides show through it, always
    check(not insides_of("J4").hide_get() and not insides_of("J4").hide_render, "insides under a clear cover stay")

    # the cut plane, as an add-on follows it
    check(parts.cut_plane() is None, "cut plane off: None")
    bpy.context.scene.kileido_cut = True
    bpy.context.view_layer.update()
    plane = parts.cut_plane()
    check(plane is not None and abs(Vector(plane[1]).length - 1.0) < 1e-6 and len(plane[0]) == 3,  # 32-bit
          f"cut plane on: a point and a unit normal: {plane}")
    bpy.context.scene.kileido_cut = False
    check(parts.cut_plane() is None, "and off again")

    # Field lines: they show, and hide again
    bpy.context.scene.kileido_show_fields = True
    check(not lines.hide_get() and not lines.hide_render, "Field lines: J1's show")
    bpy.context.scene.kileido_show_fields = False
    check(lines.hide_get() and lines.hide_render, "and hide again")

    # moved in KiCad: the part follows; a new value: placed again, and told
    moved = replace(first, footprints=tuple(
        replace(f, pos=(f.pos[0] + 2_000_000, f.pos[1]), value="1k") if f.reference == "J1" else f
        for f in first.footprints))
    apply.load_frames(footprints_message(moved, 2))
    bpy.context.view_layer.update()
    footprint, placed = objects_of("J1")
    part = case_of("J1")
    check(part["kls_test_value"] == "1k", "J1's new value")
    check((part.matrix_world.translation.xy - footprint.matrix_world.translation.xy).length < 1e-9,
          "J1's part follows its footprint")
    shifted = part.matrix_world.translation.x
    moved_again = replace(moved, footprints=tuple(
        replace(f, pos=(f.pos[0] + 1_000_000, f.pos[1])) if f.reference == "J1" else f for f in moved.footprints))
    apply.load_frames(footprints_message(moved_again, 3))
    bpy.context.view_layer.update()
    same = case_of("J1")
    check(same == part, "a move alone keeps the part")
    check(abs(same.matrix_world.translation.x - shifted - 1 * MM) < 1e-9, "the part moved with it")

    # the library's own switch changed: its parts are placed again, and told again
    told, count = len(placed_with), len(part_objects())
    parts.place_again(LIBRARY)
    check(len(placed_with) == told + 3 and len(part_objects()) == count, "placed again: place called for each part")
    part = case_of("J1")  # a new object: placed again
    check(part.animation_data is not None and part["kls_test_value"] == "1k", "and as they were: value, driver")

    # a bead's reference: the bead comes on
    renamed = replace(moved_again, footprints=tuple(
        replace(f, reference="FB3") if f.reference == "J3" else f for f in moved_again.footprints))
    apply.load_frames(footprints_message(renamed, 4))
    bead = objects_of("FB3")[1]
    check(len(bead) == 4 and all(obj.get("kls_part_name") == "Test_Bead_0603" for obj in bead), "FB3's bead")
    check(all(obj.name.startswith("FB3") for obj in bead), f"named after its reference: {[o.name for o in bead]}")

    # J1 and J2 trade references in one edit (a renumbering): each footprint keeps its own part,
    # and the names the board keeps for it lead to its objects, not to the other's; a new value on
    # one later places its part again and leaves the other's alone
    top, bottom = objects_of("J1")[0], objects_of("J2")[0]

    def own(footprint, value):
        """The footprint's part: its three objects, found by the names the board keeps for it,
        each the footprint's own, held by it, and placed with its value."""
        names = board.model_objects_by_fp.get(footprint["kls_id"], ())
        placed = [board.collection.all_objects.get(name) for name in names]
        return (len(placed) == 3 and list(board.swapped.get(footprint["kls_id"], {}).get("names", ())) == list(names) and
                all(obj is not None and obj.get("kls_model_fp_id") == footprint["kls_id"] and obj.parent == footprint
                    and obj["kls_test_value"] == value for obj in placed))

    def edit(snapshot, revision, *changes):
        """`snapshot` with each (footprint, {field: value}) of `changes` changed, applied as a live edit."""
        by_id = {footprint["kls_id"]: fields for footprint, fields in changes}
        edited = replace(snapshot, footprints=tuple(replace(f, **by_id[f.id]) if f.id in by_id else f
                                                    for f in snapshot.footprints))
        apply.load_frames(footprints_message(edited, revision))
        bpy.context.view_layer.update()
        return edited

    edited = edit(renamed, 5, (top, {"reference": "J2"}), (bottom, {"reference": "J1"}))
    check(own(top, "1k") and own(bottom, "10k"), "references traded: each footprint keeps its own part")
    edited = edit(edited, 6, (top, {"reference": "J1"}), (bottom, {"reference": "J2"}))
    check(own(top, "1k") and own(bottom, "10k"), "and traded back")
    edited = edit(edited, 7, (top, {"value": "2k2"}))
    check(own(top, "2k2") and own(bottom, "10k"), "a new value on J1 places J1's part again and leaves J2's")

    # two footprints of one reference (as KiCad's Create Array leaves them): each keeps its own part,
    # through new values on both and the references told apart again
    edited = edit(edited, 8, (bottom, {"reference": "J1"}))
    check(own(top, "2k2") and own(bottom, "10k"), "two J1s: each its own part")
    edited = edit(edited, 9, (top, {"value": "3k3"}), (bottom, {"value": "22k"}))
    check(own(top, "3k3") and own(bottom, "22k"), "two J1s, both with a new value: each its own part again")
    edited = edit(edited, 10, (bottom, {"reference": "J2"}))
    check(own(top, "3k3") and own(bottom, "22k"), "told apart again")

    # two J1s again, and the other one renumbered: the one keeping J1 holds the ".001" names. Then
    # every footprint moves (the board's origin moved) and KiCad's models are bound again: it keeps
    # its part (the banner board's three lost parts, 2026-11-26)
    edited = edit(edited, 11, (bottom, {"reference": "J1"}))
    edited = edit(edited, 12, (top, {"reference": "J5"}))
    for revision, step in ((13, -1_000_000), (14, 1_000_000)):
        edited = replace(edited, footprints=tuple(replace(f, pos=(f.pos[0] + step, f.pos[1])) for f in edited.footprints))
        apply.load_frames(footprints_message(edited, revision))
        parts.refresh(rebind=True)
        check(own(top, "3k3") and own(bottom, "22k"), f"every footprint moved, models bound again: each its own part ({revision})")
    edited = edit(edited, 15, (top, {"reference": "J1"}), (bottom, {"reference": "J2"}))

    # a part's objects gone from under the board (as a bug once removed them): the next edit places it again
    for name in tuple(board.model_objects_by_fp[bottom["kls_id"]]):
        parts._remove(board.collection.all_objects[name])  # with the mesh its `place` made
    edited = edit(edited, 16)
    check(own(bottom, "22k"), "a part whose objects are gone is placed again")
    renamed = edit(edited, 17, (top, {"value": "1k"}), (bottom, {"value": "10k"}))  # as the checks below have them
    check(own(top, "1k") and own(bottom, "10k"), "the values back")
    part = case_of("J1")

    # parts found again after their bookkeeping is lost (a saved file opened, an undo)
    count = len(part_objects())
    board.swapped.clear()
    parts.refresh()
    check(len(board.swapped) == 4 and len(part_objects()) == count and case_of("J1") == part,
          "the placed parts are the board's again, none placed twice")

    # Save to my parts: J1's model as it is, in a file of its footprint's name; My parts come first
    shiny = part.material_slots[0].material
    shiny.metallic, shiny.roughness = 1.0, 0.2
    highlight.apply_selection({"selected": [], "pair": [], "pads": [], "footprints": [footprint["kls_id"]]})
    told = len(placed_with)
    parts.add_user_library(library)  # the same file as a library of the user's own: no `place`, so it can be saved
    parts.set_default(f"user:{library}")
    check(parts.source_of(objects_of("J1")[0]) == "test_library" and not parts.kept_up_by(objects_of("J1")[0])
          and kileido.KILEIDO_OT_save_part.poll(bpy.context), "a part from the user's own library can be saved")
    saved = parts.save_to_my_parts(parts.selected())
    parts.remove_user_library(library)
    check(parts.default() == parts.KICAD, "the Default taken off the list: KiCad is the Default again")
    parts.set_default(f"addon:{LIBRARY}")
    check(saved == parts.my_parts_folder() / "R_0603_1608Metric.blend" and saved.is_file(), f"saved: {saved}")
    check(str(parts.my_parts_folder()).startswith(str(folder)), "in the test's own folder")
    with bpy.data.libraries.load(str(saved)) as (inside, _):
        check(list(inside.collections) == ["R_0603_1608Metric"] and len(inside.objects) == 2,
              f"the file holds the part, without its field lines: {list(inside.collections)} {list(inside.objects)}")
        check(not any(name.startswith("KLS") for name in inside.node_groups),
              f"and none of KiLeidoscope's own stages: {list(inside.node_groups)}")
    bpy.context.view_layer.update()
    mine, other = case_of("J1"), case_of("J2")
    check(parts.source_of(objects_of("J1")[0]) == "My parts" and parts.source_of(objects_of("J2")[0]) == "My parts",
          "every footprint of that name now takes My parts'")
    check(not any(row[0] in ("J1", "J2") for row in placed_with[told:]), "My parts have no code: placed, and that is all")
    material = mine.material_slots[0].material
    check(material != shiny and abs(material.roughness - 0.2) < 1e-6 and material.metallic == 1.0,
          "the saved part keeps the materials as they were set")
    check((mine.matrix_world.translation - part_centre(objects_of("J1")[0], 0.225)).length < 1e-9,
          "and sits where the model sat")
    check(abs(other.matrix_world.translation.z + 0.225 * MM) < 1e-9, "under the board too")
    check(parts.source_of(objects_of("FB3")[0]) == LIBRARY, "other footprints keep their library's part")
    check(parts.choices() == {"R_0603_1608Metric": parts.MY_KEY}, f"saved: picked for its name: {parts.choices()}")

    # saved again: the file is replaced, and the board takes the new one
    material.roughness = 0.6
    parts.save_to_my_parts(parts.selected())
    check(abs(case_of("J1").material_slots[0].material.roughness - 0.6) < 1e-6, "saved again: the new part is placed")
    highlight.apply_selection({"selected": [], "pair": [], "pads": [], "footprints": []})

    # a library the user adds: a folder, one .blend per part, used once it is the Default
    shelf = folder / "shelf"
    shelf.mkdir()
    shutil.copy(saved, shelf / "L_0603_1608Metric.blend")  # the file's name is the footprint's
    parts.add_user_library(shelf)
    check(parts.source_of(objects_of("FB3")[0]) == LIBRARY, "added, not the Default: not used")
    parts.set_default(f"user:{shelf}")
    check([row["kind"] for row in parts.listing()] == ["my", "user", "addon"], "the order they are listed")
    check(parts.source_of(objects_of("FB3")[0]) == "shelf" and parts.source_of(objects_of("J4")[0]) == "",
          "the Default: its part where it has one, KiCad's model where it has none")
    parts.set_default(f"addon:{LIBRARY}")
    check(parts.source_of(objects_of("FB3")[0]) == LIBRARY, "another Default: its part")
    parts.remove_user_library(shelf)
    check(parts.source_of(objects_of("FB3")[0]) == LIBRARY and len(parts.listing()) == 2, "taken off the list")
    check(settings.path().is_file() and str(settings.path()).startswith(str(folder)), "kept in the test's settings")

    # an add-on's library not the Default: its own switches stay in the column (its parts may still be picked)
    parts.set_default(parts.KICAD)
    check(("label", "Test switch") in column(), "KiCad the Default: the library's switches are still drawn")
    parts.set_default(f"addon:{LIBRARY}")

    # Model from: the Default again, KiCad's model, then My parts picked again
    check([key for key, _ in parts.options(objects_of("J1")[0])] == [parts.MY_KEY, f"addon:{LIBRARY}"],
          f"Model from offers the libraries with J1's part: {parts.options(objects_of('J1')[0])}")
    parts.set_choice("R_0603_1608Metric", None)
    check(parts.source_of(objects_of("J1")[0]) == LIBRARY and case_of("J1")["kls_test_value"] == "1k",
          "the Default: the library's part, told its value again")
    parts.set_choice("R_0603_1608Metric", parts.KICAD)
    check(parts.source_of(objects_of("J1")[0]) == "" and parts.source_of(objects_of("J2")[0]) == ""
          and parts.source_of(objects_of("FB3")[0]) == LIBRARY,
          "KiCad picked: KiCad's model for that name only, whatever the Default (J1 and J2 have none)")
    parts.set_choice("R_0603_1608Metric", parts.MY_KEY)
    check(parts.source_of(objects_of("J1")[0]) == "My parts", "My parts picked again")

    # KiCad's models everywhere: the parts come off, and come back
    parts.set_default(parts.KICAD)
    parts.set_choice("R_0603_1608Metric", None)
    check(not board.swapped and not part_objects(), "KiCad the Default, nothing picked: no library parts")
    check(not dressed_meshes(), f"and none of the meshes place made: {dressed_meshes()}")
    drawn = column()
    check(("prop", "kileido_see_through") not in drawn and parts.status() == "" and not parts.libraries_in_use()
          and ("label", "Test switch") in drawn,
          f"no case to fade: no Transparent cases; the library's box stays, greyed: {drawn}")
    parts.set_default(f"addon:{LIBRARY}")
    parts.set_choice("R_0603_1608Metric", parts.MY_KEY)
    check(len(board.swapped) == 4, f"back again: {len(board.swapped)}")

    # KiCad's models bound again (an export arrived): the parts stay in their place
    names = {footprint_id: list(names) for footprint_id, names in board.model_objects_by_fp.items()}
    board.model_objects_by_fp.clear()
    parts.refresh(rebind=True, settle=False)
    check(board.model_objects_by_fp == names, "rebound: the parts are the footprints' models again")

    # a library that cannot be used never stops the board
    check(parts._entries([{"part": ["x"]}, {"part": 5}, {"part": "y"}]) == [{"part": "y"}],
          "index entries that are not parts are passed over")
    check(parts._by_reference([{"part": "x", "reference_prefixes": 5}, {"part": "z", "reference_prefixes": None}],
                              "FB1") is None, "and prefixes that are not text")

    def problem():
        return next(row for row in parts.listing() if row["name"] == LIBRARY)["problem"]

    fb3 = objects_of("FB3")[0]
    real_find = parts._Library.find

    def failing_find(self, *args):
        if self.name == LIBRARY:
            raise TypeError("an index KiLeidoscope cannot follow")
        return real_find(self, *args)

    parts._Library.find = failing_find
    try:
        apply.load_frames(footprints_message(renamed, 5))  # a live edit while the library cannot be read
    finally:
        parts._Library.find = real_find
    check(problem() == parts.BROKEN_INDEX and parts.source_of(fb3) == ""
          and parts.source_of(objects_of("J1")[0]) == "My parts",
          "an index that fails: that library is passed over, the others place, the edit goes through")
    parts.refresh_all()
    check(problem() == "" and parts.source_of(fb3) == LIBRARY, "readable again: its parts come back")

    real_attach = parts.components.attach

    def failing_attach(obj, footprint, local):
        if obj.get("kls_part_library") == LIBRARY:
            raise RuntimeError("a part that cannot be placed")
        return real_attach(obj, footprint, local)

    parts.components.attach = failing_attach
    try:
        parts.place_again(LIBRARY)
    finally:
        parts.components.attach = real_attach
    check(problem() == parts.NOT_PLACED and parts.source_of(fb3) == ""
          and not any(obj.get("kls_model_fp_id") == fb3["kls_id"] for obj in part_objects()),
          "a part that fails to place: nothing of it left, KiCad's model back")
    told = len(placed_with)
    apply.load_frames(footprints_message(renamed, 6))
    check(problem() == parts.NOT_PLACED and len(placed_with) == told, "and it is not tried again on every edit")
    parts.place_again(LIBRARY)
    check(problem() == "" and parts.source_of(fb3) == LIBRARY, "asked again: placed")

    def dropping(objects, footprint, part):
        place(objects, footprint, part)
        bpy.data.objects.remove(objects[-1])  # a `place` that removes one of its objects

    parts.add_library(LIBRARY, str(library), place=dropping, draw=draw)
    parts.place_again(LIBRARY)
    names = board.model_objects_by_fp[fb3["kls_id"]]
    check(len(names) == 3 and all(name in bpy.data.objects for name in names),
          f"a place that removes an object: the rest stay the footprint's: {names}")
    parts.add_library(LIBRARY, str(library), place=place, draw=draw)
    parts.place_again(LIBRARY)

    # the add-on takes its library back: only My parts' stay
    parts.remove_library(LIBRARY)
    check(sorted(board.swapped) == sorted(obj["kls_id"] for obj in (objects_of("J1")[0], objects_of("J2")[0])),
          "no add-on library: My parts' only")

    # Remove from my parts: the file goes, and so does the choice; KiCad's models again
    highlight.apply_selection({"selected": [], "pair": [], "pads": [], "footprints": [objects_of("J1")[0]["kls_id"]]})
    check(kileido.KILEIDO_OT_remove_part.poll(bpy.context) and ("operator", "kileido.remove_part") in column(),
          "My parts holds J1's part: it can be removed")
    removed = parts.remove_from_my_parts(parts.selected())
    check(not removed.exists() and "R_0603_1608Metric" not in parts.choices() and not board.swapped,
          "removed: no file, no choice, KiCad's models again")
    check(not kileido.KILEIDO_OT_remove_part.poll(bpy.context), "and nothing left to remove")
    highlight.apply_selection({"selected": [], "pair": [], "pads": [], "footprints": []})

    # a saved copy opened again, without KiCad: its parts are the board's, so the cut plane and
    # Transparent cases still open them (no live edit comes to call refresh)
    parts.add_library(LIBRARY, str(library), place=place, draw=draw)
    parts.set_default(f"addon:{LIBRARY}")
    bpy.context.view_layer.update()
    placed = len(board.swapped)
    path = str(folder / "saved.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    check(board.saved_copy and len(board.swapped) == placed > 0 and parts.has_cases(),
          f"reopened: the saved parts are the board's ({len(board.swapped)} of {placed}), Transparent cases offered")
    core = insides_of("J1")
    check(core.hide_get() and core.hide_render, "reopened: J1's insides closed in, as saved")
    bpy.context.scene.kileido_cut = True
    check(not core.hide_get() and not core.hide_render, "reopened: the cut plane opens the parts")
    bpy.context.scene.kileido_cut = False
    bpy.context.scene.kileido_see_through = True
    check(not core.hide_get() and not core.hide_render, "reopened: Transparent cases open them")
    bpy.context.scene.kileido_see_through = False
    check(core.hide_get(), "and close them again")
    kileido.unregister()
    check(parts.MODULE_ALIAS not in sys.modules, "unregistered: the module's alias is gone")
    shutil.rmtree(folder, ignore_errors=True)
    print("KLS_PARTS_OK")


def part_centre(footprint, height_mm):
    """Where a part's body centre is: over its footprint, half its height off the board's face."""
    centre = footprint.matrix_world.translation.copy()
    centre.z = board.thickness_m + height_mm * MM
    return centre


main()
