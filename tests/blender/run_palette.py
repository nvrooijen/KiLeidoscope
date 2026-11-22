"""Headless Blender checks of KiCad's palette (palette.py): imported model materials in its
colours get their look and name once, others stay as imported, a user's later change is
kept, the name outlives a new colour and a saved copy, and binding KiCad's models applies it.

blender --background --factory-startup --python tests/blender/run_palette.py
"""

import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kileido  # noqa: E402
from kileido import apply, models, objects, palette, parts, shading, state  # noqa: E402
import run_solder  # noqa: E402  (its frames)

J3 = run_solder.J3


def imported(name, srgb):
    """A material as Blender's glTF importer makes KiCad's: its colour, fully metallic and rough."""
    made = bpy.data.materials.new(name)  # with its node tree (Blender 5)
    node = principled(made)
    node.inputs["Base Color"].default_value = (*shading.srgb_to_linear(srgb), 1.0)
    node.inputs["Metallic"].default_value = 1.0
    node.inputs["Roughness"].default_value = 1.0
    return made


def principled(material):
    return next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")


def value(material, name):
    return principled(material).inputs[name].default_value


def check_looks():
    tin = imported("tin", (0.824, 0.820, 0.781))
    gold = imported("gold, rounded by KiCad 10's STEP", (0.859, 0.737, 0.494))
    epoxy = imported("epoxy", (0.148, 0.145, 0.145))
    lens = imported("blue lens", (0.100, 0.250, 0.700))
    glass = imported("glass", (0.000, 0.631, 0.748))
    vendor = imported("a vendor's purple", (0.5, 0.2, 0.9))
    assert palette.apply([tin, gold, epoxy, lens, glass, vendor, None]) == 5

    assert tin[palette.TAG] == "PIN-01" and gold[palette.TAG] == "PIN-02"
    for metal in (tin, gold):
        assert value(metal, "Metallic") == 1.0 and value(metal, "Roughness") < 0.5, metal.name
    assert value(epoxy, "Metallic") == 0.0 and value(epoxy, "Specular IOR Level") < 0.5  # satin, not hazy
    for clear, weight in ((lens, 0.8), (glass, 1.0)):
        assert value(clear, "Metallic") == 0.0 and abs(value(clear, "Transmission Weight") - weight) < 1e-6
    # Not in the palette: as imported, untagged, and not metal
    assert palette.TAG not in vendor and value(vendor, "Metallic") == 1.0 and value(vendor, "Roughness") == 1.0
    assert not palette.is_metal(vendor) and not palette.is_metal(epoxy) and palette.is_metal(tin)

    # Once: what the user changed afterwards stays; a newer look version applies again
    principled(epoxy).inputs["Roughness"].default_value = 0.9
    assert palette.apply([epoxy]) == 0 and abs(value(epoxy, "Roughness") - 0.9) < 1e-6
    epoxy[palette.APPLIED] = palette.VERSION - 1
    assert palette.apply([epoxy]) == 1 and abs(value(epoxy, "Roughness") - 0.5) < 1e-6

    # The name outlives a new colour, and a saved copy keeps it (Save to my parts)
    principled(tin).inputs["Base Color"].default_value = (0.8, 0.0, 0.0, 1.0)
    assert palette.is_metal(tin)
    saved = parts._plain(tin)
    assert saved[palette.TAG] == "PIN-01" and palette.is_metal(saved)
    # An untagged material counts by its colour alone
    assert palette.is_metal(imported("bronze", (0.714, 0.428, 0.181)))


def check_bind():
    """KiCad's models bound to the board get the palette's looks."""
    apply.load_frames(run_solder.frames())
    glb = bpy.data.objects.new("GLB root", None)
    bpy.context.scene.collection.objects.link(glb)
    mesh = bpy.data.meshes.new("J3 model")
    mesh.from_pydata([(-5e-4, -5e-4, 0), (5e-4, -5e-4, 0), (5e-4, 5e-4, 0), (-5e-4, 5e-4, 0)], [], [(0, 1, 2, 3)])
    lead = imported("mat_0", (0.824, 0.820, 0.781))
    mesh.materials.append(lead)
    source = bpy.data.objects.new("J3", mesh)  # KiCad names a model's GLB node by its reference
    bpy.context.scene.collection.objects.link(source)
    source.parent = glb
    source.location = (0.031, -0.024, 0.0016)  # near J3 in the GLB's frame
    bpy.context.view_layer.update()
    assert models.bind_root(glb, "c" * 64)["matched"] == 1
    (name,) = state.board.model_objects_by_fp[J3]
    bound = objects.lookup(name).data.materials[0]
    assert bound == lead and bound[palette.TAG] == "PIN-01", dict(bound)
    assert abs(value(bound, "Roughness") - 0.3) < 1e-6


def main():
    assert bpy.app.background
    kileido.register()
    try:
        check_looks()
        check_bind()
        print("palette checks passed")
    finally:
        kileido.unregister()


main()
