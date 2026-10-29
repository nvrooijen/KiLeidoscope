"""The finding shown from the DRC or LLM column (findings.py), drawn in 3D.

Real objects in the board's "Findings" group, not viewport-only drawing, so they render
and show in a saved image. Only the shown finding is drawn; every object carries
`kls_finding` (its key) and is made again by `refresh` (a new findings frame, a snapshot,
the copper thickness toggle), so it always sits on the copper as drawn now.

- highlight, area: their items as KiCad's selection is highlighted (highlight.py kind
  "finding"), parts boxed, in the severity's colour; an area given as a polygon instead
  of items: a translucent n-gon just out from its layer.
- isolate: as highlight, and X-ray mode ticked (focus.py) while it is shown, unticked
  again when it goes (when the box was ticked for it); unticking it leaves X-ray. A
  reference-plane finding (check "plane.…") that draws anything gets X-ray the same way.
- label: near-white text on a dark plate, the severity's icon at its left (a red circle
  "!", a yellow triangle "!", a blue circle "i"), facing the view. A Track To constraint
  turns every label to one empty, "KLS finding eye", which follows the first 3D view's
  eye (a timer) and the scene camera while rendering (render_pre), so one mechanism
  serves viewport and render. Where labels go, every tool's alike, is label_place's:
  beside the finding, above the surface the eye sees, a thin leader to what it names
  (laid out again as the eye moves); on a hole finding's cut face, flat on it.
- distance: a thin tube with a tick at each end and the bridge's measured value, red
  past its limit; arrow: a tube with a cone head.
- ghost, the LLM's suggestion where it put it: a translucent via, capacitor or pad; a part
  moved (translucent copies of its pads, a thin outline of its body, a thin arrow from
  where it is); a route (a flat translucent ribbon at its width on its layer's copper); a
  row of vias (each as a via ghost). One that does not fit is in the "over" red and its
  label says so ("does not fit"); a row's vias that do not fit alone are red, each
  crossed out by a thin X on both faces of the board. The label and the column say whose verdict it is: "checking with KiCad DRC…" while KiCad's DRC runs on a
  copy with the suggestions written in, "fits (KiCad DRC)" or "does not fit (KiCad DRC):
  …" once it is in, the bridge's "(quick check)" when KiCad could not run.
- layers: a layer filter while the finding is shown: only the copper layers it names are
  shown (layers.set_filter), the others come back as their eyes say once it is hidden.
- clearance, edge_gap: as distance, between the two closest points the bridge measured;
  an edge gap's outline end has a longer tick, along the edge.
- width: the segments as ribbons at their real width just out from their copper, the
  narrow ones in the "over" red, and a light dimension across the thinnest.
- routed_length: a thin line along the copper path the bridge traced, stepping through
  the board at a via, dots at its ends, its label at the path's middle.
- bars: a small bar chart on a label's plate, under its title, facing the view as the
  labels do (label_place puts it): heights to the largest value or limit, the limit as a
  light line across, bars past it in the "over" red, values over the bars, names under.
- lift: the part lifted off its pads as it would stand if it tombstoned: copies of its
  visible model parts (else its placeholder) turned about the line through `about`, across
  the direction to `toward`, so that end rises; the part itself is hidden meanwhile and
  shown again as KiCad shows it when the finding goes (`_restore_lifted`).

Planes and return paths, measured by the bridge, in thin lines (hairlines, a little
thicker for `emphasis: strong`); never colour alone, each colour also has a shape or words:
- layer_change: the via highlighted, a measured line to the nearest return via, the other
  return vias ringed.
- plane_gap: the plane's edges near the gap lit up on its layer (the broken part of the
  plane, not the track), the crossing's stretches without plane under them as "over" red
  ribbons with a tick across each end, the gap labelled at the longest.
- return_path: the signal's stretches with plane under them as faint ribbons, without it
  as plane_gap's red ones; one label, the length without plane and the share covered.
- plane_islands: each piece a translucent area on its layer in its own colour (a fixed
  list), one not tied outlined in the "over" red; numbered labels with area and state, the
  largest first, the main plane's "main", and one for the count.
- stitching: the stitching vias ringed on its layer, each stretch of the line wider than
  the limit a thin line with a tick at each end (red past the limit), the widest labelled.
- stub: the via's barrel, the unused part (the stub) a red cylinder between its layers'
  copper and the used part a faint one; labelled with the stub's length, or "none".
- discontinuity: a small marker per mark on its layer, its shape by kind (a bar for a
  width step, a ring for a layer change, a diamond for a reference change), labelled with
  its kind over its text (the first MARK_LABELS; the column lists all).

Every measured label reads its `what` over the value against its limit ("Clearance" over
"0.080 mm < 0.20 mm min"). Points are KiCad nm with a layer name: copper sits on its
outer surface, any other layer on its side's (F. top, B. bottom, Edge.Cuts top). While the
flex board is folded nothing is drawn here (the drawing fits the flat board); the column
says so.

The 3D views are framed on the shown finding from here too (`frame_bounds`): on its box,
from the side it is on, the eye kept out of part models.
"""

import json
import math
import re
from contextlib import contextmanager

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import components, focus, fold, highlight, hole_cut, label_place, layers, materials, poses, transform
from .objects import (VIEW_CLIP_START_M, camera_rays_only, hide, link_owned, lookup, outline_bounds, owned_name,
                      set_visible, shown_views, view3d_spaces)
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
SEVERITIES = ("error", "warning", "info")  # a finding's, as the bridge sends them
TICK_STROKE = 0.13  # of an icon's radius: half the width of the "✓" strokes
TUBE_MIN_M = 5e-6
TUBE_SHARE = 1 / 10  # of a label's size: a tube's radius
LEADER = 0.5  # of a tube's radius: a leader's
TICK = 6  # a distance's end ticks, in tube radii either side
EDGE_TICK = 2.5  # of TICK: an edge gap's tick on the outline, along the edge
AREA_LIFT_M = 20e-6  # an area's n-gon and a width's ribbons out from their copper
NARROW_LIFT_M = AREA_LIFT_M * 1.5  # red ribbons (narrow segments, spans without plane) over the others
CALLOUT_LIFT_M = AREA_LIFT_M * 2  # a width's dimension over its ribbons
MIN_DEPTH_M = 1e-5  # a cylinder through the board is never flatter than this
ARROW_HEAD = 0.8  # of a label's size: an arrow's head, at most ...
ARROW_HEAD_SHARE = 0.4  # ... and at most this much of the arrow
ARROW_CONE = 3.0  # of the tube's radius: the head's radius at least, ...
ARROW_CONE_SHARE = 0.35  # ... else this much of the head's length
THIN_HEAD = 0.5  # of a label's size: a thin arrow's head (a moved part's), at most ...
THIN_HEAD_SHARE = 0.3  # ... and at most this much of the arrow
DOT = 2.5  # of a tube's radius: a routed length's end dots
END_SEGMENTS = 16  # round ends of ribbons, a routed length's end dots
# bars: in text sizes (a chart is sized to the finding as its title is)
CHART = "kls_bars"  # a bars label's chart, JSON on its title text
BAR = 1.0  # a bar's width
BAR_HEIGHT = 4.0  # the tallest bar (or limit)
BAR_DEPTH = 0.3  # how far a bar stands out from its plate
BAR_TEXT = 0.6  # a bar's value and name
BAR_GAP = 0.5  # between columns, and title to chart
BAR_MIN = 0.03  # a zero bar still shows, this tall
BAR_LINE = 0.08  # the limit and base lines' thickness
BAR_NAME = 12  # characters of a bar's name
CHAR = 0.6  # of a text's size: about one character's width, to fit a column to its texts
GHOST_ALPHA = 0.35
VIA_GHOST_M = 0.6e-3
CAP_GHOST_M = 1.0e-3  # an 0402: 1.0 x 0.5 x 0.5 mm
PAD_GHOST_M = 0.6e-3
TRACK_GHOST_NM = 200_000  # a route the bridge sent without a width
GHOST_COPPER_M = 35e-6
GHOST_HEAD = 4.0  # of a thin line's radius: a moved part's arrow head, its width
GHOST_NAMES = {"part": "part move", "track": "route", "via_row": "via row"}  # in the column, by kind
RING = 1.5  # of a return via's radius: its ring's
CROSS = 1.3  # of a via ghost's radius: the arms of the X on a row's via that does not fit
RING_SEGMENTS = 24
SPAN_TICK = 1.6  # of a span's half width: its end ticks either side
# plane_islands: a piece's colour by its index, none of them the "over" red
ISLAND_COLORS = ((0.15, 0.55, 0.95), (0.2, 0.8, 0.35), (0.7, 0.4, 0.95), (0.0, 0.8, 0.8),
                 (0.95, 0.55, 0.15), (0.95, 0.45, 0.75), (0.6, 0.75, 0.2), (0.55, 0.6, 0.95))
ISLAND_LABELS = 8  # pieces labelled in 3D (main, then not tied, then the largest); the column lists all
BARREL = 1.2  # of a stub's via radius: its barrel drawn just around the real one
MARK = 0.5  # of a label's size: a discontinuity marker's width
MARK_LABELS = 8  # marks labelled in 3D, in order; the column lists all
MARK_TITLES = {"width": "Width step", "layer": "Layer change", "reference": "Reference change"}  # by kind
LIFT_DEG = 40.0  # a lift draw without an angle
FOLLOW_S = 0.1
VIEW_NEAR_M = 2e-3  # a label pulled towards a 3D view's eye stays this far from it (its clip is 1 mm)
_UP = Matrix.Rotation(-math.pi / 2, 4, "X")  # the eye's z along the view's up: the labels' up (Track To)
_state = {"folded": False, "views": {}, "follow": None, "eye": None,  # views: area pointer -> last view matrix
          "labels": [], "layout": None,  # labels facing the eye: laid out again as it moves
          "reach": None,  # (x0, y0, x1, y1) the labels' plates cover, for framing
          "parts": None,  # part_boxes() of the drawing as last made (the parts move only with a redraw)
          "lifted": []}  # names of the parts hidden under lifted copies (_draw_lift)


# --- What is shown --------------------------------------------------------------------------

def shown_finding():
    """The finding `board.findings_shown` names, or None (gone from the newest list)."""
    source, key = board.findings_shown
    if not key:
        return None
    from . import findings  # it imports this module
    return findings.find(source, key)


def item_ids(finding, tools=None):
    """Item ids of a finding's drawn draws (only `tools`' when given), in order."""
    ids = {}
    for draw in finding.get("draws", ()):
        if draw.get("ok") and (tools is None or draw.get("tool") in tools):
            ids.update(dict.fromkeys(draw.get("ids", ())))
    return list(ids)


def xray_wanted(finding, draws):
    """A finding shown in X-ray: one with an isolate draw, or a reference-plane finding
    (check "plane.…": the plane under the copper is the point) that draws anything (a
    plane gap lights no items of its own: its edges and spans are the finding's objects)."""
    return finding is not None and (any(draw["tool"] == "isolate" for draw in draws) or
                                    (str(finding.get("check") or "").startswith("plane.") and bool(draws)))


def _follow_xray(wanted):
    """X-ray mode ticked when a finding that wants it is shown (once per finding: unticked
    by the user, it stays off for it), and unticked when it goes if it was ticked for it.
    A malformed outline may hold the tick too (`board.xray_for_outline`, apply.py): the
    box is unticked only once neither holds it."""
    scene = bpy.context.scene
    if wanted and board.xray_for_finding != board.findings_shown:
        board.xray_for_finding = board.findings_shown
        if not getattr(scene, "kileido_focus", True):
            board.xray_ticked = True
            scene.kileido_focus = True
        elif board.xray_for_outline:  # ticked for the outline: held for the finding too
            board.xray_ticked = True
    elif not wanted and board.xray_for_finding is not None:
        board.xray_for_finding = None
        if board.xray_ticked:
            board.xray_ticked = False
            if not board.xray_for_outline:
                scene.kileido_focus = False


def highlighted(finding):
    """Item ids a finding's highlight shows: its highlight, isolate and area draws' items,
    and a layer change's via (its first id; not its return vias, which are ringed)."""
    ids = set(item_ids(finding, ("highlight", "isolate", "area")))
    ids.update(draw["ids"][0] for draw in finding.get("draws", ())
               if draw.get("ok") and draw.get("tool") == "layer_change" and draw.get("ids"))
    return ids


def confirmed(finding):
    """False when KiLeidoscope could not confirm a finding: the bridge's `verdict` is refuted
    or mismatch. Without a verdict (an older payload) it is judged here from the parts: its
    items gone from the board, a target or a draw not found, a value the bridge measured
    differently, a problem whose every measurement meets its limit, or a suggestion (a
    ghost) that does not fit."""
    verdict = finding.get("verdict")
    if verdict in ("measured", "unchecked"):
        return True
    if verdict in ("refuted", "mismatch"):
        return False
    if finding.get("faded") or finding.get("disagree") or refuted(finding):
        return False
    if any(draw.get("fits") is False for draw in finding.get("draws") or ()):
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
    return finding.get("severity") if finding.get("severity") in SEVERITIES else "warning"


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
    layers = [draw.get("layer"), *(point[2] for point in (*(draw.get("points") or ()), *(draw.get("path_nm") or ()))
                                   if len(point) > 2),
              *(segment[4] for segment in _segments(draw) if len(segment) > 4),
              *(island.get("layer") for island in draw.get("islands") or ()),
              *(mark.get("layer") for mark in draw.get("marks") or ())]
    return {_outer_side(layer) for layer in layers}


def _segments(draw):
    """A draw's [x0, y0, x1, y1, layer, width_nm] stretches: a width's segments, and the
    stretches with and without plane under them."""
    for key in ("segments_nm", "covered_nm", "spans_nm"):
        yield from draw.get(key) or ()


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
            sides.setdefault(key[1], set()).add(1.0 if poses.in_root(obj).col[2].z >= 0 else -1.0)
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
    """Draw the shown finding again from scratch, or nothing; its highlight and X-ray follow."""
    if board.collection is None or board.in_snapshot:
        return
    from . import findings_markers  # it imports this module
    findings_markers.invalidate()  # the board's markers placed again on the board as drawn now
    _clear_objects()
    _restore_lifted()
    finding = shown_finding()
    if finding is None:
        board.findings_shown = ("", "")
    draws = [draw for draw in (finding or {}).get("draws", ()) if draw.get("ok")]
    chosen = highlighted(finding) if finding else set()
    isolate = xray_wanted(finding, draws)
    if finding is not None and board.finding_severity != (finding.get("severity") or "warning"):
        board.finding_severity = finding.get("severity") or "warning"
        if board.materials:
            materials.paint_finding()
    if chosen != board.highlight.get("finding") or isolate != board.findings_isolate:  # else they follow edits already
        board.highlight["finding"], board.findings_isolate = chosen, isolate
        highlight.refresh()
        focus.refresh()
    _follow_xray(isolate)
    _state["folded"] = fold.folded()
    keep = {layer for draw in draws if draw["tool"] == "layers" for layer in draw.get("layers") or ()}
    layers.set_filter(keep if finding is not None and not _state["folded"] else None)
    _state["labels"], _state["layout"], _state["reach"], _state["parts"] = [], None, None, None
    if not _state["folded"]:
        _lift_listed(finding)  # every listed finding's parts lifted (the Lifted parts toggle)
    if (finding is not None or _lifts_wanted()) and not bpy.app.timers.is_registered(_follow):
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
def nothing_drawn():
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
    if board.collection is None or (board.findings_shown == ("", "") and not _lifts_wanted()):
        return None
    try:
        if fold.folded() != _state["folded"]:
            if _state["folded"] and board.findings_shown != ("", ""):  # unfolded: the cut first, the labels
                hole_cut.show(shown_finding())  # are written on its face
            refresh()
        if board.findings_shown != ("", ""):
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
    return (Matrix.Translation(view.view_location) @ view.view_rotation.to_matrix().to_4x4() @
            Matrix.Translation((0.0, 0.0, view.view_distance)))


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
    eye = lookup(EYE) if board.collection is not None else None
    if eye is None:
        return
    matrix, ortho, near = eye_view if eye_view is not None else _eye_matrix()
    last = _state["eye"]
    if (force or last is None or last[1:] != (ortho, near) or
            any(abs(a - b) > 1e-7 for row, other in zip(last[0], matrix) for a, b in zip(row, other))):
        eye.matrix_world = matrix
        _state["eye"] = (matrix.copy(), ortho, near)
        _lay_out(poses.to_root() @ matrix, ortho, near)  # the labels are laid out on the board, in its root's space


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
            layers.set_filter(None)  # a shown finding's layer filter lifted
        except ReferenceError:
            pass


# --- Objects --------------------------------------------------------------------------------

def _new(name, data, finding):
    """A drawing object, seen by camera rays only: an overlay casts no shadow and lights
    nothing, so it adds no grain to the board in a rendered view."""
    obj = bpy.data.objects.new(owned_name(name), data)
    camera_rays_only(obj)
    obj["kileido_owned"] = 1
    obj[TAG] = finding.get("key", "")
    link_owned(obj, "findings")
    return obj


def _material(kind, alpha=1.0):
    """Flat glowing colour of a severity (or "over": a distance past its limit; "pass": within it)."""
    color = materials.FINDING_OVER_COLOR if kind == "over" else materials.FINDING_PASS_COLOR if kind == "pass" else \
        materials.FINDING_COLORS.get(kind, materials.FINDING_COLORS["warning"])
    material = materials.make(f"KLS finding {kind}" + (" ghost" if alpha < 1 else ""), color, alpha)
    materials.paint(material, color)
    return material


def _draw_points(draw):
    """Every [x_nm, y_nm] a draw names: its points, a path's, segment and stretch ends,
    plane edges, return vias, island outlines, a stub's barrel, the marks, a ghost row's
    vias and where a moved part is now."""
    for key in ("points", "path_nm", "returns", "row"):
        yield from (point[:2] for point in draw.get(key) or ())
    if len(draw.get("from") or ()) >= 2:
        yield draw["from"][:2]
    for segment in _segments(draw):
        if len(segment) >= 4:
            yield from (segment[0:2], segment[2:4])
    for line in draw.get("edges_nm") or ():
        yield from (point[:2] for point in line)
    for island in draw.get("islands") or ():
        yield from (point[:2] for point in island.get("polygon_nm") or ())
    for spot in (draw.get("barrel") or {}, *(draw.get("marks") or ())):
        if spot.get("x") is not None and spot.get("y") is not None:
            yield [spot["x"], spot["y"]]


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
        return _faces()[1] - lift, -1.0
    return _faces()[0] + lift, 1.0


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
    eye = lookup(EYE)
    if eye is None:
        eye = bpy.data.objects.new(owned_name(EYE), None)
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
    # the name Blender gave it: another object of that name (a leftover) makes it ".001"
    _state["labels"].append({"name": obj.name, "anchor": anchor.copy(), "material": material, "icon": icon})
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
    the icon at the left and the text (a bars label's chart under it too), the icon
    centred on the text's lines."""
    size = obj.data.size
    x0, y0, x1, y1 = _bounds(obj)
    icon, pad = ICON * size, PLATE_PAD * size
    centre = (x0 - ICON_GAP * size - icon / 2, (y0 + y1) / 2)
    y0, y1 = min(y0, centre[1] - icon / 2), max(y1, centre[1] + icon / 2)
    chart = _chart(obj)
    if chart is not None:
        x1, y0 = max(x1, chart["right"]), min(y0, chart["bottom"])
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
    severity = icon if icon in ICONS else \
        finding.get("severity") if finding.get("severity") in SEVERITIES else "warning"
    shape, color, glyph_mark, ink = ICONS[severity]
    radius = ICON * size / 2

    def disc(bm):
        bmesh.ops.create_circle(bm, cap_ends=True, segments=RING_SEGMENTS, radius=radius)

    def triangle(bm):
        bm.faces.new([bm.verts.new((x, y, 0.0)) for x, y in ((-radius, -radius), (radius, -radius), (0.0, radius))])
    badge = _mesh(obj.name + " icon", disc if shape == "circle" else triangle,
                  Matrix.Translation((cx, cy, -behind / 2)), finding, _plain(f"icon {severity}", color))
    ink_material = _plain("glyph dark" if max(ink) < 0.5 else "glyph light", ink)
    if glyph_mark == "✓":  # drawn, not typed: the label font may lack it
        glyph = _mesh(obj.name + " icon glyph", _tick(radius), Matrix.Translation((cx, cy, -behind / 4)), finding,
                      ink_material)
    else:
        curve = bpy.data.curves.new(obj.name + " icon glyph", "FONT")
        curve.body = glyph_mark
        curve.size = GLYPH * ICON * size
        curve.align_x, curve.align_y = "CENTER", "CENTER"
        curve.materials.append(ink_material)
        glyph = _new(curve.name, curve, finding)
        lower = radius / 4 if shape == "triangle" else 0.0  # a triangle's room is low
        glyph.location = (cx, cy - lower, -behind / 4)
    for part in (plate, badge, glyph, *_chart_parts(obj, finding, behind)):
        part.parent = obj
        part.show_in_front = True
    return plate


def _tick(radius):
    """A builder of the "✓" glyph: CHECK's two strokes as quads, in an icon of `radius`."""
    stroke = radius * TICK_STROKE

    def build(bm):
        for (x0, y0), (x1, y1) in zip(CHECK, CHECK[1:]):
            dx, dy = (x1 - x0) * radius, (y1 - y0) * radius
            length = math.hypot(dx, dy)
            nx, ny = -dy / length * stroke, dx / length * stroke
            ends = ((x0 * radius, y0 * radius), (x1 * radius, y1 * radius))
            bm.faces.new([bm.verts.new((x + side * nx, y + side * ny, 0.0))
                          for (x, y), side in ((ends[0], 1), (ends[0], -1), (ends[1], -1), (ends[1], 1))])
    return build


def figure(value):
    """A value as the column and the 3D labels show it: a float to 4 significant digits."""
    return f"{value:.4g}" if isinstance(value, float) else str(value)


def common_limit(bars):
    """The one limit every bar shares, or None (none, or each its own)."""
    limits = {bar.get("limit") for bar in bars}
    return limits.pop() if len(limits) == 1 else None


def _chart(obj):
    """A bars label's chart, under its title in the text's own axes at its present size:
    the column pitch, its first column's left, the bars' base and full height, its right
    and bottom edges; None for any other label."""
    spec = obj.get(CHART)
    if not spec:
        return None
    bars = json.loads(spec)
    size = obj.data.size
    small = BAR_TEXT * size
    x0, y0, _, _ = _bounds(obj)
    widest = max(len(text) for bar in bars for text in (figure(bar["value"]), bar["name"]))
    pitch = max(BAR, widest * CHAR * BAR_TEXT) * size + BAR_GAP * size
    peak = y0 - BAR_GAP * size - small - BAR_GAP * size / 2  # the tallest bar's top, its value over it
    base = peak - BAR_HEIGHT * size
    return {"bars": bars, "pitch": pitch, "left": x0, "base": base, "height": BAR_HEIGHT * size,
            "most": max([abs(bar["value"]) for bar in bars] + [abs(bar["limit"]) for bar in bars
                                                                if bar.get("limit") is not None]) or 1.0,
            "right": x0 + pitch * len(bars), "bottom": base - BAR_GAP * size / 2 - small}


def _chart_parts(obj, finding, behind):
    """A bars label's chart under its title: a bar per item standing out of the plate (past
    its limit in the "over" red), its value over it and its name under it, a base line, and
    the limit as a light line across (each bar's own when they differ)."""
    chart = _chart(obj)
    if chart is None:
        return []
    size = obj.data.size
    small, depth, line = BAR_TEXT * size, BAR_DEPTH * size, BAR_LINE * size
    middle = depth / 2 - behind / 2
    pitch, left, base, scale = chart["pitch"], chart["left"], chart["base"], chart["height"] / chart["most"]
    parts = []

    def text(name, body, x, y):
        curve = bpy.data.curves.new(name, "FONT")
        curve.body = body
        curve.size = small
        curve.align_x, curve.align_y = "CENTER", "CENTER"
        curve.materials.append(_plain("text"))
        part = _new(name, curve, finding)
        part.location = (x, y, middle)
        parts.append(part)

    for number, bar in enumerate(chart["bars"], start=1):
        x = left + pitch * (number - 0.5)
        height = max(abs(bar["value"]) * scale, BAR_MIN * size)
        material = _material("over" if bar.get("over") else finding.get("severity"))
        parts.append(_mesh(f"{obj.name} bar {number}", _box((BAR * size, height, depth)),
                           Matrix.Translation((x, base + height / 2, middle)), finding, material))
        text(f"{obj.name} bar {number} value", figure(bar["value"]), x, base + height + BAR_GAP * size / 2 + small / 2)
        text(f"{obj.name} bar {number} name", bar["name"], x, base - BAR_GAP * size / 2 - small / 2)
    width = pitch * len(chart["bars"])
    shared = common_limit(chart["bars"])
    if shared is not None:
        lines = [(left + width / 2, base + abs(shared) * scale, width)]
    else:
        lines = [(left + pitch * (number - 0.5), base + abs(bar["limit"]) * scale, pitch - BAR_GAP * size / 2)
                 for number, bar in enumerate(chart["bars"], start=1) if bar.get("limit") is not None]

    def rules(spans, thick):
        def build(bm):
            for x, y, long in spans:
                found = bmesh.ops.create_cube(bm, size=1.0)["verts"]
                bmesh.ops.scale(bm, vec=(long, line, thick), verts=found)
                bmesh.ops.translate(bm, vec=(x, y, 0.0), verts=found)
        return build
    parts.append(_mesh(obj.name + " base", rules([(left + width / 2, base - line / 2, width)], depth),
                       Matrix.Translation((0.0, 0.0, middle)), finding, _plain("text")))
    if lines:  # through the bars, out past their faces either side
        parts.append(_mesh(obj.name + " limit", rules(lines, depth + 2 * line), Matrix.Translation((0.0, 0.0, middle)),
                           finding, _plain("text")))
    return parts


def _place_labels(finding, box, unit):
    """Every label of the shown finding where label_place puts it, with its plate: on a
    hole finding's open cut face, flat there; else facing the eye (laid out by
    `_lay_out`, again whenever the eye moves), each with a thin leader to its anchor."""
    labels = _state["labels"]
    objects = [lookup(entry["name"]) for entry in labels]
    section = hole_cut.section(finding)
    if section is not None:
        bounds = outline_bounds()
        a, b, normal = section
        centre = Vector(((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)) if bounds else (a + b) / 2
        high, low = _faces()
        spots = label_place.on_cut([_block(obj, entry["anchor"]) for obj, entry in zip(objects, labels)],
                                   (a, b), normal, box or (a.x, a.y, b.x, b.y), centre, low, high, unit)
        corners = []
        for obj, entry, (location, turn, scale) in zip(objects, labels, spots):
            obj.data.size *= scale
            placed = Matrix.Translation(location) @ turn.to_4x4()
            poses.set_in_root(obj, placed)
            _plate(obj, finding, entry.get("icon"))
            (x0, y0, x1, y1), _ = _plate_rect(obj)
            corners += [placed @ Vector((x, y, 0.0)) for x in (x0, x1) for y in (y0, y1)]
        _reach(corners)
        _state["labels"] = []  # fixed on the face: nothing follows the eye
        return
    if box is None and labels:
        xs, ys = [entry["anchor"].x for entry in labels], [entry["anchor"].y for entry in labels]
        box = (min(xs), min(ys), max(xs), max(ys))
    radius = _hairline(unit)
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
    """The labels facing the eye (`matrix`, in the board root's space) put where label_place
    says for it, leaders too; one a part would hide brought towards the eye, in front of it
    (label_place.pull)."""
    if _state["layout"] is None or not _state["labels"]:
        return
    box, top, bottom, unit = _state["layout"]
    found = board.collection.all_objects
    entries = [entry for entry in _state["labels"] if entry["name"] in found]
    spots = label_place.beside([_block(found[entry["name"]], entry["anchor"]) for entry in entries],
                               box, matrix, top, bottom, unit)
    right, up = matrix.col[0].xyz.normalized(), matrix.col[2].xyz.normalized()
    if _state["parts"] is None:  # once per drawing, not per eye move: every object's box is read
        _state["parts"] = part_boxes(poses.to_root())
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
    poses.set_in_root(obj, matrix)
    return obj


def _box(size):
    def build(bm):
        bmesh.ops.create_cube(bm, size=1.0)
        bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    return build


def _cylinder(radius, depth, tip=None):
    """Along local z, centred; `tip` a cone's top radius."""
    def build(bm):
        bmesh.ops.create_cone(bm, cap_ends=True, segments=RING_SEGMENTS, radius1=radius,
                              radius2=radius if tip is None else tip, depth=depth)
    return build


def _strong(draw):
    """A draw with `emphasis: strong`: drawn STRONG times larger."""
    return draw.get("emphasis") == "strong"


def _faces():
    """(top, bottom): the heights of the board's outer copper surfaces (F.Cu, B.Cu)."""
    return board.heights.get("F.Cu", board.thickness_m), board.heights.get("B.Cu", 0.0)


def _circle(centre, radius, segments=RING_SEGMENTS):
    """A closed polyline round `centre` (a Blender point) in the board's plane."""
    return [centre + Vector((radius * math.cos(step), radius * math.sin(step), 0.0))
            for step in np.linspace(0.0, 2 * math.pi, segments + 1)]


def _hairline(unit):
    """A thin line's radius, as a label's leader: plane edges, stretches, limits."""
    return max(TUBE_MIN_M, unit * TUBE_SHARE * LEADER)


def _thin(unit, draw):
    """A drawn line's radius: a hairline, STRONG times it for `emphasis: strong`."""
    return _hairline(unit) * (STRONG if _strong(draw) else 1.0)


def _tube_radius(unit, length, draw):
    radius = unit * TUBE_SHARE * (STRONG if _strong(draw) else 1.0)
    return max(TUBE_MIN_M, min(radius, length / 6) if length > 0 else radius)


def _mm(nm):
    """A length as the bridge writes its own labels (drc_findings._number): "0.23", "0.080"."""
    mm = nm / 1e6
    return f"{mm:.2f}" if abs(mm) >= 0.1 else f"{mm:.3f}"


WHAT = {"distance": "Distance", "clearance": "Clearance", "edge_gap": "Edge gap", "width": "Width",
        "routed_length": "Routed length", "bars": "Values", "layer_change": "Return via", "plane_gap": "Gap",
        "return_path": "Without plane", "plane_islands": "Islands", "stitching": "Stitching gap", "stub": "Stub",
        "discontinuity": "Discontinuity"}  # a label's first line, by default


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


def covered_share(draw):
    """The share (0 to 1) of a return path's stretches with plane under them, by length;
    None without stretches."""
    def length(key):
        return sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in draw.get(key) or () if len(s) >= 4)
    covered, total = length("covered_nm"), length("covered_nm") + length("spans_nm")
    return covered / total if total > 0 else None


def covered_text(draw):
    """"82 % covered", never "100 %" while a stretch is without plane; "" without stretches."""
    share = covered_share(draw)
    if share is None:
        return ""
    percent = round(share * 100)
    return f"{min(percent, 99) if share < 1 else 100} % covered"


def side_text(side):
    """A stripline's other plane under the stretches without the named one (the bridge's
    `sides`), with both distances: "In4.Cu GND under all of it, 0.55 mm away (In2.Cu
    0.11 mm)"; "No plane on In4.Cu under it either"; "" with nothing without plane."""
    without, backed = side.get("without_nm") or 0, side.get("backed_nm") or 0
    other, plane = side.get("other") or "", side.get("plane") or "the plane"
    if not other or without <= 0:
        return ""
    if backed <= 0:
        return f"No plane on {other} under it either"
    nets = "/".join(side.get("nets") or ())
    where = f"{other} {nets}" if nets else other
    amount = "all of it" if backed >= without * 0.99 else f"{backed / 1e6:.2f} of {without / 1e6:.2f} mm"
    away, named = (side.get("other_nm") or 0) / 1e6, (side.get("plane_nm") or 0) / 1e6
    if away < named:
        return f"{where} under {amount}, {away:.2f} mm away: nearer than {plane} ({named:.2f} mm)"
    if away == named:
        return f"{where} under {amount}, {away:.2f} mm away: as near as {plane}"
    return f"{where} under {amount}, {away:.2f} mm away ({plane}: {named:.2f} mm)"


def side_lines(draw):
    """`side_text` of each of a draw's signal layers that says anything."""
    return [text for text in (side_text(side) for side in draw.get("sides") or ()) if text]


def side_short(side):
    """`side_text` for a 3D label's line (LABEL_CHARS): "In4.Cu GND under all, 0.55 mm
    (In2.Cu 0.11)", the named plane's distance in mm too; "no plane on In4.Cu either"; ""."""
    without, backed = side.get("without_nm") or 0, side.get("backed_nm") or 0
    other = side.get("other") or ""
    if not other or without <= 0:
        return ""
    if backed <= 0:
        return f"no plane on {other} either"
    where = " ".join(filter(None, (other, "/".join(side.get("nets") or ()))))
    amount = "all" if backed >= without * 0.99 else f"{backed / 1e6:.2f} mm"
    return (f"{where} under {amount}, {(side.get('other_nm') or 0) / 1e6:.2f} mm "
            f"({side.get('plane') or 'plane'} {(side.get('plane_nm') or 0) / 1e6:.2f})")


def _with_sides(first, second, draw):
    """A plane draw's 3D label: `first` and `second` on their two lines, or with a
    stripline's other plane (`side_short`) "first: second" and that plane."""
    shorts = [text for text in (side_short(side) for side in draw.get("sides") or ()) if text]
    if not shorts:
        return "\n".join(filter(None, (first, second)))
    return "\n".join((f"{first}: {second}" if second else first, "; ".join(shorts)))


def island_text(island):
    """"4.1 mm², not tied": a plane piece's area, and "main" for the main plane, else
    whether vias tie it to it."""
    area = island.get("area_mm2")
    size = "" if area is None else f"{area:.1f} mm²" if area < 100 else f"{area:.0f} mm²"
    tied = "main" if island.get("main") else "tied" if island.get("tied") else "not tied"
    return ", ".join(text for text in (size, tied) if text)


def islands_text(draw):
    """"5 pieces, 2 not tied": a net's plane pieces, and how many no via ties to the main one."""
    islands = draw.get("islands") or []
    loose = sum(1 for island in islands if not island.get("main") and not island.get("tied"))
    return f"{len(islands)} piece{'' if len(islands) == 1 else 's'}, {loose} not tied"


def stubbed(draw):
    """Whether a stub draw found a stub: its barrel's stub layers, else a length measured."""
    barrel = draw.get("barrel") or {}
    return bool(barrel.get("stub")) if barrel else bool(draw.get("measured_nm"))


def mark_text(change):
    """"width: 0.20 -> 0.15 mm": a discontinuity mark's kind and its text, as the column lists it."""
    return ": ".join(text for text in (change.get("kind") or "", change.get("text") or "") if text)


def ghost_name(draw):
    """What a ghost suggests, in the column: "via", "part move", "route", "via row"."""
    kind = draw.get("kind") or "item"
    return GHOST_NAMES.get(kind, kind)


def ghost_measure(draw):
    """What the bridge measured of a ghost: how far a part moves ("3.20 mm"), a route's
    length, a row's vias and their spacing ("12 vias, 1.50 mm pitch"); "" for the rest."""
    kind = draw.get("kind")
    if kind == "part" and draw.get("moved_nm") is not None:
        return f"{_mm(draw['moved_nm'])} mm"
    if kind == "track" and draw.get("length_nm") is not None:
        return f"{_mm(draw['length_nm'])} mm"
    if kind == "via_row" and draw.get("row"):
        count = len(draw["row"])
        return f"{count} via{'' if count == 1 else 's'}" + \
            (f", {_mm(draw['pitch_nm'])} mm pitch" if draw.get("pitch_nm") else "")
    return ""


def ghost_clashing(draw):
    """How many vias of a row clash (their own `fits` False)."""
    return sum(1 for spot in draw.get("row") or () if len(spot) > 2 and spot[2] is False)


def ghost_check(draw):
    """What KiCad's DRC made of a ghost (the bridge's `kicad_drc`): "pending", "done",
    "failed", "skipped" with a reason; "" when it was not asked (the quick check alone)."""
    check = draw.get("kicad_drc") or {}
    state = check.get("state") or ""
    return "" if state == "skipped" and not check.get("error") else state


def _kicad_reason(draw, room):
    """KiCad's first reason a ghost does not fit, without the item it names ("clearance
    0.20 mm needed, 0.075 mm"), its rule alone ("clearance") past `room` characters (the
    column has it whole); "" when KiCad found none (another reason holds it back)."""
    problems = (draw.get("kicad_drc") or {}).get("problems") or ()
    reason = problems[0].split(" (")[0] if problems else ""
    return reason if len(reason) <= room else re.split(r" \d", reason)[0]


def ghost_verdict(draw):
    """Whose verdict on a ghost, and what, short enough for a label line: "fits (KiCad
    DRC)", "does not fit (KiCad DRC): clearance", "checking with KiCad DRC…", "does not fit
    (quick check)" when KiCad could not run; "" with no KiCad check asked."""
    state, fits, clashing = ghost_check(draw), draw.get("fits"), ghost_clashing(draw)
    count = f": {clashing} clash" if clashing else ""
    if state == "done":
        if fits is not False:
            return "fits (KiCad DRC)"
        said = "does not fit (KiCad DRC): "
        reason = _kicad_reason(draw, LABEL_CHARS - len(said))
        if not reason:  # not KiCad's: a reason it does not judge
            return f"does not fit: {(draw.get('problem') or '').split(' (')[0]}".rstrip(": ")
        return said[:-2] + count if count else said + reason
    if state == "pending":
        return "checking with KiCad DRC…" + (" (quick: does not fit)" if fits is False else "")
    if state:  # failed, or skipped with a reason: the quick check's word
        return f"does not fit (quick check){count}" if fits is False else "fits (quick check)" if fits else ""
    return ""


def ghost_label(draw):
    """A ghost's label: the LLM's label, else what it suggests ("Move C12", "Suggested
    route", a row's "12 vias, 1.50 mm pitch"), over what the bridge measured and whose
    verdict it is (`ghost_verdict`); "does not fit" in its place when it does not (never
    colour alone)."""
    kind, measure = draw.get("kind"), ghost_measure(draw)
    title = draw.get("label") or (f"Move {draw.get('part') or 'part'}" if kind == "part" else
                                  "Suggested route" if kind == "track" else
                                  measure if kind == "via_row" and measure else f"Suggested {kind or 'item'}")
    verdict = ghost_verdict(draw)
    if verdict:
        shown = measure if measure and measure != title and draw.get("fits") is not False else ""
        second = ", ".join(filter(None, (shown, verdict)))
    elif draw.get("fits") is False:
        clashing = ghost_clashing(draw)
        second = "does not fit" + (f": {clashing} clash" if clashing else "")
    elif measure and measure != title:
        second = measure
    else:
        second = "" if draw.get("label") else "by the LLM"
    return f"{title}\n{second}" if second else title


def ghost_lines(draw):
    """A ghost's lines in the column, (text, icon, alert) each: whether it fits and whose
    verdict that is, KiCad's reasons and notes, why KiCad's DRC did not run."""
    name, fits, problem = ghost_name(draw), draw.get("fits"), draw.get("problem") or ""
    check, state = draw.get("kicad_drc") or {}, ghost_check(draw)
    lines = []
    if state == "pending":
        lines.append((f"Suggested {name} (by the LLM): checking with KiCad DRC…", "SORTTIME", False))
        if fits is False:
            lines.append((f"Quick check: does not fit: {problem}", "ERROR", True))
        return lines
    if state == "done":
        problems = check.get("problems") or []
        if fits is not False:
            lines.append((f"Suggested {name} (by the LLM): fits (KiCad DRC)", "CHECKMARK", False))
        elif problems:
            said = "KiCad DRC: " + "; ".join(problems)
            lines.append((f"Suggested {name} does not fit (KiCad DRC): {'; '.join(problems)}", "ERROR", True))
            rest = problem[len(said):].lstrip("; ") if problem.startswith(said) else ""
            if rest:  # what KiCad does not judge, standing too
                lines.append((rest, "ERROR", True))
        else:
            lines.append((f"Suggested {name} does not fit: {problem}", "ERROR", True))
        return lines + [(f"KiCad DRC: {note}", "INFO", False) for note in check.get("notes") or ()]
    if fits is False:
        lines.append((f"Suggested {name} does not fit" + (" (quick check)" if state else "") + f": {problem}",
                      "ERROR", True))
    else:
        lines.append((f"Suggested {name} (by the LLM)" + ((": fits (quick check)" if state else ": fits") if fits
                                                          else ""), "LIGHT", False))
        if problem:  # a note, e.g. a large part drawn by its body alone
            lines.append((problem, "INFO", False))
    if state == "failed":
        lines.append((f"KiCad DRC could not run: {check.get('error') or 'unknown error'}", "INFO", False))
    elif state:
        lines.append((f"Not checked by KiCad DRC: {check.get('error')}", "INFO", False))
    return lines


def ghost_line(draw):
    """A ghost's measured line in the column: "Move C12: 3.20 mm", "Route: 14.20 mm long,
    0.20 mm wide", "Via row: 12 vias, 1.50 mm pitch"; "" for the rest."""
    measure = ghost_measure(draw)
    if not measure:
        return ""
    kind = draw.get("kind")
    if kind == "part":
        return f"Move {draw.get('part') or 'part'}: {measure}"
    if kind == "track":
        width = f", {_mm(draw['width_nm'])} mm wide" if draw.get("width_nm") else ""
        return f"Route: {measure} long{width}"
    return f"Via row: {measure}"


# --- The tools ------------------------------------------------------------------------------

def _draw_label(draw, unit, name, finding):
    """Its leader ends on the point, or on top of the part it names."""
    at, up = _point(draw["points"][0])
    top = _part_top(draw, up) if any(target.get("kind") == "part" for target in draw.get("targets", ())) else None
    if top is not None:
        at.z = top
    _label(draw.get("label") or draw.get("text") or "", at, unit, name, finding,
           _material(finding.get("severity")), _strong(draw))


def _part_top(draw, up):
    """The outer face of the first part among the draw's items (its model, else its box)."""
    for item_id in draw.get("ids", ()):
        frame = components.find(components.FRAME, item_id)
        extent = highlight._component_extent(item_id, frame) if frame is not None else None
        if extent is None:
            continue
        placed = poses.in_root(frame)
        heights = [(placed @ Vector(tuple(float(v) for v in corner))).z for corner in extent]
        return max(heights) if up > 0 else min(heights)
    return None


def _ends(draw, unit, points=None, thin=False):
    """The two points of an arrow or a distance, lifted onto their surfaces by the tube's
    radius (a hairline's when `thin`)."""
    points = points or draw["points"]
    (a, up), (b, _) = _point(points[0]), _point(points[1])
    radius = _thin(unit, draw) if thin else _tube_radius(unit, (b - a).length, draw)
    (a, up), (b, _) = _point(points[0], radius), _point(points[1], radius)
    return a, b, up, radius


def _draw_distance(draw, unit, name, finding, thin=False):
    """A distance, clearance or edge gap: a tube between its two points (a hairline when
    `thin`), a tick across each end, red past its limit. An edge gap's outline end (its
    Edge.Cuts point, else the second) sits on the copper end's layer, its tick longer:
    along the edge, as the closest gap meets the outline square."""
    points, edge = list(draw["points"][:2]), None
    if draw.get("tool") == "edge_gap":
        edge = next((i for i, point in enumerate(points) if len(point) > 2 and point[2] == "Edge.Cuts"), 1)
        copper = points[1 - edge]
        points[edge] = [*points[edge][:2], copper[2] if len(copper) > 2 else ""]
    a, b, up, radius = _ends(draw, unit, points, thin)
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
    _label(label_text(draw), (a + b) / 2, unit, name + " label", finding, material, _strong(draw),
           icon=_icon(draw))


def _arrow(name, a, b, radius, head, head_radius, finding, material):
    """A tube from `a` towards `b` ending in a cone `head` long at `b`; the cone."""
    along = b - a
    direction = along / along.length
    base = b - direction * head
    _tube(name, [[a, base]], radius, finding, material)
    matrix = Matrix.Translation(base + direction * head / 2) @ direction.to_track_quat("Z", "Y").to_matrix().to_4x4()
    return _mesh(name + " head", _cylinder(head_radius, head, tip=0.0), matrix, finding, material)


def _draw_arrow(draw, unit, name, finding):
    a, b, up, radius = _ends(draw, unit)
    material = _material(finding.get("severity"))
    length = (b - a).length
    if length > 1e-9:
        head = min(unit * ARROW_HEAD, length * ARROW_HEAD_SHARE)
        _arrow(name, a, b, radius, head, max(radius * ARROW_CONE, head * ARROW_CONE_SHARE), finding, material)
    if draw.get("label"):
        _label(draw["label"], a, unit, name + " label", finding, material, _strong(draw))


def _ghost_look(draw, finding):
    """(translucent material, its line and label material) of a ghost: the "over" red when it does not fit."""
    kind = "over" if draw.get("fits") is False else finding.get("severity")
    return _material(kind, GHOST_ALPHA), _material(kind)


def _draw_ghost(draw, unit, name, finding):
    """A via through the board, a cap's box, a pad's square; a part moved, a route, a row
    of vias by their own builders."""
    builder = {"part": _ghost_part, "track": _ghost_route, "via_row": _ghost_row}.get(draw.get("kind"))
    if builder is not None:
        builder(draw, unit, name, finding)
        return
    at, up = _point(draw["points"][0])
    size = (draw.get("size_nm") or 0) * 1e-9
    kind = draw.get("kind")
    if kind == "via":
        top, bottom = _faces()
        build = _cylinder((size or VIA_GHOST_M) / 2, max(top - bottom, MIN_DEPTH_M))
        middle = Vector((at.x, at.y, (top + bottom) / 2))
    elif kind == "cap":
        length = size or CAP_GHOST_M
        build = _box((length, length / 2, length / 2))
        middle = at + Vector((0.0, 0.0, up * length / 4))
    else:  # pad
        side = size or PAD_GHOST_M
        build = _box((side, side, GHOST_COPPER_M))
        middle = at + Vector((0.0, 0.0, up * GHOST_COPPER_M / 2))
    material, line = _ghost_look(draw, finding)
    turn = Matrix.Rotation(math.radians(draw.get("rotation_deg") or 0.0), 4, "Z")  # KiCad's sense, seen from the top
    _mesh(name, build, Matrix.Translation(middle) @ turn, finding, material)
    _label(ghost_label(draw), at, unit, name + " label", finding, line, _strong(draw))


def _ghost_part(draw, unit, name, finding):
    """A part moved: translucent copies of its pads a hair out from their layers' copper,
    a thin outline of its body and a thin arrow from where it is to where it would go, on
    its side; one label at the new place."""
    points = [point for point in draw.get("points") or () if len(point) >= 2]
    if not points:
        return
    material, line = _ghost_look(draw, finding)
    layer = draw.get("layer") or (points[0][2] if len(points[0]) > 2 else "")
    faces = [(pad.get("layer") or layer, _ring(pad.get("polygon_nm"))) for pad in draw.get("pads_nm") or ()]
    faces = [face for face in faces if len(face[1]) >= 3]

    def pads(bm):
        for pad_layer, ring in faces:
            z, _ = _surface(pad_layer, AREA_LIFT_M)
            bm.faces.new([bm.verts.new((x, y, z)) for x, y in ring])
    if faces:
        try:
            _mesh(name, pads, Matrix.Identity(4), finding, material).show_in_front = True  # over the mask
        except ValueError:  # a pad bmesh cannot make one face of
            pass
    thin = _hairline(unit)
    body = _ring(draw.get("polygon_nm"))
    if len(body) >= 3:
        z, _ = _surface(layer, AREA_LIFT_M + thin)
        _tube(name + " body", [[Vector((x, y, z)) for x, y in (*body, body[0])]], thin, finding, line)
    at = _point([*points[0][:2], layer], thin)[0]
    start = draw.get("from") or ()
    if len(start) >= 2:
        _thin_arrow(name + " arrow", _point([*start[:2], layer], thin)[0], at, thin, unit, finding, line)
    _label(ghost_label(draw), at, unit, name + " label", finding, line, _strong(draw))


def _thin_arrow(name, a, b, thin, unit, finding, material):
    """A thin line from `a` to `b` with a small cone at `b` (none when they meet)."""
    length = (b - a).length
    if length < 1e-9:
        return
    head = min(unit * THIN_HEAD, length * THIN_HEAD_SHARE)
    _arrow(name, a, b, thin, head, thin * GHOST_HEAD, finding, material).show_in_front = True


def _ghost_route(draw, unit, name, finding):
    """A suggested route: a flat translucent ribbon at its width a hair out from its layer's
    copper, round at its ends as KiCad's tracks are (copper, not a tube); its label at the
    route's middle."""
    points = [point for point in draw.get("path_nm") or () if len(point) >= 2]
    if len(points) < 2:
        return
    layer = draw.get("layer") or (points[0][2] if len(points[0]) > 2 else "")
    width = draw.get("width_nm") or TRACK_GHOST_NM
    material, line = _ghost_look(draw, finding)
    segments = [[*a[:2], *b[:2], layer, width] for a, b in zip(points, points[1:])]
    _mesh(name, _ribbons(segments, AREA_LIFT_M), Matrix.Identity(4), finding, material).show_in_front = True
    middle = _along([_point([*point[:2], layer], AREA_LIFT_M)[0] for point in points], 0.5)
    _label(ghost_label(draw), middle, unit, name + " label", finding, line, _strong(draw))


def _ghost_row(draw, unit, name, finding):
    """A row of suggested vias, each through the board as a via ghost is: those that fit in
    one object, those that do not in another in the "over" red, each of those crossed out
    on both faces of the board by a thin X (never colour alone); one label for the row at
    its middle."""
    spots = [spot for spot in draw.get("row") or () if len(spot) >= 2]
    if not spots:
        return
    top, bottom = _faces()
    radius, depth = ((draw.get("size_nm") or 0) * 1e-9 or VIA_GHOST_M) / 2, max(top - bottom, MIN_DEPTH_M)
    for clash in (False, True):
        members = [spot[:2] for spot in spots if (len(spot) > 2 and spot[2] is False) == clash]
        if not members:
            continue
        centres = transform.xy_m(members, board.origin_nm)

        def vias(bm, centres=centres):
            for x, y in centres:
                bmesh.ops.create_cone(bm, cap_ends=True, segments=RING_SEGMENTS, radius1=radius, radius2=radius,
                                      depth=depth, matrix=Matrix.Translation((float(x), float(y), 0.0)))
        _mesh(name + (" over" if clash else ""), vias, Matrix.Translation((0.0, 0.0, (top + bottom) / 2)), finding,
              _material("over" if clash else finding.get("severity"), GHOST_ALPHA))
        if clash:
            thin = _thin(unit, draw)
            arm = radius * CROSS
            crosses = [[Vector((float(x) - dx * arm, float(y) - arm, z)), Vector((float(x) + dx * arm, float(y) + arm, z))]
                       for x, y in centres for z in (top + thin, bottom - thin) for dx in (1.0, -1.0)]
            _tube(name + " over marks", crosses, thin, finding, _material("over"))
    layer = next((point[2] for point in draw.get("points") or () if len(point) > 2 and point[2]), "F.Cu")
    ends = (spots[0], spots[-1])
    middle = _point([(ends[0][0] + ends[1][0]) / 2, (ends[0][1] + ends[1][1]) / 2, layer])[0]
    _label(ghost_label(draw), middle, unit, name + " label", finding, _ghost_look(draw, finding)[1],
           _strong(draw))


def _ring(polygon):
    """A polygon [[x_nm, y_nm], ...] as Blender (x, y) points, repeats and the closing point dropped."""
    points = []
    if not polygon or len(polygon) < 3:
        return points
    for x, y in transform.xy_m([point[:2] for point in polygon], board.origin_nm):
        point = (float(x), float(y))
        if not points or point != points[-1]:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    return points


def _face(name, points, z, finding, material):
    """One n-gon over Blender (x, y) points at height z, or None (a polygon bmesh cannot make one face of)."""
    def build(bm):
        bm.faces.new([bm.verts.new((x, y, 0.0)) for x, y in points])
    try:
        return _mesh(name, build, Matrix.Translation((0.0, 0.0, z)), finding, material)
    except ValueError:
        return None


def _draw_area(draw, unit, name, finding):
    """A polygon area (an area on targets is their highlight)."""
    points = _ring(draw.get("polygon_nm"))
    if len(points) < 3:
        return
    z, _ = _surface(draw.get("layer"), AREA_LIFT_M)
    if _face(name, points, z, finding, _material(finding.get("severity"), GHOST_ALPHA)) is None:
        return
    if draw.get("label"):
        x = sum(p[0] for p in points) / len(points)
        y = sum(p[1] for p in points) / len(points)
        _label(draw["label"], Vector((x, y, z)), unit, name + " label", finding, _material(finding.get("severity")),
               _strong(draw))


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
                bmesh.ops.create_circle(bm, cap_ends=True, segments=END_SEGMENTS, radius=half,
                                        matrix=Matrix.Translation(end))
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
    for over, lift in ((False, AREA_LIFT_M), (True, NARROW_LIFT_M)):
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
        centre, _ = _point([*at[:2], layer], CALLOUT_LIFT_M + radius)
        side = Vector((-along.y, along.x, 0.0))
        tick, poke = along * radius * TICK, side * radius * TICK
        a, b = centre - side * half, centre + side * half
        _tube(name + " callout", [[a - poke, b + poke], [a - tick, a + tick], [b - tick, b + tick]], radius, finding,
              _plain("text"))
    else:
        centre, _ = _point(at)
    _label(label_text(draw), centre, unit, name + " label", finding, material, _strong(draw),
           icon=_icon(draw))


def _path(points, lift):
    """A copper path [x_nm, y_nm, layer] in Blender, `lift` out from each layer's copper:
    where the layer changes it reaches the next point on the old layer, then steps
    through the board (a via)."""
    line = []
    for previous, point in zip([None, *points], points):
        if previous is not None and len(previous) > 2 and len(point) > 2 and previous[2] != point[2]:
            line.append(_point([*point[:2], previous[2]], lift)[0])
        line.append(_point(point, lift)[0])
    return [point for i, point in enumerate(line) if not i or (point - line[i - 1]).length > 1e-12]


def _along(line, share):
    """The point `share` of the way along a polyline."""
    left = sum((b - a).length for a, b in zip(line, line[1:])) * share
    for a, b in zip(line, line[1:]):
        step = (b - a).length
        if step >= left and step > 0:
            return a.lerp(b, left / step)
        left -= step
    return line[-1].copy()


def _draw_routed_length(draw, unit, name, finding):
    """A thin line along the copper path just out from it, through the board at a via, a
    dot at each end, its length labelled at the path's middle."""
    points = [point for point in draw.get("path_nm") or () if len(point) >= 2]
    if len(points) < 2:
        return
    radius = _thin(unit, draw)
    line = _path(points, radius)
    material = _material(_judged(draw, finding))
    _tube(name, [line], radius, finding, material)

    def dot(bm):
        bmesh.ops.create_uvsphere(bm, u_segments=END_SEGMENTS, v_segments=END_SEGMENTS // 2, radius=radius * DOT)
    for end, at in (("start", line[0]), ("end", line[-1])):
        _mesh(f"{name} {end}", dot, Matrix.Translation(at), finding, material).show_in_front = True
    _label(label_text(draw), _along(line, 0.5), unit, name + " label", finding, material,
           _strong(draw), icon=_icon(draw))


def _bar_name(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= BAR_NAME else text[:BAR_NAME - 1] + "…"


def _draw_bars(draw, unit, name, finding):
    """A bar chart at the anchor: a label (its title and, when the bars share one, the
    limit) with the chart under it, drawn with its plate (_chart_parts)."""
    bars = [{"name": _bar_name(bar.get("label")), "value": bar["value"], "limit": bar.get("limit"),
             "over": bar.get("over")} for bar in draw.get("bars") or ()
            if isinstance(bar.get("value"), (int, float)) and not isinstance(bar.get("value"), bool)]
    if not bars or not draw.get("points"):
        return
    at, _ = _point(draw["points"][0])
    shared, unit_name = common_limit(bars), draw.get("unit") or ""
    second = f"limit {figure(shared)} {unit_name}" if shared is not None else (f"in {unit_name}" if unit_name else "")
    title = what_text(draw) + (f"\n{second.rstrip()}" if second else "")
    obj = _label(title, at, unit, name, finding, _material(finding.get("severity")), _strong(draw))
    obj[CHART] = json.dumps(bars)


def _draw_layer_change(draw, unit, name, finding):
    """The via (highlighted, `highlighted`) and a measured thin line on its layer to the
    nearest return via, red past the limit; the other return vias ringed; a thin circle of the limit
    round the via: where a return via must be. Without one, its label at the via says none
    was found. Where one should go is the LLM's to suggest (a ghost)."""
    points = [point for point in draw.get("points") or () if len(point) >= 2]
    if not points:
        return
    via, strong = points[0], _strong(draw)
    layer = via[2] if len(via) > 2 else ""
    if len(points) > 1:  # the return via on the via's layer: the distance across the board, not through it
        _draw_distance(dict(draw, points=[via, [*points[1][:2], layer]]), unit, name, finding, thin=True)
    else:
        _label(f"{what_text(draw)}\nnone found", _point(via)[0], unit, name + " label", finding, _material("over"),
               strong)
    nearest = tuple(float(v) for v in points[1][:2]) if len(points) > 1 else None
    _via_rings([found for found in draw.get("returns") or ()
                if len(found) >= 2 and tuple(float(v) for v in found[:2]) != nearest], layer, unit, draw,
               name + " returns", finding)
    if draw.get("limit_nm"):
        thin = _hairline(unit)
        centre, _ = _point([*via[:2], layer], thin)
        _tube(name + " limit", [_circle(centre, draw["limit_nm"] * 1e-9, RING_SEGMENTS * 2)], thin, finding,
              _material(finding.get("severity")))


def _via_rings(vias, layer, unit, draw, name, finding):
    """Vias [x_nm, y_nm, size_nm, id] ringed on `layer` (RING times their radius), thin lines."""
    rings = [(found[:2], (found[2] * 1e-9 if len(found) > 2 and found[2] else VIA_GHOST_M) / 2 * RING)
             for found in vias]
    if not rings:
        return
    radius = _thin(unit, draw)
    lines = [_circle(_point([*xy, layer], radius)[0], ring) for xy, ring in rings]
    _tube(name, lines, radius, finding, _material(finding.get("severity")))


def _plane_edges(draw, unit, name, finding):
    """The plane's edges near a gap as tubes on its layer, a hair out from its copper; the
    middle of the longest (Blender), or None."""
    layer = draw.get("layer") or ""
    raw = [[point[:2] for point in line if len(point) >= 2] for line in draw.get("edges_nm") or ()]
    raw = [line for line in raw if len(line) > 1]
    if not raw:
        return None

    def placed(line, lift):
        return [_point([*point, layer], lift)[0] for point in line]
    lengths = [sum((b - a).length for a, b in zip(flat, flat[1:])) for flat in (placed(line, 0.0) for line in raw)]
    radius = _hairline(unit)
    lines = [placed(line, radius) for line in raw]
    _tube(name + " edges", lines, radius, finding, _material(finding.get("severity")))
    return _along(lines[lengths.index(max(lengths))], 0.5)


def _gap_spans(draw, unit, name, finding):
    """A draw's stretches without plane under them: "over" red ribbons at their width a
    hair out from their copper, a tick across each end (a gap's span, as a dimension);
    the middle of the longest (Blender), or None."""
    spans = [span for span in draw.get("spans_nm") or () if len(span) >= 6]
    if not spans:
        return None
    material = _material("over")
    lift = NARROW_LIFT_M
    backed = [span for span in draw.get("backed_nm") or () if len(span) >= 6]
    bare = [span for span in draw.get("bare_nm") or () if len(span) >= 6] if backed else spans
    if bare:
        ribbon = _mesh(name + " spans", _ribbons(bare, lift), Matrix.Identity(4), finding, material)
        ribbon.show_in_front = True  # an inner layer's too, as the tubes
    if backed:  # a stripline's other plane under them: faded red, and the label says so (`side_text`)
        ribbon = _mesh(name + " spans backed", _ribbons(backed, lift), Matrix.Identity(4), finding,
                       _material("over", GHOST_ALPHA))
        ribbon.show_in_front = True
    radius = _thin(unit, draw)
    ticks = []
    for x0, y0, x1, y1, layer, width in (span[:6] for span in spans):
        a, b = _point([x0, y0, layer], lift + radius)[0], _point([x1, y1, layer], lift + radius)[0]
        along = b - a
        side = Vector((-along.y, along.x, 0.0)).normalized() if along.length > 1e-12 else Vector((0.0, 1.0, 0.0))
        reach = max(width * 1e-9 / 2 * SPAN_TICK, radius * TICK)
        ticks += [[end - side * reach, end + side * reach] for end in (a, b)]
    _tube(name + " span ends", ticks, radius, finding, material)
    return _longest_middle(spans, lift + radius)


def _longest_middle(spans, lift):
    """The middle of the longest of [x0, y0, x1, y1, layer, ...] stretches, `lift` out from its copper."""
    x0, y0, x1, y1, layer = max(spans, key=lambda span: math.hypot(span[2] - span[0], span[3] - span[1]))[:5]
    return _point([(x0 + x1) / 2, (y0 + y1) / 2, layer], lift)[0]


def _draw_plane_gap(draw, unit, name, finding):
    """The broken part of the plane, not the track: its edges near the gap lit up on its
    layer and named; the crossing's stretches without plane under them red, the gap
    labelled at the longest."""
    strong = _strong(draw)
    edge = _plane_edges(draw, unit, name, finding)
    if edge is not None:
        plane = next(iter(draw.get("planes") or ()), "") or draw.get("layer") or ""
        _label("Plane edge" + (f"\n{plane}" if plane else ""), edge, unit, name + " edges label", finding,
               _material(finding.get("severity")), strong)
    at = _gap_spans(draw, unit, name, finding)
    if at is None and draw.get("points"):
        at = _point(draw["points"][0])[0]
    if at is not None:
        _label(_with_sides(what_text(draw), measured_text(draw), draw), at, unit, name + " label", finding,
               _material("over"), strong)


def _draw_return_path(draw, unit, name, finding):
    """The signal's stretches with plane under them as faint ribbons, those without it
    red (_gap_spans); one label, the length without plane and the share covered."""
    covered = [span for span in draw.get("covered_nm") or () if len(span) >= 6]
    if covered:
        ribbon = _mesh(name + " covered", _ribbons(covered, AREA_LIFT_M), Matrix.Identity(4), finding,
                       _material(finding.get("severity"), GHOST_ALPHA))
        ribbon.show_in_front = True
    _plane_edges(draw, unit, name, finding)  # when the bridge sends any
    at = _gap_spans(draw, unit, name, finding)
    if at is None and covered:
        at = _longest_middle(covered, AREA_LIFT_M)
    if at is None:
        return
    share = covered_text(draw)
    second = " ".join(text for text in (measured_text(draw), f"({share})" if share else "") if text)
    _label(_with_sides(what_text(draw), second, draw), at, unit, name + " label", finding,
           _material("over" if draw.get("spans_nm") else finding.get("severity")), _strong(draw))


def _label_spot(points):
    """A point inside a polygon (Blender (x, y) points) to name it at: its centroid, else
    (a C shape) the corner nearest it."""
    xy = np.array(points)
    x, y = xy[:, 0], xy[:, 1]
    nx, ny = np.roll(x, -1), np.roll(y, -1)
    cross = x * ny - nx * y
    area = cross.sum() / 2
    centre = (xy.mean(axis=0) if abs(area) < 1e-18 else
              np.array(((x + nx) @ cross, (y + ny) @ cross)) / (6 * area))
    crosses = ((y > centre[1]) != (ny > centre[1])) & \
        (centre[0] < x + (centre[1] - y) * (nx - x) / np.where(ny != y, ny - y, 1e-30))
    if np.count_nonzero(crosses) % 2:
        return float(centre[0]), float(centre[1])
    nearest = xy[np.argmin(np.hypot(x - centre[0], y - centre[1]))]
    return float(nearest[0]), float(nearest[1])


def _draw_plane_islands(draw, unit, name, finding):
    """Each piece of the plane a translucent area on its layer in its own colour, one not
    tied to the main plane outlined red; labels "Island 2, In1.Cu" over "4.1 mm², not tied" for the
    main one, then those not tied, then the largest (ISLAND_LABELS), and one with the count."""
    strong = _strong(draw)
    radius = _hairline(unit)
    placed = []
    for number, island in enumerate(draw.get("islands") or (), start=1):
        points = _ring(island.get("polygon_nm"))
        if len(points) < 3:
            continue
        index = island.get("index") or number
        z, up = _surface(island.get("layer") or draw.get("layer"), AREA_LIFT_M)
        slot = (index - 1) % len(ISLAND_COLORS)
        material = materials.make(f"KLS finding island {slot + 1}", ISLAND_COLORS[slot], GHOST_ALPHA)
        materials.paint(material, ISLAND_COLORS[slot])
        area = _face(f"{name} island {index}", points, z, finding, material)
        if area is None:
            continue
        area.show_in_front = True  # an inner layer's too
        loose = not island.get("main") and not island.get("tied")
        if loose:
            outline = [Vector((x, y, z + up * radius)) for x, y in (*points, points[0])]
            _tube(f"{name} island {index} outline", [outline], radius, finding, _material("over"))
        placed.append((island, index, loose, Vector((*_label_spot(points), z))))
    if not placed:
        return
    placed.sort(key=lambda entry: (not entry[0].get("main"), not entry[2], -(entry[0].get("area_mm2") or 0.0)))
    for island, index, loose, at in placed[:ISLAND_LABELS]:
        title = f"Island {index}, {island['layer']}" if island.get("layer") else f"Island {index}"  # main per layer
        _label(f"{title}\n{island_text(island)}", at, unit, f"{name} island {index} label", finding,
               _material("over" if loose else finding.get("severity")), strong)
    _label(f"{what_text(draw)}\n{islands_text(draw)}", placed[0][3], unit, name + " label", finding,
           _material(finding.get("severity")), strong)


def _draw_stitching(draw, unit, name, finding):
    """The stitching vias ringed on the draw's layer; each stretch of the line wider than the
    limit (`edges_nm`, the widest first) a thin line along the pour edge with a tick at each
    end, red past the limit, the widest labelled with its length. Without stretches, the
    label at the first via (or the line's start)."""
    layer = draw.get("layer") or ""
    vias = [via for via in draw.get("returns") or () if len(via) >= 2]
    _via_rings(vias, layer, unit, draw, name + " vias", finding)
    thin = _hairline(unit)
    material = _material(_judged(draw, finding))
    stretches = []
    for raw in draw.get("edges_nm") or ():
        line = [_point([*point[:2], layer], thin)[0] for point in raw if len(point) >= 2]
        if len(line) > 1:
            stretches.append(line)
    strong = _strong(draw)
    if stretches:
        ticks = []
        for line in stretches:  # square to the line at each end, along the board
            for end, toward in ((line[0], line[1]), (line[-1], line[-2])):
                along = (toward - end).normalized()
                across = Vector((-along.y, along.x, 0.0)) * thin * TICK * 2
                ticks.append([end - across, end + across])
        _tube(name, [*stretches, *ticks], thin, finding, material)
        _label(label_text(draw), _along(stretches[0], 0.5), unit, name + " label", finding, material, strong,
               icon=_icon(draw))
        return
    spots = [*(draw.get("points") or ()), *vias]
    if spots:
        _label(label_text(draw), _point([*spots[0][:2], layer])[0], unit, name + " label", finding, material, strong,
               icon=_icon(draw))


def _draw_stub(draw, unit, name, finding):
    """The via's barrel just around the real one: the stub (past the last layer used) a red
    cylinder between its layers' copper, the used part a faint one; labelled with the
    stub's length at its middle, or "none" at the used part's."""
    barrel = draw.get("barrel") or {}
    points = [point for point in draw.get("points") or () if len(point) >= 2]
    xy = [barrel["x"], barrel["y"]] if barrel.get("x") is not None and barrel.get("y") is not None else \
        points[0][:2] if points else None
    if xy is None:
        return
    x, y = (float(value) for value in transform.xy_m([xy], board.origin_nm)[0])
    radius = ((barrel.get("size_nm") or 0) * 1e-9 or VIA_GHOST_M) / 2 * BARREL
    middles = {}
    for part, material in (("used", _material(finding.get("severity"), GHOST_ALPHA)), ("stub", _material("over"))):
        layers = [layer for layer in barrel.get(part) or () if layer]
        if len(layers) < 2:
            continue
        low, high = sorted(_surface(layer)[0] for layer in layers[:2])
        middles[part] = Vector((x, y, (low + high) / 2))
        _mesh(name + (" used" if part == "used" else ""), _cylinder(radius, max(high - low, MIN_DEPTH_M)),
              Matrix.Translation(middles[part]), finding, material).show_in_front = True  # inside the board
    stub = stubbed(draw)
    at = middles.get("stub" if stub else "used") or next(iter(middles.values()), None) or _point([*xy, ""])[0]
    _label(label_text(draw) if stub else f"{what_text(draw)}\nnone", at, unit, name + " label", finding,
           _material("over" if stub else finding.get("severity")), _strong(draw))


def _draw_discontinuity(draw, unit, name, finding):
    """A small marker per mark on its layer, its shape by its kind (never colour alone): a
    short bar for a width step, a ring for a layer change, a diamond for a reference change
    (any other kind); the first MARK_LABELS labelled with their kind over their text."""
    half = unit * MARK / 2
    radius = _thin(unit, draw)
    material = _material(finding.get("severity"))
    labelled = 0
    for number, change in enumerate(draw.get("marks") or (), start=1):
        if change.get("x") is None or change.get("y") is None:
            continue
        centre, _ = _point([change["x"], change["y"], change.get("layer") or draw.get("layer") or ""], radius)
        kind = change.get("kind")
        if kind == "layer":
            line = _circle(centre, half)
        else:
            corners = ((-half, 0.0), (half, 0.0)) if kind == "width" else \
                ((half, 0.0), (0.0, half), (-half, 0.0), (0.0, -half), (half, 0.0))
            line = [centre + Vector((dx, dy, 0.0)) for dx, dy in corners]
        _tube(f"{name} mark {number}", [line], radius, finding, material)
        if labelled < MARK_LABELS:
            title = MARK_TITLES.get(kind, what_text(draw))
            _label(title + (f"\n{change['text']}" if change.get("text") else ""), centre, unit,
                   f"{name} mark {number} label", finding, material, _strong(draw))
            labelled += 1


# highlight, isolate and an area on targets are the finding's highlight (`highlighted`):
# nothing of their own; layers is a layer filter (`refresh`, layers.set_filter)
# --- A part lifted -----------------------------------------------------------------------------

def _draw_lift(draw, unit, name, finding):
    """A part lifted off its pads: copies of its visible model parts (else its placeholder),
    turned `rotation_deg` about the line through `about` across the direction to `toward`,
    so the `toward` end rises out from the board; the part itself hidden meanwhile
    (`_restore_lifted` shows it again on the next refresh)."""
    points = draw.get("points") or []
    if len(points) < 2 or not draw.get("ids"):
        return
    pivot, up = _point(points[0])
    toward, _ = _point(points[1])
    along = Vector((toward.x - pivot.x, toward.y - pivot.y, 0.0))
    if along.length < 1e-9:
        return
    axis = along.normalized().cross(Vector((0.0, 0.0, up)))  # turning about it lifts the toward end
    angle = math.radians(float(draw.get("rotation_deg") or LIFT_DEG))
    turn = Matrix.Translation(pivot) @ Matrix.Rotation(angle, 4, axis) @ Matrix.Translation(-pivot)
    bpy.context.view_layer.update()  # the parts' world matrices as placed now
    world_to_root = poses.to_root()
    count = 0
    for item_id in draw["ids"]:
        for original in _part_objects(item_id):
            copy = original.copy()
            copy.name = f"{name} {count}"
            count += 1
            copy.parent = None
            copy.animation_data_clear()
            for key in ("kls_model_fp_id", "kls_footprint_placeholder", "kls_footprint_id", "kls_side",
                        "kls_model_part", "kls_model_local_matrix", "kls_flat_matrix", "kls_folded_matrix",
                        "kls_layer_hidden"):  # a drawing, not a part: nothing else places or folds it
                copy.pop(key, None)
            copy[TAG] = finding.get("key", "")
            link_owned(copy, "findings")
            poses.set_in_root(copy, turn @ poses.in_root(original, world_to_root))
            hide(original, True)
            original.hide_render = True
            original["kls_lift_hidden"] = 1
            _state["lifted"].append(original.name)


def _lift_listed(shown):
    """Every listed finding's lift draws, any source: the parts standing as they would
    without each finding being shown (the Lifted parts toggle). `shown`: the finding the
    builders draw (its own lift is theirs), or None. A part two findings lift stands once."""
    from . import findings  # it imports this module
    if not getattr(bpy.context.scene, "kileido_lift_parts", True):
        return
    number = 0
    for name, _ in findings.SOURCES:
        for finding in findings.listed(name):
            if shown is not None and (name, finding.get("key")) == tuple(board.findings_shown):
                continue
            for draw in finding.get("draws", ()):
                if draw.get("ok") and draw.get("tool") == "lift":
                    number += 1
                    _draw_lift(draw, 0.0, f"KLS lifted {number}", finding)


def _lifts_wanted():
    """Whether the Lifted parts toggle has parts to lift: a listed finding with a lift draw."""
    from . import findings
    if board.collection is None or not getattr(bpy.context.scene, "kileido_lift_parts", True):
        return False
    return any(draw.get("ok") and draw.get("tool") == "lift"
               for name, _ in findings.SOURCES for finding in findings.listed(name)
               for draw in finding.get("draws", ()))


def _part_objects(footprint_id):
    """A part's objects as shown: its bound model parts, else its placeholder box."""
    parts = [lookup(name) for name in board.model_objects_by_fp.get(footprint_id, ())]
    parts = [obj for obj in parts if obj is not None and not obj.hide_get()]
    if parts:
        return parts
    box = components.find(components.PLACEHOLDER, footprint_id)
    return [box] if box is not None and not box.hide_get() else []


def _restore_lifted():
    """The parts hidden under lifted copies (`_draw_lift`) shown again, as KiCad shows them."""
    from . import models  # (it imports this module's neighbours)
    _state["lifted"] = []
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_lift_hidden") is None:
            continue
        del obj["kls_lift_hidden"]
        footprint_id = obj.get("kls_footprint_id") or obj.get("kls_model_fp_id") or ""
        frame = obj.parent if obj.parent is not None and obj.parent.get("kls_footprint") == 1 else \
            components.find(components.FRAME, footprint_id)
        if obj.get("kls_footprint_placeholder") == 1:
            set_visible(obj, models.placeholder_visible(frame, footprint_id))
        else:
            set_visible(obj, models.model_is_visible(frame, obj))


def rehide_lifted():
    """After a footprints update (apply): a part a lifted copy stands in for stays hidden
    (placing a moved part shows its objects again; the next findings frame redraws)."""
    for name in _state["lifted"]:
        obj = lookup(name)
        if obj is not None and obj.get("kls_lift_hidden") is not None:
            hide(obj, True)
            obj.hide_render = True


_BUILDERS = {"label": _draw_label, "distance": _draw_distance, "arrow": _draw_arrow, "ghost": _draw_ghost,
             "area": _draw_area, "clearance": _draw_distance, "edge_gap": _draw_distance, "width": _draw_width,
             "routed_length": _draw_routed_length, "bars": _draw_bars, "layer_change": _draw_layer_change,
             "plane_gap": _draw_plane_gap, "return_path": _draw_return_path, "plane_islands": _draw_plane_islands,
             "stitching": _draw_stitching, "stub": _draw_stub, "discontinuity": _draw_discontinuity,
             "lift": _draw_lift}


# --- Framing the views ----------------------------------------------------------------------

FRAME_MARGIN = 2.5  # of the framed extent: the view's distance, room around the finding
FRAME_SIDE_ON = 1e-3  # a view this close to edge-on sees both sides: left as it is
FRAME_PART_CLEARANCE_M = 1e-3  # how far outside a part's box the framed eye stays


def frame_bounds(xmin, ymin, xmax, ymax, side=None):
    """Centre every 3D view on a box (Blender metres) and fit it with room around it,
    keeping each view's direction: a finding shown from its column. With `side`
    (+1 top, -1 bottom), a view looking from the other side is mirrored through the board."""
    extent = label_place.extent((xmin, ymin, xmax, ymax))  # a hole gap is a fraction of a millimetre: at least FRAME_MIN_M
    boxes = part_boxes()
    to_world = poses.to_world()
    middle = to_world @ Vector(((xmin + xmax) / 2, (ymin + ymax) / 2, board.thickness_m / 2))
    if side is not None and to_world.col[2].z < 0:  # the board turned over: its top looks down
        side = -side
    for space in view3d_spaces():
        view = space.region_3d
        if side is not None:
            _look_from(view, side)
        view.view_location = middle
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


def part_boxes(to=None):
    """Boxes (low, high) of the shown parts' models and placeholder boxes: in world space, or
    in the space the matrix `to` takes world points to."""
    boxes = []
    for obj in board.collection.all_objects if board.collection is not None else ():
        key = components.key_of(obj)
        if key is None or key[0] == components.FRAME or not obj.visible_get():
            continue
        matrix = obj.matrix_world if to is None else to @ obj.matrix_world
        corners = [matrix @ Vector(corner) for corner in obj.bound_box]
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
