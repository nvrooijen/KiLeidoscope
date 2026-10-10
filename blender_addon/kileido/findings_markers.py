"""Markers on the board for the DRC column's findings: one icon per finding, as its
row's (findings_draw.ICONS: a red circle "!", a yellow triangle "!", a blue circle "i", a
grey "?" for one KiLeidoscope could not confirm), just out from the side it is on.

They are drawn over the 3D view (a POST_PIXEL handler, as the column tabs are), not as
objects: the same size on screen at any zoom, over every part, never in a render, and
moving the view restarts nothing. Like pins on a map, markers that would overlap on screen
share one, showing the worst icon and a count; zooming in splits them. Dismissed findings
have none, the severity filter leaves out the milder ones, and while a finding is shown
the others fade, as do markers on the side the view does not see.

A click on a marker (KILEIDO_OT_pick asks `hit` first) opens the DRC column with its
group unfolded, and shows its finding as its title there does: drawn, framed, its items
selected in KiCad, its details under its row. A shared marker's click opens their groups
and frames its findings, so they split.
"""

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import findings, findings_draw, fold, transform
from .state import board

MARKER_PX = 18  # unscaled: a marker's width on screen
MERGE = 1.1  # of a marker's width: markers nearer than this on screen share one
FADED = 0.35  # the alpha of a marker beside the shown finding, or on the side the view does not see
LIFT_M = 30e-6  # out from the outer surface
TEXTURE_PX = 64
COUNT_SIZE = 11  # unscaled: a shared marker's count
COUNT_PAD = 3  # unscaled: around the count on its plate
COUNT_PLATE = (0.03, 0.03, 0.03, 0.85)
FONT = 0
LEVELS = (("error", "Errors", "Markers for errors only"),
          ("warning", "Warnings", "Markers for errors and warnings"),
          ("all", "All", "Markers for every finding, info too"))
ALLOWED = {"error": {"error"}, "warning": {"error", "warning"}}  # a level -> the severities shown; "all": any

_handle = None
_cache = {"markers": None}
_textures = {}
_drawn = {}  # region pointer -> [((x0, y0, x1, y1), [marker, ...])] as last drawn: what a click hits


# --- Which markers, where -------------------------------------------------------------------

def invalidate():
    """A new findings frame or a rebuilt board: the markers placed again, the views redrawn."""
    _cache["markers"] = None
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except (AttributeError, ReferenceError):
        pass


def markers():
    """Every finding with a place on the board: {"source", "key", "finding", "kind" (its
    ICONS key), "at" (Blender, out from its side), "side" (+1, -1 or None), "box" (Blender
    x0, y0, x1, y1)}. Placed once per findings frame and board (item_sides reads every
    copper object once)."""
    if _cache["markers"] is not None:
        return _cache["markers"]
    out = []
    if board.collection is not None:
        sides = findings_draw.item_sides()
        top, bottom = findings_draw._surface("F.Cu")[0], findings_draw._surface("B.Cu")[0]
        for name, _ in findings.SOURCES:
            for finding in findings.source(name).get("findings") or ():
                box = finding.get("bbox_nm")
                if not box:
                    continue
                (x0, y0), (x1, y1) = transform.xy_m([box[:2], box[2:]], board.origin_nm)
                x0, y0, x1, y1 = float(min(x0, x1)), float(min(y0, y1)), float(max(x0, x1)), float(max(y0, y1))
                side = findings_draw.finding_side(finding, sides)
                z = bottom - LIFT_M if side == -1.0 else top + LIFT_M
                out.append({"source": name, "key": finding.get("key", ""), "finding": finding,
                            "kind": findings_draw.mark(finding), "side": side,
                            "at": Vector(((x0 + x1) / 2, (y0 + y1) / 2, z)), "box": (x0, y0, x1, y1)})
    _cache["markers"] = out
    return out


def shown(scene):
    """The markers to draw: not dismissed, as severe as the filter asks (by severity, a
    "?" too)."""
    allowed = ALLOWED.get(scene.kileido_markers_level)
    return [marker for marker in markers() if marker["finding"].get("state") != "dismissed" and
            (allowed is None or (marker["finding"].get("severity") or "warning") in allowed)]


def merge(points, reach):
    """Markers placed on screen [(x, y, marker)] gathered, the worst first: each joins the
    first group whose marker sits within `reach`, else starts one. [(x, y, [marker, ...])],
    each group at its first (worst) marker."""
    order = findings_draw.MARK_ORDER
    groups = []
    for x, y, marker in sorted(points, key=lambda point: order.index(point[2]["kind"])):
        for gx, gy, members in groups:
            if (gx - x) ** 2 + (gy - y) ** 2 <= reach * reach:
                members.append(marker)
                break
        else:
            groups.append((x, y, [marker]))
    return groups


def hit(pointer, x, y):
    """The markers of the group drawn under region pixel (x, y) in the region `pointer`
    (the one drawn last on top), or None."""
    for (x0, y0, x1, y1), members in reversed(_drawn.get(pointer, ())):
        if x0 <= x <= x1 and y0 <= y <= y1:
            return members
    return None


# --- Clicking one ---------------------------------------------------------------------------

def open_markers(context, members):
    """A marker clicked: its finding shown (as its title in the column), its group opened
    there. A stacked marker (several findings at one spot, the count beside it): the next
    one of them each click, the worst first, round again after the last. A message for the
    status bar."""
    from . import columns
    for source_name, check in dict.fromkeys((marker["source"], marker["finding"].get("check") or "other")
                                            for marker in members):
        findings.open_group(context.scene, source_name, check)  # its row in sight, its details under it
    if context.area is not None and context.scene.kileido_column != "DRC":
        columns.open_column(context, "DRC")
    keys = [(marker["source"], marker["key"]) for marker in members]
    showing = keys.index(board.findings_shown) if board.findings_shown in keys else -1
    number = (showing + 1) % len(members) if len(members) > 1 else 0
    marker = members[number]
    bpy.ops.kileido.finding_show(source=marker["source"], key=marker["key"])
    title = marker["finding"].get("title") or marker["finding"].get("check") or "finding"
    if len(members) == 1:
        return f"KiLeidoscope: {title}"
    return f"KiLeidoscope: {title} ({number + 1} of {len(members)} here; click again for the next)"


# --- Drawing --------------------------------------------------------------------------------

def _texture(kind):
    texture = _textures.get(kind)
    if texture is None:
        values = findings_draw.icon_pixels(kind, TEXTURE_PX).ravel()
        texture = gpu.types.GPUTexture((TEXTURE_PX, TEXTURE_PX), format="RGBA32F",
                                       data=gpu.types.Buffer("FLOAT", len(values), values.tolist()))
        _textures[kind] = texture
    return texture


def _quad(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]


def _draw():
    context = bpy.context
    region, view = context.region, context.region_data
    if region is None or view is None:
        return
    pointer = region.as_pointer()
    _drawn[pointer] = []
    scene = context.scene
    if not getattr(scene, "kileido_markers", False) or board.collection is None or not board.findings or \
            fold.folded():
        return
    try:
        items = shown(scene)
    except ReferenceError:  # the board freed under us
        return
    if not items:
        return
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    scale = context.preferences.system.ui_scale or 1.0
    size = MARKER_PX * scale
    from_top = view.view_matrix.inverted().col[2].z >= 0  # the eye above the board
    points = []
    for marker in items:
        xy = location_3d_to_region_2d(region, view, marker["at"])
        if xy is not None and -size <= xy.x <= region.width + size and -size <= xy.y <= region.height + size:
            points.append((xy.x, xy.y, marker))
    showing = board.findings_shown
    image = gpu.shader.from_builtin("IMAGE_COLOR")
    plain = gpu.shader.from_builtin("UNIFORM_COLOR")
    blf.size(FONT, COUNT_SIZE * scale)
    for x, y, members in merge(points, size * MERGE):
        gpu.state.blend_set("ALPHA")  # each time: drawing a count's text leaves blending off
        faded = (showing != ("", "") and all((m["source"], m["key"]) != showing for m in members)) or \
            all(m["side"] is not None and (m["side"] > 0) != from_top for m in members)
        alpha = FADED if faded else 1.0
        half = size / 2
        x0, y0, x1, y1 = x - half, y - half, x + half, y + half
        image.uniform_sampler("image", _texture(members[0]["kind"]))
        image.uniform_float("color", (1.0, 1.0, 1.0, alpha))
        batch_for_shader(image, "TRIS", {"pos": _quad(x0, y0, x1, y1),
                                         "texCoord": _quad(0.0, 0.0, 1.0, 1.0)}).draw(image)
        if len(members) > 1:  # the count on a dark plate at the icon's right
            text = str(len(members))
            width, height = blf.dimensions(FONT, text)
            pad = COUNT_PAD * scale
            plate = (x1 + pad / 2, y - height / 2 - pad, x1 + pad * 2.5 + width, y + height / 2 + pad)
            plain.uniform_float("color", (*COUNT_PLATE[:3], COUNT_PLATE[3] * alpha))
            batch_for_shader(plain, "TRIS", {"pos": _quad(*plate)}).draw(plain)
            blf.color(FONT, 0.95, 0.95, 0.95, alpha)
            blf.position(FONT, plate[0] + pad, y - height / 2, 0)
            blf.draw(FONT, text)
            x1 = plate[2]
        _drawn[pointer].append(((x0, y0, x1, y1), members))
    gpu.state.blend_set("NONE")


def install():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")


def uninstall():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    _textures.clear()
    _drawn.clear()
    _cache["markers"] = None
