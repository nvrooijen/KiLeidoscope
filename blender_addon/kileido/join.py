"""Another KiCad's board joins this Blender (multi-board), or opens in a window of its own.

A KiLeidoscope Blender started from KiCad lists itself in the per-user cache folder,
`viewers/viewer-<pid>.json`. Open in Blender in another KiCad (kileido_bridge/launcher.py)
then leaves a request there, `join-<pid>-<nonce>.json`, with its bridge's port and token
and the board it serves. A timer here takes the request (an atomic rename: until then the
launcher can withdraw it) and answers in `reply-<nonce>.json`:

- the board is shown already (its KiCad came back after a crash, or Open in Blender was
  clicked again): its slot takes the new bridge, without asking;
- else the user's remembered choice, or the question "Add here / Open in its own window"
  (a popup, and a box in the Boards section until answered or the launcher gives up);
- no slot free (four boards): the launcher opens a window of its own.

The add-on keeps its own copy of the file format: it never imports kileido_bridge.
"""

import atexit
import json
import os
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from . import kicad_cli, live, settings
from .objects import redraw_views
from .state import SLOTS, boards, same_file

PROTOCOL = 1  # kileido_bridge/launcher.py JOIN_PROTOCOL
POLL_S = 0.5
ANSWER_S = 110.0  # a question unanswered this long is dropped: the launcher gives up at 120 s
CHOICES = (("ASK", "Ask", "Ask each time a board from another KiCad wants to join"),
           ("ADD", "Add here", "Show boards from other KiCads in this window, next to the others"),
           ("OWN", "Own window", "Open boards from other KiCads in Blender windows of their own"))
_registered = [None]  # this Blender's viewer file, while it takes requests
_pending = []  # questions to the user: {"nonce", "offer", "since"}


def folder():
    """The per-user folder of viewer files and requests (the launcher computes the same)."""
    path = Path(kicad_cli.cache_root()).parent / "viewers"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def choice():
    """The remembered answer: "ASK" (no answer remembered), "ADD" or "OWN"."""
    value = settings.read().get("join", "ASK")
    return value if value in {key for key, _, _ in CHOICES} else "ASK"


def set_choice(value):
    settings.put(join=value)


def _shown(state, offered):
    """`offered` ({"board_path", "board_name"}) is the board `state` shows: the same file
    when both know theirs, else the same name ("" when the bridge could not read KiCad
    in time: never the same)."""
    if not state.loaded:
        return False
    if state.board_path and offered["board_path"]:
        return same_file(state.board_path, offered["board_path"])
    return bool(offered["board_name"]) and state.board_name == offered["board_name"]


def choose_slot(offered):
    """(slot or None, reason) for the board a new bridge offers."""
    for slot in SLOTS:
        if _shown(boards[slot], offered):
            return slot, "already shown"
    for slot in SLOTS:
        if not boards[slot].loaded and not live.links[slot].enabled:
            return slot, "free"
    return None, "full"


def offer(force=False):
    """Take requests from now on (a Blender started from KiCad; `force`: headless tests)."""
    if _registered[0] is not None or (bpy.app.background and not force):
        return
    path = folder() / f"viewer-{os.getpid()}.json"
    settings.write(path, {"pid": os.getpid(), "protocol": PROTOCOL})
    _registered[0] = path
    atexit.register(withdraw)  # Blender does not unregister a script-started add-on on quit
    if not bpy.app.timers.is_registered(poll):
        bpy.app.timers.register(poll, first_interval=POLL_S, persistent=True)


def withdraw():
    path = _registered[0]
    _registered[0] = None
    _pending.clear()
    if bpy.app.timers.is_registered(poll):
        bpy.app.timers.unregister(poll)
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def poll():
    """Timer: take every waiting request; drop questions the launcher has given up on."""
    if _registered[0] is None:
        return None
    for request in sorted(folder().glob(f"join-{os.getpid()}-*.json")):
        handle(request)
    now = time.monotonic()
    expired = [entry for entry in _pending if now - entry["since"] > ANSWER_S]
    for entry in expired:
        _pending.remove(entry)
    if expired:
        redraw_views()
    return POLL_S


def handle(request):
    """Take one request and answer it, now or once the user has: the slot it got, or None."""
    taken = request.with_suffix(".taken")
    try:
        os.replace(request, taken)  # fails when the launcher withdrew it
    except OSError:
        return None
    nonce = request.stem.rsplit("-", 1)[-1]
    try:
        data = json.loads(taken.read_text(encoding="utf-8"))
        offered = {"port": int(data["port"]), "token": str(data["token"]),
                   "board_name": str(data.get("board_name", "")), "board_path": str(data.get("board_path", ""))}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _reply(nonce, False, None, f"bad request: {exc}")
        return None
    finally:
        try:
            taken.unlink()
        except OSError:
            pass
    slot, reason = choose_slot(offered)
    from . import arrangement  # it imports modules that import this one
    if slot is not None and reason == "free" and arrangement.expects(offered["board_path"], offered["board_name"]):
        return _accept(nonce, offered, slot, "arranged")  # an opened arrangement waits for it: no question
    if reason == "already shown" or (slot is not None and choice() == "ADD"):
        return _accept(nonce, offered, slot, reason)
    if slot is None or choice() == "OWN":
        _reply(nonce, False, None, "full" if slot is None else "own window")
        return None
    _pending.append({"nonce": nonce, "offer": offered, "since": time.monotonic()})
    _ask()
    redraw_views()
    return None


def answer(nonce, add, remember=False):
    """The user's answer to a pending question: the slot the board got, or None."""
    entry = next((entry for entry in _pending if entry["nonce"] == nonce), None)
    if entry is None:
        return None
    _pending.remove(entry)
    if remember:
        set_choice("ADD" if add else "OWN")
    slot = None
    if add:
        offered = entry["offer"]
        slot, reason = choose_slot(offered)
        if slot is None:
            _reply(nonce, False, None, "full")
        else:
            _accept(nonce, offered, slot, reason)
    else:
        _reply(nonce, False, None, "own window")
    redraw_views()
    return slot


def pending():
    return list(_pending)


def _accept(nonce, offered, slot, reason):
    live.connect(offered["port"], offered["token"], slot)  # a board shown: the new bridge takes over
    _reply(nonce, True, slot, reason)
    return slot


def _reply(nonce, accepted, slot, reason):
    reply = {"accepted": accepted, "slot": slot, "reason": reason}
    settings.write(folder() / f"reply-{nonce}.json", reply)
    print("KiLeidoscope join: " + json.dumps(reply), flush=True)


# --- Asking the user ------------------------------------------------------------------------

def _board_label(entry):
    name = entry["offer"]["board_name"]
    return Path(name).stem if name else "A board from another KiCad"


def _draw_question(layout, entry, context):
    layout.label(text=f"{_board_label(entry)} wants to join this window", icon="LINKED")
    row = layout.row(align=True)
    add = row.operator(KILEIDO_OT_join_answer.bl_idname, text="Add here")
    add.nonce, add.add = entry["nonce"], True
    own = row.operator(KILEIDO_OT_join_answer.bl_idname, text="Open in its own window")
    own.nonce, own.add = entry["nonce"], False
    layout.prop(context.window_manager, "kileido_join_remember")


def _ask():
    """A popup in Blender's window; the Boards section keeps the question until answered
    (a popup closes when the mouse leaves it)."""
    window_manager = bpy.context.window_manager
    if bpy.app.background or not _pending or not window_manager.windows:
        return
    entry = _pending[-1]
    try:
        with bpy.context.temp_override(window=window_manager.windows[0]):
            window_manager.popup_menu(lambda menu, context: _draw_question(menu.layout, entry, context),
                                      title="KiLeidoscope", icon="LINKED")
    except (RuntimeError, TypeError, AttributeError):
        pass


def draw(layout, context, listed=False):
    """The Boards section's part: open questions, and the remembered choice once there is one
    (`listed`, the Boards list shown: it sits in the Boards menu, `draw_choice`)."""
    for entry in _pending:
        _draw_question(layout.box(), entry, context)
    if choice() != "ASK" and not listed:
        layout.prop(context.window_manager, "kileido_join_choice", text="Other KiCads")


def draw_choice(layout, context):
    """The remembered choice as menu entries: what Open in Blender in another KiCad does."""
    layout.label(text="Open in Blender in another KiCad")
    layout.props_enum(context.window_manager, "kileido_join_choice")


class KILEIDO_OT_join_answer(bpy.types.Operator):
    bl_idname = "kileido.join_answer"
    bl_label = "Board from another KiCad"
    bl_options = {"INTERNAL"}

    nonce: StringProperty()
    add: BoolProperty()

    @classmethod
    def description(cls, context, properties):
        return ("Show this board in this window, next to the others" if properties.add
                else "Open this board in a Blender window of its own")

    def execute(self, context):
        remember = bool(getattr(context.window_manager, "kileido_join_remember", False))
        slot = answer(self.nonce, self.add, remember)
        if self.add and slot is None:
            self.report({"WARNING"}, "KiLeidoscope: this window shows four boards already; "
                                     "the board opens in a window of its own")
        context.window_manager.kileido_join_remember = False
        return {"FINISHED"}


def _get_choice(_self):
    return [key for key, _, _ in CHOICES].index(choice())


def _set_choice(_self, index):
    set_choice(CHOICES[index][0])


def register():
    bpy.utils.register_class(KILEIDO_OT_join_answer)
    bpy.types.WindowManager.kileido_join_remember = BoolProperty(
        name="Remember my choice", description="Answer boards from other KiCads this way from now on "
                                               "(changed again under Other KiCads)")
    bpy.types.WindowManager.kileido_join_choice = EnumProperty(
        name="Other KiCads", description="What Open in Blender in another KiCad does while this window is open",
        items=CHOICES, get=_get_choice, set=_set_choice)


def unregister():
    withdraw()
    for name in ("kileido_join_remember", "kileido_join_choice"):
        if hasattr(bpy.types.WindowManager, name):
            delattr(bpy.types.WindowManager, name)
    bpy.utils.unregister_class(KILEIDO_OT_join_answer)
