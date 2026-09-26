"""See-through drilled holes: one hole-mask image sampled by board and copper materials.

Copper geometry is never cut (no booleans; copper is never merged), and a track's
end cap sits on its via, so an opening in the via land alone would
stay plugged. Instead every through hole is drawn into one antialiased mask in
board XY; board faces and copper turn transparent inside it (`add_to`), and a
plated or bare wall object lines the hole (nodes.drills, nodes.vias).
"""

import hashlib

import bpy
import numpy as np

from . import focus, shading
from .state import board

RESOLUTION = 2048  # long side; ~22 um per pixel on the reference board, edges sharpened in shading
IMAGE = "KLS holes"
HOLED = ("board", "board_bottom", "vias", "highlight_selected", "highlight_pair")  # + copper:<layer>

_sources = {"vias": np.empty((0, 3), np.float64), "pads": np.empty((0, 6), np.float64)}
_bounds = None
_digest = None
_drawn = None  # (bounds, rows, alpha, pixels) of the last redraw, for partial redraws
PARTIAL_MAX = 64  # more changed holes than this: redraw the whole mask


def set_vias(xy_m, drill_m):
    """Through vias only: x, y and drill diameter in Blender metres."""
    _sources["vias"] = np.column_stack((np.asarray(xy_m, np.float64).reshape(-1, 2),
                                        np.asarray(drill_m, np.float64).reshape(-1)))
    rebuild()


def set_pads(xy_m, size_m, angle, oval):
    """Pad drills: x, y, width, height (m), angle (rad), oval flag."""
    xy = np.asarray(xy_m, np.float64).reshape(-1, 2)
    _sources["pads"] = np.column_stack((xy, np.asarray(size_m, np.float64).reshape(-1, 2),
                                        np.asarray(angle, np.float64).reshape(-1),
                                        np.asarray(oval, np.float64).reshape(-1)))
    rebuild()


def set_bounds(bounds):
    global _bounds
    _bounds = tuple(float(value) for value in bounds)
    rebuild()


def _distance_field(px, py, hole):
    """Signed distance (m) from pixel centres to a round or stadium hole outline."""
    x, y, width, height, angle, oval = hole
    dx, dy = px - x, py - y
    if not oval or abs(width - height) < 1e-9:
        return np.hypot(dx, dy) - min(width, height) / 2
    cos, sin = np.cos(-angle), np.sin(-angle)
    u, v = dx * cos - dy * sin, dx * sin + dy * cos  # hole frame: long axis along u
    if height > width:
        u, v, width, height = v, u, height, width
    half = (width - height) / 2
    return np.hypot(u - np.clip(u, -half, half), v) - height / 2


def _grid(bounds, resolution=RESOLUTION):
    """(pixel size, width, height) of the mask over `bounds`."""
    xmin, ymin, xmax, ymax = bounds
    pixel = max(xmax - xmin, ymax - ymin, 1e-9) / resolution
    return (pixel, max(1, int(np.ceil((xmax - xmin) / pixel))),
            max(1, int(np.ceil((ymax - ymin) / pixel))))


def _window(bounds, pixel, shape, hole):
    """(r0, r1, c0, c1): the pixels a hole can cover."""
    xmin, ymin = bounds[0], bounds[1]
    reach = max(hole[2], hole[3]) / 2 + pixel
    c0 = max(0, int((hole[0] - reach - xmin) / pixel))
    c1 = min(shape[1], int((hole[0] + reach - xmin) / pixel) + 2)
    r0 = max(0, int((hole[1] - reach - ymin) / pixel))
    r1 = min(shape[0], int((hole[1] + reach - ymin) / pixel) + 2)
    return r0, r1, c0, c1


def _draw(alpha, bounds, pixel, holes, clip=None):
    """Add `holes` to `alpha`, only inside `clip` (r0, r1, c0, c1) when given."""
    for hole in holes:
        r0, r1, c0, c1 = _window(bounds, pixel, alpha.shape, hole)
        if clip is not None:
            r0, r1, c0, c1 = max(r0, clip[0]), min(r1, clip[1]), max(c0, clip[2]), min(c1, clip[3])
        if c0 >= c1 or r0 >= r1:
            continue
        px = bounds[0] + (np.arange(c0, c1) + 0.5) * pixel
        py = bounds[1] + (np.arange(r0, r1) + 0.5) * pixel
        grid_x, grid_y = np.meshgrid(px, py)
        coverage = np.clip(0.5 - _distance_field(grid_x, grid_y, hole) / pixel, 0, 1)
        np.maximum(alpha[r0:r1, c0:c1], coverage, out=alpha[r0:r1, c0:c1])


def rasterize(bounds, holes, resolution=RESOLUTION):
    """Coverage (0..1) of `holes` rows (x, y, w, h, angle, oval) over `bounds`."""
    pixel, width, height = _grid(bounds, resolution)
    alpha = np.zeros((height, width), np.float32)
    _draw(alpha, bounds, pixel, holes)
    return alpha


def _redraw_changed(alpha, bounds, old_rows, rows):
    """Redraw only around holes that were added or removed (a moved via is both).
    Every hole overlapping a cleared window is drawn again inside it. None when too
    many holes changed for this to pay off."""
    old, new = {tuple(row) for row in old_rows}, {tuple(row) for row in rows}
    changed = old ^ new
    if len(changed) > PARTIAL_MAX:
        return None
    pixel = _grid(bounds)[0]
    for hole in changed:
        clip = _window(bounds, pixel, alpha.shape, hole)
        r0, r1, c0, c1 = clip
        if c0 >= c1 or r0 >= r1:
            continue
        alpha[r0:r1, c0:c1] = 0.0
        x0, x1 = bounds[0] + c0 * pixel, bounds[0] + c1 * pixel
        y0, y1 = bounds[1] + r0 * pixel, bounds[1] + r1 * pixel
        reach = np.maximum(rows[:, 2], rows[:, 3]) / 2 + pixel
        near = ((rows[:, 0] + reach >= x0) & (rows[:, 0] - reach <= x1) &
                (rows[:, 1] + reach >= y0) & (rows[:, 1] - reach <= y1))
        _draw(alpha, bounds, pixel, rows[near], clip)
    return alpha


def rebuild():
    """Redraw the mask when holes or the board bounds changed. A few moved holes
    redraw only their own pixels; a new board or bounds redraws everything.
    Inside a snapshot this waits for `snapshot_end`, which calls it once."""
    global _digest, _drawn
    if _bounds is None or board.in_snapshot:
        return
    vias = _sources["vias"]
    rows = np.concatenate((
        np.column_stack((vias[:, :2], vias[:, 2], vias[:, 2],
                         np.zeros(len(vias)), np.zeros(len(vias)))) if len(vias) else np.empty((0, 6)),
        _sources["pads"]))
    digest = hashlib.blake2b(np.asarray(_bounds).tobytes() + rows.tobytes(), digest_size=16).digest()
    if digest == _digest and bpy.data.images.get(IMAGE) is not None:
        return
    _digest = digest
    image = bpy.data.images.get(IMAGE)
    alpha = pixels = None
    if _drawn is not None and _drawn[0] == _bounds and image is not None:
        alpha = _redraw_changed(_drawn[2], _bounds, _drawn[1], rows)
        pixels = _drawn[3]
    if alpha is None:
        alpha = rasterize(_bounds, rows)
        pixels = None
    height, width = alpha.shape
    if image is None or tuple(image.size) != (width, height):
        if image is not None:
            bpy.data.images.remove(image)
        image = bpy.data.images.new(IMAGE, width, height, alpha=True)
        image.colorspace_settings.name = "Non-Color"
    if pixels is None or pixels.shape[:2] != alpha.shape:
        pixels = np.ones((height, width, 4), np.float32)  # reused while the size holds
    pixels[..., 3] = alpha
    image.pixels.foreach_set(pixels.ravel())
    image.update()
    _drawn = (_bounds, rows, alpha, pixels)
    for key, material in board.materials.items():
        if key in HOLED or key.startswith("copper:"):
            add_to(material)
    for material in mask_materials():
        add_to(material)


def mask_materials():
    """Solder mask and silkscreen: an open via goes through them as well."""
    return [material for layer in ("F.Mask", "B.Mask", "F.SilkS", "B.SilkS")
            if (material := bpy.data.materials.get(f"KLS overlay {layer} material")) is not None]


def add_to(material):
    """Transparent inside drilled holes: a Mix Shader before the output.

    `materials.set_surface` links the lit/flat shader into the first Mix Shader's
    second input, so this also survives colour-mode switches.
    """
    image = bpy.data.images.get(IMAGE)
    if image is None or _bounds is None:
        return
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    texture = nodes.get("KLS holes plot")
    if texture is None:
        surface = focus.surface_input(material)  # before the focus node, if there is one
        shader = surface.links[0].from_socket if surface.is_linked else None
        texture = nodes.new("ShaderNodeTexImage")
        texture.name, texture.extension = "KLS holes plot", "CLIP"
        shading.project_plot(tree, nodes.new("ShaderNodeNewGeometry").outputs["Position"], texture,
                             "KLS holes offset", "KLS holes scale")
        opening = shading.sharp_alpha(material, texture)
        solid = nodes.new("ShaderNodeMath")
        solid.operation = "SUBTRACT"
        solid.inputs[0].default_value = 1.0
        links.new(opening, solid.inputs[1])
        mix = next((node for node in nodes if node.type == "MIX_SHADER"), None)
        if mix is None:  # opaque material: add the see-through mix
            mix = nodes.new("ShaderNodeMixShader")
            mix.name = "KLS holes mix"
            links.new(nodes.new("ShaderNodeBsdfTransparent").outputs[0], mix.inputs[1])
            if shader is not None:
                links.new(shader, mix.inputs[2])
            links.new(mix.outputs[0], surface)
            links.new(solid.outputs[0], mix.inputs[0])
        else:  # already translucent: multiply its coverage by "not a hole"
            both = nodes.new("ShaderNodeMath")
            both.operation = "MULTIPLY"
            if mix.inputs[0].is_linked:
                links.new(mix.inputs[0].links[0].from_socket, both.inputs[0])
            else:
                both.inputs[0].default_value = mix.inputs[0].default_value
            links.new(solid.outputs[0], both.inputs[1])
            links.new(both.outputs[0], mix.inputs[0])
        focus.set_render_method(material)
    texture.image = image
    xmin, ymin, xmax, ymax = _bounds
    width, height = image.size
    pixel = max(xmax - xmin, ymax - ymin) / RESOLUTION
    shading.set_plot_rectangle(tree, "KLS holes offset", "KLS holes scale", xmin, ymin, width * pixel, height * pixel)
