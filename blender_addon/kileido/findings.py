"""The DRC column (its bookmark tab, columns.py): the bridge's findings, listed.

The bridge sends one `findings` frame with two sources (kileido_bridge/findings.py builds
each): "drc", KiCad's own DRC run on a copy of the board (the KiCad DRC button), and
"file", what a tool or script wrote into the project's .kileidoscope folder. Each is listed
in its own section, grouped by check, errors first. A finding's title shows it
(findings_draw: its highlight, label, distance, ...) and frames the 3D views on it; its
buttons select its items in KiCad, confirm it or dismiss it; a hole finding also cuts the
board open through both holes (hole_cut.py). Everything in a finding is plain text from the
file: shown as labels, never run or opened.
"""

import bpy
import bpy.utils.previews
from bpy.props import EnumProperty, StringProperty

from . import findings_draw, fold, hole_cut
from .state import board

SOURCES = (("drc", "KiCad DRC"), ("file", "File"))
SEVERITY_ICONS = {"error": "ERROR", "warning": "ERROR", "info": "INFO", "unknown": "QUESTION"}  # without previews
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
FOLD_PAST = 20  # a source with more findings starts with its groups folded
PROBLEM_LINES = 3  # a file's problems shown; the rest counted
UNITS = (("_mm2", "mm²"), ("_cm2", "cm²"), ("_mm", "mm"), ("_ps", "ps"), ("_mv", "mV"),
         ("_a", "A"), ("_pct", "%"))  # longest first; as the bridge's findings.UNITS
STATES = {"confirm": "confirmed", "dismiss": "dismissed"}
MEASURED = ("distance", "clearance", "edge_gap", "width")  # a measured value against a limit
ICON_PX = 32  # a severity icon's size, drawn 4 x 4 times finer and averaged
_icons = None  # bpy.utils.previews: the severity icons, made on first use


def severity_icon(severity):
    """Layout keywords for a severity's icon in the column: coloured as its 3D label's
    (findings_draw.ICONS: a red circle "!", a yellow triangle "!", a blue circle "i"; a grey
    "?" for "unknown", a finding not confirmed), else Blender's own white one."""
    global _icons
    if severity not in findings_draw.ICONS:
        severity = "warning"
    try:
        if _icons is None:
            _icons = bpy.utils.previews.new()
        if severity not in _icons:
            _paint_icon(_icons.new(severity), severity)
        if _icons[severity].icon_id:  # 0 in background Blender: no icons there
            return {"icon_value": _icons[severity].icon_id}
    except (AttributeError, RuntimeError, KeyError):
        pass
    return {"icon": SEVERITY_ICONS[severity]}


def _paint_icon(preview, kind):
    pixels = findings_draw.icon_pixels(kind, ICON_PX).copy()
    pixels[..., :3] *= pixels[..., 3:4]  # Blender's float previews are premultiplied: else the icon's colour bleeds into a square
    for prefix in ("image", "icon"):
        setattr(preview, f"{prefix}_size", (ICON_PX, ICON_PX))
        getattr(preview, f"{prefix}_pixels_float").foreach_set(pixels.ravel())


def release_icons():
    """The severity icons freed (the add-on uninstalled)."""
    global _icons
    if _icons is not None:
        bpy.utils.previews.remove(_icons)
        _icons = None


def apply_findings(header):
    """A `findings` frame: the newest list replaces the last; the shown finding is drawn
    again from it (or hidden, when it is gone)."""
    board.findings = header.get("findings") or {}
    findings_draw.refresh()  # the board's markers placed again too
    hole_cut.follow(findings_draw.shown_finding())  # a hole finding gone: the user's cut back


def source(name):
    """One source's payload ({} when the bridge sent none)."""
    return ((board.findings or {}).get("sources") or {}).get(name) or {}


def find(source_name, key):
    return next((finding for finding in source(source_name).get("findings", ()) if finding.get("key") == key), None)


def open_group(scene, source_name, check):
    """Unfold a check's group in a source's list (a long list starts with its groups folded)."""
    visible = [finding for finding in source(source_name).get("findings") or ()
               if scene.kileido_findings_dismissed or finding.get("state") != "dismissed"]
    tag = f"{source_name}:{check}"
    toggled = set(filter(None, scene.kileido_findings_open.split("\n")))
    toggled = toggled | {tag} if len(visible) > FOLD_PAST else toggled - {tag}  # toggled: the other way
    scene.kileido_findings_open = "\n".join(sorted(toggled))


def dismissed_toggled(scene):
    """"Show dismissed" switched: a shown finding the list now hides is hidden with it. The
    list redraws on its own; nothing drawn changes otherwise."""
    if scene.kileido_findings_dismissed:
        return
    finding = findings_draw.shown_finding()
    if finding is not None and finding.get("state") == "dismissed":
        findings_draw.clear()
        hole_cut.restore(scene)


def tab_state():
    """"unavailable" without a live link, "on" while a finding is shown, else "off"."""
    from . import live
    if not live.linked():
        return "unavailable"
    return "on" if findings_draw.shown_finding() is not None else "off"


# --- The column -----------------------------------------------------------------------------

def _lines(layout, wrap, text, icon="NONE"):
    for index, line in enumerate(wrap(text)):
        layout.label(text=line, icon=icon if index == 0 else ("BLANK1" if icon != "NONE" else "NONE"))


def draw_column(layout, scene, wrap):
    """The column: the KiCad DRC button and its state, then each source's findings."""
    from . import live
    layout.label(text="DRC")
    found = board.findings or {}
    status = found.get("status", "")
    drc = found.get("drc") or {}
    running = drc.get("state") == "running"
    box = layout.box()
    linked = live.linked()
    if not linked:
        _lines(box, wrap, "KiCad not connected: use Open in Blender in KiCad to check the board", "INFO")
    if status == "unsaved":
        _lines(box, wrap, "Save the board first: DRC runs and findings files live beside it", "ERROR")
    row = box.row()
    row.enabled = linked and status != "unsaved" and not running
    row.operator(KILEIDO_OT_findings_run_drc.bl_idname, text="Running KiCad DRC…" if running else "KiCad DRC",
                 icon="TIME" if running else "PLAY")
    if drc.get("state") == "failed":
        _lines(box, wrap, f"DRC failed: {drc.get('error') or 'no report'}", "ERROR")
    box.prop(scene, "kileido_findings_dismissed", text="Show dismissed")
    markers = box.row(align=True)
    markers.prop(scene, "kileido_markers", text="Markers")
    level = markers.row(align=True)
    level.active = scene.kileido_markers
    level.prop(scene, "kileido_markers_level", expand=True)
    if findings_draw.shown_finding() is not None and fold.folded():
        _lines(box, wrap, "The drawing is hidden while the board is folded: set Fold to 0", "INFO")
    listed = False
    for name, title in SOURCES:
        data = source(name)
        if not data or (data.get("status") in ("", "none") and not data.get("findings")):
            continue  # nothing from this source yet
        listed = True
        _draw_source(layout, scene, wrap, name, title, data)
    if not listed and status != "unsaved":
        folder = found.get("folder") or "the board's .kileidoscope folder"
        _lines(layout, wrap, f"No findings yet. Press KiCad DRC, or put a findings file into {folder}")


def _source_line(name, title, data, findings):
    """The header ("KiCad DRC 10.0.6, run 3: 34 findings, 2 dismissed") and the notes the
    bridge put after the DRC tool's name ("2 library notices left out", "saved file,
    unsaved edits not checked"), each shown on a line of its own under it."""
    dismissed = sum(1 for finding in findings if finding.get("state") == "dismissed")
    tool = data.get("tool") or title
    head, *notes = [part.strip() for part in tool.split(";")] if name == "drc" else [tool]
    head = head or title
    if data.get("run"):
        head += f", run {data['run']}"
    text = f"{head}: {len(findings)} finding{'' if len(findings) == 1 else 's'}"
    return text + (f", {dismissed} dismissed" if dismissed else ""), [note for note in notes if note]


def _draw_source(layout, scene, wrap, name, title, data):
    findings = data.get("findings") or []
    box = layout.box()
    line, notes = _source_line(name, title, data, findings)
    _lines(box, wrap, line)
    for note in notes:
        _lines(box, wrap, note[:1].upper() + note[1:], "INFO")
    status = data.get("status")
    if status in ("error", "unknown_format"):
        _lines(box, wrap, data.get("error") or "The findings file could not be read", "ERROR")
    if data.get("changed"):
        _lines(box, wrap, "Board changed since this check: run it again to be sure", "INFO")
    if status == "ok" and not data.get("connected", True):
        _lines(box, wrap, "KiCad not connected: targets are found once it is", "INFO")
    problems = data.get("problems") or []
    for problem in problems[:PROBLEM_LINES]:
        _lines(box, wrap, problem, "INFO")
    if len(problems) > PROBLEM_LINES:
        box.label(text=f"… and {len(problems) - PROBLEM_LINES} more problems in the file", icon="BLANK1")
    shown_dismissed = scene.kileido_findings_dismissed
    visible = [finding for finding in findings if shown_dismissed or finding.get("state") != "dismissed"]
    groups = {}
    for finding in visible:  # one check together, in the order they come
        groups.setdefault(finding.get("check") or "other", []).append(finding)
    ordered = sorted(groups.items(), key=lambda item: min(SEVERITY_ORDER.get(f.get("severity"), 1) for f in item[1]))
    toggled = set(scene.kileido_findings_open.split("\n"))
    folded = len(visible) > FOLD_PAST
    for check, members in ordered:
        if len(members) == 1:
            _draw_finding(box, wrap, name, members[0])
            continue
        tag = f"{name}:{check}"
        opened = (tag in toggled) == folded  # toggled: the other way from how it starts
        worst = min(members, key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 1)).get("severity")
        header = box.row(align=True)
        header.alert = worst == "error"
        operator = header.operator(KILEIDO_OT_findings_group.bl_idname, text=f"{check} ({len(members)})", emboss=False,
                                   icon="DISCLOSURE_TRI_DOWN" if opened else "DISCLOSURE_TRI_RIGHT")
        operator.group = tag
        if opened:
            indent = box.split(factor=0.04)
            indent.label(text="")
            column = indent.column()
            for finding in members:
                _draw_finding(column, wrap, name, finding)


def _draw_finding(layout, wrap, source_name, finding):
    """One row: severity, title (shows it), select in KiCad, confirm, dismiss; the shown one
    opens below with everything the file says about it."""
    key = finding.get("key", "")
    state = finding.get("state", "")
    shown = board.findings_shown == (source_name, key)
    row = layout.row(align=True)
    row.active = not finding.get("faded") and state != "dismissed"
    title = row.row(align=True)
    title.alert = finding.get("severity") == "error" and state != "confirmed"
    icon = {"icon": "CHECKMARK"} if state == "confirmed" else severity_icon(findings_draw.mark(finding))
    text = (wrap(finding.get("title") or finding.get("check") or "?") or ["?"])[0]
    operator = title.operator(KILEIDO_OT_finding_show.bl_idname, text=text, emboss=shown, depress=shown, **icon)
    operator.source, operator.key = source_name, key
    buttons = row.row(align=True)
    operator = buttons.operator(KILEIDO_OT_finding_kicad.bl_idname, text="", icon="RESTRICT_SELECT_OFF", emboss=False)
    operator.source, operator.key = source_name, key
    for action, icon_name in (("confirm", "CHECKMARK"), ("dismiss", "X")):
        operator = buttons.operator(KILEIDO_OT_finding_state.bl_idname, text="", icon=icon_name,
                                    emboss=state == STATES[action], depress=state == STATES[action])
        operator.source, operator.key, operator.action = source_name, key, action
    if shown:
        _draw_detail(layout.box(), wrap, finding)


def _unit(name):
    """(label, unit) of a value name: "distance_mm" -> ("distance", "mm")."""
    for suffix, unit in UNITS:
        if name.endswith(suffix):
            return name[:-len(suffix)].replace("_", " "), unit
    return name.replace("_", " "), ""


def _number(value):
    return f"{value:.4g}" if isinstance(value, float) else str(value)


def _draw_detail(box, wrap, finding):
    if finding.get("message"):
        _lines(box, wrap, finding["message"])
    if findings_draw.refuted(finding):
        _lines(box, wrap, "Measured within the limit: the measurement does not support this finding", "CHECKMARK")
    for name, value in (finding.get("values") or {}).items():
        label, unit = _unit(name)
        box.label(text=f"{label}: {_number(value)} {unit}".rstrip())
    for draw in finding.get("draws") or ():
        tool = draw.get("tool", "")
        if not draw.get("ok", True):
            _lines(box, wrap, f"{tool} not drawn: {draw.get('problem') or 'not found'}", "ERROR")
        for target in draw.get("targets") or ():
            if not target.get("found", True):
                line = box.column()
                line.active = False
                _lines(line, wrap, f"{target.get('text')}: {target.get('note') or 'not found on board'}", "QUESTION")
        measured = findings_draw.measured_text(draw) if tool in MEASURED else ""
        if measured:  # "Clearance: 0.080 mm < 0.20 mm min"; without a `what`, "Measured …"
            line = box.row()
            line.alert = bool(draw.get("over"))
            line.label(text=f"{draw['what']}: {measured}" if draw.get("what") else f"Measured {measured}",
                       icon="DRIVER_DISTANCE")
        if tool == "width" and draw.get("narrow"):
            _lines(box, wrap, f"{len(draw['narrow'])} of {len(draw.get('segments_nm') or ())} segments too narrow")
        if draw.get("label") and tool != "label":
            _lines(box, wrap, draw["label"])
    for problem in finding.get("problems") or ():
        _lines(box, wrap, problem, "INFO")
    if finding.get("faded"):
        _lines(box, wrap, "Its items are no longer on the board", "INFO")


# --- Operators ------------------------------------------------------------------------------

class KILEIDO_OT_finding_show(bpy.types.Operator):
    bl_idname = "kileido.finding_show"
    bl_label = "Show finding"
    bl_description = ("Draw this finding on the board, frame the views on it and select its items in KiCad; "
                      "click again to hide it")

    source: StringProperty()
    key: StringProperty()

    def execute(self, context):
        from . import live
        if board.findings_shown == (self.source, self.key):
            findings_draw.clear()
            hole_cut.restore()
            if live.linked():
                live.request_select([], False)  # hidden: KiCad's selection of it goes too
            return {"FINISHED"}
        finding = find(self.source, self.key)
        if finding is None:
            return {"CANCELLED"}
        board.findings_shown = (self.source, self.key)
        folded = fold.folded()  # nothing is drawn on a folded board: no cut, no framing either
        hole_cut.show(None if folded else finding)  # a hole finding cut open through both holes; else the user's cut
        findings_draw.refresh()  # after the cut: its labels are written on the cut face
        if not folded:
            frame(finding)
        ids = findings_draw.item_ids(finding)
        if ids and live.linked():  # as a click on a part: selected (and centred) in KiCad too
            live.request_select(ids, False, context.scene.kileido_center_in_kicad, exact=True)
        if fold.folded():
            self.report({"INFO"}, "KiLeidoscope: the drawing shows on the flat board (Fold at 0)")
        return {"FINISHED"}


def frame(finding):
    """Fit the 3D views to a finding's items and points (a hole finding's: its two holes)
    and the labels drawn for it, from the side it is on (a bottom part: from below)."""
    from . import transform
    box = finding.get("bbox_nm")
    ends = hole_cut.hole_ends(finding) if board.collection is not None else None
    if (not box and ends is None) or board.collection is None:
        return
    if ends is not None:
        (x0, y0), (x1, y1) = ends
    else:
        (x0, y0), (x1, y1) = transform.xy_m([box[:2], box[2:]], board.origin_nm)
    x0, y0, x1, y1 = float(min(x0, x1)), float(min(y0, y1)), float(max(x0, x1)), float(max(y0, y1))
    reach = findings_draw.label_reach()
    if reach is not None:
        x0, y0, x1, y1 = min(x0, reach[0]), min(y0, reach[1]), max(x1, reach[2]), max(y1, reach[3])
    findings_draw.frame_bounds(x0, y0, x1, y1, side=None if ends is not None else findings_draw.finding_side(finding))


class KILEIDO_OT_finding_kicad(bpy.types.Operator):
    bl_idname = "kileido.finding_kicad"
    bl_label = "Select in KiCad"
    bl_description = "Select this finding's items in KiCad (and centre KiCad on them)"

    source: StringProperty()
    key: StringProperty()

    @classmethod
    def poll(cls, context):
        from . import live
        if not live.linked():
            cls.poll_message_set("Not live with KiCad")
            return False
        return True

    def execute(self, context):
        from . import live
        finding = find(self.source, self.key)
        ids = findings_draw.item_ids(finding) if finding is not None else []
        if not ids:
            self.report({"WARNING"}, "KiLeidoscope: none of this finding's items is on the board")
            return {"CANCELLED"}
        live.request_select(ids, False, context.scene.kileido_center_in_kicad, exact=True)
        return {"FINISHED"}


class KILEIDO_OT_finding_state(bpy.types.Operator):
    bl_idname = "kileido.finding_state"
    bl_label = "Confirm or dismiss"

    source: StringProperty()
    key: StringProperty()
    action: EnumProperty(items=(("confirm", "Confirm", "A real problem: keep it marked"),
                                ("dismiss", "Dismiss", "Not a problem: hide it from the list")))

    @classmethod
    def description(cls, context, properties):
        finding = find(properties.source, properties.key) or {}
        if finding.get("state") == STATES[properties.action]:
            return f"Take it back: no longer {STATES[properties.action]}"
        if properties.action == "confirm":
            return "Confirm: a real problem (kept in the project's .kileidoscope folder)"
        return "Dismiss: not a problem; hidden from the list until Show dismissed"

    @classmethod
    def poll(cls, context):
        from . import live
        if not live.linked():
            cls.poll_message_set("Not live with KiCad: the bridge keeps these marks")
            return False
        return True

    def execute(self, context):
        from . import live
        finding = find(self.source, self.key)
        if finding is None:
            return {"CANCELLED"}
        state = "" if finding.get("state") == STATES[self.action] else STATES[self.action]
        finding["state"] = state  # at once; the bridge's next frame says the same
        live.request_findings(self.action if state else "reset", self.key, self.source)
        if state == "dismissed" and board.findings_shown == (self.source, self.key) and \
                not context.scene.kileido_findings_dismissed:
            findings_draw.clear()
            hole_cut.restore()
        return {"FINISHED"}


class KILEIDO_OT_findings_run_drc(bpy.types.Operator):
    bl_idname = "kileido.findings_run_drc"
    bl_label = "KiCad DRC"
    bl_description = ("Run KiCad's DRC on a copy of the board as it is now (unsaved edits included) and list "
                      "what it finds here")

    @classmethod
    def poll(cls, context):
        from . import live
        found = board.findings or {}
        if not live.linked():
            cls.poll_message_set("Not live with KiCad")
            return False
        if found.get("status") == "unsaved":
            cls.poll_message_set("Save the board first")
            return False
        if (found.get("drc") or {}).get("state") == "running":
            cls.poll_message_set("DRC is running")
            return False
        return True

    def execute(self, context):
        from . import live
        live.request_findings("run_drc", "", "drc")
        board.findings.setdefault("drc", {})["state"] = "running"  # at once; the bridge says when it is done
        return {"FINISHED"}


class KILEIDO_OT_findings_group(bpy.types.Operator):
    bl_idname = "kileido.findings_group"
    bl_label = "Show or hide"
    bl_options = {"INTERNAL"}

    group: StringProperty()

    @classmethod
    def description(cls, context, properties):
        return "Show or hide the findings of this check"

    def execute(self, context):
        toggled = set(filter(None, context.scene.kileido_findings_open.split("\n")))
        toggled ^= {self.group}
        context.scene.kileido_findings_open = "\n".join(sorted(toggled))
        return {"FINISHED"}


CLASSES = (KILEIDO_OT_finding_show, KILEIDO_OT_finding_kicad, KILEIDO_OT_finding_state, KILEIDO_OT_findings_run_drc,
           KILEIDO_OT_findings_group)
