"""Two inspection softboxes matching PCB_analyzerV3's lighting preset, and a studio
environment that only reflections see."""

import math
import os

import bpy
from mathutils import Vector

from . import materials, shading
from .objects import OUTLINE, hide, view3d_spaces
from .state import board


def ensure_black_background():
    """Black camera background, retaining the world's lighting for other rays."""
    scene = bpy.context.scene
    world = scene.world
    if world is None or not world.get("kls_black_background"):
        world = world.copy() if world else bpy.data.worlds.new("KiLeidoscope World")
        world.name = "KiLeidoscope World"
        world["kls_black_background"] = True
        world.use_nodes = True
        tree = world.node_tree
        output = next(node for node in tree.nodes if node.type == "OUTPUT_WORLD" and node.is_active_output)
        previous = output.inputs["Surface"].links[0].from_socket if output.inputs["Surface"].is_linked else None
        black = tree.nodes.new("ShaderNodeBackground")
        black.inputs["Color"].default_value = (0, 0, 0, 1)
        rays = tree.nodes.new("ShaderNodeLightPath")
        mix = tree.nodes.new("ShaderNodeMixShader")
        mix.name = CAMERA_MIX
        tree.links.new(rays.outputs["Is Camera Ray"], mix.inputs[0])
        tree.links.new(previous or black.outputs[0], mix.inputs[1])
        tree.links.new(black.outputs[0], mix.inputs[2])
        tree.links.new(mix.outputs[0], output.inputs["Surface"])
        scene.world = world
    _studio_environment(world)
    scene.render.film_transparent = False
    for space in view3d_spaces():
        space.shading.background_type = "WORLD"
        space.shading.use_scene_world = True
        space.shading.use_scene_world_render = True
        space.overlay.show_floor = False
        space.overlay.show_axis_x = False
        space.overlay.show_axis_y = False
    world.color = (0, 0, 0)


# Softboxes above and below (user's standard: 100 mW each), black surroundings.
# Both are panel settings (kileido_light_power, kileido_fill_color).
SOFTBOX_POWER_W = 0.1
SOFTBOX_HEIGHT_M = 0.06  # above the top face and below the bottom one
SOFTBOX_SIZE_M = 0.08  # disk diameter over a board up to COVER_FRACTION of it
COVER_FRACTION = 1.2  # a larger board or assembly: the disk spans its extent times this
FILL_COLOR = (0.0, 0.0, 0.0)
# The softbox power was set on dark masks. A light mask (white, light blue) lit as
# brightly washes out to white and loses the copper under it, so the softboxes dim
# by the mask's brightness: mask over laminate brighter than this (linear
# luminance) gets proportionally less light, down to MIN_EXPOSURE.
EXPOSURE_ALBEDO = 0.1
MIN_EXPOSURE = 0.3
# Reflections (kileido_reflections): one of Blender's bundled studio-light HDRIs (CC0,
# in every install), seen by glossy rays only. Diffuse light stays the softboxes and
# the fill colour; the glossy mask, metal finishes and parts get something to reflect
# instead of black.
REFLECTIONS = ("interior", "studio", "courtyard", "city", "forest", "sunrise", "sunset", "night")
DEFAULT_REFLECTIONS = "interior"
REFLECTION_STRENGTH = 1.0
CAMERA_MIX = "KLS camera mix"


def _setting(name, default):
    return getattr(bpy.context.scene, name, default)


def apply_settings(_scene=None, _context=None):
    """Panel changed the light strength or fill colour: update in place."""
    scene = bpy.context.scene
    if scene.world is not None and scene.world.get("kls_black_background"):
        _studio_environment(scene.world)
    for collection in scene.collection.children:
        if collection.get("kls_studio_lights") == 1:
            for obj in collection.objects:
                if obj.type == "LIGHT" and obj.get("kls_studio_side"):
                    obj.data.energy = _energy(obj.get("kls_scale", 1.0))


def exposure():
    """The softboxes' factor for the live board's top mask (1 on dark masks)."""
    mask = materials.mask_color("F", shown=True) if board.appearance else None
    if not mask:
        return 1.0
    core = materials.core_color()
    red, green, blue = shading.srgb_to_linear(materials.mask_on_laminate(mask, core))
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return min(1.0, max(MIN_EXPOSURE, EXPOSURE_ALBEDO / max(luminance, 1e-6)))


def _energy(scale):
    """A softbox's power: the panel's, for its size (`scale`), dimmed for light masks."""
    return float(_setting("kileido_light_power", SOFTBOX_POWER_W)) * scale ** 2 * exposure()


def _camera_mix(tree):
    """The mix that keeps the camera's background black (the world output's input)."""
    named = tree.nodes.get(CAMERA_MIX)
    if named is not None:
        return named
    output = next((node for node in tree.nodes if node.type == "OUTPUT_WORLD" and node.is_active_output), None)
    if output is None or not output.inputs["Surface"].is_linked:
        return None
    mix = output.inputs["Surface"].links[0].from_node
    return mix if mix.type == "MIX_SHADER" else None


def _studio_paths():
    """{file name: path} of the studio-light HDRIs bundled with this Blender install."""
    return {light.name: light.path for light in bpy.context.preferences.studio_lights if light.type == "WORLD"}


def _reflection_image(name):
    """Blender's bundled studio-light HDRI `name`, or None when this install lacks it.
    Found by path every time: a file saved elsewhere refers to that install's copy."""
    path = _studio_paths().get(f"{name}.exr")
    return bpy.data.images.load(path, check_existing=True) if path else None


def refresh_reflections():
    """After a file load: the reflection HDRI saved in it is another Blender install's
    copy (another OS or version) when its path is none of this install's, and has no
    data here; find this install's copy instead."""
    world = bpy.context.scene.world
    if world is None or not world.get("kls_black_background") or world.node_tree is None:
        return
    def normal(path):  # saved paths may be relative to the .blend
        return os.path.normcase(os.path.normpath(bpy.path.abspath(path)))

    texture = world.node_tree.nodes.get("KLS reflection image")
    if texture is not None and texture.image is not None and \
            normal(texture.image.filepath) not in {normal(path) for path in _studio_paths().values()}:
        _studio_environment(world)


def _studio_environment(world):
    """Fill colour: what shadows and diffuse bounces see; reflections see the chosen
    studio HDRI (or the fill colour too with reflections off). Never the camera,
    whose background stays black."""
    tree = world.node_tree
    mix = _camera_mix(tree)
    if mix is None:
        return
    nodes, links = tree.nodes, tree.links
    environment = nodes.get("KLS studio environment")
    if environment is None:
        environment = nodes.new("ShaderNodeBackground")
        environment.name = "KLS studio environment"
    environment.inputs["Color"].default_value = (*_setting("kileido_fill_color", FILL_COLOR)[:3], 1)
    environment.inputs["Strength"].default_value = 1.0
    choice = _setting("kileido_reflections", DEFAULT_REFLECTIONS)
    image = _reflection_image(choice) if choice in REFLECTIONS else None
    if image is None:
        links.new(environment.outputs[0], mix.inputs[1])
        return
    pick = nodes.get("KLS reflection pick")
    if pick is None:
        texture = nodes.new("ShaderNodeTexEnvironment")
        texture.name = "KLS reflection image"
        reflections = nodes.new("ShaderNodeBackground")
        reflections.name = "KLS reflections"
        links.new(texture.outputs["Color"], reflections.inputs["Color"])
        rays = nodes.new("ShaderNodeLightPath")
        pick = nodes.new("ShaderNodeMixShader")
        pick.name = "KLS reflection pick"
        links.new(rays.outputs["Is Glossy Ray"], pick.inputs[0])
        links.new(reflections.outputs[0], pick.inputs[2])
    texture = nodes["KLS reflection image"]
    previous, texture.image = texture.image, image
    if previous is not None and previous != image and previous.users == 0:
        bpy.data.images.remove(previous)  # the last choice, or another install's copy
    nodes["KLS reflections"].inputs["Strength"].default_value = REFLECTION_STRENGTH * exposure()  # as the softboxes
    links.new(environment.outputs[0], pick.inputs[1])
    links.new(pick.outputs[0], mix.inputs[1])


def _board_bounds():
    """World (low, high) corners around every board shown (live and view-only), from
    their board solids, or None before any board."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    corners = []
    for collection in bpy.data.collections:
        if not (collection.get("kileido_owned") or collection.get("kls_view_only")) or collection.hide_viewport:
            continue
        for obj in collection.all_objects:
            if (obj.name == OUTLINE or obj.name.endswith(" " + OUTLINE)) and len(obj.data.vertices):
                box = obj.evaluated_get(depsgraph).bound_box
                corners += [obj.matrix_world @ Vector(corner) for corner in box]
    if not corners:
        return None
    return (Vector([min(c[axis] for c in corners) for axis in range(3)]),
            Vector([max(c[axis] for c in corners) for axis in range(3)]))


def fit_to_boards():
    """Centre the softboxes over everything shown and grow them with it: a larger
    board or assembly gets a larger disk, further away, with power by area, so every
    board is lit as one small board is (unchanged up to SOFTBOX_SIZE_M / COVER_FRACTION)."""
    scene = bpy.context.scene
    collection = next((child for child in scene.collection.children if child.get("kls_studio_lights") == 1), None)
    if collection is None:
        return
    bpy.context.view_layer.update()
    bounds = _board_bounds()
    if bounds is None:
        center, scale = (0.0, 0.0, 0.0), 1.0
    else:
        low, high = bounds
        center = (low + high) / 2
        extent = max(high.x - low.x, high.y - low.y)
        scale = max(1.0, extent * COVER_FRACTION / SOFTBOX_SIZE_M)
    for obj in collection.objects:
        side = obj.get("kls_studio_side")
        if obj.type != "LIGHT" or not side:
            continue
        height = SOFTBOX_HEIGHT_M * scale
        if bounds is None:
            z = height if side == "top" else -height
        else:
            z = high.z + height if side == "top" else low.z - height
        obj.location = (center[0], center[1], z)
        obj["kls_scale"] = scale
        obj.data.size = SOFTBOX_SIZE_M * scale
        obj.data.energy = _energy(scale)


def ensure_studio_lights():
    """One softbox above and one below the boards (created once, then fitted)."""
    ensure_black_background()
    scene = bpy.context.scene
    collection = next((child for child in scene.collection.children
                       if child.get("kls_studio_lights") == 1), None)
    if collection is None:
        collection = bpy.data.collections.new("KiLeidoscope Studio Lights")
        collection["kileido_owned"] = 1
        collection["kls_studio_lights"] = 1
        scene.collection.children.link(collection)
    collection.hide_select = True  # fitted to the boards; a stray click must not move them
    for side, rotation in (("top", 0.0), ("bottom", math.pi)):
        obj = next((item for item in collection.objects
                    if item.get("kls_studio_side") == side and item.type == "LIGHT"), None)
        if obj is None:
            name = f"KLS Studio softbox ({side})"
            light = bpy.data.lights.new(name, "AREA")
            obj = bpy.data.objects.new(name, light)
            obj["kileido_owned"] = 1
            obj["kls_studio_side"] = side
            collection.objects.link(obj)
        obj.rotation_euler = (rotation, 0.0, 0.0)
        obj.data.type = "AREA"
        obj.data.shape = "DISK"
        obj.data.color = (1.0, 1.0, 1.0)
        obj.hide_render = False
        hide(obj, False)
    for space in view3d_spaces():
        space.shading.use_scene_lights = True
        space.shading.use_scene_lights_render = True
        space.overlay.show_extras = False  # the softboxes' outlines (and empties); they still light
    fit_to_boards()
    return collection
