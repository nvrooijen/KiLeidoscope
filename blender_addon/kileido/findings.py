"""The DRC and LLM columns (their bookmark tabs, columns.py): the bridge's findings, listed.

The bridge sends one `findings` frame with two sources (kileido_bridge/findings.py builds
each): "drc", KiCad's own DRC run on a copy of the board (the KiCad DRC button), listed in
the DRC tab, and "file", what the user's LLM (or a script) wrote into the project's
.kileidoscope folder, listed in the LLM tab. Each is grouped by check, errors first. A finding's title shows it
(findings_draw: its highlight, label, distance, ...) and frames the 3D views on it; its
buttons select its items in KiCad, confirm it or dismiss it; a hole finding also cuts the
board open through both holes (hole_cut.py). Everything in a finding is plain text from the
file: shown as labels, never run or opened.

At the top of the LLM tab, the prompt box: the bridge's check catalogue as tick boxes by
group, notes for the LLM (ready-made sentences to tick, and the user's own line), and Copy
prompt. The bridge freezes a run for the ticked checks and sends the prompt back; the add-on
puts it on the clipboard once, for the user to paste into their LLM tool. Under it, how far
the LLM is (the payload's `progress`, from what it writes into the run's folder): its ages
counted here from the bridge's unix times, a 1 s timer redrawing them while the LLM works.
"""

import time

import bpy
import bpy.utils.previews
from bpy.props import EnumProperty, IntProperty, StringProperty

from . import findings_draw, fold, hole_cut
from .state import board

SOURCES = (("drc", "KiCad DRC"), ("file", "LLM"))
TABS = {"drc": "DRC", "file": "LLM"}  # source -> the bookmark tab (columns.TABS) that lists it
MENTION_ICONS = {"part": "MESH_CUBE", "pad": "MESH_PLANE", "net": "LINKED", "via": "RADIOBUT_OFF"}  # by kind
SEVERITY_ICONS = {"error": "ERROR", "warning": "ERROR", "info": "INFO", "unknown": "QUESTION"}  # without previews
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
FOLD_PAST = 20  # a source with more findings starts with its groups folded
PROBLEM_LINES = 3  # a file's problems shown; the rest counted
UNITS = (("_mm2", "mm²"), ("_cm2", "cm²"), ("_mm", "mm"), ("_ps", "ps"), ("_mv", "mV"),
         ("_a", "A"), ("_pct", "%"))  # longest first; as the bridge's findings.UNITS
STATES = {"confirm": "confirmed", "dismiss": "dismissed"}
MEASURED = ("distance", "clearance", "edge_gap", "width", "routed_length", "layer_change", "plane_gap",
            "return_path", "stitching", "stub")  # a measured value against a limit
ISLAND_LINES = 16  # plane pieces listed; the rest counted
NOTE_TOGGLES = (  # (scene property, the sentence sent): ready-made notes for the LLM, in this order
    ("kileido_note_tools", "Use any KiCad analysis tools or skills you have installed"),
    ("kileido_note_cheap", "Keep it cheap: no sub-agents, as few tokens as possible"),
    ("kileido_note_confident", "Report only findings you are confident about"))
NOTES_MAX = 500  # characters the bridge takes
NOTES_LINES = 8  # lines the bridge takes
NOTES_TAG = "notes"  # the Notes sub-section opened (kileido_findings_open)
STALE_S = 300  # no word from the LLM for this long: it may have stalled, or wait for the user
PROGRESS_TICK_S = 1.0  # the ages redrawn this often while the LLM works
MAKING = "\n"  # board.prompt_wait once the bridge says it is making the prompt asked for (never a run id)
LIVE = ("waiting", "working")  # progress states that age
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
    again from it only when it changed there (a frame that only says how far the LLM is
    redraws nothing), or hidden when it is gone; the board's markers are placed again. A
    prompt asked for, ready: to the clipboard."""
    from . import findings_markers  # it imports this module
    found = header.get("findings") or {}
    if found.get("folder") != (board.findings or {}).get("folder"):  # another board: its runs are numbered apart
        board.prompt_wait, board.prompt_copied = None, ""
    before = findings_draw.shown_finding()
    board.findings = found
    shown = findings_draw.shown_finding()
    if shown is None or shown != before:
        findings_draw.refresh()  # the board's markers placed again too
    else:
        findings_markers.invalidate()
    hole_cut.follow(shown)  # a hole finding gone: the user's cut back
    _take_prompt()
    _follow_progress()


def _take_prompt():
    """The prompt of a new run, once Copy prompt asked for one: copied once, and no other
    after it until Copy prompt is pressed again (a resync sends the payload again). Until
    the bridge says it is making the prompt, the prompt shown when it was pressed is not
    taken for the new one (`prompt_wait` holds its run); once it has said so, the next
    ready run is the new one whatever its number, and a failure ends the wait."""
    prompt = (board.findings or {}).get("prompt") or {}
    run, state = prompt.get("run") or "", prompt.get("state")
    if board.prompt_wait is None:
        return
    if state == "making":
        board.prompt_wait = MAKING
    elif state == "failed" and board.prompt_wait == MAKING:
        board.prompt_wait = None
    elif state == "ready" and run not in ("", board.prompt_wait):
        set_clipboard(prompt.get("text") or "")
        board.prompt_copied, board.prompt_wait = run, None


def notes(scene):
    """What Copy prompt tells the LLM besides the checks: the ticked sentences, one per line,
    then the user's own line; at most NOTES_LINES lines and NOTES_MAX characters."""
    lines = [sentence for name, sentence in NOTE_TOGGLES if getattr(scene, name)]
    lines += [line.strip() for line in scene.kileido_prompt_note.splitlines() if line.strip()]
    return "\n".join(lines[:NOTES_LINES])[:NOTES_MAX].rstrip()


def notes_count(scene):
    """The notes given: each ticked sentence, and the user's line."""
    return sum(1 for name, _ in NOTE_TOGGLES if getattr(scene, name)) + bool(scene.kileido_prompt_note.strip())


def now():
    """The time the ages count to (the tests set it)."""
    return time.time()


def progress():
    """The LLM's progress ({} before the bridge sends it)."""
    return (board.findings or {}).get("progress") or {}


def _age_s(unix):
    """Seconds since a bridge unix time; None without one."""
    try:
        return max(0.0, now() - float(unix)) if unix else None
    except (TypeError, ValueError):
        return None


def _count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def age_text(seconds):
    """"40 s", "5 min", "2 h"."""
    if seconds < 60:
        return f"{int(seconds)} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    return f"{int(seconds // 3600)} h"


def progress_lines(found=None):
    """What the progress line says: [(kind, text, icon, factor)], kind "label", "bar" or
    "alert"; [] when there is nothing to say. Working: "check 2 of 3" is the one after
    the `done` ones."""
    state = (found if found is not None else board.findings or {}).get("progress") or {}
    kind = state.get("state")
    run = state.get("run") or "?"
    if kind == "done":
        return [("label", f"LLM answer for run {run} arrived", "CHECKMARK", None)]
    if kind not in LIVE:
        return []
    if kind == "waiting":
        age = _age_s(state.get("made_unix"))
        lines = [("label", "Waiting for the LLM…" + (f" prompt copied {age_text(age)} ago" if age is not None else ""),
                  "TIME", None)]
    else:
        age = _age_s(state.get("updated_unix"))
        total, done = _count(state.get("total")), _count(state.get("done"))
        text = f"Run {run}: " + (f"check {min(done + 1, total)} of {total}" if total else "working")
        if state.get("check"):
            text += f" — {state['check']}"
        lines = [("bar", text, "NONE", min(done / total, 1.0) if total else 0.0)]
        note = (state.get("note") or "").strip()
        when = f"{age_text(age)} ago" if age is not None else ""
        if note or when:
            lines.append(("label", " — ".join(filter(None, (note, when))), "BLANK1", None))
    if age is not None and age > STALE_S:
        lines.append(("alert", f"No update for {age_text(age)} — the LLM may have stalled or be waiting for your "
                               "approval", "ERROR", None))
    return lines


def _progress_tick():
    """Timer: the sidebars redrawn while the LLM works (its ages count up); stops otherwise."""
    if progress().get("state") not in LIVE:
        return None
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    for region in area.regions:
                        if region.type == "UI":
                            region.tag_redraw()
    except (AttributeError, ReferenceError):
        pass
    return PROGRESS_TICK_S


def _follow_progress():
    """The redraw timer on while the LLM works, off otherwise."""
    if progress().get("state") in LIVE:
        if not bpy.app.timers.is_registered(_progress_tick):
            bpy.app.timers.register(_progress_tick, first_interval=PROGRESS_TICK_S)
    else:
        stop_progress()


def stop_progress():
    """No redraw timer (uninstalled, or a file loaded)."""
    if bpy.app.timers.is_registered(_progress_tick):
        bpy.app.timers.unregister(_progress_tick)


def set_clipboard(text):
    """The system clipboard (background Blender has none: the tests record it here)."""
    bpy.context.window_manager.clipboard = text


def source(name):
    """One source's payload ({} when the bridge sent none)."""
    return ((board.findings or {}).get("sources") or {}).get(name) or {}


def find(source_name, key):
    return next((finding for finding in source(source_name).get("findings", ()) if finding.get("key") == key), None)


def catalogue():
    """The bridge's checks: {"id", "group", "title", "v1"} each, [] before it sends them."""
    return (board.findings or {}).get("catalogue") or []


def chosen(scene):
    """The ticked check ids: the scene's list, or the v1 checks while it is "" (Quick)."""
    stored = scene.kileido_prompt_checks
    if not stored:
        return {row.get("id") for row in catalogue() if row.get("v1")}
    return set(filter(None, stored.split("\n")))


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


def choose(scene, ids):
    """Tick exactly these checks ("\n" holds none: "" would be Quick again)."""
    scene.kileido_prompt_checks = "\n".join(sorted(ids)) or "\n"


def tab_state(tab):
    """"unavailable" without a live link, "on" while one of this tab's findings is shown,
    else "off"."""
    from . import live
    if not live.linked():
        return "unavailable"
    shown = findings_draw.shown_finding() is not None and TABS.get(board.findings_shown[0]) == tab
    return "on" if shown else "off"


# --- The column -----------------------------------------------------------------------------

def _lines(layout, wrap, text, icon="NONE"):
    for index, line in enumerate(wrap(text)):
        layout.label(text=line, icon=icon if index == 0 else ("BLANK1" if icon != "NONE" else "NONE"))


def draw_column(layout, scene, wrap, tab):
    """One tab's column: "DRC", the KiCad DRC button and its findings; "LLM", the prompt box
    and the findings file's findings."""
    from . import live
    layout.label(text=tab)
    found = board.findings or {}
    status = found.get("status", "")
    box = layout.box()
    linked = live.linked()
    if not linked:
        _lines(box, wrap, "KiCad not connected: use Open in Blender in KiCad to check the board", "INFO")
    if status == "unsaved":
        _lines(box, wrap, "Save the board first: DRC runs and findings files live beside it", "ERROR")
    if tab == "DRC":
        drc = found.get("drc") or {}
        running = drc.get("state") == "running"
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
    if tab_state(tab) == "on" and fold.folded():
        _lines(box, wrap, "The drawing is hidden while the board is folded: set Fold to 0", "INFO")
    if tab == "LLM":
        _draw_prompt(layout, scene, wrap, found, linked)
    listed = False
    for name, title in SOURCES:
        data = source(name)
        if TABS[name] != tab or not data or (data.get("status") in ("", "none") and not data.get("findings")):
            continue  # another tab's, or nothing from this source yet
        listed = True
        _draw_source(layout, scene, wrap, name, title, data)
    if not listed and status != "unsaved":
        folder = found.get("folder") or "the board's .kileidoscope folder"
        _lines(layout, wrap, "No findings yet. Press KiCad DRC" if tab == "DRC" else
               f"No findings yet. Copy the prompt into your LLM tool; its answer lands in {folder}")


def _draw_prompt(layout, scene, wrap, found, linked):
    """The LLM prompt box: checks by group (folded), Quick / All, notes (folded), Copy prompt
    and its state, then how far the LLM is."""
    rows = catalogue()
    ticked = chosen(scene) & {row.get("id") for row in rows}
    prompt = found.get("prompt") or {}
    making = prompt.get("state") == "making"
    box = layout.box()
    head = box.row(align=True)
    head.label(text=f"LLM prompt: {len(ticked)} of {len(rows)} checks" if rows else "LLM prompt")
    for preset, text in (("quick", "Quick"), ("all", "All")):
        head.operator(KILEIDO_OT_prompt_preset.bl_idname, text=text).preset = preset
    if not rows:
        _lines(box, wrap, "The checks come from the bridge once KiCad is connected", "INFO")
    toggled = set(scene.kileido_findings_open.split("\n"))
    titles = {group.get("id"): group.get("title") or group.get("id") for group in found.get("groups") or ()}
    for group in dict.fromkeys([*titles, *(row.get("group") for row in rows)]):  # the bridge's order
        members = [row for row in rows if row.get("group") == group]
        if not members:
            continue
        tag = f"prompt:{group}"
        opened = tag in toggled  # folded until opened
        count = sum(1 for row in members if row.get("id") in ticked)
        operator = box.operator(KILEIDO_OT_findings_group.bl_idname, emboss=False,
                                text=f"{titles.get(group) or group} ({count}/{len(members)})",
                                icon="DISCLOSURE_TRI_DOWN" if opened else "DISCLOSURE_TRI_RIGHT")
        operator.group = tag
        if opened:
            indent = box.split(factor=0.04)
            indent.label(text="")
            column = indent.column(align=True)
            for row in members:
                text = (wrap(row.get("title") or row.get("id") or "?") or ["?"])[0]
                operator = column.operator(KILEIDO_OT_prompt_check.bl_idname, text=text, emboss=False,
                                           icon="CHECKBOX_HLT" if row.get("id") in ticked else "CHECKBOX_DEHLT")
                operator.check = row.get("id") or ""
    _draw_notes(box, scene, toggled)
    row = box.row()
    row.enabled = linked and found.get("status") != "unsaved" and not making and bool(ticked)
    row.operator(KILEIDO_OT_findings_prompt.bl_idname, text="Making prompt…" if making else "Copy prompt",
                 icon="TIME" if making else "COPYDOWN")
    run = prompt.get("run") or ""
    if prompt.get("state") == "failed":
        _lines(box, wrap, f"Prompt failed: {prompt.get('error') or 'no reason given'}", "ERROR")
    elif prompt.get("state") == "ready" and run:
        if board.prompt_copied == run:
            _lines(box, wrap, f"Prompt for run {run} copied — paste it into your LLM tool", "CHECKMARK")
        else:
            _lines(box, wrap, f"Prompt for run {run} made", "INFO")
    _draw_progress(box, wrap, found)
    if prompt.get("text") and not making:
        box.prop(scene, "kileido_prompt_show", text="Show prompt")
        if scene.kileido_prompt_show:
            inner = box.box()
            for line in prompt["text"].splitlines():
                if line.strip():
                    _lines(inner, wrap, line)
                else:
                    inner.label(text="")


def _draw_notes(box, scene, toggled):
    """Notes for the LLM, folded until opened: the ready-made sentences, the user's line."""
    opened = NOTES_TAG in toggled
    count = notes_count(scene)
    operator = box.operator(KILEIDO_OT_findings_group.bl_idname, emboss=False,
                            text=f"Notes ({count})" if count else "Notes",
                            icon="DISCLOSURE_TRI_DOWN" if opened else "DISCLOSURE_TRI_RIGHT")
    operator.group = NOTES_TAG
    if not opened:
        return
    indent = box.split(factor=0.04)
    indent.label(text="")
    column = indent.column(align=True)
    for name, sentence in NOTE_TOGGLES:
        column.prop(scene, name, text=sentence)
    column.prop(scene, "kileido_prompt_note", text="", placeholder="Notes for the LLM", icon="GREASEPENCIL")


def _draw_progress(box, wrap, found):
    """How far the LLM is: a bar while it works, how long ago it said so; an alert past
    STALE_S (an icon and words, not colour alone)."""
    for kind, text, icon, factor in progress_lines(found):
        if kind == "bar" and hasattr(box, "progress"):
            box.progress(factor=factor, text=text)
        elif kind == "alert":
            line = box.column()
            line.alert = True
            _lines(line, wrap, text, icon)
        else:
            _lines(box, wrap, text, "TIME" if kind == "bar" else icon)


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


def origin_line(data):
    """Where a findings file came from (the bridge's `handshake`), in plain words: "" for
    an answer to a run this board's KiLeidoscope made."""
    origin = data.get("handshake")
    if origin == "outside":
        tool = (data.get("tool") or "").strip()
        return f"From {tool}, not from a KiLeidoscope prompt" if tool else "Not from a KiLeidoscope prompt"
    if origin == "other_run":
        run = data.get("run")
        named = f"run {run}" if run else "a run"
        return f"Answer to {named}, not one this board's KiLeidoscope made (or the board has changed since)"
    return ""


def _draw_source(layout, scene, wrap, name, title, data):
    findings = data.get("findings") or []
    box = layout.box()
    line, notes = _source_line(name, title, data, findings)
    _lines(box, wrap, line)
    origin = origin_line(data)
    if origin:
        _lines(box, wrap, origin, "INFO")
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
    left_out = data.get("left_out") or {}  # check -> findings the bridge kept back (the worst are shown)
    for check, members in ordered:
        more = left_out.get(check, 0)
        if len(members) == 1 and not more:
            _draw_finding(box, wrap, name, members[0])
            continue
        tag = f"{name}:{check}"
        opened = (tag in toggled) == folded  # toggled: the other way from how it starts
        worst = min(members, key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 1)).get("severity")
        header = box.row(align=True)
        header.alert = worst == "error"
        count = f"{len(members)} of {len(members) + more:,}" if more else f"{len(members)}"
        operator = header.operator(KILEIDO_OT_findings_group.bl_idname, text=f"{check} ({count})", emboss=False,
                                   icon="DISCLOSURE_TRI_DOWN" if opened else "DISCLOSURE_TRI_RIGHT")
        operator.group = tag
        if opened:
            indent = box.split(factor=0.04)
            indent.label(text="")
            column = indent.column()
            for finding in members:
                _draw_finding(column, wrap, name, finding)
            if more:
                where = "all in KiCad's DRC dialog" if name == "drc" else "all in the findings file"
                _lines(column, wrap, f"{more:,} more not shown: {where}", "INFO")
    skipped = data.get("skipped") or []
    if skipped:
        inner = box.box()
        inner.label(text=f"Not checked ({len(skipped)})", icon="INFO")
        for entry in skipped:
            _lines(inner, wrap, f"{entry.get('check') or '?'}: {entry.get('reason') or ''}".rstrip(": "))


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
        _draw_detail(layout.box(), wrap, source_name, finding)


def _unit(name):
    """(label, unit) of a value name: "distance_mm" -> ("distance", "mm"); a rate reads
    "per" its unit: "via_density_per_cm2" -> ("via density", "per cm²")."""
    for suffix, unit in UNITS:
        if name.endswith(suffix):
            label = name[:-len(suffix)]
            if label.endswith("_per") and len(label) > 4:
                return label[:-4].replace("_", " "), f"per {unit}"
            return label.replace("_", " "), unit
    return name.replace("_", " "), ""


def _number(value):
    return f"{value:.4g}" if isinstance(value, float) else str(value)


def _draw_bars(box, wrap, draw):
    """A bars draw as lines: "CLK_P: 12.3 mm"; one past its limit red, with an icon and
    "over the limit" (never colour alone); the shared limit once, else each bar's after
    its value."""
    bars = draw.get("bars") or []
    unit = draw.get("unit") or ""
    shared = findings_draw.common_limit(bars)
    if draw.get("what"):
        _lines(box, wrap, draw["what"])
    for bar in bars:
        own = f" (limit {_number(bar['limit'])})" if shared is None and bar.get("limit") is not None else ""
        over = findings_draw.bar_over(bar)
        line = box.row()
        line.alert = over
        line.label(text=f"{bar.get('label') or '?'}: {_number(bar.get('value'))} {unit}".rstrip() + own +
                   (", over the limit" if over else ""), icon="ERROR" if over else "BLANK1")
    if shared is not None:
        box.label(text=f"limit: {_number(shared)} {unit}".rstrip())


def _draw_islands(box, wrap, draw):
    """A plane_islands draw as lines: "Islands: 5 pieces, 2 not tied", then each piece by
    its number ("2: 4.1 mm², not tied"), red when not tied; past ISLAND_LINES counted."""
    islands = draw.get("islands") or []
    _lines(box, wrap, f"{findings_draw.what_text(draw)}: {findings_draw.islands_text(draw)}", "MESH_PLANE")
    for number, island in enumerate(islands[:ISLAND_LINES], start=1):
        line = box.row()
        line.alert = not island.get("main") and not island.get("tied")
        layer = f" ({island['layer']})" if island.get("layer") and island.get("layer") != draw.get("layer") else ""
        line.label(text=f"{island.get('index') or number}: {findings_draw.island_text(island)}{layer}")
    if len(islands) > ISLAND_LINES:
        box.label(text=f"… and {len(islands) - ISLAND_LINES} more")


def _draw_mentions(box, source_name, finding):
    """A button per name the text mentions (the bridge's `mentions`): selects it in KiCad,
    which highlights it here; a code that names nothing on the board, greyed."""
    found = finding.get("mentions") or ()
    if not found:
        return
    column = box.column(align=True)
    for index, mention in enumerate(found):
        text = f"{mention.get('text')}: {mention.get('what')}" if mention.get("what") else mention.get("text", "")
        if not mention.get("found"):
            line = column.row()
            line.active = False
            line.label(text=text, icon="QUESTION")
            continue
        operator = column.operator(KILEIDO_OT_finding_mention.bl_idname, text=text,
                                   icon=MENTION_ICONS.get(mention.get("kind"), "RESTRICT_SELECT_OFF"))
        operator.source, operator.key, operator.index = source_name, finding.get("key", ""), index


def _draw_detail(box, wrap, source_name, finding):
    for label, text in (("", finding.get("message")), ("Fix: ", finding.get("fix"))):
        if text:
            _lines(box, wrap, label + text)
    _draw_mentions(box, source_name, finding)
    if finding.get("confidence"):
        box.label(text=f"Confidence: {finding['confidence']}")
    if findings_draw.refuted(finding):
        _lines(box, wrap, "Measured within the limit: the measurement does not support this finding", "CHECKMARK")
    disagree = {entry.get("key"): entry for entry in finding.get("disagree") or ()}
    for name, value in (finding.get("values") or {}).items():
        label, unit = _unit(name)
        line = box.row()
        if name in disagree:  # the bridge measured it differently: say both
            line.alert = True
            line.label(text=f"{label}: claimed {_number(disagree[name]['claimed'])} {unit}, measured "
                            f"{_number(disagree[name]['measured'])} {unit}".replace("  ", " "), icon="ERROR")
        else:
            line.label(text=f"{label}: {_number(value)} {unit}".rstrip())
    for name, text in (finding.get("values_text") or {}).items():
        _lines(box, wrap, f"{name.replace('_', ' ')}: {text}")
    for draw in finding.get("draws") or ():
        tool, ok = draw.get("tool", ""), draw.get("ok", True)
        if not ok:
            _lines(box, wrap, f"{tool} not drawn: {draw.get('problem') or 'not found'}", "ERROR")
        elif tool == "ghost":  # the LLM's suggestion: whether it fits, whose verdict that is, what was measured
            for text, icon, alert in findings_draw.ghost_lines(draw):
                line = box.column()
                line.alert = alert
                _lines(line, wrap, text, icon)
            if findings_draw.ghost_line(draw):
                box.label(text=findings_draw.ghost_line(draw), icon="DRIVER_DISTANCE")
        for target in draw.get("targets") or ():
            if not target.get("found", True):
                line = box.column()
                line.active = False
                _lines(line, wrap, f"{target.get('text')}: {target.get('note') or 'not found on board'}", "QUESTION")
        if not ok:
            continue  # nothing was measured: none of its tool's facts
        if tool == "layers" and draw.get("layers"):
            _lines(box, wrap, f"Layers shown: {', '.join(draw['layers'])} (other copper hidden while shown)",
                   "RENDERLAYERS")
        no_stub = tool == "stub" and not findings_draw.stubbed(draw)
        measured = findings_draw.measured_text(draw) if tool in MEASURED and not no_stub else ""
        if measured:  # "Clearance: 0.080 mm < 0.20 mm min"; without a `what`, "Measured …"
            line = box.row()
            line.alert = bool(draw.get("over"))
            line.label(text=f"{draw['what']}: {measured}" if draw.get("what") else f"Measured {measured}",
                       icon="DRIVER_DISTANCE")
        if tool == "layer_change" and len(draw.get("points") or ()) < 2:  # no return via
            line = box.row()
            line.alert = True
            line.label(text=f"{findings_draw.what_text(draw)}: none found", icon="DRIVER_DISTANCE")
        if no_stub:  # its barrel all used
            box.label(text=f"{findings_draw.what_text(draw)}: none", icon="DRIVER_DISTANCE")
        if tool == "return_path" and findings_draw.covered_text(draw):
            box.label(text=findings_draw.covered_text(draw))
        if draw.get("planes"):
            _lines(box, wrap, f"Planes: {', '.join(draw['planes'])}", "MESH_PLANE")
        if tool == "plane_islands":
            _draw_islands(box, wrap, draw)
        if tool == "discontinuity":  # "width: 0.20 -> 0.15 mm", each mark (3D labels only the first few)
            for mark in draw.get("marks") or ():
                _lines(box, wrap, findings_draw.mark_text(mark))
        if tool == "width" and draw.get("narrow"):
            _lines(box, wrap, f"{len(draw['narrow'])} of {len(draw.get('segments_nm') or ())} segments too narrow")
        if tool == "bars":
            _draw_bars(box, wrap, draw)
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


class KILEIDO_OT_finding_mention(bpy.types.Operator):
    bl_idname = "kileido.finding_mention"
    bl_label = "Select in KiCad"
    bl_description = "Select this item in KiCad (and centre KiCad on it); it is highlighted here too"

    source: StringProperty()
    key: StringProperty()
    index: IntProperty()

    @classmethod
    def poll(cls, context):
        from . import live
        if not live.linked():
            cls.poll_message_set("Not live with KiCad")
            return False
        return True

    def execute(self, context):
        from . import live
        found = (find(self.source, self.key) or {}).get("mentions") or ()
        ids = found[self.index].get("ids") if 0 <= self.index < len(found) else None
        if not ids:
            self.report({"WARNING"}, "KiLeidoscope: that item is not on the board")
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


class KILEIDO_OT_findings_prompt(bpy.types.Operator):
    bl_idname = "kileido.findings_prompt"
    bl_label = "Copy prompt"
    bl_description = ("Freeze the board as it is now (unsaved edits included) for the ticked checks, in the "
                      "project's .kileidoscope folder, and copy a prompt for your LLM tool (with your notes) to "
                      "the clipboard")

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
        if (found.get("prompt") or {}).get("state") == "making":
            cls.poll_message_set("The prompt is being made")
            return False
        if not chosen(context.scene) & {row.get("id") for row in catalogue()}:
            cls.poll_message_set("Tick at least one check")
            return False
        return True

    def execute(self, context):
        from . import live
        ticked = chosen(context.scene)
        ids = [row.get("id") for row in catalogue() if row.get("id") in ticked]  # in the catalogue's order
        prompt = board.findings.setdefault("prompt", {})
        board.prompt_wait = prompt.get("run") or ""  # that run's prompt, sent again, is not copied
        live.request_findings("prompt", "", "file", checks=ids, notes=notes(context.scene))
        prompt.update(state="making", error="")  # at once; the bridge says when it is ready
        return {"FINISHED"}


class KILEIDO_OT_prompt_check(bpy.types.Operator):
    bl_idname = "kileido.prompt_check"
    bl_label = "Tick check"
    bl_options = {"INTERNAL"}

    check: StringProperty()

    @classmethod
    def description(cls, context, properties):
        row = next((row for row in catalogue() if row.get("id") == properties.check), {})
        return f"{row.get('title') or properties.check} ({properties.check}): ask the LLM to check it, or not"

    def execute(self, context):
        choose(context.scene, chosen(context.scene) ^ {self.check})
        return {"FINISHED"}


class KILEIDO_OT_prompt_preset(bpy.types.Operator):
    bl_idname = "kileido.prompt_preset"
    bl_label = "Checks preset"
    bl_options = {"INTERNAL"}

    preset: EnumProperty(items=(("quick", "Quick", "The first checks built (v1)"),
                                ("all", "All", "Every check in the catalogue")))

    @classmethod
    def description(cls, context, properties):
        if properties.preset == "quick":
            return "Tick the quick checks only (the first ones built)"
        return "Tick every check (a long review: the LLM takes much longer)"

    def execute(self, context):
        if self.preset == "quick":
            context.scene.kileido_prompt_checks = ""
        else:
            choose(context.scene, {row.get("id") for row in catalogue()})
        return {"FINISHED"}


class KILEIDO_OT_findings_group(bpy.types.Operator):
    bl_idname = "kileido.findings_group"
    bl_label = "Show or hide"
    bl_options = {"INTERNAL"}

    group: StringProperty()

    @classmethod
    def description(cls, context, properties):
        if properties.group.startswith("prompt:"):
            return "Show or hide the checks of this group"
        if properties.group == NOTES_TAG:
            return "Show or hide the notes sent to the LLM with the prompt"
        return "Show or hide the findings of this check"

    def execute(self, context):
        toggled = set(filter(None, context.scene.kileido_findings_open.split("\n")))
        toggled ^= {self.group}
        context.scene.kileido_findings_open = "\n".join(sorted(toggled))
        return {"FINISHED"}


CLASSES = (KILEIDO_OT_finding_show, KILEIDO_OT_finding_kicad, KILEIDO_OT_finding_mention, KILEIDO_OT_finding_state,
           KILEIDO_OT_findings_run_drc, KILEIDO_OT_findings_prompt, KILEIDO_OT_prompt_check, KILEIDO_OT_prompt_preset,
           KILEIDO_OT_findings_group)
