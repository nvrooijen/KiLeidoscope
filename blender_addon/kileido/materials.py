"""KiLeidoscope's board materials and the three colour modes (board stackup, realistic, PCB Editor).

Materials are emission shaders (flat colours) that gain a Principled BSDF in
Realistic mode. Colours come from the bridge's appearance data (board_specs):
KiCad's 3D-viewer colours, saved stackup colours, or the PCB Editor theme.
"""

import bpy

from . import cut, focus, holes, laminate, section, shading
from .placement import copper_thickness
from .state import board

MODES = ("FAB", "EDITOR", "REALISTIC")
BARE_COPPER = (184 / 255, 115 / 255, 50 / 255)  # KiCad 3D viewer's "Copper" finish colour
# What copper under the mask reflects back through it (sRGB of copper's PBR reflectance,
# linear 0.955, 0.638, 0.538): far more than the laminate, so mask over copper is lighter.
COPPER_UNDER_MASK = (0.98, 0.82, 0.76)
LAMINATE_SHADE = 0.2  # the laminate returns little light: mask over it this much darker (full mask)
FALLBACK_COPPER = (0.7, 0.6, 0.1)
FALLBACK_MASK = (0.29, 0.49, 0.71)
FALLBACK_CORE = (0.43, 0.45, 0.29)

HIGHLIGHT_COLORS = {"selected": (1.0, 0.27, 0.0), "pair": (0.0, 0.2, 1.0)}  # red-orange / blue (sRGB)
PLACEHOLDER_COLOR = (0.36, 0.43, 0.52)  # a component whose model file is missing: a grey-blue box
OUTLINE_PROBLEM_COLOR = (1.0, 0.0, 0.0)  # a malformed board outline: bright red
OUTLINE_PROBLEM_GLOW = 0.8  # higher reads orange in AgX
HIGHLIGHT_METALLIC = 1.0
HIGHLIGHT_ROUGHNESS = 0.25
HIGHLIGHT_GLOW = 0.5  # emission strength: vivid under any lighting (glow 1.0 washed to salmon)
HIGHLIGHT_BOX_ALPHA = 0.3
TENT_ALPHA = 0.999  # below 1: `make` builds the mix that hides a tent with its mask (`paint_tents`)

COPPER_METALLIC, COPPER_ROUGHNESS = 1.0, 0.25
# KiCad's finish colours are display swatches (ENIG: 0.70, 0.61, 0.0): as a metal's
# reflectance they are far too dark, and full metal then read brown. Realistic mode
# keeps their hue at a metal's brightness, whitened like measured reflectances
# (gold 1.0, 0.78, 0.34 linear) a little, so it stays gold under bright reflections.
METAL_WHITEN = 0.12
# LPI solder mask: a pigmented body under a glossy resin surface (Principled coat),
# not on the pads it leaves open or under the silkscreen ink printed on it.
MASK_COAT_ROUGHNESS = 0.08
COAT_WEIGHT = "KLS coat weight"
COAT_AMOUNT = "KLS coat amount"  # 1 while the mask covers the copper (0: mask hidden)
RELIEF_BUMP = "KLS relief bump"
RELIEF_HEIGHT = 1.0  # the mask's rise over copper edges, times the copper thickness
# Materials whose copper shows the board finish in mask openings, and the mask plots they sample.
FINISHED = {"copper:F.Cu": ("F",), "copper:B.Cu": ("B",), "vias": ("F", "B")}
BASE_COLOR = "KLS base color"  # an RGB node `paint` sets where the colour feeds the silkscreen mix
SILK_OPACITY = "KLS silk opacity"
SHEET_OPACITY = "KLS silk sheet opacity"  # the flat silkscreen sheet (cosmetics._overlay_material)


def create_all():
    """Every shared KiLeidoscope material, by role (existing ones are reused by name).
    Copper gets one material per layer on first use (`layer_material`)."""
    board.materials = {
        "vias": make("KLS Vias", (1.0, 0.6, 0.16)),
        "board": make("KLS Board mask top", FALLBACK_MASK),
        "board_bottom": make("KLS Board mask bottom", FALLBACK_MASK),
        "board_core": make("KLS Board FR4 core", FALLBACK_CORE),
        "footprint_placeholder": make("KLS Component placeholders", PLACEHOLDER_COLOR, 0.55),
        "solder": make("KLS Solder", (0.5, 0.5, 0.5)),
        "plating": make("KLS Hole plating", (0.75, 0.61, 0.23)),
        # A via barrel the finish never reached (a tent, plug or fill closes it): bare copper.
        "plating_bare": make("KLS Hole plating bare", BARE_COPPER),
        # The solder mask spanning a tented via's drill, one per side; see-through while hidden.
        # A plugged via's plug: solder mask ink, in the board's mask colour.
        "via_plug": make("KLS Via plug ink", FALLBACK_MASK),
        "tent_F": make("KLS Via tent top", FALLBACK_MASK, TENT_ALPHA),
        "tent_B": make("KLS Via tent bottom", FALLBACK_MASK, TENT_ALPHA),
        # A resin plug's core: milky, so the barrel around it shows through (as in the cut).
        "via_resin": make("KLS Via resin", section.RESIN, cut.RESIN_OPACITY),
        "highlight_selected": make("KLS Highlight selected", HIGHLIGHT_COLORS["selected"]),
        "highlight_pair": make("KLS Highlight pair", HIGHLIGHT_COLORS["pair"]),
        # Via barrels sit inside the drill, where the hole mask makes the land
        # materials see-through; they need their own solid copy.
        "highlight_selected_barrel": make("KLS Highlight selected barrel", HIGHLIGHT_COLORS["selected"]),
        "highlight_pair_barrel": make("KLS Highlight pair barrel", HIGHLIGHT_COLORS["pair"]),
        # A glow shell, not a surface: flat translucent red-orange in every mode.
        "highlight_box": make("KLS Highlight component", HIGHLIGHT_COLORS["selected"], HIGHLIGHT_BOX_ALPHA),
        "highlight_outline": make("KLS Highlight outline", OUTLINE_PROBLEM_COLOR),
    }


def make(name, color, alpha=1.0):
    """A flat emission material (translucent below alpha 1); an existing one is reused."""
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing
    material = bpy.data.materials.new(name)
    material.diffuse_color = (*color, alpha)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (*color, 1)
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    if alpha < 1:
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        mix = tree.nodes.new("ShaderNodeMixShader")
        mix.inputs[0].default_value = alpha
        tree.links.new(transparent.outputs[0], mix.inputs[1])
        tree.links.new(emission.outputs[0], mix.inputs[2])
        tree.links.new(mix.outputs[0], output.inputs["Surface"])
    else:
        tree.links.new(emission.outputs[0], output.inputs["Surface"])
    return material


def paint(material, color):
    """Recolour a KiLeidoscope-owned material without rebuilding geometry (`color` is sRGB)."""
    linear = shading.srgb_to_linear(color[:3])
    material.diffuse_color = (*linear, 1.0)
    for node in material.node_tree.nodes:
        if node.type == "EMISSION":
            node.inputs["Color"].default_value = (*linear, 1.0)
        elif node.type == "BSDF_PRINCIPLED":
            node.inputs["Base Color"].default_value = (*linear, 1.0)
        elif node.name == BASE_COLOR:  # the colour under the silkscreen ink (`print_silk`)
            node.outputs[0].default_value = (*linear, 1.0)


def via_inputs(highlight_material=None):
    """The via group's materials besides its lands': finished and bare barrels, milky resin
    and copper fills, and the tents (or a highlight's for all, so a selected via stays
    solid in X-ray mode)."""
    if highlight_material is not None:
        return {name: highlight_material for name in ("Drill Material", "Bare Drill Material", "Fill Material",
                                                      "Copper Fill Material", "Plug Material", "Tent Top Material",
                                                      "Tent Bottom Material")}
    return {"Drill Material": board.materials["plating"], "Bare Drill Material": board.materials["plating_bare"],
            "Fill Material": board.materials["via_resin"], "Copper Fill Material": board.materials["plating"],
            "Plug Material": board.materials["via_plug"],
            "Tent Top Material": board.materials["tent_F"], "Tent Bottom Material": board.materials["tent_B"]}


def plug_ink():
    """A plug's solder mask ink (sRGB): the top mask's colour, else the bottom's, shown or not."""
    mask = mask_color("F", shown=True) or mask_color("B", shown=True)
    return tuple(mask[:3]) if mask else FALLBACK_MASK


def paint_tents():
    """A tent is the mask over a hole: coloured like the mask over copper around it (the
    land's covered colour), gone while that side's mask is hidden. Plugs are the ink itself."""
    realistic = board.color_mode == "REALISTIC"
    if "via_plug" in board.materials:
        paint(board.materials["via_plug"], plug_ink())
        set_surface(board.materials["via_plug"], realistic, 0.0, 0.4)
    for side in "FB":
        material = board.materials.get(f"tent_{side}")
        if material is None:
            continue
        mask = mask_color(side)
        paint(material, seen_through(mask, COPPER_UNDER_MASK) if mask else FALLBACK_MASK)
        set_surface(material, realistic, 0.0, 0.35)
        mix = next(node for node in material.node_tree.nodes if node.type == "MIX_SHADER")
        mix.inputs[0].default_value = 1.0 if mask else 0.0
    cut.invalidate()  # the section draws the tents in the mask colour too


def set_surface(material, realistic, metallic=0.0, roughness=0.4):
    """Route the lit (Realistic) or flat (emission) shader to the material's surface."""
    tree = material.node_tree
    emission = next(node for node in tree.nodes if node.type == "EMISSION")
    principled = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if principled is None:
        principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled.inputs["Base Color"].default_value = emission.inputs["Color"].default_value
    principled.inputs["Metallic"].default_value = metallic
    principled.inputs["Roughness"].default_value = roughness
    mix = next((node for node in tree.nodes if node.type == "MIX_SHADER"), None)
    target = mix.inputs[2] if mix else focus.surface_input(material)
    tree.links.new((principled if realistic else emission).outputs[0], target)


# --- Colours --------------------------------------------------------------------------------

def finish_color():
    """Display swatches, not measured optical material properties.

    For a known finish, the bridge's resolved KiCad 3D-viewer copper colour wins, so
    Blender shows the same gold/tin/silver as KiCad (board_specs.viewer_colors).
    """
    finish = str(board.appearance.get("copper_finish") or "").strip().casefold()
    if finish in {"none", "no finish", "osp"}:
        return BARE_COPPER
    viewer_copper = board.appearance.get("viewer", {}).get("copper")
    if viewer_copper and finish and finish != "not specified":
        return tuple(viewer_copper[:3])
    if finish in {"enig", "enig + hard gold", "enepig", "hard gold"}:
        return (0.83, 0.68, 0.30)
    if finish in {"hasl", "hasl lead-free", "lead-free hasl", "immersion tin", "immersion silver"}:
        return (0.78, 0.80, 0.82)
    return None


def metal_color(color):
    """A finish swatch (sRGB) as a metal's reflectance (sRGB): same hue, brightest
    channel at full reflectance, whitened by METAL_WHITEN."""
    linear = shading.srgb_to_linear(color[:3])
    peak = max(max(linear), 1e-6)
    whitened = tuple(channel / peak * (1 - METAL_WHITEN) + METAL_WHITEN for channel in linear)
    return tuple(c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in whitened)


def _lit_metal(color):
    """`color` (sRGB), or its metal reflectance in Realistic mode."""
    return metal_color(color) if board.color_mode == "REALISTIC" else color


def _copper_color(layer, fallback=FALLBACK_COPPER):
    """Colour of exposed copper; `finish_mask` swaps in bare copper where mask covers it."""
    appearance = board.appearance
    if board.color_mode == "EDITOR":
        value = appearance.get("editor_copper", {}).get(layer)
    elif layer not in ("F.Cu", "B.Cu", "vias") and finish_color() is not None:
        value = BARE_COPPER  # inner copper is never exposed, so it never gets the finish
    else:
        value = finish_color() or appearance.get("viewer", {}).get("copper")
    return value[:3] if value else fallback


def mask_color(side, shown=None):
    """The mask colour of the current colour mode, or None when that side's mask is
    hidden (`shown`: as if its eye were on or off instead)."""
    if shown is None:
        shown = getattr(bpy.context.scene, f"kileido_show_{side}_Mask", True)
    if not shown:
        return None
    appearance = board.appearance
    suffix = "top" if side == "F" else "bottom"
    if board.color_mode == "EDITOR":
        return appearance.get(f"editor_mask_{suffix}")
    return (appearance.get("viewer", {}).get(f"soldermask_{suffix}") or
            appearance.get("saved_colors", {}).get(f"{side}.Mask") or (0.29, 0.49, 0.71, 0.7))


def layer_material(layer):
    """The copper material of one layer, created on first use and coloured for the mode."""
    key = f"copper:{layer}"
    if key not in board.materials:
        board.materials[key] = make(f"KLS {layer} copper", FALLBACK_COPPER)
        holes.add_to(board.materials[key])
        if cut.enabled():  # a layer's first copper while the board is cut open
            cut.add_to(board.materials[key])
    material = board.materials[key]
    paint(material, _copper_color(layer))
    set_surface(material, board.color_mode == "REALISTIC", COPPER_METALLIC, COPPER_ROUGHNESS)
    finish_mask(material, key)
    return material


# --- Finish in mask openings ----------------------------------------------------------------

def set_mask_image(side, image, bounds):
    """Called by cosmetics when a saved-board mask plot loads ("F" or "B")."""
    previous = board.mask_images.get(side, (None,))[0]
    board.mask_images[side] = (image, bounds)
    refresh_mask_colors()
    if previous is not None and previous != image and previous.users == 0:
        bpy.data.images.remove(previous)


def refresh_mask_colors():
    for key, material in board.materials.items():
        if key in FINISHED:
            finish_mask(material, key)
    paint_tents()


def finish_mask(material, key):
    """Board finish only where solder mask is open; bare copper where it covers.

    The mask plot's alpha is 1 at openings (KiCad plots openings on *.Mask).
    Outer copper samples its own side's plot; vias pick the plot by height.
    """
    sides = FINISHED.get(key, ())
    nodes = material.node_tree.nodes
    active = board.color_mode != "EDITOR" and sides and all(side in board.mask_images for side in sides)
    if not active:
        _unlink_finish_mask(material)
        return
    mix = nodes.get("KLS finish mix") or _build_finish_mask(material, sides)
    for side in sides:
        image, (xmin, ymin, xmax, ymax) = board.mask_images[side]
        nodes[f"KLS mask plot {side}"].image = image
        shading.set_plot_rectangle(material.node_tree, f"KLS mask offset {side}", f"KLS mask scale {side}",
                                   xmin, ymin, xmax - xmin, ymax - ymin)
    if "KLS mask side" in nodes:
        nodes["KLS mask side"].inputs[1].default_value = board.thickness_m / 2
    exposed = _lit_metal(finish_color() or board.appearance.get("viewer", {}).get("copper") or BARE_COPPER)
    mix.inputs["B"].default_value = (*shading.srgb_to_linear(exposed[:3]), 1.0)
    metal, rough = _metal_ramps(material, mix)
    metal.inputs["To Max"].default_value = COPPER_METALLIC
    rough.inputs["To Max"].default_value = COPPER_ROUGHNESS
    links = material.node_tree.links
    for node in nodes:
        if node.type == "EMISSION":
            links.new(mix.outputs["Result"], node.inputs["Color"])
        elif node.type == "BSDF_PRINCIPLED":
            links.new(mix.outputs["Result"], node.inputs["Base Color"])
            links.new(metal.outputs["Result"], node.inputs["Metallic"])
            links.new(rough.outputs["Result"], node.inputs["Roughness"])
    # The ink lies on the mask, so only where it covers the copper (never on a pad).
    print_silk(material, sides, mix.outputs["Result"], mix.inputs["Factor"].links[0].from_socket)
    set_covered(material, covered_state(sides[0]))
    for side in sides:
        silk = board.silk.get(side, {})
        set_silk_state(material, side, silk.get("active", False), silk.get("shown", True))
        if silk:
            set_silk_plot(material, side, silk["image"], silk["bounds"], silk["color"])
    set_relief(material, sides)


def mask_opacity():
    """The panel's solder mask opacity: a factor on KiCad's translucent mask colour
    (1: as KiCad draws it; lower lets the copper and laminate under it show)."""
    return min(1.0, max(0.0, float(getattr(bpy.context.scene, "kileido_mask_opacity", 1.0))))


def seen_through(mask, under):
    """The mask lying on `under` (sRGB): its colour at KiCad's alpha times the panel's opacity."""
    alpha = (mask[3] if len(mask) > 3 else 1.0) * mask_opacity()
    return shading.blend_srgb((*mask[:3], alpha), under)


def mask_on_laminate(mask, core):
    """The mask lying on the laminate (sRGB): `seen_through`, and a little darker the
    more mask there is, so copper under it (`covered_state`) stands out lighter."""
    alpha = (mask[3] if len(mask) > 3 else 1.0) * mask_opacity()
    return tuple(channel * (1 - LAMINATE_SHADE * alpha) for channel in seen_through(mask, core))


def covered_state(side, shown=None):
    """Covered copper is seen through the mask: KiCad's translucent mask colour over
    the light the copper reflects, as a non-metal, lighter than the mask over the
    laminate as on a real board. With the mask hidden it is bare metal copper.
    (colour as linear RGBA, metallic, roughness) for `set_covered`."""
    return covered_from(mask_color(side, shown))


def covered_from(covered):
    """`covered_state` for a mask colour (sRGBA), or None for no mask."""
    covered_rgb = seen_through(covered, COPPER_UNDER_MASK) if covered else _lit_metal(BARE_COPPER)
    return {"color": [*(float(channel) for channel in shading.srgb_to_linear(covered_rgb)), 1.0],
            "metallic": 0.0 if covered else COPPER_METALLIC,
            "roughness": 0.35 if covered else COPPER_ROUGHNESS,  # the mask's body vs metal
            "coat": 1.0 if covered else 0.0}  # the mask's glossy surface


def set_covered(material, state):
    """Colour covered copper per `covered_state` (a material with the finish mix)."""
    nodes = material.node_tree.nodes
    nodes["KLS finish mix"].inputs["A"].default_value = state["color"]
    nodes["KLS metal amount"].inputs["To Min"].default_value = state["metallic"]
    nodes["KLS metal roughness"].inputs["To Min"].default_value = state["roughness"]
    nodes[COAT_AMOUNT].inputs[1].default_value = state["coat"]


def _unlink_finish_mask(material):
    """PCB Editor colours or no mask plot yet: the finish nodes stay but drive nothing."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    for name in ("KLS finish mix", "KLS metal amount", "KLS metal roughness", "KLS silk mix", "KLS silk bump",
                 COAT_WEIGHT):
        node = nodes.get(name)
        if node is not None:  # a Mix node has one "Result" output per data type
            for link in [link for output in node.outputs for link in output.links]:
                links.remove(link)


def _build_finish_mask(material, sides):
    """The colour mix between covered and exposed copper, driven by the mask plot(s)."""
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    geometry = nodes.new("ShaderNodeNewGeometry")
    mix = nodes.new("ShaderNodeMix")
    mix.name = "KLS finish mix"
    mix.data_type = "RGBA"
    alphas = {}
    for side in sides:
        texture = nodes.new("ShaderNodeTexImage")
        texture.name, texture.extension = f"KLS mask plot {side}", "CLIP"
        shading.project_plot(tree, geometry.outputs["Position"], texture,
                             f"KLS mask offset {side}", f"KLS mask scale {side}")
        alphas[side] = shading.sharp_alpha(material, texture)
    if len(sides) == 2:  # vias: top plot above mid-board, bottom plot below
        split = nodes.new("ShaderNodeSeparateXYZ")
        above = nodes.new("ShaderNodeMath")
        above.name, above.operation = "KLS mask side", "GREATER_THAN"
        choose = nodes.new("ShaderNodeMix")
        choose.data_type = "FLOAT"
        links.new(geometry.outputs["Position"], split.inputs[0])
        links.new(split.outputs["Z"], above.inputs[0])
        links.new(above.outputs[0], choose.inputs["Factor"])
        links.new(alphas["B"], choose.inputs["A"])
        links.new(alphas["F"], choose.inputs["B"])
        opening = choose.outputs["Result"]
    else:
        opening = alphas[sides[0]]
    links.new(opening, mix.inputs["Factor"])
    return mix


def _metal_ramps(material, mix):
    """Metallic and roughness follow the mask opening: metal where the finish shows."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    metal = nodes.get("KLS metal amount")
    if metal is None:
        metal = nodes.new("ShaderNodeMapRange")
        metal.name = "KLS metal amount"
        rough = nodes.new("ShaderNodeMapRange")
        rough.name = "KLS metal roughness"
        opening = mix.inputs["Factor"].links[0].from_socket
        links.new(opening, metal.inputs["Value"])
        links.new(opening, rough.inputs["Value"])
    return metal, nodes["KLS metal roughness"]


# --- Silkscreen printed on the board -------------------------------------------------------

def print_silk(material, sides, base, opening, coordinates=None):
    """Silkscreen ink as part of a surface (the mask sheet, outer copper, vias) instead
    of a flat sheet above the board: printed on the mask, the ink follows the copper
    under it, so a trace under a silkscreen fill still shows as a raised step.

    `base` is the colour under the ink, `opening` the mask opening (no ink there) and
    `coordinates` where the plots are sampled (world position by default). The nodes
    are built once; `set_silk_state` switches them on, `set_silk_settings` sets the
    panel's opacity and ink thickness (a bump along the ink's edges). Relinked on
    every call: the lit shader appears with the first switch to Realistic.

    The same surfaces carry the mask's glossy coat (not in openings, not under the
    ink) and its relief over the copper (`set_relief`), under the ink's own bump.
    """
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    mix = nodes.get("KLS silk mix") or _build_silk(material, sides, opening, coordinates)
    coat = nodes.get(COAT_WEIGHT) or _build_coat(material)
    relief = nodes.get(RELIEF_BUMP) or _build_relief(material, sides)
    links.new(relief.outputs["Normal"], nodes["KLS silk bump"].inputs["Normal"])
    links.new(base, mix.inputs["A"])
    for node in nodes:
        if node.type == "EMISSION":
            links.new(mix.outputs["Result"], node.inputs["Color"])
        elif node.type == "BSDF_PRINCIPLED":
            links.new(mix.outputs["Result"], node.inputs["Base Color"])
            links.new(nodes["KLS silk bump"].outputs["Normal"], node.inputs["Normal"])
            links.new(coat.outputs[0], node.inputs["Coat Weight"])
            links.new(nodes["KLS silk bump"].outputs["Normal"], node.inputs["Coat Normal"])
            node.inputs["Coat Roughness"].default_value = MASK_COAT_ROUGHNESS


def _build_silk(material, sides, opening, coordinates):
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    if coordinates is None:
        coordinates = nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    inks, colors = {}, {}
    for side in sides:
        texture = nodes.new("ShaderNodeTexImage")
        texture.name, texture.extension = f"KLS silk plot {side}", "CLIP"
        shading.project_plot(tree, coordinates, texture, f"KLS silk offset {side}", f"KLS silk scale {side}")
        ink = shading.sharp_alpha(material, texture)
        for name in (f"KLS silk active {side}", f"KLS silk shown {side}"):  # both 1: printed
            gate = nodes.new("ShaderNodeMath")
            gate.name, gate.operation = name, "MULTIPLY"
            gate.inputs[1].default_value = 0.0
            links.new(ink, gate.inputs[0])
            ink = gate.outputs[0]
        inks[side] = ink
        colors[side] = nodes.new("ShaderNodeRGB")
        colors[side].name = f"KLS silk color {side}"
    if len(sides) == 2:  # vias: the top ink above mid-board, the bottom ink below
        split = nodes.new("ShaderNodeSeparateXYZ")
        above = nodes.new("ShaderNodeMath")
        above.name, above.operation = "KLS silk side", "GREATER_THAN"
        links.new(coordinates, split.inputs[0])
        links.new(split.outputs["Z"], above.inputs[0])
        ink = _choose(tree, "FLOAT", above.outputs[0], inks["B"], inks["F"])
        color = _choose(tree, "RGBA", above.outputs[0], colors["B"].outputs[0], colors["F"].outputs[0])
    else:
        ink, color = inks[sides[0]], colors[sides[0]].outputs[0]
    closed = nodes.new("ShaderNodeMath")
    closed.operation = "SUBTRACT"
    closed.inputs[0].default_value = 1.0
    links.new(opening, closed.inputs[1])
    printed = nodes.new("ShaderNodeMath")
    printed.operation = "MULTIPLY"
    links.new(ink, printed.inputs[0])
    links.new(closed.outputs[0], printed.inputs[1])
    opacity = nodes.new("ShaderNodeMath")
    opacity.name, opacity.operation = SILK_OPACITY, "MULTIPLY"
    opacity.inputs[1].default_value = 1.0
    links.new(printed.outputs[0], opacity.inputs[0])
    mix = nodes.new("ShaderNodeMix")
    mix.name, mix.data_type = "KLS silk mix", "RGBA"
    links.new(opacity.outputs[0], mix.inputs["Factor"])
    links.new(color, mix.inputs["B"])
    bump = nodes.new("ShaderNodeBump")
    bump.name = "KLS silk bump"
    bump.inputs["Distance"].default_value = 0.0
    links.new(printed.outputs[0], bump.inputs["Height"])
    return mix


def _build_coat(material):
    """Coat weight: where the mask covers (not in its openings) and no ink is printed,
    times COAT_AMOUNT (`set_covered`)."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    printed = nodes[SILK_OPACITY]  # printed ink times the panel's opacity
    closed = printed.inputs[0].links[0].from_node.inputs[1].links[0].from_socket  # 1 - opening
    bare = nodes.new("ShaderNodeMath")
    bare.operation = "SUBTRACT"
    links.new(closed, bare.inputs[0])
    links.new(printed.outputs[0], bare.inputs[1])
    amount = nodes.new("ShaderNodeMath")
    amount.name, amount.operation = COAT_AMOUNT, "MULTIPLY"
    amount.inputs[1].default_value = 1.0
    links.new(bare.outputs[0], amount.inputs[0])
    weight = nodes.new("ShaderNodeClamp")
    weight.name = COAT_WEIGHT
    links.new(amount.outputs[0], weight.inputs["Value"])
    return weight


def _build_relief(material, sides):
    """The mask's surface over the copper: a bump from the blurred copper plot(s),
    sampled where the silkscreen plots are (vias: the side by height)."""
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    coordinates = nodes[f"KLS silk offset {sides[0]}"].inputs[0].links[0].from_socket
    heights = {}
    for side in sides:
        texture = nodes.new("ShaderNodeTexImage")
        texture.name, texture.extension, texture.interpolation = f"KLS relief plot {side}", "CLIP", "Cubic"
        shading.project_plot(tree, coordinates, texture, f"KLS relief offset {side}", f"KLS relief scale {side}")
        heights[side] = texture.outputs["Alpha"]
    if len(sides) == 2:
        height = _choose(tree, "FLOAT", nodes["KLS silk side"].outputs[0], heights["B"], heights["F"])
    else:
        height = heights[sides[0]]
    bump = nodes.new("ShaderNodeBump")
    bump.name = RELIEF_BUMP
    bump.inputs["Distance"].default_value = 0.0
    links.new(height, bump.inputs["Height"])
    return bump


def set_relief_image(side, image, bounds):
    """Called by cosmetics when a blurred copper plot loads (None: no copper there)."""
    previous = board.relief_images.get(side, (None,))[0]
    if image is None:
        board.relief_images.pop(side, None)
    else:
        board.relief_images[side] = (image, bounds)
    refresh_mask_colors()
    if previous is not None and previous != image and previous.users == 0:
        bpy.data.images.remove(previous)


def set_relief(material, sides):
    """The relief's plots and height (flat until every side's plot is loaded, and with
    the copper thickness toggle off)."""
    nodes = material.node_tree.nodes
    if RELIEF_BUMP not in nodes:
        return
    loaded = all(side in board.relief_images for side in sides)
    for side in sides:
        image, bounds = board.relief_images.get(side, (None, None))
        nodes[f"KLS relief plot {side}"].image = image if loaded else None
        if loaded:
            xmin, ymin, xmax, ymax = bounds
            shading.set_plot_rectangle(material.node_tree, f"KLS relief offset {side}", f"KLS relief scale {side}",
                                       xmin, ymin, xmax - xmin, ymax - ymin)
    height = max(copper_thickness(f"{side}.Cu") for side in sides) * RELIEF_HEIGHT if loaded else 0.0
    nodes[RELIEF_BUMP].inputs["Distance"].default_value = height


def _choose(tree, data_type, factor, below, above):
    choose = tree.nodes.new("ShaderNodeMix")
    choose.data_type = data_type
    tree.links.new(factor, choose.inputs["Factor"])
    tree.links.new(below, choose.inputs["A"])
    tree.links.new(above, choose.inputs["B"])
    return choose.outputs["Result"]


def set_silk_plot(material, side, image, bounds, color):
    """One side's silkscreen plot, its world XY `bounds` and its ink colour (linear RGBA)."""
    nodes = material.node_tree.nodes
    if f"KLS silk plot {side}" not in nodes:
        return
    nodes[f"KLS silk plot {side}"].image = image
    xmin, ymin, xmax, ymax = bounds
    shading.set_plot_rectangle(material.node_tree, f"KLS silk offset {side}", f"KLS silk scale {side}",
                               xmin, ymin, xmax - xmin, ymax - ymin)
    nodes[f"KLS silk color {side}"].outputs[0].default_value = color
    if "KLS silk side" in nodes:
        nodes["KLS silk side"].inputs[1].default_value = board.thickness_m / 2


def set_silk_state(material, side, active, shown):
    """Ink printed on this surface (`active`: that side's silkscreen is drawn on the
    surfaces, not as the flat sheet) while the silkscreen's eye is on (`shown`)."""
    nodes = material.node_tree.nodes
    for name, value in ((f"KLS silk active {side}", active), (f"KLS silk shown {side}", shown)):
        if name in nodes:
            nodes[name].inputs[1].default_value = float(bool(value))
    if material.get("kls_silk_side") == side:  # that side's flat sheet: see-through while printed
        material["kls_silk_on_surface"] = bool(active)


def set_silk_settings(material, opacity, thickness):
    """The panel's silkscreen opacity (0..1) and ink thickness (m, the bump's height)."""
    nodes = material.node_tree.nodes if material.node_tree is not None else {}
    if SILK_OPACITY in nodes:
        nodes[SILK_OPACITY].inputs[1].default_value = opacity
        nodes["KLS silk bump"].inputs["Distance"].default_value = thickness
    if SHEET_OPACITY in nodes:
        nodes[SHEET_OPACITY].inputs[1].default_value = 0.0 if material.get("kls_silk_on_surface") else opacity


# --- Colour modes ---------------------------------------------------------------------------

def set_color_mode(mode):
    """Recolour every board material for `mode`. Overlays follow via cosmetics.recolor."""
    if mode not in MODES:
        raise ValueError(mode)
    board.color_mode = mode
    if not board.materials:
        return
    realistic = mode == "REALISTIC"
    viewer = board.appearance.get("viewer", {})
    _paint_board_faces(viewer)
    for key, material in board.materials.items():
        if key.startswith("copper:"):
            paint(material, _copper_color(key.removeprefix("copper:")))
    paint(board.materials["vias"],
          (board.appearance.get("editor_via") if mode == "EDITOR" else
           (finish_color() or viewer.get("copper"))) or FALLBACK_COPPER)
    # Hole walls: copper plated, then finished like the pads.
    paint(board.materials["plating"], _lit_metal(finish_color() or viewer.get("copper") or BARE_COPPER))
    set_surface(board.materials["plating"], realistic, COPPER_METALLIC, 0.3)
    paint(board.materials["plating_bare"], _lit_metal(BARE_COPPER))
    set_surface(board.materials["plating_bare"], realistic, COPPER_METALLIC, 0.3)
    paint_tents()
    paint(board.materials["via_resin"], section.RESIN)
    set_surface(board.materials["via_resin"], realistic, 0.0, 0.5)
    _paint_highlights()
    paint(board.materials["solder"], viewer.get("solderpaste") or (0.5, 0.5, 0.5))  # KiCad's 3D paste colour
    for key, material in board.materials.items():
        if key == "solder":
            set_surface(material, realistic, 0.9, 0.3)  # tin-silver-copper alloy
        if key in {"board", "board_bottom", "board_core", "vias"} or key.startswith("copper:"):
            metal = key == "vias" or key.startswith("copper:")
            set_surface(material, realistic, COPPER_METALLIC if metal else 0.0,
                        COPPER_ROUGHNESS if metal else 0.42)
            if metal:
                finish_mask(material, key)
    laminate.set_edges(board.materials["board_core"], realistic)  # routed edges and bare drills show the layers
    # Copper in the cut plane's section: bare (a cut never has the finish), lit like the rest.
    cut.set_look(realistic, shading.srgb_to_linear(metal_color(BARE_COPPER)), COPPER_METALLIC, COPPER_ROUGHNESS)


def _paint_board_faces(viewer):
    appearance = board.appearance
    editor = board.color_mode == "EDITOR"
    core = viewer.get("core") or FALLBACK_CORE
    for key, side, suffix in (("board", "F", "top"), ("board_bottom", "B", "bottom")):
        mask = ((appearance.get(f"editor_mask_{suffix}") if editor else
                 viewer.get(f"soldermask_{suffix}") or appearance.get("saved_colors", {}).get(f"{side}.Mask")) or
                FALLBACK_MASK)
        # Once the mask plot for this side is loaded, the translucent mask overlay
        # supplies the mask colour; the board face underneath is bare substrate, as
        # in KiCad. Mask openings without copper (fiducial rings) then show the
        # substrate. Until then, paint the face with the mask colour.
        paint(board.materials[key], core if side in board.mask_images and not editor else mask)
    paint(board.materials["board_core"], core)


def _paint_highlights():
    painted = [(f"highlight_{kind}{part}", color) for kind, color in HIGHLIGHT_COLORS.items()
               for part in ("", "_barrel")] + [("highlight_outline", OUTLINE_PROBLEM_COLOR)]
    for key, color in painted:
        # Metallic and glowing in every mode: lit metal alone washed out to pink
        # under the 0.2 W softboxes and AgX (compared on the reference board).
        material = board.materials[key]
        paint(material, color)
        set_surface(material, True, HIGHLIGHT_METALLIC, HIGHLIGHT_ROUGHNESS)
        principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        principled.inputs["Emission Color"].default_value = principled.inputs["Base Color"].default_value
        principled.inputs["Emission Strength"].default_value = HIGHLIGHT_GLOW
    # A malformed outline must read as pure red from every side and under any light:
    # only its glow (lit red, metal and softbox reflections read as salmon in AgX).
    outline = board.materials["highlight_outline"]
    set_surface(outline, True, 0.0, 0.6)
    principled = next(node for node in outline.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    principled.inputs["Emission Strength"].default_value = OUTLINE_PROBLEM_GLOW
    principled.inputs["Specular IOR Level"].default_value = 0.0  # no white softbox reflections
    principled.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)  # unlit: only the glow shows
    paint(board.materials["highlight_box"], HIGHLIGHT_COLORS["selected"])
