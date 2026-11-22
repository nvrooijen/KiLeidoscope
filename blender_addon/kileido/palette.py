"""KiCad's model materials, recognised by their colour.

KiCad's models reach Blender through kicad-cli's GLB export, which keeps each material's
colour and nothing else: the names come in as mat_0, mat_1, ... and Blender's importer
makes every material fully metallic and fully rough (measured 2026-10-19, KiCad 10.0.6,
Blender 4.5 and 5.2). KiCad 10's library holds STEP models only, and those carry a colour
per face and no more: no names, no transparency (measured 2026-11-22 on 285 files).

KiCad's library paints its models from one palette of named materials (KiCad 9's .wrl
files: 38 names, Mario Luzeiro's 2016 "Component materials reference list"), and KiCad 10's
STEP models carry the same colours. So a colour gives back the name, and the name a look:
metal, plastic, ceramic, glass. In a sample of KiCad 10's STEP files, 74 % use only palette
colours, 18 % some. Four colours carry two names each; the look is the more common one's
(an LED's red lens is RED-BODY's opaque plastic, the gold of a USB connector's GLASS-29 is
PIN-02's metal).

`apply` gives an imported model's materials their look and tags each with its name
(`kls_kicad_material`), once. A colour not in the palette is left as imported. The tag
stays on a copy of the material (Save to my parts), so its metal is still known after
the colour is changed. `is_metal` is what the reflowed solder climbs.

The looks are ours, not KiCad's numbers: the palette's specular colours and shininess were
picked for KiCad's own viewer (PIN-01's specular is CAP-CERAMIC-01's), and none says metal.
"""

VERSION = 1  # a material tagged with an older version gets its look again
TAG = "kls_kicad_material"  # the palette name a material was recognised as
APPLIED = "kls_palette"  # the version of the look it has
TOLERANCE = 0.004  # sRGB: the GLB's colours match the library's to 3 decimals; KiCad 10's
                   # STEP files round a few (PIN-02 0.859 0.737 0.494); two palette colours
                   # are no closer than 0.036 (PIN-02 gold and RES-THT-01)

METAL, PLASTIC, LENS, GLASS = "metal", "plastic", "lens", "glass"

# (name, sRGB diffuse as KiCad's .wrl files give it, kind, roughness)
ENTRIES = (
    ("IC-BODY-EPOXY-04", (0.148, 0.145, 0.145), PLASTIC, 0.5),
    ("IC-BODY-EPOXY-01", (0.250, 0.262, 0.281), PLASTIC, 0.5),
    ("RES-SMD-01", (0.082, 0.086, 0.094), PLASTIC, 0.55),
    ("PIN-01", (0.824, 0.820, 0.781), METAL, 0.3),  # tinned leads, terminations and pins
    ("PIN-02", (0.859, 0.738, 0.496), METAL, 0.2),  # gold; also GLASS-29
    ("MET-01", (0.298, 0.298, 0.298), METAL, 0.45),
    ("MET-SILVER", (0.508, 0.508, 0.508), METAL, 0.3),
    ("MET-COPPER", (0.704, 0.270, 0.083), METAL, 0.3),
    ("MET-ALUMINUM", (0.372, 0.372, 0.373), METAL, 0.4),
    ("MET-BRONZE", (0.714, 0.428, 0.181), METAL, 0.35),
    ("IC-LABEL-01", (0.691, 0.664, 0.598), PLASTIC, 0.8),
    ("PLASTIC-WHITE-01", (0.895, 0.891, 0.813), PLASTIC, 0.4),  # also LED-WHITE
    ("PLASTIC-BLUE-01", (0.137, 0.402, 0.727), PLASTIC, 0.4),
    ("PLASTIC-GREEN-01", (0.340, 0.680, 0.445), PLASTIC, 0.4),
    ("PLASTIC-YELLOW-01", (0.832, 0.680, 0.066), PLASTIC, 0.4),
    ("PLASTIC-ORANGE-01", (0.809, 0.426, 0.148), PLASTIC, 0.4),
    ("PLASTIC-GREEN-27", (0.136, 0.191, 0.122), PLASTIC, 0.4),
    ("CAP-CERAMIC-02", (0.578, 0.336, 0.352), PLASTIC, 0.65),
    ("CAP-CERAMIC-05", (0.273, 0.273, 0.273), PLASTIC, 0.65),
    ("CAP-CERAMIC-06", (0.379, 0.270, 0.215), PLASTIC, 0.65),
    ("RES-THT-01", (0.883, 0.711, 0.492), PLASTIC, 0.45),
    ("RED-BODY", (0.700, 0.100, 0.050), PLASTIC, 0.35),  # also LED-RED
    ("LED-BLUE", (0.100, 0.250, 0.700), LENS, 0.1),
    ("LED-GREEN", (0.400, 0.700, 0.150), LENS, 0.1),
    ("LED-YELLOW", (0.980, 0.840, 0.066), LENS, 0.1),
    ("LED-GREY", (0.270, 0.250, 0.270), LENS, 0.1),
    ("LED-BLACK", (0.100, 0.050, 0.100), LENS, 0.1),
    ("GLASS-19", (0.401, 0.442, 0.459), GLASS, 0.05),  # also GLASS-GREY
    ("GLASS-13", (0.000, 0.631, 0.748), GLASS, 0.05),
    ("GLASS-GREEN", (0.000, 0.750, 0.440), GLASS, 0.05),
    ("GLASS-ORANGE", (0.750, 0.440, 0.000), GLASS, 0.05),
    ("BOARD-GREEN-02", (0.070, 0.300, 0.120), PLASTIC, 0.4),
    ("BOARD-BLUE-01", (0.070, 0.120, 0.300), PLASTIC, 0.4),
    ("BOARD-BLACK-03", (0.160, 0.160, 0.160), PLASTIC, 0.4),
)
BY_NAME = {entry[0]: entry for entry in ENTRIES}
DARK = 0.3  # a plastic darker than this (sRGB) gets a satin sheen, not a grey haze
DARK_SPECULAR = 0.25
TRANSMISSION = {LENS: 0.8, GLASS: 1.0}


def _to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _principled(material):
    tree = material.node_tree if material is not None else None
    return next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None) if tree else None


def colour(material):
    """A material's base colour in sRGB, or None (no nodes' colour known: a texture)."""
    if material is None:
        return None
    if material.node_tree is None:
        return tuple(_to_srgb(c) for c in material.diffuse_color[:3])
    principled = _principled(material)
    if principled is None or principled.inputs["Base Color"].is_linked:
        return None
    return tuple(_to_srgb(c) for c in principled.inputs["Base Color"].default_value[:3])


def by_colour(srgb):
    """The palette entry of this sRGB colour, or None."""
    if srgb is None:
        return None
    return next((entry for entry in ENTRIES
                 if all(abs(a - b) <= TOLERANCE for a, b in zip(srgb, entry[1]))), None)


def entry(material):
    """A material's palette entry: by its tag, else by its colour; None for neither."""
    if material is None:
        return None
    tagged = BY_NAME.get(material.get(TAG))
    return tagged if tagged is not None else by_colour(colour(material))


def is_metal(material):
    """True for a material KiCad's palette calls metal (PIN-01, PIN-02, MET-*)."""
    found = entry(material)
    return found is not None and found[2] == METAL


def _set(node, names, value):
    for name in names:
        if name in node.inputs:
            node.inputs[name].default_value = value
            return


def look(material, found):
    """Give the material the look of palette entry `found`, keeping its colour."""
    principled = _principled(material)
    if principled is None:
        return
    name, srgb, kind, roughness = found
    principled.inputs["Metallic"].default_value = 1.0 if kind == METAL else 0.0
    principled.inputs["Roughness"].default_value = roughness
    _set(principled, ("Specular IOR Level",), DARK_SPECULAR if kind == PLASTIC and max(srgb) < DARK else 0.5)
    _set(principled, ("Transmission Weight",), TRANSMISSION.get(kind, 0.0))
    if kind in TRANSMISSION:
        _set(principled, ("IOR",), 1.5)
        if hasattr(material, "use_raytrace_refraction"):
            material.use_raytrace_refraction = True


def apply(materials):
    """Each of KiCad's imported model materials in the palette gets its look and its name
    tag, once per VERSION; the others are left as imported. Returns how many changed."""
    changed = 0
    for material in materials:
        if material is None or material.get(APPLIED) == VERSION:
            continue
        found = by_colour(colour(material))
        if found is not None:
            look(material, found)
            material[TAG] = found[0]
            changed += 1
        material[APPLIED] = VERSION
    return changed


def materials_of(objects):
    """The materials of these objects' meshes and slots."""
    found = set()
    for obj in objects:
        if obj.type != "MESH":
            continue
        found.update(slot.material for slot in obj.material_slots if slot.material is not None)
        found.update(material for material in obj.data.materials if material is not None)
    return found
