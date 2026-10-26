"""Headless checks of a board from another KiCad joining this Blender (multi-board, join.py):
the question and its answers, the remembered choice, a board shown already taking its new
bridge without asking, four boards at most, and the files the launcher reads.

blender --background --factory-startup --python tests/blender/run_join.py
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
from kileido import join, live, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402
from kileido_bridge.protocol import FrameDecoder, snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
BASE = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))


class FakeClient:
    def __init__(self, name):
        self.state = "disconnected"
        board = replace(BASE, board_name=name)
        self.frames = FrameDecoder().feed(b"".join(snapshot_frames(board, revision=1)))

    def connect(self):
        self.state = "connected"

    def poll_io(self):
        frames, self.frames = self.frames, []
        return frames

    def request_resync(self, adopt=False):
        pass

    def close(self):
        self.state = "disconnected"


def request(folder, port, name, path=""):
    """What the launcher leaves: join-<this pid>-<nonce>.json."""
    nonce = f"n{port}"
    (folder / f"join-{os.getpid()}-{nonce}.json").write_text(json.dumps(
        {"port": port, "token": f"t{port}", "board_name": name, "board_path": path, "protocol": 1}),
        encoding="utf-8")
    return nonce


def reply(folder, nonce):
    path = folder / f"reply-{nonce}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def settle():
    for _ in range(200):
        live.tick()
        if all(not boards.in_snapshot for boards in state.boards.values()) and not live.busy():
            return


def main():
    assert bpy.app.background
    kileido.register()
    folder = Path(tempfile.mkdtemp(prefix="kileido_join_"))
    join.folder = lambda: folder
    join._settings_path = lambda: folder / "settings.json"
    names = {47811: "Board 1.kicad_pcb", 47812: "Board 2.kicad_pcb", 47813: "Board 3.kicad_pcb",
             47814: "Board 4.kicad_pcb", 47815: "Board 5.kicad_pcb", 47816: "Board 6.kicad_pcb",
             47822: "Board 2.kicad_pcb"}
    real_socket_client = live.SocketClient
    live.SocketClient = lambda host, port, token: FakeClient(names[port])
    try:
        live.connect(47811, "t47811")  # this Blender's own board, from its own KiCad
        settle()
        join.offer(force=True)
        viewer = json.loads((folder / f"viewer-{os.getpid()}.json").read_text(encoding="utf-8"))
        assert viewer == {"pid": os.getpid(), "protocol": join.PROTOCOL}
        assert join.choice() == "ASK"

        # Asked: nothing is answered before the user is.
        nonce = request(folder, 47812, "Board 2.kicad_pcb", "/boards/b2.kicad_pcb")
        join.poll()
        assert reply(folder, nonce) is None and [entry["nonce"] for entry in join.pending()] == [nonce]
        assert not list(folder.glob("join-*")), "the request was not taken"
        assert join.answer(nonce, add=True) == 2
        assert reply(folder, nonce) == {"accepted": True, "slot": 2, "reason": "free"}
        settle()
        assert state.boards[2].board_name == "Board 2.kicad_pcb" and live.links[2].port == 47812

        # "Open in its own window", remembered: the next board is not asked about.
        nonce = request(folder, 47813, "Board 3.kicad_pcb")
        join.poll()
        assert join.answer(nonce, add=False, remember=True) is None
        assert reply(folder, nonce)["reason"] == "own window" and join.choice() == "OWN"
        nonce = request(folder, 47814, "Board 4.kicad_pcb")
        join.poll()
        assert not join.pending() and reply(folder, nonce)["reason"] == "own window"

        # Board 2's KiCad came back after a crash: its slot takes the new bridge, unasked.
        nonce = request(folder, 47822, "Board 2.kicad_pcb", "/boards/b2.kicad_pcb")
        join.poll()
        assert reply(folder, nonce) == {"accepted": True, "slot": 2, "reason": "already shown"}
        assert live.links[2].port == 47822 and not join.pending()

        # Remembered "Add here": boards fill the free slots, then the window is full.
        join.set_choice("ADD")
        for port, slot in ((47813, 3), (47814, 4)):
            nonce = request(folder, port, names[port])
            join.poll()
            assert reply(folder, nonce) == {"accepted": True, "slot": slot, "reason": "free"}
        settle()
        nonce = request(folder, 47815, names[47815])
        join.poll()
        assert reply(folder, nonce) == {"accepted": False, "slot": None, "reason": "full"}
        assert [state.boards[slot].board_name for slot in state.SLOTS] == [names[p] for p in (47811, 47822, 47813,
                                                                                              47814)]

        # A question the launcher gave up on goes; a withdrawn request is never answered.
        join.set_choice("ASK")
        with state.using(4):
            live.disconnect()
        state.boards[4].reset()
        nonce = request(folder, 47816, names[47816])
        join.poll()
        assert join.pending()
        join._pending[0]["since"] -= join.ANSWER_S + 1
        join.poll()
        assert not join.pending() and reply(folder, nonce) is None
        withdrawn = folder / f"join-{os.getpid()}-gone.json"
        assert join.handle(withdrawn) is None and reply(folder, "gone") is None

        join.withdraw()
        assert not (folder / f"viewer-{os.getpid()}.json").exists()
        assert state.current() is state.boards[1], "the slot in use leaked"
        print("join checks passed")
    finally:
        live.SocketClient = real_socket_client
        for slot in state.SLOTS:
            live.links[slot].disconnect()
        kileido.unregister()


main()
