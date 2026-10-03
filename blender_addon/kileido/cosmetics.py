"""Solder mask, silkscreen and drawing overlays, exported outside the live timer.

The worker exports the bridge's live board copy (unsaved edits included) when the
bridge provides one, else the saved board, as Gerbers in one kicad-cli call
(~0.8 s on the reference board; KiCad's SVG plot took ~4.4 s), and `gerber.py`
draws them into images. Results are cached by board content. The outer copper
layers are plotted too, blurred, as the relief of the mask lying over the copper
(materials.set_relief_image): no overlay of their own.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
import numpy as np

from . import cut, focus, gerber, holes, kicad_cli, materials, nodes, shading
from .objects import (OUTLINE, find, link_owned, outline_bounds, owned_object, set_modifier, set_node_input,
                      set_visible)
from .placement import laminate_faces, mask_thickness, outward
from .state import board
from .watcher import BoardWatcher

LAYERS = (  # (KiCad layer, panel label, visible by default)
    ("F.Mask", "Top Solder Mask", True),
    ("B.Mask", "Bottom Solder Mask", True),
    ("F.SilkS", "Top Overlay", True),
    ("B.SilkS", "Bottom Overlay", True),
    ("F.Fab", "Top Fabrication", False),
    ("B.Fab", "Bottom Fabrication", False),
    ("Dwgs.User", "User Drawings", False),
    ("Cmts.User", "User Comments", False),
    ("Eco1.User", "User Eco 1", False),
    ("Eco2.User", "User Eco 2", False),
    *((f"User.{number}", f"User {number}", False) for number in range(1, 46)),
)
DEFAULT_VISIBLE = {layer: default for layer, _, default in LAYERS}
FALLBACK_COLOR = (0.85, 0.85, 0.85)
FALLBACK_MASK_OPACITY = 0.7
# Heights above the board faces (display clearances, not physical thicknesses).
MASK_SEPARATION_M = 4e-6
SILKSCREEN_SEPARATION_M = 8e-6
DRAWING_SEPARATION_M = 12e-6
SHEET_BELOW_COPPER_M = 0.5e-6
# The mask sheet's inner face stands this far out of the laminate face: outer copper's own faces
# there (staggered by up to 3 um, transform.copper_z) lie inside it, so from inside the board (cut
# open, hidden or see-through) copper is seen where there is copper, and the mask only between it.
SHEET_OFF_LAMINATE_M = 4e-6
SHEET_THINNEST_M = 1e-6  # what a thin mask's sheet keeps
WALLED = (".SilkS",)  # layers whose outlines can get walls (the panel's silkscreen thickness)
RELIEF = ("F.Cu", "B.Cu")  # plotted for the mask's relief over the copper, not shown as overlays
# The mask flows over copper edges rather than stepping: its surface rises over this width.
RELIEF_BLUR_M = 50e-6

OVERLAY_CACHE = "gerber-5"  # part of the cache key: bump when the plots or the entry layout change


def object_name(layer):
    return f"KLS overlay {layer}"


def material_name(layer):
    return f"KLS overlay {layer} material"


def property_name(layer):
    return "kileido_show_" + layer.replace(".", "_")


def walls_name(layer):
    return f"KLS overlay {layer} walls"


def wall_material_name(layer):
    return f"KLS overlay {layer} wall material"


def silkscreen_thickness():
    """Silkscreen ink height (m), or 0 when the panel's tick box is off."""
    scene = bpy.context.scene
    if not getattr(scene, "kileido_silk_3d", False):
        return 0.0
    return max(0.0, float(getattr(scene, "kileido_silk_um", 15.0))) * 1e-6


def silkscreen_opacity():
    """The panel's silkscreen opacity: 1 hides what lies under the ink, as real ink nearly does."""
    return min(1.0, max(0.0, float(getattr(bpy.context.scene, "kileido_silk_opacity", 1.0))))


def status():
    return _watcher.status


def follow_board(board_path, export=None):
    _watcher.follow(board_path, export)


def stop_following():
    _watcher.stop()


def drain():
    """Handle one finished export now (headless tools; the GUI timer does this)."""
    return _watcher.drain()


# --- Visibility and colour ------------------------------------------------------------------

def set_layer_visible(layer, visible):
    if board.collection is None:
        return
    obj = board.collection.all_objects.get(object_name(layer))
    shown = False
    if obj is not None:
        shown = (visible and obj.get("kls_board_path") == board.board_path and
                 obj.get("kls_overlay_available", True))
        set_visible(obj, shown)
    walls = board.collection.all_objects.get(walls_name(layer))
    if walls is not None:
        set_visible(walls, shown and walls.get("kls_walls_active", False))
    if layer.endswith((".Mask", ".SilkS")):
        refresh_silk()  # covered copper shows mask colour, and carries the ink, only while the mask is shown


def visible_layers():
    """The panel's Appearance list: layers with an overlay for the current board."""
    if board.collection is None or board.drop_if_freed("panel draw"):
        return []
    return [entry for entry in LAYERS
            if (obj := board.collection.all_objects.get(object_name(entry[0]))) is not None
            and obj.get("kls_board_path") == board.board_path]


def _layer_color(layer):
    """sRGBA of an overlay in the current colour mode, or None."""
    appearance = board.appearance
    viewer = appearance.get("viewer", {})
    saved = appearance.get("saved_colors", {})
    editor_layers = appearance.get("editor_layers", {})
    top = layer.startswith("F.")
    if board.color_mode == "EDITOR":
        if layer.endswith(".Mask"):
            return appearance.get("editor_mask_top" if top else "editor_mask_bottom")
        return editor_layers.get(layer)
    # `viewer` is KiCad's final 3D-viewer colour (theme + stackup names such as
    # "White" resolved by the bridge); raw saved colours are only a fallback.
    if layer.endswith(".SilkS"):
        return viewer.get("silkscreen_top" if top else "silkscreen_bottom") or saved.get(layer)
    if layer.endswith(".Mask"):
        return viewer.get("soldermask_top" if top else "soldermask_bottom") or saved.get(layer)
    return editor_layers.get(layer)


def recolor():
    """Colour, opacity and height of every overlay for the current colour mode (and,
    through `set_layer_visible`, where the silkscreen is drawn)."""
    core = materials.core_color()
    for layer, _, default in LAYERS:
        material = bpy.data.materials.get(material_name(layer))
        if material is None:
            continue
        color = _layer_color(layer)
        opacity = color[3] if color and len(color) > 3 else FALLBACK_MASK_OPACITY
        if layer.endswith(".Mask") and board.color_mode != "EDITOR" and color:
            # A solid mask sheet: KiCad's translucent colour over the laminate below it,
            # drawn opaque (copper under it gets the same treatment in materials.finish_mask).
            color = materials.mask_on_laminate(color, core)
            opacity = 1.0
        elif layer.endswith(".Mask"):  # PCB Editor colours: the translucent sheet itself
            opacity *= materials.mask_opacity()
        materials.paint(material, color or FALLBACK_COLOR)
        materials.set_surface(material, board.color_mode == "REALISTIC", roughness=0.4)
        wall = bpy.data.materials.get(wall_material_name(layer))
        if wall is not None:  # the same colour, solid (the sheet's alpha is the plot)
            materials.paint(wall, color or FALLBACK_COLOR)
            materials.set_surface(wall, board.color_mode == "REALISTIC", roughness=0.4)
        if layer.endswith(".Mask"):
            _mask_opacity(material, opacity)
        obj = board.collection.all_objects.get(object_name(layer)) if board.collection else None
        if obj is not None:
            _place(obj)
        set_layer_visible(layer, getattr(bpy.context.scene, property_name(layer), default))


def _place(obj):
    """Height (and thickness) of an overlay sheet.

    Board-stackup/Realistic colours: the mask is a solid sheet with its outer surface the
    stackup's mask thickness above the laminate (its inner face a little off the laminate,
    `SHEET_OFF_LAMINATE_M`); outer copper (thicker) rises through it and is
    coloured as mask-covered where the mask plot is closed. Silkscreen and drawings
    sit just above the copper tops. PCB Editor colours keep a translucent mask
    sheet above everything, like the 2D editor.

    Silkscreen thickness on: the silkscreen sheet (the ink's top face) rises by
    that height and walls stand along the ink's edges below it (not in PCB Editor
    colours, and not while the ink is printed on the surfaces: `refresh_silk`).
    The mask needs no walls: it is a solid sheet with the openings cut through both
    faces, so its far edge already shows inside an opening.
    """
    layer = obj["kls_cosmetic_layer"]
    thickness = 0.0
    walls_z = walls_height = None
    if layer.endswith(".Mask") and board.color_mode != "EDITOR":
        side = layer[0]
        top, bottom = laminate_faces()
        sheet = mask_thickness(side)
        inset = max(0.0, min(SHEET_OFF_LAMINATE_M, sheet - SHEET_THINNEST_M))  # its inner face, off the laminate
        # The opaque sheet must stay below the copper's outer surface, or flat copper
        # (thickness toggle off) would be hidden under it.
        if side == "F":
            surface = min(top + sheet, board.thickness_m - SHEET_BELOW_COPPER_M)
            z, thickness = surface - sheet + inset, sheet - inset
        else:
            surface = max(bottom - sheet, SHEET_BELOW_COPPER_M)
            z, thickness = surface + sheet - inset, inset - sheet
    else:
        separation = MASK_SEPARATION_M if layer.endswith(".Mask") else SILKSCREEN_SEPARATION_M
        if layer.startswith("F."):
            z = board.thickness_m + separation
        elif layer.startswith("B."):
            z = -separation
        else:
            z = board.thickness_m + DRAWING_SEPARATION_M
        ink = (silkscreen_thickness() if layer.endswith(".SilkS") and board.color_mode != "EDITOR" and
               not obj.get("kls_silk_on_surface") else 0.0)
        if ink:
            walls_z, walls_height = z, ink if layer.startswith("F.") else -ink
            z += walls_height
    obj.location.z = z
    walls = find(walls_name(layer)) if board.collection else None
    if walls is not None:
        walls["kls_walls_active"] = walls_height is not None
        if walls_height is not None:
            walls.location.z = walls_z
            set_node_input(walls, "Height", walls_height)
    if obj.get("kls_sheet_thickness") != thickness:
        obj["kls_sheet_thickness"] = thickness
        set_node_input(obj, "Thickness", thickness)


def _mask_opacity(material, opacity):
    """Keep pad openings clear; apply color alpha only where mask is present."""
    multiply = _coverage_factor(material, "KLS mask opacity")
    multiply.inputs[1].default_value = max(0.0, min(1.0, opacity))
    material.diffuse_color = (*material.diffuse_color[:3], multiply.inputs[1].default_value)
    focus.set_render_method(material)


def _coverage_factor(material, name):
    """A factor on a sheet's plot coverage (the opacity of what it draws), added once."""
    tree = material.node_tree
    multiply = tree.nodes.get(name)
    if multiply is None:
        mix = next(node for node in tree.nodes if node.type == "MIX_SHADER")
        coverage = mix.inputs[0].links[0].from_socket
        multiply = tree.nodes.new("ShaderNodeMath")
        multiply.name = name
        multiply.operation = "MULTIPLY"
        multiply.inputs[1].default_value = 1.0
        tree.links.new(coverage, multiply.inputs[0])
        tree.links.new(multiply.outputs[0], mix.inputs[0])
    return multiply


# --- Silkscreen printed on the surfaces -----------------------------------------------------

def _silk_on_surface(side, sheet):
    """Board stackup and Realistic colours with that side's mask plotted and shown:
    the silkscreen is printed on the mask and the copper it covers, so it follows
    the copper's relief (traces show through a silkscreen fill). Else the flat sheet."""
    return (board.color_mode != "EDITOR" and sheet is not None and sheet.get("kls_board_path") == board.board_path
            and sheet.get("kls_overlay_available", True) and side in board.mask_images
            and materials.mask_color(side) is not None)


def refresh_silk():
    """Where each side's silkscreen is drawn (the flat sheet or the surfaces), in its
    colour, with the panel's opacity and ink thickness."""
    if board.collection is None:
        return
    scene = bpy.context.scene
    for side in "FB":
        layer = f"{side}.SilkS"
        sheet = board.collection.all_objects.get(object_name(layer))
        material = bpy.data.materials.get(material_name(layer))
        if sheet is None or material is None:
            board.silk.pop(side, None)
            continue
        active = _silk_on_surface(side, sheet)
        color = _layer_color(layer) or FALLBACK_COLOR
        board.silk[side] = {"image": next(node for node in material.node_tree.nodes if node.type == "TEX_IMAGE").image,
                            "bounds": tuple(sheet.get("kls_plot_rect", ())),
                            "color": (*shading.srgb_to_linear(color[:3]), 1.0), "active": active,
                            "shown": bool(getattr(scene, property_name(layer), DEFAULT_VISIBLE[layer]))}
        sheet["kls_silk_on_surface"] = active
        materials.set_silk_state(material, side, active, True)
        _place(sheet)
        walls = board.collection.all_objects.get(walls_name(layer))
        if walls is not None:
            set_visible(walls, not sheet.hide_get() and walls.get("kls_walls_active", False))
    materials.refresh_mask_colors()  # outer copper and vias
    for side in "FB":
        mask = bpy.data.materials.get(material_name(f"{side}.Mask"))
        if mask is None:
            continue
        _print_on_mask(mask, side)  # also the mask's gloss and relief, with or without ink
        silk = board.silk.get(side)
        if silk is not None and len(silk["bounds"]) == 4:
            materials.set_silk_state(mask, side, silk["active"], silk["shown"])
            materials.set_silk_plot(mask, side, silk["image"], silk["bounds"], silk["color"])
        else:
            materials.set_silk_state(mask, side, False, False)
        materials.set_relief(mask, (side,))
    apply_silk_settings()


def _remove_unused(images):
    """Superseded silkscreen and copper relief plots: the sheets, walls and copper let go
    of one first, but the mask sheets sample it until `refresh_silk` hands them the new
    plot, so the earlier removals (users == 0) never saw it free."""
    for image in images:
        try:
            if image is not None and image.users == 0:
                bpy.data.images.remove(image)
        except ReferenceError:
            pass  # removed already, by whichever user let go of it last


def _print_on_mask(material, side):
    """The mask sheet carries its side's ink, sampled in its own plot coordinates."""
    nodes = material.node_tree.nodes
    texture = next(node for node in nodes if node.type == "TEX_IMAGE")  # the mask plot
    base = nodes.get(materials.BASE_COLOR)
    if base is None:
        base = nodes.new("ShaderNodeRGB")
        base.name = materials.BASE_COLOR
        emission = next(node for node in nodes if node.type == "EMISSION")
        base.outputs[0].default_value = emission.inputs["Color"].default_value[:]
    coordinates = next(node for node in nodes if node.type == "TEX_COORD").outputs["Object"]
    materials.print_silk(material, (side,), base.outputs[0], shading.sharp_alpha(material, texture), coordinates)


def apply_silk_settings():
    """The panel's silkscreen opacity and thickness on every board's materials."""
    opacity, thickness = silkscreen_opacity(), silkscreen_thickness()
    for material in bpy.data.materials:
        materials.set_silk_settings(material, opacity, thickness)


# --- Export (worker thread) -----------------------------------------------------------------

def _gerber_command(cli, board_path, layers, output, project):
    """Rounded and rotated pads come out as plain regions without aperture macros."""
    return [str(cli), "pcb", "export", "gerbers", "--layers", layers, "--disable-aperture-macros",
            "--no-x2", "--no-netlist", "--no-protel-ext", *kicad_cli.defines(project),
            "--output", str(output), str(board_path)]


def layer_display_names(board_text):
    """Canonical layer -> the name KiCad shows (and uses in plot file names)."""
    names = {}
    table = re.search(r"\(layers\s*((?:\s*\(\d+\s+\"[^\"]+\"\s+\w+(?:\s+\"[^\"]*\")?\s*\))+)", board_text)
    for canonical, custom in re.findall(r'\(\d+\s+"([^"]+)"\s+\w+(?:\s+"([^"]*)")?\s*\)',
                                        table[1] if table else ""):
        names[canonical] = custom or canonical
    return names


def _export_gerbers(cli, board_path, board_text, layers, directory, project):
    """All layers in one kicad-cli call (each call reloads the whole board), mapped
    back to layers: files are `<board stem>-<display name, '.' as '_'>.gbr`."""
    output = directory / "gerbers"
    output.mkdir()
    result = kicad_cli.run(_gerber_command(cli, board_path, ",".join(layers), output, project), 120)
    if result.returncode != 0:
        raise RuntimeError(f"kicad-cli Gerber export failed: {result.stderr.strip()[-300:]}")
    names = layer_display_names(board_text)
    found = {}
    for layer in layers:
        path = output / f"{Path(board_path).stem}-{names.get(layer, layer).replace('.', '_')}.gbr"
        if path.is_file():
            found[layer] = path
    return found


def _on_board_change(job, _memory):
    """Worker: plot every used drawing layer; results are cached by board content."""
    with job.scratch_directory("kileido_overlays_") as directory:
        cli = kicad_cli.executable(job.export)
        board_bytes = job.source.read_bytes()
        board_text = board_bytes.decode("utf-8")
        finish = re.search(r'\(copper_finish\s+"([^"]*)"\)', board_text)
        finish = finish[1] if finish else None
        used_layers = set(re.findall(r'\(layers?\s+"([^"]+)"', board_text))
        wanted = [layer for layer, _, _ in LAYERS if layer in used_layers or layer.endswith(".Mask")]
        project = kicad_cli.project_dir(job.export, job.source)
        key = kicad_cli.cache_key(board_bytes, kicad_cli.identity(cli), ",".join(wanted), project, OVERLAY_CACHE)
        entry = kicad_cli.lookup("overlays", key)
        if entry is not None:
            rows = json.loads((entry / "results.json").read_text(encoding="utf-8"))
            job.emit(directory, finish=finish,
                     plots=[(layer, str(entry / png), rect, str(entry / outline) if outline else None)
                            for layer, png, rect, outline in rows])
            return
        board_file = kicad_cli.board_copy(job.source, board_bytes, directory)  # exactly what `key` says
        files = _export_gerbers(cli, board_file, board_text, [*wanted, *RELIEF, "Edge.Cuts"], directory, project)
        if job.stop.is_set():
            return
        with ThreadPoolExecutor(max_workers=4) as pool:  # numpy releases the GIL while drawing
            parsed = dict(zip(files, pool.map(lambda path: gerber.parse(path.read_text(encoding="utf-8")),
                                              files.values())))
            edge = parsed.pop("Edge.Cuts", None)
            # The images cover the board outline's box, as KiCad's board-area SVG plots did.
            bounds = edge.bounds(centre_line=True) if edge is not None else None
            if bounds is None:
                boxes = [box for plot in parsed.values() if (box := plot.bounds()) is not None]
                bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                          max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None
            if bounds is None:
                job.emit(directory, finish=finish, plots=[])
                return
            pixel, _, _, rect = gerber.grid(bounds)

            def draw(layer):
                plot = parsed[layer]
                if not plot.groups and not layer.endswith(".Mask"):
                    return None  # an empty layer gets no overlay (a mask without openings does)
                png = directory / f"{layer}.png"
                coverage = gerber.rasterize(plot, bounds)
                if layer in RELIEF:
                    coverage = gerber.blur(coverage, max(0.7, RELIEF_BLUR_M * 1e9 / pixel))
                gerber.write_png(png, coverage)
                outline = None
                if layer.endswith(WALLED):
                    points, sizes = gerber.contours(plot, spacing=pixel)
                    outline = directory / f"{layer}.npz"
                    # float32 relative to the image corner: a few nm at board scale
                    np.savez(outline, points=(points - rect[:2]).astype(np.float32), sizes=sizes)
                return layer, str(png), str(outline) if outline else None

            plots = [row for row in pool.map(draw, [layer for layer in (*wanted, *RELIEF) if layer in parsed])
                     if row]
        if job.stop.is_set():
            return
        rows = [(layer, Path(png).name, list(rect), Path(outline).name if outline else None)
                for layer, png, outline in plots]
        (directory / "results.json").write_text(json.dumps(rows), encoding="utf-8")
        files = {Path(path).name: path for row in plots for path in row[1:] if path}
        entry = kicad_cli.store("overlays", key, {**files, "results.json": directory / "results.json"}) or directory
        job.emit(directory, finish=finish,
                 plots=[(layer, str(entry / png), rect, str(entry / outline) if outline else None)
                        for layer, png, rect, outline in rows])


def _on_result(result):
    """Main thread: show the plotted layers and the finish read from the same board text."""
    board.appearance["copper_finish"] = result.data["finish"]
    count = _apply(result.data["plots"], result.board_path)
    materials.set_color_mode(board.color_mode)
    recolor()
    return f"Loaded {count} drawing layers"


_watcher = BoardWatcher(
    "overlay export", _on_board_change, _on_result,
    starting=lambda export: ("Loading overlays from the open board…" if export.get("live")
                             else "Loading saved-board overlays…"),
    unavailable="", failed="Overlay export failed", import_failed="Overlay import failed")


# --- Overlay objects (main thread) ----------------------------------------------------------

def _overlay_material(layer, image, bounds):
    """A plot image as a transparent sheet (a solid sheet with clear openings for mask)."""
    name = material_name(layer)
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        material.node_tree.nodes.clear()
        nodes = material.node_tree.nodes
        output = nodes.new("ShaderNodeOutputMaterial")
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        emission = nodes.new("ShaderNodeEmission")
        mix = nodes.new("ShaderNodeMixShader")
        texture = nodes.new("ShaderNodeTexImage")
        links = material.node_tree.links
        links.new(texture.outputs["Alpha"], mix.inputs[0])
        links.new(transparent.outputs[0], mix.inputs[1])
        links.new(emission.outputs[0], mix.inputs[2])
        links.new(mix.outputs[0], output.inputs["Surface"])
        if layer.endswith(".Mask"):
            invert = nodes.new("ShaderNodeMath")
            invert.operation = "SUBTRACT"
            invert.inputs[0].default_value = 1
            links.new(texture.outputs["Alpha"], invert.inputs[1])
            links.new(invert.outputs[0], mix.inputs[0])
    texture = next(node for node in material.node_tree.nodes if node.type == "TEX_IMAGE")
    shading.sharp_alpha(material, texture)
    if layer.endswith((".Mask", ".SilkS")):
        holes.add_to(material)  # open vias and drills go through mask and silkscreen too
    if layer.endswith(".SilkS"):
        material["kls_silk_side"] = layer[0]
        _coverage_factor(material, materials.SHEET_OPACITY)
    previous = texture.image
    texture.image = image
    _mapping(material, bounds)
    if previous is not None and previous.users == 0:
        bpy.data.images.remove(previous)
    material.diffuse_color = (*FALLBACK_COLOR, 1)
    return material


def plot_rect_m(rect_nm):
    """A plot rectangle from Gerber nanometres (KiCad's y negated) to Blender metres."""
    ox, oy = board.origin_nm
    xmin, ymin, xmax, ymax = rect_nm
    return ((xmin - ox) * 1e-9, (ymin + oy) * 1e-9, (xmax - ox) * 1e-9, (ymax + oy) * 1e-9)


def _mapping(material, bounds):
    """The image covers exactly `bounds` (Blender metres): the board outline's box,
    rounded out to whole pixels."""
    tree = material.node_tree
    texture = next(node for node in tree.nodes if node.type == "TEX_IMAGE")
    texture.extension = "CLIP"
    if tree.nodes.get("KLS plot offset") is None:
        shading.project_plot(tree, tree.nodes.new("ShaderNodeTexCoord").outputs["Object"], texture,
                             "KLS plot offset", "KLS plot scale")
    xmin, ymin, xmax, ymax = bounds
    shading.set_plot_rectangle(tree, "KLS plot offset", "KLS plot scale", xmin, ymin, xmax - xmin, ymax - ymin)


def _geometry(obj, bounds, outline):
    """Mask (and clipped silkscreen): the board outline filled; else a plain quad."""
    layer = obj["kls_cosmetic_layer"]
    clipped = layer.endswith(".Mask") or (layer.endswith(".SilkS") and
                                          getattr(bpy.context.scene, "kileido_clip_silkscreen", True))
    if clipped:
        _outline_mesh(obj.data, outline.data)
        set_modifier(obj, board.groups["fill_single"], obj.data.materials[0],
                     {"Thickness": float(obj.get("kls_sheet_thickness", 0.0)), "Up": outward(layer)})
    else:
        _quad_mesh(obj.data, bounds)
    for modifier in obj.modifiers:
        if modifier.type == "NODES":
            modifier.show_viewport = clipped
            modifier.show_render = clipped


def _outline_mesh(mesh, outline):
    """The outline's contours, flat at the object's origin (Geometry Nodes fills them)."""
    mesh.clear_geometry()
    coordinates = np.empty(len(outline.vertices) * 3, dtype=np.float32)
    edges = np.empty(len(outline.edges) * 2, dtype=np.int32)
    outline.vertices.foreach_get("co", coordinates)
    coordinates[2::3] = 0
    outline.edges.foreach_get("vertices", edges)
    mesh.vertices.add(len(coordinates) // 3)
    mesh.vertices.foreach_set("co", coordinates)
    mesh.edges.add(len(edges) // 2)
    mesh.edges.foreach_set("vertices", edges)
    mesh.update()


def _quad_mesh(mesh, bounds):
    xmin, ymin, xmax, ymax = bounds
    mesh.clear_geometry()
    mesh.vertices.add(4)
    mesh.vertices.foreach_set("co", np.array((xmin, ymin, 0, xmax, ymin, 0,
                                                xmax, ymax, 0, xmin, ymax, 0), dtype=np.float32))
    mesh.loops.add(4)
    mesh.loops.foreach_set("vertex_index", np.array((0, 1, 2, 3), dtype=np.int32))
    mesh.polygons.add(1)
    mesh.polygons.foreach_set("loop_start", np.array((0,), dtype=np.int32))
    mesh.polygons.foreach_set("loop_total", np.array((4,), dtype=np.int32))
    uv = mesh.uv_layers.new(name="KiCad plot")
    uv.data.foreach_set("uv", np.array((0, 0, 1, 0, 1, 1, 0, 1), dtype=np.float32))
    mesh.update()


def _walls(layer, outline_path, image, rect_nm, board_path):
    """Walls along the layer's outlines (silkscreen ink); `_place` sets their height. Geometry Nodes build them (nodes.plot_walls)."""
    data = np.load(outline_path)
    points, sizes = data["points"].astype(np.float64), data["sizes"].astype(np.int64)
    ox, oy = board.origin_nm
    count = len(points)
    coordinates = np.zeros((count, 3), np.float32)
    coordinates[:, 0] = (points[:, 0] + rect_nm[0] - ox) * 1e-9
    coordinates[:, 1] = (points[:, 1] + rect_nm[1] + oy) * 1e-9
    edges = np.column_stack((np.arange(count), np.arange(1, count + 1))).astype(np.int32)
    ends = np.cumsum(sizes)
    if count:
        edges[ends - 1, 1] = ends - sizes  # each outline closes on its first point
    obj = owned_object(walls_name(layer))
    obj["kls_overlay_walls"] = layer
    obj["kls_board_path"] = board_path
    mesh = obj.data
    mesh.clear_geometry()
    mesh.vertices.add(count)
    mesh.edges.add(count)
    if count:
        mesh.vertices.foreach_set("co", coordinates.ravel())
        mesh.edges.foreach_set("vertices", edges.ravel())
    mesh.update()
    material = materials.make(wall_material_name(layer), FALLBACK_COLOR)
    rect = plot_rect_m(rect_nm)
    group = nodes.plot_walls()
    modifier = obj.modifiers.get(group.name)
    previous = nodes.modifier_value(modifier, next(item.identifier for item in group.interface.items_tree
                                                   if getattr(item, "name", "") == "Image")) if modifier else None
    set_modifier(obj, group, material, {
        "Image": image, "Plot Offset": (rect[0], rect[1], 0.0),
        "Plot Size": (rect[2] - rect[0], rect[3] - rect[1], 1.0),
        "Probe": (rect[2] - rect[0]) / max(1, image.size[0]),  # one pixel outside the edge
        "Height": 0.0})
    _clip_walls(obj)
    if previous is not None and previous != image and previous.users == 0:
        bpy.data.images.remove(previous)  # the sheet's material let go of it already


def _clip_walls(obj):
    """Walls follow the sheet: cut at the board outline when silkscreen is clipped."""
    group = nodes.plot_walls()
    modifier = obj.modifiers.get(group.name)
    if modifier is None:
        return
    outline = board.collection.all_objects.get(OUTLINE)
    clip = outline is not None and getattr(bpy.context.scene, "kileido_clip_silkscreen", True)
    nodes.modifier_input(modifier, group, "Outline", outline)
    nodes.modifier_input(modifier, group, "Clip", 1.0 if clip else 0.0)
    obj.update_tag()


def refresh_geometry():
    """The outline changed or the clip setting toggled: rebuild the overlay sheets."""
    bounds = outline_bounds()
    if bounds is None:
        return
    outline = board.collection.all_objects[OUTLINE]
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_cosmetic_layer") and obj.get("kls_board_path") == board.board_path:
            _geometry(obj, bounds, outline)
            _mapping(obj.data.materials[0], tuple(obj.get("kls_plot_rect", bounds)))
        elif obj.get("kls_overlay_walls"):
            _clip_walls(obj)


def _apply(plots, board_path):
    """Create or update one overlay object per plotted layer; hide the rest."""
    bounds = outline_bounds()
    if bounds is None:
        raise ValueError("Board outline is unavailable for overlay alignment")
    outline = board.collection.all_objects[OUTLINE]
    superseded = [silk["image"] for silk in board.silk.values()]
    superseded += [image for image, _ in board.relief_images.values()]
    loaded = set()
    reliefs = set()
    for layer, png, rect_nm, outline_path in plots:
        rect = plot_rect_m(rect_nm)
        image = bpy.data.images.load(png, check_existing=False)
        image.pack()
        if layer in RELIEF:
            materials.set_relief_image(layer[0], image, rect)
            reliefs.add(layer[0])
            continue
        name = object_name(layer)
        obj = board.collection.all_objects.get(name)
        if obj is None:
            obj = bpy.data.objects.new(name, bpy.data.meshes.new(name))
            link_owned(obj)
        obj["kileido_owned"] = 1
        obj["kls_cosmetic_layer"] = layer
        obj["kls_board_path"] = board_path
        obj["kls_overlay_available"] = True
        obj["kls_plot_rect"] = rect
        material = _overlay_material(layer, image, rect)
        if layer.endswith(".Mask"):  # copper shows the board finish only in these openings
            materials.set_mask_image(layer[0], image, rect)
        obj.data.materials.clear()
        obj.data.materials.append(material)
        _geometry(obj, bounds, outline)
        if outline_path:
            _walls(layer, outline_path, image, rect_nm, board_path)
        _place(obj)
        set_layer_visible(layer, getattr(bpy.context.scene, property_name(layer), DEFAULT_VISIBLE[layer]))
        loaded.add(layer)
    for side in "FB":
        if side not in reliefs:  # no copper on that side: a flat mask
            materials.set_relief_image(side, None, None)
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_cosmetic_layer") and obj.get("kls_cosmetic_layer") not in loaded:
            obj["kls_overlay_available"] = False
            set_visible(obj, False)
        elif obj.get("kls_overlay_walls") and obj.get("kls_overlay_walls") not in loaded:
            set_visible(obj, False)
    recolor()
    focus.refresh()  # new overlay materials fade in focus mode too
    cut.add_materials()  # and are cut open with the board
    _remove_unused(superseded)
    return len(loaded)
