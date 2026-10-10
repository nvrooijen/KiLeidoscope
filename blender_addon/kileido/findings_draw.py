"""The finding shown from the DRC column (findings.py), drawn in 3D.

Real objects in the board's "Findings" group, not viewport-only drawing, so they render
and show in a saved image. Only the shown finding is drawn; every object carries
`kls_finding` (its key) and is made again by `refresh` (a new findings frame, a snapshot,
the copper thickness toggle), so it always sits on the copper as drawn now.

- highlight, area: their items as KiCad's selection is highlighted (highlight.py kind
  "finding"), parts boxed, in the severity's colour.
- label: near-white text on a dark plate, the severity's icon at its left (a red circle
  "!", a yellow triangle "!", a blue circle "i"), facing the view. A Track To constraint
  turns every label to one empty, "KLS finding eye", which follows the first 3D view's
  eye (a timer) and the scene camera while rendering (render_pre), so one mechanism
  serves viewport and render. Where labels go, every tool's alike, is label_place's:
  beside the finding, above the surface the eye sees, a thin leader to what it names
  (laid out again as the eye moves); on a hole finding's cut face, flat on it.
- distance: a thin tube with a tick at each end and the bridge's measured value, red
  past its limit; arrow: a tube with a cone head.
- clearance, edge_gap: as distance, between the two closest points the bridge measured;
  an edge gap's outline end has a longer tick, along the edge.
- width: the segments as ribbons at their real width just out from their copper, the
  narrow ones in the "over" red, and a light dimension across the thinnest.

Every measured label reads its `what` over the value against its limit ("Clearance" over
"0.080 mm < 0.20 mm min"). Points are KiCad nm with a layer name: copper sits on its
outer surface, any other layer on its side's (F. top, B. bottom, Edge.Cuts top). While the
flex board is folded nothing is drawn here (the drawing fits the flat board); the column
says so.

The 3D views are framed on the shown finding from here too (`frame_bounds`): on its box,
from the side it is on, the eye kept out of part models.
"""

import math
from contextlib import contextmanager

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import components, focus, fold, highlight, hole_cut, label_place, materials, transform
from .objects import (VIEW_CLIP_START_M, camera_rays_only, hide, link_owned, outline_bounds, shown_views,
                      view3d_spaces)
from .placement import outward
from .state import board

TAG = "kls_finding"
EYE = "KLS finding eye"
LABEL_CHARS = 48  # a line, in 3D; the column shows the whole text
LABEL_LINE = 16  # a longer label without a number takes two lines
STRONG = 1.4  # `emphasis: strong` draws this much larger
TEXT_COLOR = (0.95, 0.95, 0.95)
PLATE_COLOR = (0.03, 0.03, 0.03)  # opaque: a see-through plate shows the render's grain behind it
PLATE_PAD = 0.25  # of the text size: the plate past the text on every side
PLATE_BEHIND = 0.04  # of the text size: the plate behind the text (the icon half as far)
ICON = 0.9  # of the text size: the severity icon's width, about a line's height
ICON_GAP = 0.3  # of the text size: icon to text
GLYPH = 0.7  # of the icon: its "!" or "i"
# severity: (shape, its colour, glyph, the glyph's colour); "unknown" marks a finding
# KiLeidoscope could not confirm (`confirmed`), in the column and on the board only.
ICONS = {"error": ("circle", (0.85, 0.08, 0.12), "!", (0.95, 0.95, 0.95)),
         "warning": ("triangle", (1.0, 0.78, 0.05), "!", (0.05, 0.05, 0.05)),
         "info": ("circle", (0.15, 0.55, 0.95), "i", (0.95, 0.95, 0.95)),
         "unknown": ("circle", (0.55, 0.55, 0.55), "?", (0.98, 0.98, 0.98)),
         "pass": ("circle", (0.1, 0.65, 0.25), "✓", (0.98, 0.98, 0.98))}  # a label's measurement within its limit
CHECK = ((-0.45, 0.0), (-0.12, -0.35), (0.5, 0.4))  # the tick of "✓", in an icon's radius
MARK_ORDER = ("error", "warning", "unknown", "info")  # the worst first: a shared marker shows the first
TUBE_MIN_M = 5e-6
TUBE_SHARE = 1 / 10  # of a label's size: a tube's radius
LEADER = 0.5  # of a tube's radius: a leader's
TICK = 6  # a distance's end ticks, in tube radii either side
EDGE_TICK = 2.5  # of TICK: an edge gap's tick on the outline, along the edge
AREA_LIFT_M = 20e-6  # a width's ribbons out from their copper
FOLLOW_S = 0.1
VIEW_NEAR_M = 2e-3  # a label pulled towards a 3D view's eye stays this far from it (its clip is 1 mm)
_UP = Matrix.Rotation(-math.pi / 2, 4, "X")  # the eye's z along the view's up: the labels' up (Track To)
_state = {"folded": False, "views": {}, "follow": None, "eye": None,  # views: area pointer -> last view matrix
          "labels": [], "layout": None,  # labels facing the eye: laid out again as it moves
          "reach": None,  # (x0, y0, x1, y1) the labels' plates cover, for framing
          "parts": None}  # part_boxes() of the drawing as last made (the parts move only with a redraw)


# --- What is shown --------------------------------------------------------------------------

def shown_finding():
    """The finding `board.findings_shown` names, or None (gone from the newest list)."""
    source, key = board.findings_shown
    if not key:
        return None
    found = ((board.findings or {}).get("sources") or {}).get(source) or {}
    return next((finding for finding in found.get("findings", ()) if finding.get("key") == key), None)


def item_ids(finding, tools=None):
    """Item ids of a finding's drawn draws (only `tools`' when given), in order."""
    ids = {}
    for draw in finding.get("draws", ()):
        if draw.get("ok") and (tools is None or draw.get("tool") in tools):
            ids.update(dict.fromkeys(draw.get("ids", ())))
    return list(ids)


def highlighted(finding):
    """Item ids a finding's highlight shows: its highlight and area draws' items."""
    return set(item_ids(finding, ("highlight", "area")))


def confirmed(finding):
    """False when KiLeidoscope could not confirm a finding: its items gone from the board,
    a target or a draw not found, or a problem whose every measurement meets its limit."""
    if finding.get("faded") or refuted(finding):
        return False
    return all(draw.get("ok", True) and all(target.get("found", True) for target in draw.get("targets") or ())
               for draw in finding.get("draws") or ())


def refuted(finding):
    """A problem (error or warning) whose every measured limit is met: the bridge's
    `passes`. An info finding may well report a value within its limit."""
    return bool(finding.get("passes")) and finding.get("severity") in ("error", "warning")


def passed(draw):
    """A measured draw that meets its limit."""
    return draw.get("ok", True) and draw.get("limit_nm") is not None and not draw.get("over")


def _judged(draw, finding):
    """A measured draw's colour: "over" past its limit, "pass" within it, else the severity's."""
    return "over" if draw.get("over") else "pass" if passed(draw) else finding.get("severity")


def _icon(draw):
    """A measured draw's label icon: "pass" within its limit, else the finding's."""
    return "pass" if passed(draw) else None


def mark(finding):
    """The icon a finding shows (ICONS key): "unknown" unconfirmed, else its severity."""
    if not confirmed(finding):
        return "unknown"
    return finding.get("severity") if finding.get("severity") in ICONS else "warning"


def icon_pixels(kind, px):
    """RGBA rows (bottom first) of an ICONS icon `px` wide, drawn 4 x 4 times finer and
    averaged: the column's rows and the board's markers."""
    shape, color, glyph_mark, ink = ICONS.get(kind, ICONS["warning"])
    fine = 4
    u = (np.arange(px * fine) + 0.5) / fine * (32 / px)  # laid out on a 32 unit square, y up
    x, y = np.meshgrid(u, u)
    dx = np.abs(x - 16)
    if shape == "triangle":
        body = (y >= 2.5) & (dx <= 15 * (30 - y) / 27.5)
        bar, dot = (11, 22), 6.5  # a triangle's room is low
    else:
        body = np.hypot(dx, y - 16) <= 14
        bar, dot = ((13, 25), 8.5) if glyph_mark == "!" else ((6, 19), 23.5) if glyph_mark == "i" else ((11.5, 17), 7.5)
    glyph = ((dx <= 2) & (y >= bar[0]) & (y <= bar[1])) | (np.hypot(dx, y - dot) <= 2.3)
    if glyph_mark == "✓":  # two strokes: CHECK on the 32 unit square
        ticks = [(16 + cx * 14, 16 + cy * 14) for cx, cy in CHECK]
        glyph = np.zeros_like(body)
        for (x0, y0), (x1, y1) in zip(ticks, ticks[1:]):
            t = np.clip(((x - x0) * (x1 - x0) + (y - y0) * (y1 - y0)) / ((x1 - x0) ** 2 + (y1 - y0) ** 2), 0, 1)
            glyph |= np.hypot(x - x0 - t * (x1 - x0), y - y0 - t * (y1 - y0)) <= 2.2
    if glyph_mark == "?":  # a hook over the stem: a ring open at its lower left
        angle = np.degrees(np.arctan2(y - 20.5, x - 16))
        glyph |= (np.abs(np.hypot(x - 16, y - 20.5) - 4.7) <= 2) & (angle >= -90) & (angle <= 170)
    blocks = (px, fine, px, fine)
    cover = body.reshape(blocks).mean(axis=(1, 3))
    inked = (glyph & body).reshape(blocks).mean(axis=(1, 3)) / np.maximum(cover, 1e-6)
    pixels = np.empty((px, px, 4), np.float32)
    pixels[..., :3] = np.array(color) * (1 - inked[..., None]) + np.array(ink) * inked[..., None]
    pixels[..., 3] = cover
    return pixels


def _outer_side(layer):
    """+1 for a top layer (F.), -1 for a bottom one (B.), None for the rest."""
    layer = str(layer or "")
    return 1.0 if layer.startswith("F.") else -1.0 if layer.startswith("B.") else None


def _draw_sides(draw):
    """The outer sides a draw's own layers name (None for the rest)."""
    layers = [draw.get("layer"), *(point[2] for point in draw.get("points") or () if len(point) > 2),
              *(segment[4] for segment in _segments(draw) if len(segment) > 4)]
    return {_outer_side(layer) for layer in layers}


def _segments(draw):
    """A draw's [x0, y0, x1, y1, layer, width_nm] stretches: a width's segments."""
    yield from draw.get("segments_nm") or ()


def item_sides():
    """{item id: {+1, -1}}: the outer copper each track, pad and zone lies on, and the side
    each part faces (a bottom part: scale (1, -1, -1)). Built once for many findings."""
    sides = {}
    if board.collection is None:
        return sides
    for obj in board.collection.all_objects:
        copper = obj.get("kls_copper")
        if copper and copper[1] in ("tracks", "pads", "zones") and _outer_side(copper[0]) is not None:
            for item_id in obj.get("kls_ids", ()):
                sides.setdefault(item_id, set()).add(_outer_side(copper[0]))
        key = components.key_of(obj)
        if key is not None and key[0] == components.FRAME:
            sides.setdefault(key[1], set()).add(1.0 if obj.matrix_world.col[2].z >= 0 else -1.0)
    return sides


def finding_side(finding, sides=None):
    """The side a finding is on, to look at it from: +1 top, -1 bottom, None when it is on
    both or neither (inner layers, Edge.Cuts, through-holes). From its draws' layers, the
    outer copper its items lie on and the side its parts face (`sides`: item_sides(),
    passed in when many findings are placed at once)."""
    sides = item_sides() if sides is None else sides
    found = set()
    for draw in finding.get("draws", ()):
        if draw.get("ok", True):
            found.update(_draw_sides(draw))
            for item_id in draw.get("ids", ()):
                found |= sides.get(item_id, set())
    found.discard(None)
    return found.pop() if len(found) == 1 else None


def refresh():
    """Draw the shown finding again from scratch, or nothing; its highlight follows."""
    if board.collection is None or board.in_snapshot:
        return
    from . import findings_markers  # it imports this module
    findings_markers.invalidate()  # the board's markers placed again on the board as drawn now
    _clear_objects()
    finding = shown_finding()
    if finding is None:
        board.findings_shown = ("", "")
    draws = [draw for draw in (finding or {}).get("draws", ()) if draw.get("ok")]
    chosen = highlighted(finding) if finding else set()
    if finding is not None and board.finding_severity != (finding.get("severity") or "warning"):
        board.finding_severity = finding.get("severity") or "warning"
        if board.materials:
            materials.paint_finding()
    if chosen != board.highlight.get("finding"):  # else it follows edits already
        board.highlight["finding"] = chosen
        highlight.refresh()
        focus.refresh()
    _state["folded"] = fold.folded()
    _state["labels"], _state["layout"], _state["reach"], _state["parts"] = [], None, None, None
    if finding is not None and not bpy.app.timers.is_registered(_follow):
        bpy.app.timers.register(_follow, first_interval=FOLLOW_S)  # on a folded board too: it draws on unfolding
    if finding is None or _state["folded"]:
        return
    box = _finding_box(finding)
    unit = label_place.size(box or (0.0, 0.0, 0.0, 0.0))
    for number, draw in enumerate(draws, start=1):
        builder = _BUILDERS.get(draw["tool"])
        if builder is not None:
            builder(draw, unit, f"KLS finding {number} {draw['tool']}", finding)
    _place_labels(finding, box, unit)
    _place_eye(force=True)


def clear():
    """Show no finding."""
    board.findings_shown = ("", "")
    refresh()


@contextmanager
def left_out():
    """No finding drawn while inside (an exported board package is the board, not this session's check)."""
    shown = board.findings_shown
    clear()
    try:
        yield
    finally:
        board.findings_shown = shown
        refresh()


def _remove(obj):
    data = obj.data
    bpy.data.objects.remove(obj)
    if data is not None and data.users == 0:
        (bpy.data.meshes if isinstance(data, bpy.types.Mesh) else bpy.data.curves).remove(data)


def _clear_objects():
    for obj in tuple(board.collection.all_objects):
        if obj.get(TAG) is not None:
            _remove(obj)


def purge():
    """A file was loaded: its finding objects were drawn for another session's check (not
    kept, they may be stale)."""
    for obj in tuple(bpy.data.objects):
        if obj.get(TAG) is not None:
            _remove(obj)


# --- Following the view ---------------------------------------------------------------------

def _follow():
    """Timer while a finding is shown: the eye follows the view; folding hides the drawing,
    unfolding draws it (and cuts the board open for a hole finding shown while folded)."""
    if board.findings_shown == ("", "") or board.collection is None:
        return None
    try:
        if fold.folded() != _state["folded"]:
            if _state["folded"]:  # unfolded: the cut first, the labels are written on its face
                hole_cut.show(shown_finding())
            refresh()
        _place_eye()
    except ReferenceError:  # the board freed under us (undo, file load)
        return None
    return FOLLOW_S


def _followed_view():
    """The view the labels face: the one last turned or moved, so with a split screen they
    follow whichever view the user works in; at first, the first one shown."""
    views = dict(shown_views())
    matrices = {key: tuple(tuple(row) for row in _view_eye(view)) for key, view in views.items()}
    moved = [key for key, matrix in matrices.items()
             if key in _state["views"] and _state["views"][key] != matrix]
    _state["views"] = matrices
    if moved:
        _state["follow"] = moved[0]
    if _state["follow"] not in views:
        _state["follow"] = next(iter(views), None)
    return views.get(_state["follow"])


def _view_eye(view):
    """Where a 3D view looks from (its view matrix inverted), from what sets it: the view
    matrix itself is only brought up to date by a redraw."""
    return Matrix.Translation(view.view_location) @ view.view_rotation.to_matrix().to_4x4() @         Matrix.Translation((0.0, 0.0, view.view_distance))


def _camera_eye(camera):
    """(matrix, orthographic, near clip) of a camera as the labels' eye."""
    return camera.matrix_world @ _UP, camera.data.type == "ORTHO", camera.data.clip_start


def _eye_matrix():
    """(matrix, orthographic, near clip) of where the labels look from: the followed view's
    eye (its camera when it looks through one), else the scene camera, else straight above
    the board."""
    scene = bpy.context.scene
    view = _followed_view()
    if view is not None:
        if view.view_perspective == "CAMERA" and scene.camera is not None:
            return _camera_eye(scene.camera)
        return _view_eye(view) @ _UP, view.view_perspective == "ORTHO", VIEW_NEAR_M
    if scene.camera is not None:
        return _camera_eye(scene.camera)
    return Matrix.Translation((0.0, 0.0, 1.0)) @ _UP, False, 0.0


def _place_eye(eye_view=None, force=False):
    """The eye where the view looks from (`eye_view`: matrix, orthographic, near clip); the
    labels facing it laid out for it when it moved. Compared with the view last placed, not
    the eye's matrix_world: Blender reads that back a few um off, and every needless move
    restarts a rendered view."""
    eye = board.collection.all_objects.get(EYE) if board.collection is not None else None
    if eye is None:
        return
    matrix, ortho, near = eye_view if eye_view is not None else _eye_matrix()
    last = _state["eye"]
    if force or last is None or last[1:] != (ortho, near) or             any(abs(a - b) > 1e-7 for row, other in zip(last[0], matrix) for a, b in zip(row, other)):
        eye.matrix_world = matrix
        _state["eye"] = (matrix.copy(), ortho, near)
        _lay_out(matrix, ortho, near)


@bpy.app.handlers.persistent
def _render_pre(scene, *_):
    """A render sees the labels facing its camera."""
    try:
        if scene.camera is not None and board.collection is not None:
            _place_eye(_camera_eye(scene.camera))
    except ReferenceError:
        pass


def install():
    if _render_pre not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(_render_pre)


def uninstall():
    if _render_pre in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(_render_pre)
    if bpy.app.timers.is_registered(_follow):
        bpy.app.timers.unregister(_follow)
    if board.collection is not None:
        try:
            _clear_objects()
        except ReferenceError:
            pass


# --- Objects --------------------------------------------------------------------------------

def _new(name, data, finding):
    """A drawing object, seen by camera rays only: an overlay casts no shadow and lights
    nothing, so it adds no grain to the board in a rendered view."""
    obj = bpy.data.objects.new(name, data)
    camera_rays_only(obj)
    obj["kileido_owned"] = 1
    obj[TAG] = finding.get("key", "")
    link_owned(obj, "findings")
    return obj


def _material(kind):
    """Flat glowing colour of a severity (or "over": a distance past its limit; "pass": within it)."""
    color = materials.FINDING_OVER_COLOR if kind == "over" else materials.FINDING_PASS_COLOR if kind == "pass" else \
        materials.FINDING_COLORS.get(kind, materials.FINDING_COLORS["warning"])
    material = materials.make(f"KLS finding {kind}", color)
    materials.paint(material, color)
    return material


def _draw_points(draw):
    """Every [x_nm, y_nm] a draw names: its points, segment ends."""
    yield from (point[:2] for point in draw.get("points") or ())
    for segment in _segments(draw):
        if len(segment) >= 4:
            yield from (segment[0:2], segment[2:4])


def _finding_box(finding):
    """The finding's box in Blender (x0, y0, x1, y1): its bbox_nm, else its draws' points; or None."""
    box = finding.get("bbox_nm")
    points = [box[:2], box[2:]] if box else \
        [point for draw in finding.get("draws", ()) if draw.get("ok") for point in _draw_points(draw)]
    if not points:
        return None
    xy = transform.xy_m(points, board.origin_nm)
    return float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max())


def _surface(layer, lift=0.0):
    """(z, outward) of a layer: copper at its outer surface, any other layer on its side's
    outer copper surface (F. top, B. bottom, Edge.Cuts and the rest top); `lift` outward."""
    layer = str(layer or "")
    if layer in board.heights:
        up = outward(layer)
        return transform.copper_z(layer, "drills", board.heights) + up * lift, up
    if layer.startswith("B."):
        return board.heights.get("B.Cu", 0.0) - lift, -1.0
    return board.heights.get("F.Cu", board.thickness_m) + lift, 1.0


def _point(point, lift=0.0):
    """A payload point [x_nm, y_nm, layer] in Blender, and which way is out from its side."""
    x, y = (float(value) for value in transform.xy_m([point[:2]], board.origin_nm)[0])
    z, up = _surface(point[2] if len(point) > 2 else "", lift)
    return Vector((x, y, z)), up


def _cut(text):
    return text if len(text) <= LABEL_CHARS else text[:LABEL_CHARS - 1] + "…"


def _short(text):
    """A label's text on at most two lines, each cut to LABEL_CHARS. Every label reads the
    same: what is wrong, then the value from its first number on ("Hole clearance error" over "0.23 mm
    < 0.25 mm min"); a text with its own line break keeps it; one without a number goes on
    two lines past LABEL_LINE, after a "name:" lead-in, else at the space nearest the middle."""
    lines = [line for line in (" ".join(line.split()) for line in str(text).split("\n")) if line]
    if len(lines) > 1:
        return _cut(lines[0]) + "\n" + _cut(" ".join(lines[1:]))
    text = _cut(lines[0] if lines else "")
    if " " not in text:
        return text
    words = text.split(" ")
    first_number = next((i for i, word in enumerate(words) if i and word.lstrip("+-.")[:1].isdigit()), 0)
    if not first_number and len(text) <= LABEL_LINE:
        return text
    lead_in = next((i + 1 for i, word in enumerate(words[:-1]) if word.endswith(":")), 0)
    cut = first_number or lead_in or \
        min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(text) / 2))
    return " ".join(words[:cut]) + "\n" + " ".join(words[cut:])


def _eye():
    eye = board.collection.all_objects.get(EYE)
    if eye is None:
        eye = bpy.data.objects.new(EYE, None)
        eye["kileido_owned"] = 1
        eye[TAG] = "eye"
        eye.empty_display_size = 1e-4
        link_owned(eye, "findings")
        hide(eye, True)  # hidden, still evaluated: the labels' constraints read it
        _place_eye()
    return eye


def _label(text, anchor, unit, name, finding, material, strong=False, icon=None):
    """A label naming `anchor` (a Blender point); _place_labels decides where it goes.
    `material` is the severity's: its leader's. `icon`: an ICONS key in place of the
    severity's ("pass" for a measurement within its limit)."""
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = _short(text)
    curve.size = unit * (STRONG if strong else 1.0)
    curve.align_x, curve.align_y = "LEFT", "CENTER"
    curve.materials.append(_plain("text"))
    obj = _new(name, curve, finding)
    obj.show_in_front = True  # in the viewport, never behind the copper it names
    _state["labels"].append({"name": name, "anchor": anchor.copy(), "material": material, "icon": icon})
    return obj


def _plain(name, color=TEXT_COLOR, alpha=1.0):
    """A label's own flat colour, shared ("text", "plate", an icon's): never the cut's to clip."""
    material = materials.make(f"KLS finding {name}", color, alpha)
    materials.paint(material, color)
    for node in material.node_tree.nodes:  # one from a file saved with a see-through plate
        if node.type == "MIX_SHADER":
            node.inputs[0].default_value = alpha
    return material


def _bounds(obj):
    """(x0, y0, x1, y1) of a label's text in its own axes, at its present size."""
    corners = [tuple(corner) for corner in obj.bound_box]
    return (min(c[0] for c in corners), min(c[1] for c in corners),
            max(c[0] for c in corners), max(c[1] for c in corners))


def _plate_rect(obj):
    """(plate, icon centre) in a label's own axes, at its present size: the plate around
    the icon at the left and the text, the icon centred on the text's lines."""
    size = obj.data.size
    x0, y0, x1, y1 = _bounds(obj)
    icon, pad = ICON * size, PLATE_PAD * size
    centre = (x0 - ICON_GAP * size - icon / 2, (y0 + y1) / 2)
    y0, y1 = min(y0, centre[1] - icon / 2), max(y1, centre[1] + icon / 2)
    return (centre[0] - icon / 2 - pad, y0 - pad, x1 + pad, y1 + pad), centre


def _block(obj, anchor):
    """The label as label_place sees it: its plate's size and centre."""
    (x0, y0, x1, y1), _ = _plate_rect(obj)
    return label_place.Label(anchor, x1 - x0, y1 - y0, ((x0 + x1) / 2, (y0 + y1) / 2))


def _plate(obj, finding, icon=None):
    """The dark plate behind a label's text and its severity icon (or `icon`) at the left,
    children of the text so they turn with it: the plate a hair behind, the icon half as far."""
    size = obj.data.size
    (x0, y0, x1, y1), (cx, cy) = _plate_rect(obj)
    behind = PLATE_BEHIND * size

    def build(bm):
        bm.faces.new([bm.verts.new((x, y, 0.0)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))])
    plate = _mesh(obj.name + " plate", build, Matrix.Translation((0.0, 0.0, -behind)), finding,
                  _plain("plate", PLATE_COLOR))
    severity = icon if icon in ICONS else finding.get("severity") if finding.get("severity") in ICONS else "warning"
    shape, color, mark, ink = ICONS[severity]
    radius = ICON * size / 2

    def disc(bm):
        bmesh.ops.create_circle(bm, cap_ends=True, segments=24, radius=radius)

    def triangle(bm):
        bm.faces.new([bm.verts.new((x, y, 0.0)) for x, y in ((-radius, -radius), (radius, -radius), (0.0, radius))])
    icon = _mesh(obj.name + " icon", disc if shape == "circle" else triangle,
                 Matrix.Translation((cx, cy, -behind / 2)), finding, _plain(f"icon {severity}", color))
    ink_material = _plain("glyph dark" if max(ink) < 0.5 else "glyph light", ink)
    if mark == "✓":  # drawn, not typed: the label font may lack it
        glyph = _mesh(obj.name + " icon glyph", _tick(radius), Matrix.Translation((cx, cy, -behind / 4)), finding,
                      ink_material)
    else:
        curve = bpy.data.curves.new(obj.name + " icon glyph", "FONT")
        curve.body = mark
        curve.size = GLYPH * ICON * size
        curve.align_x, curve.align_y = "CENTER", "CENTER"
        curve.materials.append(ink_material)
        glyph = _new(curve.name, curve, finding)
        lower = radius / 4 if shape == "triangle" else 0.0  # a triangle's room is low
        glyph.location = (cx, cy - lower, -behind / 4)
    for part in (plate, icon, glyph):
        part.parent = obj
        part.show_in_front = True
    return plate


def _tick(radius):
    """A builder of the "✓" glyph: CHECK's two strokes as quads, in an icon of `radius`."""
    stroke = radius * 0.13

    def build(bm):
        for (x0, y0), (x1, y1) in zip(CHECK, CHECK[1:]):
            dx, dy = (x1 - x0) * radius, (y1 - y0) * radius
            length = math.hypot(dx, dy)
            nx, ny = -dy / length * stroke, dx / length * stroke
            ends = ((x0 * radius, y0 * radius), (x1 * radius, y1 * radius))
            bm.faces.new([bm.verts.new((x + side * nx, y + side * ny, 0.0))
                          for (x, y), side in ((ends[0], 1), (ends[0], -1), (ends[1], -1), (ends[1], 1))])
    return build


def _place_labels(finding, box, unit):
    """Every label of the shown finding where label_place puts it, with its plate: on a
    hole finding's open cut face, flat there; else facing the eye (laid out by
    `_lay_out`, again whenever the eye moves), each with a thin leader to its anchor."""
    labels = _state["labels"]
    objects = [board.collection.all_objects[entry["name"]] for entry in labels]
    section = hole_cut.section(finding)
    if section is not None:
        bounds = outline_bounds()
        a, b, normal = section
        centre = Vector(((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)) if bounds else (a + b) / 2
        low, high = board.heights.get("B.Cu", 0.0), board.heights.get("F.Cu", board.thickness_m)
        spots = label_place.on_cut([_block(obj, entry["anchor"]) for obj, entry in zip(objects, labels)],
                                   (a, b), normal, box or (a.x, a.y, b.x, b.y), centre, low, high, unit)
        corners = []
        for obj, entry, (location, turn, scale) in zip(objects, labels, spots):
            obj.data.size *= scale
            obj.matrix_world = Matrix.Translation(location) @ turn.to_4x4()
            _plate(obj, finding, entry.get("icon"))
            (x0, y0, x1, y1), _ = _plate_rect(obj)
            corners += [obj.matrix_world @ Vector((x, y, 0.0)) for x in (x0, x1) for y in (y0, y1)]
        _reach(corners)
        _state["labels"] = []  # fixed on the face: nothing follows the eye
        return
    if box is None and labels:
        xs, ys = [entry["anchor"].x for entry in labels], [entry["anchor"].y for entry in labels]
        box = (min(xs), min(ys), max(xs), max(ys))
    radius = max(TUBE_MIN_M, unit * TUBE_SHARE * LEADER)
    for obj, entry in zip(objects, labels):
        track = obj.constraints.new("TRACK_TO")
        track.target = _eye()
        track.track_axis, track.up_axis, track.use_target_z = "TRACK_Z", "UP_Y", True
        _plate(obj, finding, entry.get("icon"))
        anchor = entry["anchor"]
        entry["leader"] = _tube(obj.name + " leader", [[anchor + Vector((0.0, 0.0, radius)), anchor]], radius,
                                finding, entry["material"]).name
    _state["layout"] = (box, _surface("F.Cu")[0], _surface("B.Cu")[0], unit)


def _lay_out(matrix, ortho=False, near=0.0):
    """The labels facing the eye (`matrix`) put where label_place says for it, leaders too;
    one a part would hide brought towards the eye, in front of it (label_place.pull)."""
    if _state["layout"] is None or not _state["labels"]:
        return
    box, top, bottom, unit = _state["layout"]
    found = board.collection.all_objects
    entries = [entry for entry in _state["labels"] if entry["name"] in found]
    spots = label_place.beside([_block(found[entry["name"]], entry["anchor"]) for entry in entries],
                               box, matrix, top, bottom, unit)
    right, up = matrix.col[0].xyz.normalized(), matrix.col[2].xyz.normalized()
    if _state["parts"] is None:  # once per drawing, not per eye move: every object's box is read
        _state["parts"] = part_boxes()
    parts = _state["parts"]
    corners = []
    for entry, (location, foot) in zip(entries, spots):
        obj = found[entry["name"]]
        (x0, y0, x1, y1), _ = _plate_rect(obj)
        corners += [location + right * x + up * y for x in (x0, x1) for y in (y0, y1)]  # framed where it was
        x, y = label_place.facing(location, matrix)
        plate = [location + x * u + y * v for u in (x0, (x0 + x1) / 2, x1) for v in (y0, (y0 + y1) / 2, y1)]
        share = label_place.pull(plate, parts, matrix, ortho, near)
        (location, foot), scale = label_place.toward_eye([location, foot], share, matrix, ortho)
        if abs(obj.scale.x - scale) > 1e-9:
            obj.scale = (scale, scale, scale)
        if (obj.location - location).length > 1e-9:
            obj.location = location
        leader = found.get(entry.get("leader") or "")
        if leader is not None:
            start, end = leader.data.splines[0].points
            start.co = (*foot, 1.0)
            end.co = (*entry["anchor"], 1.0)
    _reach(corners)


def _reach(corners):
    _state["reach"] = (min(c.x for c in corners), min(c.y for c in corners),
                       max(c.x for c in corners), max(c.y for c in corners)) if corners else None


def label_reach():
    """(x0, y0, x1, y1) in Blender the shown finding's labels cover as last laid out, or None."""
    return _state["reach"]


def _tube(name, lines, radius, finding, material):
    """Polylines (Blender points) as one round tube curve."""
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = radius
    curve.bevel_resolution = 2
    curve.use_fill_caps = True
    for line in lines:
        spline = curve.splines.new("POLY")
        spline.points.add(len(line) - 1)
        for point, position in zip(spline.points, line):
            point.co = (*position, 1.0)
    curve.materials.append(material)
    obj = _new(name, curve, finding)
    obj.show_in_front = True
    return obj


def _mesh(name, build, matrix, finding, material):
    """A mesh object from `build(bm)`, placed at `matrix`."""
    bm = bmesh.new()
    try:
        build(bm)
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.materials.append(material)
    obj = _new(name, mesh, finding)
    obj.matrix_world = matrix
    return obj


def _cylinder(radius, depth, tip=None):
    """Along local z, centred; `tip` a cone's top radius."""
    def build(bm):
        bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=radius,
                              radius2=radius if tip is None else tip, depth=depth)
    return build


def _tube_radius(unit, length, draw):
    radius = unit * TUBE_SHARE * (STRONG if draw.get("emphasis") == "strong" else 1.0)
    return max(TUBE_MIN_M, min(radius, length / 6) if length > 0 else radius)


def _mm(nm):
    """A length as the bridge writes its own labels (drc_findings._number): "0.23", "0.080"."""
    mm = nm / 1e6
    return f"{mm:.2f}" if abs(mm) >= 0.1 else f"{mm:.3f}"


WHAT = {"distance": "Distance", "clearance": "Clearance", "edge_gap": "Edge gap",
        "width": "Width"}  # a label's first line, by default


def measured_text(draw):
    """"0.080 mm < 0.20 mm min": the bridge's measurement (a width's narrowest) against
    the draw's limit, as the bridge's own labels read ("Hole clearance error 0.23 mm < 0.25 mm min")."""
    measured = draw.get("measured_nm") if draw.get("measured_nm") is not None else draw.get("min_nm")
    if measured is None:
        return ""
    text = f"{_mm(measured)} mm"
    if draw.get("limit_nm") is not None:
        below = measured < draw["limit_nm"]
        sign = "<" if below else (">" if measured > draw["limit_nm"] else "=")
        text += f" {sign} {_mm(draw['limit_nm'])} mm {draw.get('limit_is') or ('min' if below else 'max')}"
    return text


def what_text(draw):
    """What a measured draw shows: its `what` ("Clearance"), else its `label`, else the tool's."""
    return draw.get("what") or draw.get("label") or WHAT.get(draw.get("tool"), "Distance")


def label_text(draw):
    """A measured draw's label: what it shows, then the measured value against its limit."""
    measured = measured_text(draw)
    what = what_text(draw)
    return f"{what}\n{measured}" if measured else what


# --- The tools ------------------------------------------------------------------------------

def _draw_label(draw, unit, name, finding):
    """Its leader ends on the point, or on top of the part it names."""
    at, up = _point(draw["points"][0])
    top = _part_top(draw, up) if any(target.get("kind") == "part" for target in draw.get("targets", ())) else None
    if top is not None:
        at.z = top
    _label(draw.get("label") or draw.get("text") or "", at, unit, name, finding,
           _material(finding.get("severity")), draw.get("emphasis") == "strong")


def _part_top(draw, up):
    """The outer face of the first part among the draw's items (its model, else its box)."""
    for item_id in draw.get("ids", ()):
        frame = components.find(components.FRAME, item_id)
        extent = highlight._component_extent(item_id, frame) if frame is not None else None
        if extent is None:
            continue
        heights = [(frame.matrix_world @ Vector(tuple(float(v) for v in corner))).z for corner in extent]
        return max(heights) if up > 0 else min(heights)
    return None


def _ends(draw, unit, points=None):
    """The two points of an arrow or a distance, lifted onto their surfaces by the tube's radius."""
    points = points or draw["points"]
    (a, up), (b, _) = _point(points[0]), _point(points[1])
    radius = _tube_radius(unit, (b - a).length, draw)
    (a, up), (b, _) = _point(points[0], radius), _point(points[1], radius)
    return a, b, up, radius


def _draw_distance(draw, unit, name, finding):
    """A distance, clearance or edge gap: a tube between its two points, a tick across each
    end, red past its limit. An edge gap's outline end (its Edge.Cuts point, else the
    second) sits on the copper end's layer, its tick longer: along the edge, as the
    closest gap meets the outline square."""
    points, edge = list(draw["points"][:2]), None
    if draw.get("tool") == "edge_gap":
        edge = next((i for i, point in enumerate(points) if len(point) > 2 and point[2] == "Edge.Cuts"), 1)
        copper = points[1 - edge]
        points[edge] = [*points[edge][:2], copper[2] if len(copper) > 2 else ""]
    a, b, up, radius = _ends(draw, unit, points)
    material = _material(_judged(draw, finding))
    along = b - a
    side = along.cross(Vector((0.0, 0.0, 1.0)))
    if side.length < 1e-12:  # straight up through the board, or no gap at all
        side = Vector((1.0, 0.0, 0.0))
    ticks = [side.normalized() * radius * TICK * (EDGE_TICK if end == edge else 1.0) for end in (0, 1)]
    lines = [[a - ticks[0], a + ticks[0]], [b - ticks[1], b + ticks[1]]]
    if along.length > 1e-9:
        lines.insert(0, [a, b])
    _tube(name, lines, radius, finding, material)
    _label(label_text(draw), (a + b) / 2, unit, name + " label", finding, material, draw.get("emphasis") == "strong",
           icon=_icon(draw))


def _draw_arrow(draw, unit, name, finding):
    a, b, up, radius = _ends(draw, unit)
    material = _material(finding.get("severity"))
    along = b - a
    length = along.length
    if length > 1e-9:
        direction = along / length
        head = min(unit * 0.8, length * 0.4)
        base = b - direction * head
        _tube(name, [[a, base]], radius, finding, material)
        matrix = Matrix.Translation(base + direction * head / 2) @ direction.to_track_quat("Z", "Y").to_matrix().to_4x4()
        _mesh(name + " head", _cylinder(max(radius * 3, head * 0.35), head, tip=0.0), matrix, finding, material)
    if draw.get("label"):
        _label(draw["label"], a, unit, name + " label", finding, material, draw.get("emphasis") == "strong")


def _ribbons(segments, lift):
    """Builds flat ribbons at their real width over segments [x1, y1, x2, y2, layer,
    width_nm], `lift` out from their copper, round at the ends as KiCad's tracks are."""
    def build(bm):
        for x1, y1, x2, y2, layer, width in (segment[:6] for segment in segments):
            (ax, ay), (bx, by) = transform.xy_m([(x1, y1), (x2, y2)], board.origin_nm)
            z, _ = _surface(layer, lift)
            a, b, half = Vector((float(ax), float(ay), z)), Vector((float(bx), float(by), z)), width * 1e-9 / 2
            if half <= 0:
                continue
            along = b - a
            if along.length > 1e-12:
                side = Vector((-along.y, along.x, 0.0)).normalized() * half
                bm.faces.new([bm.verts.new(corner) for corner in (a - side, b - side, b + side, a + side)])
            for end in (a, b):
                bmesh.ops.create_circle(bm, cap_ends=True, segments=16, radius=half, matrix=Matrix.Translation(end))
    return build


def _thinnest(segments, at):
    """The narrowest segment (the nearest `at` [x_nm, y_nm] on a tie)."""
    def distance(segment):
        (x1, y1, x2, y2), (x, y) = segment[:4], at[:2]
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy))) if dx or dy else 0.0
        return math.hypot(x1 + t * dx - x, y1 + t * dy - y)
    return min(segments, key=lambda segment: (segment[5], distance(segment)))


def _draw_width(draw, unit, name, finding):
    """The segments as ribbons at their width (the narrow ones red, a hair further out),
    a light dimension across the thinnest at the bridge's point: a line from edge to edge
    poking out past both, a tick along the track at each edge."""
    segments = [segment for segment in draw.get("segments_nm") or () if len(segment) >= 6]
    narrow = set(draw.get("narrow") or ())
    for over, lift in ((False, AREA_LIFT_M), (True, AREA_LIFT_M * 1.5)):
        members = [segment for index, segment in enumerate(segments) if (index in narrow) == over]
        if members:
            ribbon = _mesh(name + (" narrow" if over else ""), _ribbons(members, lift), Matrix.Identity(4), finding,
                           _material("over" if over else finding.get("severity")))
            ribbon.show_in_front = True  # an inner layer's too, as the tubes
    if not draw.get("points"):
        return
    at = draw["points"][0]
    material = _material(_judged(draw, finding))
    if segments:
        x1, y1, x2, y2, layer, width = _thinnest(segments, at)[:6]
        (ax, ay), (bx, by) = transform.xy_m([(x1, y1), (x2, y2)], board.origin_nm)
        along = Vector((float(bx - ax), float(by - ay), 0.0))
        along = along.normalized() if along.length > 1e-12 else Vector((1.0, 0.0, 0.0))
        half = width * 1e-9 / 2
        radius = _tube_radius(unit, half * 2, draw)
        centre, _ = _point([*at[:2], layer], AREA_LIFT_M * 2 + radius)
        side = Vector((-along.y, along.x, 0.0))
        tick, poke = along * radius * TICK, side * radius * TICK
        a, b = centre - side * half, centre + side * half
        _tube(name + " callout", [[a - poke, b + poke], [a - tick, a + tick], [b - tick, b + tick]], radius, finding,
              _plain("text"))
    else:
        centre, _ = _point(at)
    _label(label_text(draw), centre, unit, name + " label", finding, material, draw.get("emphasis") == "strong",
           icon=_icon(draw))


# highlight and area draws are the finding's highlight (`highlighted`): nothing of their own
_BUILDERS = {"label": _draw_label, "distance": _draw_distance, "arrow": _draw_arrow, "clearance": _draw_distance,
             "edge_gap": _draw_distance, "width": _draw_width}


# --- Framing the views ----------------------------------------------------------------------

FRAME_MARGIN = 2.5  # of the framed extent: the view's distance, room around the finding
FRAME_SIDE_ON = 1e-3  # a view this close to edge-on sees both sides: left as it is
FRAME_PART_CLEARANCE_M = 1e-3  # how far outside a part's box the framed eye stays


def frame_bounds(xmin, ymin, xmax, ymax, side=None):
    """Centre every 3D view on a box (Blender metres) and fit it with room around it,
    keeping each view's direction: a finding shown from the DRC column. With `side`
    (+1 top, -1 bottom), a view looking from the other side is mirrored through the board."""
    extent = label_place.extent((xmin, ymin, xmax, ymax))  # a hole gap is a fraction of a millimetre: at least FRAME_MIN_M
    boxes = part_boxes()
    for space in view3d_spaces():
        view = space.region_3d
        if side is not None:
            _look_from(view, side)
        view.view_location = ((xmin + xmax) / 2, (ymin + ymax) / 2, board.thickness_m / 2)
        view.view_distance = _outside(boxes, view.view_location, view.view_rotation @ Vector((0, 0, 1)),
                                      extent * FRAME_MARGIN)
        space.clip_start = VIEW_CLIP_START_M


def _look_from(view, side):
    """Turn a view that looks at the board from the other side than `side` to look from
    `side`: mirrored through the board, so it keeps its compass direction, tilt and right."""
    rotation = view.view_rotation.to_matrix()
    right, up, back = (rotation.col[i].copy() for i in range(3))
    if back.z * side >= -FRAME_SIDE_ON:
        return
    for axis in (right, up, back):
        axis.z = -axis.z
    turned = Matrix((right, -up, back)).transposed()  # columns; -up keeps it a rotation
    view.view_rotation = turned.to_quaternion()


def part_boxes():
    """World boxes (low, high) of the shown parts' models and placeholder boxes."""
    boxes = []
    for obj in board.collection.all_objects if board.collection is not None else ():
        key = components.key_of(obj)
        if key is None or key[0] == components.FRAME or not obj.visible_get():
            continue
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        boxes.append((Vector([min(c[i] for c in corners) for i in range(3)]),
                      Vector([max(c[i] for c in corners) for i in range(3)])))
    return boxes


def _outside(boxes, target, back, distance):
    """`distance`, lengthened until the eye at target + back * distance is outside every
    box: framing a finding beside a tall part must not put the eye inside its model."""
    for _ in range(len(boxes) + 1):
        moved = False
        for low, high in boxes:
            near, far = -math.inf, math.inf
            for axis in range(3):  # where the line from the target out to the eye is in this box
                if abs(back[axis]) < 1e-12:
                    if not low[axis] <= target[axis] <= high[axis]:
                        break
                    continue
                t0, t1 = sorted(((low[axis] - target[axis]) / back[axis], (high[axis] - target[axis]) / back[axis]))
                near, far = max(near, t0), min(far, t1)
            else:
                if near <= distance <= far:
                    distance, moved = far + FRAME_PART_CLEARANCE_M, True
        if not moved:
            break
    return distance
