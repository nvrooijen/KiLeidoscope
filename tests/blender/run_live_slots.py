"""Headless Blender check of two live links (multi-board): each bridge's frames reach its own
board, a click is sent to the board's own bridge, and one link failing or going away
leaves the other live.

blender --background --factory-startup --python tests/blender/run_live_slots.py
"""

import json
import sys
from dataclasses import replace
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))

import kileido  # noqa: E402
from kileido import components, live, objects, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402
from kileido_bridge.protocol import FrameDecoder, messages_for, snapshot_frames  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def decoded(frames):
    return FrameDecoder().feed(b"".join(frames))


class FakeClient:
    """A bridge connection that hands over queued frames and records requests."""

    def __init__(self, frames):
        self.state = "disconnected"
        self.frames = list(frames)
        self.selects = []

    def connect(self):
        self.state = "connected"

    def poll_io(self):
        frames, self.frames = self.frames, []
        return frames

    def request_resync(self, adopt=False):
        pass

    def request_select(self, ids, extend=False, center=False, exact=False):
        self.selects.append(list(ids))

    def close(self):
        self.state = "disconnected"


def run_until(condition, what):
    for _ in range(200):
        live.tick()
        if condition():
            return
    raise AssertionError(f"{what}: not reached")


def frame_location(slot, footprint_id):
    with state.using(slot):
        return tuple(components.find(components.FRAME, footprint_id).location)


def main():
    assert bpy.app.background
    kileido.register()
    base = snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))
    snapshots = {slot: replace(base, board_name=f"Board {slot}.kicad_pcb") for slot in (1, 2)}
    clients = {47811: FakeClient(decoded(snapshot_frames(snapshots[1], revision=1))),
               47812: FakeClient(decoded(snapshot_frames(snapshots[2], revision=1)))}
    real_socket_client = live.SocketClient
    live.SocketClient = lambda host, port, token: clients[port]
    try:
        live.connect(47811, "one")
        live.connect(47812, "two", slot=2)
        run_until(lambda: all(state.boards[slot].loaded and not state.boards[slot].in_snapshot
                              for slot in (1, 2)), "both snapshots applied")
        assert state.boards[1].board_name == "Board 1.kicad_pcb"
        assert state.boards[2].board_name == "Board 2.kicad_pcb"
        with state.using(2):
            assert objects.lookup(objects.OUTLINE).name == "B2 KLS outline"

        # A footprint moved in board 2's KiCad moves it on board 2 only.
        footprint = snapshots[2].footprints[0]
        before_1, before_2 = frame_location(1, footprint.id), frame_location(2, footprint.id)
        shifted = replace(footprint, pos=(footprint.pos[0] + 2_000_000, footprint.pos[1]))
        moved = replace(snapshots[2], footprints=(shifted, *snapshots[2].footprints[1:]))
        clients[47812].frames = decoded(messages_for(moved, frozenset({("", "footprints")}), revision=2))
        run_until(lambda: frame_location(2, footprint.id) != before_2, "board 2's footprint moved")
        assert abs(frame_location(2, footprint.id)[0] - before_2[0] - 0.002) < 1e-6
        assert frame_location(1, footprint.id) == before_1, "board 1 moved with board 2's edit"

        # A click on a board goes to that board's bridge.
        with state.using(2):
            live.request_select(["track-on-2"])
        live.request_select(["track-on-1"])
        assert clients[47812].selects == [["track-on-2"]] and clients[47811].selects == [["track-on-1"]]

        # A frame board 2 cannot apply makes board 2 stale; board 1 stays live.
        clients[47812].frames = [({"type": "layer_data", "layer": "F.Cu", "kind": "zones", "ids": ["x"],
                                   "revision": 3, "protocol": live.PROTOCOL}, {})]
        live.tick()
        assert state.boards[2].status.startswith("Stale"), state.boards[2].status
        assert live.links[1].enabled and live.links[1].client.state == "connected"
        assert not state.boards[1].status.startswith("Stale"), state.boards[1].status

        # Board 2's KiCad goes away: its link stops, board 1's runs on.
        with state.using(2):
            live.disconnect()
        assert not live.links[2].enabled and live.links[1].enabled
        assert bpy.app.timers.is_registered(live.links[1]._timer)
        assert not bpy.app.timers.is_registered(live.links[2]._timer)
        assert state.current() is state.boards[1], "the slot in use leaked"
        print("live slots checks passed")
    finally:
        live.SocketClient = real_socket_client
        for slot in state.SLOTS:
            live.links[slot].disconnect()
        kileido.unregister()


main()
