"""See-through drilled holes: one hole-mask image sampled by board and copper materials.

Copper geometry is never cut (no booleans; copper is never merged), and a track's
end cap sits on its via, so an opening in the via land alone would
stay plugged. Instead every hole is drawn into one antialiased mask in board XY;
board faces and copper turn transparent inside it (`add_to`), and a plated or bare
wall object lines the hole (nodes.drills, nodes.vias).

A blind via is a hole on one side only. The mask has a channel per side: red holes
the top side (every through hole and the blind vias from F.Cu), green the bottom,
alpha only the through holes. A material picks by height: at or above the top
laminate face it is on the top side, at or below the bottom face on the bottom side,
between (inner copper, a blind via's floor) only through holes reach it.

Blue is the board's area (outline_mask): 1 inside the Edge.Cuts outline, 0 outside, and
0 past the image too (it covers only the outline's box; the texture clips). Copper drawn
past the edge (castellated pads, edge fingers) is milled away by the fab, so copper,
via, plating, paste and highlight materials turn see-through outside it
(`clip_to_board`). The board, its mask and the IMS base already have the outline's
shape; the plated board edge stands outside it on purpose and keeps its own material.
"""

import hashlib

import bpy
import numpy as np

from . import focus, outline_mask, shading
from .placement import laminate_faces
from .state import board

RESOLUTION = 2048  # long side; ~22 um per pixel on the reference board, edges sharpened in shading
IMAGE = "KLS holes"
HOLED = ("board", "board_bottom", "board_edge", "ims_base", "vias", "via_rings", "highlight_selected",
         "highlight_pair")  # + copper:<layer>
CLIPPED = ("vias", "via_rings", "plating", "plating_bare", "solder", "highlight_selected", "highlight_pair",
           "highlight_selected_barrel", "highlight_pair_barrel")  # + copper:<layer>

THROUGH, TOP, BOTTOM = 0, 1, 2  # a via hole's side
SIDE_MARGIN_M = 10e-6  # this far inside a laminate face still counts as that side (the board's own faces)
CHANNELS = (("F", TOP), ("B", BOTTOM), ("through", THROUGH))  # red, green, alpha
SIDE_NODE = "KLS holes side"  # a holed material's channel pick (`_side_select`)
WALLS = ("plating", "plating_bare")  # drill walls: kept a little past the outline (`clip_to_board`)
WALL_RAMP = (0.02, 0.12)  # their coverage ramp: about a pixel past the outline still shows
CLIP_TEXTURE = "KLS board plot"  # a clipped material's sample of the mask (`clip_to_board`)
CLIP_OFF = "KLS board clip off"  # 1: no outline to clip to, everything shows
CLIP_EDGE = "KLS board clip edge"  # the coverage ramp: crisp at 0.5, or `WALL_RAMP`

_sources = {"vias": np.empty((0, 4), np.float64), "pads": np.empty((0, 6), np.float64)}
_bounds = None
_outline = (np.empty((0, 2)), np.empty((0, 2)))  # the board outline's edges a -> b, Blender metres
_inside = None  # (key, coverage) of the last drawn board area
_digest = None
_drawn = None  # (bounds, {channel: (rows, alpha)}, pixels) of the last redraw, for partial redraws
PARTIAL_MAX = 64  # more changed holes than this: redraw the whole mask


def set_vias(xy_m, diameter_m, side=None):
    """Via holes: x, y and hole diameter in Blender metres, and per via THROUGH (default),
    TOP or BOTTOM: the side a blind via opens on."""
    xy = np.asarray(xy_m, np.float64).reshape(-1, 2)
    side = np.full(len(xy), THROUGH) if side is None else np.asarray(side, np.float64).reshape(-1)
    _sources["vias"] = np.column_stack((xy, np.asarray(diameter_m, np.float64).reshape(-1), side))
    rebuild()


def set_pads(xy_m, size_m, angle, oval):
    """Pad drills: x, y, width, height (m), angle (rad), oval flag."""
    xy = np.asarray(xy_m, np.float64).reshape(-1, 2)
    _sources["pads"] = np.column_stack((xy, np.asarray(size_m, np.float64).reshape(-1, 2),
                                        np.asarray(angle, np.float64).reshape(-1),
                                        np.asarray(oval, np.float64).reshape(-1)))
    rebuild()


def set_bounds(bounds, outline=None):
    """The board outline's box (xmin, ymin, xmax, ymax, metres), and its edges (a, b),
    each (n, 2): the board area copper is clipped to. No edges given: the last ones
    stay; empty edges: nothing is clipped."""
    global _bounds, _outline
    _bounds = tuple(float(value) for value in bounds)
    if outline is not None:
        _outline = tuple(np.asarray(part, np.float64).reshape(-1, 2) for part in outline)
    rebuild()


def clipping():
    """True when there is an outline to clip copper to."""
    return len(_outline[0]) > 0


def clear_outline():
    """No closed outline: copper shows wherever it is drawn."""
    global _outline
    if clipping():
        _outline = (np.empty((0, 2)), np.empty((0, 2)))
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


def _rows(channel_side):
    """Hole rows (x, y, w, h, angle, oval) on one channel: through holes, plus that
    side's blind vias."""
    vias = _sources["vias"]
    vias = vias[np.isin(vias[:, 3], (THROUGH, channel_side))]
    return np.concatenate((np.column_stack((vias[:, :3], vias[:, 2], np.zeros((len(vias), 2)))), _sources["pads"]))


def _board_area(shape):
    """Blue: the board area over `_bounds` (1 everywhere without an outline), redrawn
    only when the bounds or the outline change."""
    global _inside
    if not clipping():
        return np.ones(shape, np.float32)
    key = hashlib.blake2b(np.asarray(_bounds).tobytes() + _outline[0].tobytes() + _outline[1].tobytes()
                          + np.asarray(shape).tobytes(), digest_size=16).digest()
    if _inside is None or _inside[0] != key:
        pixel = _grid(_bounds)[0]
        _inside = (key, outline_mask.coverage(_bounds, pixel, shape, *_outline))
    return _inside[1]


def rebuild():
    """Redraw the mask when holes, the board bounds or its outline changed. A few moved
    holes redraw only their own pixels; a new board or bounds redraws everything.
    Inside a snapshot this waits for `snapshot_end`, which calls it once."""
    global _digest, _drawn
    if _bounds is None or board.in_snapshot:
        return
    rows = {name: _rows(side) for name, side in CHANNELS}
    digest = hashlib.blake2b(np.asarray(_bounds).tobytes() + b"".join(r.tobytes() for r in rows.values())
                             + _outline[0].tobytes() + _outline[1].tobytes(), digest_size=16).digest()
    if digest == _digest and bpy.data.images.get(IMAGE) is not None:
        refresh_sides()
        return
    _digest = digest
    image = bpy.data.images.get(IMAGE)
    previous = _drawn[1] if _drawn is not None and _drawn[0] == _bounds and image is not None else {}
    pixels = _drawn[2] if previous else None
    alphas = {}
    for name, _ in CHANNELS:
        same = next((alphas[other] for other in alphas if np.array_equal(rows[other], rows[name])), None)
        if same is not None:  # no blind vias on this side: the through holes' drawing
            alphas[name] = same.copy()
            continue
        alpha = None
        if name in previous:
            alpha = _redraw_changed(previous[name][1], _bounds, previous[name][0], rows[name])
        alphas[name] = alpha if alpha is not None else rasterize(_bounds, rows[name])
    height, width = alphas["through"].shape
    if image is None or tuple(image.size) != (width, height):
        if image is not None:
            bpy.data.images.remove(image)
        image = bpy.data.images.new(IMAGE, width, height, alpha=True)
        image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "CHANNEL_PACKED"  # the colour channels are holes too, not colour under alpha
    if pixels is None or pixels.shape[:2] != (height, width):
        pixels = np.ones((height, width, 4), np.float32)  # reused while the size holds
    pixels[..., 0], pixels[..., 1], pixels[..., 3] = alphas["F"], alphas["B"], alphas["through"]
    pixels[..., 2] = _board_area((height, width))
    image.pixels.foreach_set(pixels.ravel())
    image.update()
    _drawn = (_bounds, {name: (rows[name], alphas[name]) for name in alphas}, pixels)
    for key, material in board.materials.items():
        if key in HOLED or key.startswith("copper:"):
            add_to(material)
        if key in CLIPPED or key.startswith("copper:"):
            clip_to_board(material)
    for material in mask_materials():
        add_to(material)


@bpy.app.handlers.persistent
def pack_for_save(*_):
    """Before a save: pack the mask into the file. It is drawn in memory, so a plain save
    keeps no pixels, and the file would reopen with the board and copper see-through.
    Packed once, it is packed again only when redrawn since (8-bit PNG, exact enough)."""
    image = bpy.data.images.get(IMAGE)
    if image is not None and (image.packed_file is None or image.is_dirty):
        image.pack()


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
        texture = nodes.new("ShaderNodeTexImage")
        texture.name, texture.extension = "KLS holes plot", "CLIP"
        shading.project_plot(tree, shading.flat_position(tree), texture, "KLS holes offset", "KLS holes scale")
        opening = shading.sharp_alpha(material, texture)
        solid = nodes.new("ShaderNodeMath")
        solid.operation = "SUBTRACT"
        solid.inputs[0].default_value = 1.0
        links.new(opening, solid.inputs[1])
        _multiply_coverage(material, solid.outputs[0], "KLS holes mix")  # "not a hole"
    if nodes.get(SIDE_NODE) is None:  # a new material, or one from before the side channels
        _side_select(material, texture)
    _set_side_heights(material)
    texture.image = image
    xmin, ymin, xmax, ymax = _bounds
    width, height = image.size
    pixel = max(xmax - xmin, ymax - ymin) / RESOLUTION
    shading.set_plot_rectangle(tree, "KLS holes offset", "KLS holes scale", xmin, ymin, width * pixel, height * pixel)


def _multiply_coverage(material, coverage, name):
    """Multiply the material's see-through by `coverage` (1 shown, 0 see-through): into
    its first Mix Shader's factor, or, for an opaque material, a new Mix Shader `name`
    before the output. `materials.set_surface` links the lit/flat shader into the first
    Mix Shader's second input, so this survives colour-mode switches."""
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    mix = next((node for node in nodes if node.type == "MIX_SHADER"), None)
    if mix is None:
        surface = focus.surface_input(material)  # before the focus node, if there is one
        shader = surface.links[0].from_socket if surface.is_linked else None
        mix = nodes.new("ShaderNodeMixShader")
        mix.name = name
        links.new(nodes.new("ShaderNodeBsdfTransparent").outputs[0], mix.inputs[1])
        if shader is not None:
            links.new(shader, mix.inputs[2])
        links.new(mix.outputs[0], surface)
        links.new(coverage, mix.inputs[0])
    else:
        factor = mix.inputs[0]
        before = factor.links[0].from_socket if factor.is_linked else factor.default_value
        links.new(shading.math_node(tree, "MULTIPLY", before, coverage), factor)
    focus.set_render_method(material)


def clip_to_board(material):
    """See-through outside the board outline (the mask's blue channel), as the fab mills
    copper there away; built once per material. Drill walls (`WALLS`) reach a little
    past the outline: one whose board edge follows the drill (a castellation drawn as a
    notch in Edge.Cuts) stands right on it, where the fab leaves its plating. Without
    an outline, `CLIP_OFF` keeps everything shown."""
    image = bpy.data.images.get(IMAGE)
    if image is None or _bounds is None or material.node_tree is None:
        return
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    texture = nodes.get(CLIP_TEXTURE)
    if texture is None:
        texture = nodes.new("ShaderNodeTexImage")
        texture.name, texture.extension = CLIP_TEXTURE, "CLIP"  # past the outline's box: 0, outside
        shading.project_plot(tree, shading.flat_position(tree), texture, "KLS board offset", "KLS board scale")
        channels = nodes.new("ShaderNodeSeparateColor")
        links.new(texture.outputs["Color"], channels.inputs["Color"])
        edge = nodes.new("ShaderNodeMapRange")  # crisp at the outline, as `shading.sharp_alpha`
        edge.name, edge.clamp = CLIP_EDGE, True
        links.new(channels.outputs["Blue"], edge.inputs["Value"])
        off = nodes.new("ShaderNodeValue")
        off.name = CLIP_OFF
        inside = shading.math_node(tree, "MAXIMUM", edge.outputs["Result"], off.outputs[0])
        _multiply_coverage(material, inside, "KLS board clip mix")  # "on the board"
    texture.image = image
    edge = nodes.get(CLIP_EDGE)
    if edge is not None:
        wall = any(board.materials.get(key) == material for key in WALLS)
        edge.inputs["From Min"].default_value, edge.inputs["From Max"].default_value = WALL_RAMP if wall else (0.4, 0.6)
    nodes[CLIP_OFF].outputs[0].default_value = 0.0 if clipping() else 1.0
    xmin, ymin, xmax, ymax = _bounds
    width, height = image.size
    pixel = max(xmax - xmin, ymax - ymin) / RESOLUTION
    shading.set_plot_rectangle(tree, "KLS board offset", "KLS board scale", xmin, ymin, width * pixel, height * pixel)


def _side_select(material, texture):
    """Feed the hole edge the channel of this point's side (see the module docstring)."""
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    edge = nodes.get(f"KLS alpha edge {texture.name}")
    channels = nodes.new("ShaderNodeSeparateColor")
    links.new(texture.outputs["Color"], channels.inputs["Color"])
    z = nodes.new("ShaderNodeSeparateXYZ")
    links.new(shading.flat_position(tree), z.inputs["Vector"])
    picked = texture.outputs["Alpha"]
    for name, channel, operation in (("KLS holes bottom", "Green", "LESS_THAN"), ("KLS holes top", "Red",
                                                                                 "GREATER_THAN")):
        limit = nodes.new("ShaderNodeValue")
        limit.name = name
        beyond = nodes.new("ShaderNodeMath")
        beyond.operation = operation
        links.new(z.outputs["Z"], beyond.inputs[0])
        links.new(limit.outputs[0], beyond.inputs[1])
        mix = nodes.new("ShaderNodeMix")
        mix.data_type = "FLOAT"
        links.new(beyond.outputs[0], mix.inputs["Factor"])
        links.new(picked, mix.inputs["A"])
        links.new(channels.outputs[channel], mix.inputs["B"])
        picked = mix.outputs["Result"]
    picked.node.name = SIDE_NODE
    links.new(picked, edge.inputs["Value"])


def _set_side_heights(material):
    nodes = material.node_tree.nodes
    top, bottom = laminate_faces()
    if "KLS holes top" in nodes:
        nodes["KLS holes top"].outputs[0].default_value = top - SIDE_MARGIN_M
        nodes["KLS holes bottom"].outputs[0].default_value = bottom + SIDE_MARGIN_M


def refresh_sides():
    """The laminate faces moved (thickness settings): every holed material's side limits."""
    for key, material in board.materials.items():
        if (key in HOLED or key.startswith("copper:")) and material.node_tree is not None:
            _set_side_heights(material)
    for material in mask_materials():
        _set_side_heights(material)
