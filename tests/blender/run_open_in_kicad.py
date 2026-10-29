"""Headless checks of getting an offline board back live (multi-board): Open in KiCad starts
the PCB editor beside the kicad-cli the board came with, with the board's file (caught here:
no KiCad is started), a board without its file or KiCad says why, the boards an opened
arrangement waits for are rows of their own with Open in KiCad and an X, and such a board
joining from its KiCad is placed without the "Add here" question.

blender --background --factory-startup --python tests/blender/run_open_in_kicad.py
"""

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))
import kileido  # noqa: E402
from kileido import apply, arrangement, join, kicad_app, live, poses, settings, slots, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402  (no kipy import)
from kileido_bridge.protocol import FrameDecoder, snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
BASE = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
FOLDER = Path(tempfile.mkdtemp(prefix="kileido_open_"))
EXE = ".exe" if sys.platform == "win32" else ""
CLI, EDITOR = FOLDER / "KiCad" / "bin" / f"kicad-cli{EXE}", FOLDER / "KiCad" / "bin" / f"pcbnew{EXE}"
MAIN, DAUGHTER = FOLDER / "Main.kicad_pcb", FOLDER / "Daughter.kicad_pcb"
EXPORT = {"kicad_cli": str(CLI)}


def frames(path):
    return b"".join(snapshot_frames(replace(BASE, board_name=path.name), board_path=str(path), export=EXPORT))


class FakeClient:
    """A bridge for a board joining: its snapshot, then quiet."""

    def __init__(self, path):
        self.state = "disconnected"
        self.frames = FrameDecoder().feed(frames(path))

    def connect(self):
        self.state = "connected"

    def poll_io(self):
        frames_now, self.frames = self.frames, []
        return frames_now

    def request_resync(self, adopt=False):
        pass

    def close(self):
        self.state = "disconnected"


class Layout:
    def __init__(self, drawn=None):
        self.drawn = [] if drawn is None else drawn
        self.alignment, self.active, self.enabled, self.alert = "EXPAND", True, True, False

    def row(self, *args, **kwargs):
        return type(self)(self.drawn)

    def label(self, text="", icon="NONE", icon_value=0):
        self.drawn.append(("label", text))

    def operator(self, idname, text="", **kwargs):
        self.drawn.append(("operator", idname))
        return type("Properties", (), {})()


def drawn_row(value, label):
    layout = Layout()
    item = type("Row", (), {"value": value, "label": label})()
    kileido.KILEIDO_UL_boards.draw_item(None, bpy.context, layout, None, item, 0, None, "", 0)
    return layout.drawn


def main():
    for path in (CLI, EDITOR, MAIN, DAUGHTER):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    started, apart = [], []
    kicad_app._start = lambda command, **options: started.append((command, options))
    kicad_app._keep_kicad_connections_apart = lambda: apart.append(True)
    settings.path = lambda: FOLDER / "settings.json"
    join.folder = lambda: FOLDER
    live.SocketClient = lambda host, port, token: FakeClient(DAUGHTER)
    os.environ["KICAD_API_SOCKET"] = "ipc://the-first-kicad"  # as a Blender started by KiCad may have
    kileido.register()
    try:
        apply.load_frames(frames(MAIN))  # shown, not live: a saved board, say
        assert json.loads((FOLDER / "settings.json").read_text(encoding="utf-8"))["kicad_cli"] == str(CLI)
        assert kicad_app.editor() == EDITOR

        # Open in KiCad: its PCB editor with its file, KiCad's pipes kept apart, our environment left out.
        assert bpy.ops.kileido.open_in_kicad(slot=1) == {"FINISHED"}
        (command, options), = started
        assert command == [str(EDITOR), str(MAIN)] and apart == [True], command
        assert "KICAD_API_SOCKET" not in options["env"] and "PYTHONHOME" not in options["env"]
        assert options["cwd"] == str(FOLDER)
        properties = bpy.ops.kileido.open_in_kicad.get_rna_type().properties
        assert all(properties[name].is_skip_save for name in ("slot", "path")), \
            "a waiting board's button would open the board of the last offline-row click"
        try:
            kicad_app.open_board(str(FOLDER / "gone.kicad_pcb"))
        except FileNotFoundError as exc:
            assert "not there" in str(exc)
        else:
            raise AssertionError("a missing board file was opened")

        # An arrangement with a board not shown: a row of its own, with Open in KiCad and an X.
        with state.using(2):
            apply.load_frames(frames(DAUGHTER))
            poses.set_shown(offset=(0.01, 0.0, 0.004), angles=(0.0, 0.0, 0.5))
            placed = state.board.pose
        file = FOLDER / "boards.usda"
        arrangement.save(file)
        slots.remove(2)
        arrangement.load(file)
        assert arrangement.waiting() == ["Daughter.kicad_pcb"] and arrangement.expects(str(DAUGHTER), "")
        rows = kileido._board_rows_wanted()
        assert rows[-1] == ("W0", "Daughter.kicad_pcb") and kileido._several_boards(), rows
        row = drawn_row("W0", "Daughter.kicad_pcb")
        assert ("label", "waiting") in row and ("operator", "kileido.open_in_kicad") in row, row
        assert ("operator", "kileido.forget_waiting") in row, row
        assert ("operator", "kileido.open_in_kicad") in drawn_row("S1", "Main.kicad_pcb"), "offline: no button"

        # Its KiCad's Open in Blender: no question, placed where the arrangement says.
        assert join.choice() == "ASK"
        nonce = "n47900"
        (FOLDER / f"join-{os.getpid()}-{nonce}.json").write_text(json.dumps(
            {"port": 47900, "token": "t", "board_name": DAUGHTER.name, "board_path": str(DAUGHTER), "protocol": 1}),
            encoding="utf-8")
        join.offer(force=True)
        join.poll()
        assert json.loads((FOLDER / f"reply-{nonce}.json").read_text(encoding="utf-8")) == \
            {"accepted": True, "slot": 2, "reason": "arranged"} and not join.pending()
        for _ in range(100):
            live.tick()
            if state.boards[2].loaded and not state.boards[2].in_snapshot:
                break
        assert state.boards[2].board_name == DAUGHTER.name and state.boards[2].pose is not None
        assert all(abs(a - b) < 1e-9 for a, b in zip((*placed[0], *placed[1]), (*state.boards[2].pose[0],
                                                                                 *state.boards[2].pose[1])))
        assert arrangement.waiting() == []

        # Stop waiting: the row goes, the file keeps the board.
        live.links[2].disconnect()
        slots.remove(2)
        assert arrangement.waiting() == ["Daughter.kicad_pcb"]
        assert bpy.ops.kileido.forget_waiting(path=str(DAUGHTER), name=DAUGHTER.name) == {"FINISHED"}
        assert arrangement.waiting() == [] and len(arrangement.read(file)) == 2
        print("open in KiCad checks passed")
    finally:
        join.withdraw()
        kileido.unregister()


main()
