"""Two inspection softboxes matching PCB_analyzerV3's lighting preset, and a studio
environment that only reflections see."""

import math
import os

import bpy
from mathutils import Vector

from . import cut, materials, shading
from .objects import OUTLINE, hide, view3d_spaces
from .state import board


def claimed(scene=None):
    """A studio add-on looks after this scene's lights and world (studio.claim_lighting):
    KiLeidoscope leaves them alone."""
    return bool((scene or bpy.context.scene).get("kls_lighting_owner"))


def ensure_background():
    """A world whose camera rays see the Studio background (one colour, a gradient or
    nothing), retaining the world's lighting for other rays."""
    scene = bpy.context.scene
    if claimed(scene):
        return
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
        black.name = CAMERA_BACKGROUND
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
    _apply_background(world)
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
# Shaded flat colours (shading.lit_flat_group): the softboxes at their default power give
# a face under them 1.86 times its colour as diffuse light (measured, Cycles and EEVEE);
# scaled so that face shows its flat colour, with the ambient share.
LIT_GAIN = (1 - shading.LIT_AMBIENT) / 1.86
CAMERA_MIX = "KLS camera mix"
# The background (kileido_background): what camera rays see, and only they. One colour,
# a gradient from it at the frame's bottom to a second colour at its top, or nothing.
BACKGROUND_COLOR = (0.0, 0.0, 0.0)
BACKGROUND_TOP = (0.09, 0.10, 0.12)
CAMERA_BACKGROUND = "KLS camera background"
GRADIENT = "KLS background gradient"


def _setting(name, default):
    return getattr(bpy.context.scene, name, default)


def apply_settings(_scene=None, _context=None):
    """Panel changed the light strength or fill colour, or the mask colour changed: update in
    place. With the lighting claimed, only what stays KiLeidoscope's whoever owns the lights:
    the shaded colours' gain and the cut light."""
    scene = bpy.context.scene
    if not claimed(scene):
        if scene.world is not None and scene.world.get("kls_black_background"):
            _studio_environment(scene.world)
            _apply_background(scene.world)
        collection = _lights_collection(scene)
        if collection is not None:
            for obj in collection.objects:
                if obj.type == "LIGHT" and obj.get("kls_studio_side"):
                    obj.data.energy = softbox_energy(obj.get("kls_scale", 1.0))
    # Shaded flat colours as bright as flat under the default light, whatever the mask.
    shading.set_lit_gain(LIT_GAIN / exposure())
    cut.place_light(scene)  # the cut light follows the softboxes' strength


def apply_background(_scene=None, _context=None):
    """Panel changed the background: the world's camera side only."""
    scene = bpy.context.scene
    if claimed(scene) or scene.world is None or not scene.world.get("kls_black_background"):
        return
    _apply_background(scene.world)


def _set_value(socket, value):
    """Assign a socket's value only when it changes: an assignment tags the world for
    update, restarting a rendered viewport, whether or not the value differed."""
    current = socket.default_value
    if isinstance(value, tuple):
        same = len(current) == len(value) and all(abs(a - b) < 1e-6 for a, b in zip(current, value))
    else:
        same = abs(current - value) < 1e-6
    if not same:
        socket.default_value = value


def exposure():
    """The softboxes' factor for the live board's top mask (1 on dark masks)."""
    mask = materials.mask_color("F", shown=True) if board.appearance else None
    if not mask:
        return 1.0
    core = materials.core_color()
    red, green, blue = shading.srgb_to_linear(materials.mask_on_laminate(mask, core))
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return min(1.0, max(MIN_EXPOSURE, EXPOSURE_ALBEDO / max(luminance, 1e-6)))


def softbox_energy(scale):
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


def _camera_background(tree):
    """The Background node the camera rays see (the mix's second shader)."""
    node = tree.nodes.get(CAMERA_BACKGROUND)
    if node is not None:
        return node
    mix = _camera_mix(tree)
    if mix is None or not mix.inputs[2].is_linked:
        return None
    node = mix.inputs[2].links[0].from_node  # a world saved before the node had its name
    node.name = CAMERA_BACKGROUND
    return node


def _apply_background(world):
    """What the camera sees behind the board: the Studio colour, a gradient from it at the
    bottom of the frame to a second colour at the top, or nothing (renders come out
    transparent). Lights and reflections see none of it: this feeds only the camera-ray
    side of the mix. One colour is the gradient with both its colours the same."""
    scene = bpy.context.scene
    mode = _setting("kileido_background", "SOLID")
    if scene.render.film_transparent != (mode == "TRANSPARENT"):
        scene.render.film_transparent = mode == "TRANSPARENT"
    tree = world.node_tree
    background = _camera_background(tree)
    if background is None:
        return
    bottom = tuple(_setting("kileido_background_color", BACKGROUND_COLOR)[:3])
    top = tuple(_setting("kileido_background_top", BACKGROUND_TOP)[:3]) if mode == "GRADIENT" else bottom
    gradient = tree.nodes.get(GRADIENT)
    if gradient is None:
        window = tree.nodes.new("ShaderNodeTexCoord")
        height = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(window.outputs["Window"], height.inputs[0])  # the frame: 0 at its bottom, 1 at its top
        gradient = tree.nodes.new("ShaderNodeMix")
        gradient.name = GRADIENT
        gradient.data_type = "RGBA"
        tree.links.new(height.outputs["Y"], gradient.inputs[0])
        tree.links.new(shading.typed_socket(gradient.outputs, "Result"), background.inputs["Color"])
    _set_value(shading.typed_socket(gradient.inputs, "A"), (*bottom, 1.0))
    _set_value(shading.typed_socket(gradient.inputs, "B"), (*top, 1.0))


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
    if world is None or not world.get("kls_black_background") or world.node_tree is None or claimed():
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
    _set_value(environment.inputs["Color"], (*_setting("kileido_fill_color", FILL_COLOR)[:3], 1.0))
    _set_value(environment.inputs["Strength"], 1.0)
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
    _set_value(nodes["KLS reflections"].inputs["Strength"], REFLECTION_STRENGTH * exposure())  # as the softboxes
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
    collection = _lights_collection(scene)
    if collection is None or claimed(scene):
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
        if obj.type != "LIGHT" or not side or _user_placed(obj):
            continue  # a softbox moved by hand stays where it was put, until reset_lights
        height = SOFTBOX_HEIGHT_M * scale
        if bounds is None:
            z = height if side == "top" else -height
        else:
            z = high.z + height if side == "top" else low.z - height
        obj.location = (center[0], center[1], z)
        obj.rotation_euler = (0.0 if side == "top" else math.pi, 0.0, 0.0)
        obj["kls_scale"] = scale
        obj.data.size = SOFTBOX_SIZE_M * scale
        obj.data.energy = softbox_energy(scale)
        obj["kls_fitted"] = (*obj.location, *obj.rotation_euler)


def _lights_collection(scene):
    return next((child for child in scene.collection.children if child.get("kls_studio_lights") == 1), None)


def _user_placed(obj):
    """Moved or turned since it was last fitted: the user's now."""
    fitted = obj.get("kls_fitted")
    return fitted is not None and any(abs(was - now) > 1e-6
                                      for was, now in zip(fitted, (*obj.location, *obj.rotation_euler)))


def user_placed_lights():
    """The softboxes moved by hand (shown with kileido_show_rig), which fit_to_boards leaves."""
    collection = _lights_collection(bpy.context.scene)
    return [obj for obj in collection.objects if _user_placed(obj)] if collection is not None else []


def reset_lights():
    """Fit the softboxes to the boards again, those moved by hand included."""
    collection = _lights_collection(bpy.context.scene)
    if collection is None:
        return
    for obj in collection.objects:
        obj.pop("kls_fitted", None)
    fit_to_boards()


def show_rig(_scene=None, _context=None):
    """The Studio column's Show camera and lights: the softboxes and the camera drawn in
    the 3D views as the objects they are, to select and move (G). Off, they still light
    and render. Blender draws lights and cameras as "extras", all of them or none, so this
    runs when the setting changes and once when the lights are made; after that the
    Extras overlay is the user's."""
    show = bool(_setting("kileido_show_rig", False))
    collection = _lights_collection(bpy.context.scene)
    if collection is not None:
        collection.hide_select = not show  # fitted to the boards: a stray click must not move them
    for space in view3d_spaces():
        space.overlay.show_extras = show


def ensure_studio_lights():
    """One softbox above and one below the boards (created once, then fitted); None
    while a studio add-on looks after the scene's lights."""
    if claimed():
        return None
    ensure_background()
    scene = bpy.context.scene
    collection = _lights_collection(scene)
    if collection is None:
        collection = bpy.data.collections.new("KiLeidoscope Studio Lights")
        collection["kileido_owned"] = 1
        collection["kls_studio_lights"] = 1
        scene.collection.children.link(collection)
        show_rig()
    for side in ("top", "bottom"):
        obj = next((item for item in collection.objects
                    if item.get("kls_studio_side") == side and item.type == "LIGHT"), None)
        if obj is None:
            name = f"KLS Studio softbox ({side})"
            light = bpy.data.lights.new(name, "AREA")
            obj = bpy.data.objects.new(name, light)
            obj["kileido_owned"] = 1
            obj["kls_studio_side"] = side
            collection.objects.link(obj)
        obj.data.type = "AREA"
        obj.data.shape = "DISK"
        obj.data.color = (1.0, 1.0, 1.0)
        obj.hide_render = False
        hide(obj, False)
    for space in view3d_spaces():
        space.shading.use_scene_lights = True
        space.shading.use_scene_lights_render = True
    fit_to_boards()
    return collection
