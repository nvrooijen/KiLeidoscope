"""Live-link transport tests: synthetic board records over real loopback sockets."""

import importlib.util
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from kileido_bridge.kicad_reader import KiCadBusy, PollResult
from kileido_bridge.loop import BridgeRuntime
from kileido_bridge.server import BridgeServer


ROOT = Path(__file__).resolve().parents[1]


def addon_client():
    path = ROOT / "blender_addon" / "kileido" / "client.py"
    spec = importlib.util.spec_from_file_location("kileido_addon_live_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    from kileido_bridge.model import snapshot_from_jsonable
    path = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
    return snapshot_from_jsonable(json.loads(path.read_text(encoding="utf-8")))


class FakeReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.events = []
        self.closed = False

    def poll(self, full=False):
        if self.events:
            event = self.events.pop(0)
            if isinstance(event, Exception):
                raise event
            self.snapshot, dirty = event
        else:
            dirty = frozenset()
        return PollResult(self.snapshot, frozenset(dirty), full)

    def close(self):
        self.closed = True


def exchange(runtime, client, until, limit=100):
    received = []
    for _ in range(limit):
        client.poll_io()  # send hello/resync if queued
        runtime.step()
        received.extend(client.poll_io())
        if until(received):
            return received
        time.sleep(0.001)
    raise AssertionError(f"condition not reached; received {[h['type'] for h, _ in received]}")


def test_authenticated_snapshot_incremental_resync_and_reconnect():
    snapshot = fixture()
    reader = FakeReader(snapshot)
    server = BridgeServer(port=0, token="test-secret")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client_type = addon_client().SocketClient
    client = client_type("127.0.0.1", server.port, "test-secret")
    try:
        runtime.step()  # initial read, before Blender connects
        client.connect()
        initial = exchange(runtime, client, lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        assert initial[0][0]["type"] == "snapshot_begin"
        assert any(h.get("kind") == "pads" and "drill" in arrays for h, arrays in initial)

        moved = replace(snapshot.tracks[0], start=(4_000_000, 5_000_000))
        changed = replace(snapshot, tracks=(moved, *snapshot.tracks[1:]))
        reader.events.append((changed, {("F.Cu", "tracks")}))
        update = exchange(runtime, client, lambda frames: any(h.get("kind") == "tracks" for h, _ in frames))
        assert [(h["layer"], h["kind"]) for h, _ in update if h["type"] == "layer_data"] == [("F.Cu", "tracks")]
        assert update[0][0]["revision"] > initial[0][0]["revision"]

        client.request_resync()
        again = exchange(runtime, client, lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        assert again[0][0]["type"] == "snapshot_begin"

        client.close()
        runtime.step()
        replacement = client_type("127.0.0.1", server.port, "test-secret")
        replacement.connect()
        reconnected = exchange(runtime, replacement,
                               lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        assert reconnected[0][0]["type"] == "snapshot_begin"
        replacement.close()
    finally:
        client.close()
        runtime.close()


def test_wrong_token_cannot_receive_a_snapshot():
    server = BridgeServer(port=0, token="correct")
    client = addon_client().SocketClient("127.0.0.1", server.port, "wrong")
    try:
        client.connect()
        for _ in range(50):
            client.poll_io()
            server.pump()
            frames = client.poll_io()
            assert not frames
            if client.state == "disconnected":
                break
            time.sleep(0.001)
        assert client.state == "disconnected"
        assert not server.authenticated
    finally:
        client.close()
        server.close()


def test_unauthenticated_connection_cannot_disconnect_the_viewer():
    import socket
    from kileido_bridge.protocol import encode_frame
    server = BridgeServer(port=0, token="secret")
    viewer = addon_client().SocketClient("127.0.0.1", server.port, "secret")
    intruders = []
    try:
        viewer.connect()
        for _ in range(100):
            viewer.poll_io()
            server.pump()
            if server.authenticated:
                break
            time.sleep(0.001)
        assert server.authenticated
        viewer_socket = server.client

        def settle():
            for _ in range(20):
                server.pump()
                time.sleep(0.001)

        intruders.append(socket.create_connection(("127.0.0.1", server.port)))  # never says hello
        settle()
        intruders.append(socket.create_connection(("127.0.0.1", server.port)))
        intruders[-1].sendall(encode_frame({"type": "hello", "token": "guess"}))
        settle()
        intruders.append(socket.create_connection(("127.0.0.1", server.port)))
        intruders[-1].sendall((10 * 1024 * 1024).to_bytes(4, "big"))  # announces a 10 MB frame before any hello
        settle()
        assert server.client is viewer_socket
        assert server.candidate is None  # the oversized frame was refused, not buffered
    finally:
        for connection in intruders:
            connection.close()
        viewer.close()
        server.close()


def test_board_switch_is_a_complete_snapshot_and_reuses_server_connection():
    snapshot = fixture()
    reader = FakeReader(snapshot)
    server = BridgeServer(port=0, token="secret")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "secret")
    try:
        client.connect()
        exchange(runtime, client, lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        second = replace(snapshot, board_name="second.kicad_pcb")
        reader.events.append((second, frozenset()))
        switched = exchange(runtime, client,
                            lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        board = next(h for h, _ in switched if h["type"] == "board")
        assert board["board_name"] == "second.kicad_pcb"
        assert client.state == "connected"
    finally:
        client.close()
        runtime.close()


def test_kicad_busy_disconnect_and_restart_preserve_blender_connection():
    snapshot = fixture()
    first = FakeReader(snapshot)
    second = FakeReader(snapshot)
    readers = [first, second]
    server = BridgeServer(port=0, token="secret")
    runtime = BridgeRuntime(server, connector=lambda: readers.pop(0),
                            poll_interval_s=0.0, reconnect_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "secret")
    try:
        client.connect()
        exchange(runtime, client, lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        first.events.extend([KiCadBusy("editing"), ConnectionError("KiCad closed")])
        statuses = exchange(runtime, client,
                            lambda frames: any(h.get("kicad") == "editing" for h, _ in frames))
        assert any(h.get("kicad") == "editing" for h, _ in statuses)
        restarted = exchange(runtime, client,
                             lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        assert any(h.get("kicad") == "disconnected" for h, _ in restarted)
        assert first.closed and client.state == "connected"
        assert runtime.reader is second
    finally:
        client.close()
        runtime.close()

def test_connection_error_reaches_client_before_first_snapshot():
    def fail():
        raise RuntimeError('synthetic connection failure')
    server = BridgeServer(port=0, token='test')
    runtime = BridgeRuntime(server, connector=fail)
    client = addon_client().SocketClient('127.0.0.1', server.port, 'test')
    try:
        runtime.step()
        client.connect()
        frames = exchange(runtime, client, lambda frames: any(h.get('error') for h, _ in frames))
        assert any(h.get('error') == 'RuntimeError: synthetic connection failure' for h, _ in frames)
    finally:
        client.close()
        runtime.close()


def test_second_kicad_on_windows_is_explained_not_shown_raw():
    """A plugin launched from a second KiCad reaches the first, which refuses its token
    (measured on KiCad 10.0.3); the viewer gets the reason and what to do."""
    from kipy.proto.common import ApiStatusCode
    from kileido_bridge.kicad_reader import explain_connection_error

    class TokenMismatch(Exception):
        code = ApiStatusCode.AS_TOKEN_MISMATCH

    def fail():
        raise TokenMismatch("the provided kicad_token did not match this KiCad instance's token")
    server = BridgeServer(port=0, token='test')
    runtime = BridgeRuntime(server, connector=fail)
    client = addon_client().SocketClient('127.0.0.1', server.port, 'test')
    try:
        runtime.step()
        client.connect()
        frames = exchange(runtime, client, lambda frames: any(h.get('error') for h, _ in frames))
        error = next(h['error'] for h, _ in frames if h.get('error'))
        assert "another KiCad instance" in error and "#20880" in error
    finally:
        client.close()
        runtime.close()
    refused = explain_connection_error(ConnectionError("Failed to connect to KiCad: Connection refused"))
    assert "Enable KiCad API" in refused
    leftover = "Task Manager" if sys.platform == "win32" else "pgrep -a kicad"
    assert leftover in refused and leftover in error


def test_new_kicad_is_followed_only_after_the_user_presses_resync():
    from kileido_bridge.kicad_reader import NewKiCad
    snapshot = fixture()
    state = {"followed": False}
    follows = []

    def connect():
        if not state["followed"]:
            raise NewKiCad("KiCad was restarted. Press Resync to follow it.")
        return FakeReader(snapshot)

    def follow():
        follows.append(True)
        state["followed"] = True
    server = BridgeServer(port=0, token="secret")
    runtime = BridgeRuntime(server, connector=connect, poll_interval_s=0.0, reconnect_interval_s=0.0,
                            follower=follow)
    client = addon_client().SocketClient("127.0.0.1", server.port, "secret")
    try:
        client.connect()
        frames = exchange(runtime, client, lambda frames: any(h.get("new_kicad") for h, _ in frames))
        assert any("Press Resync" in h.get("error", "") for h, _ in frames)
        client.request_resync()  # a plain resync (Blender reconnecting) does not follow
        for _ in range(20):
            client.poll_io()
            runtime.step()
        assert not follows
        client.request_resync(adopt=True)
        frames = exchange(runtime, client, lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        assert follows == [True] and runtime.new_kicad is False
        assert any(h.get("type") == "status" and not h.get("new_kicad") for h, _ in frames)
    finally:
        client.close()
        runtime.close()


def test_saved_color_change_sends_appearance_without_geometry(tmp_path, monkeypatch):
    """KiCad IPC has no colours (measured), so a save must push new colours by itself."""
    import os
    import kileido_bridge.loop as loop_module
    board = tmp_path / "board.kicad_pcb"

    def save(color):
        board.write_text('(kicad_pcb (setup (stackup (layer "F.Mask" (type "Top Solder Mask") '
                         f'(color "{color}")))) (copper_finish "ENIG"))', encoding="utf-8")
        stamp = board.stat().st_mtime_ns + 1_000_000_000 * (1 + save.count)
        os.utime(board, ns=(stamp, stamp))  # distinct mtime even on coarse file systems
        save.count += 1
    save.count = 0

    save("#FF0000FF")
    monkeypatch.setattr(loop_module, "saved_board_path", lambda _board: str(board))
    reader = FakeReader(fixture())
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        initial = exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        board_header = next(h for h, _ in initial if h["type"] == "board")
        assert board_header["appearance"]["saved_colors"]["F.Mask"][0] == 1.0  # red
        save("#00FF00FF")  # the user saves the board with a new mask colour
        update = exchange(runtime, client, lambda f: any(h["type"] == "appearance" for h, _ in f))
        header = next(h for h, _ in update if h["type"] == "appearance")
        assert header["appearance"]["saved_colors"]["F.Mask"][:3] == [0.0, 1.0, 0.0]
        assert header["appearance"]["copper_finish"] == "ENIG"
        assert not any(h["type"] == "layer_data" for h, _ in update)  # no geometry resent
        quiet = []
        for _ in range(5):  # unchanged files: nothing more is sent
            runtime.step()
            quiet.extend(client.poll_io())
        assert not any(h["type"] == "appearance" for h, _ in quiet)
    finally:
        client.close()
        runtime.close()


def test_unsaved_board_text_reaches_blender_through_the_live_copy(tmp_path, monkeypatch):
    """Mask, silkscreen and colours follow unsaved edits: the bridge writes the open
    board's text (SaveDocumentToString) to its own folder for Blender's kicad-cli."""
    import kileido_bridge.loop as loop_module
    from kileido_bridge.live_copy import LiveBoardCopy
    project = tmp_path / "project"
    project.mkdir()
    saved = project / "board.kicad_pcb"
    saved.write_text("(kicad_pcb)", encoding="utf-8")
    (project / "board.kicad_pro").write_text("{}", encoding="utf-8")

    def text(color):
        return ('(kicad_pcb (setup (stackup (layer "F.Mask" (type "Top Solder Mask") '
                f'(color "{color}")))))')

    class FakeBoard:
        contents = text("#FF0000FF")

        def get_as_string(self):
            return self.contents

    board = FakeBoard()
    reader = FakeReader(fixture())
    reader.board = board
    monkeypatch.setattr(loop_module, "saved_board_path", lambda _board: str(saved))
    server = BridgeServer(port=0, token="t")
    copy = LiveBoardCopy(tmp_path / "live", debounce_s=0.0, recheck_s=0.0)
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0, live_copy=copy)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        initial = exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        header = next(h for h, _ in initial if h["type"] == "board")
        live = Path(header["export"]["path"])
        assert header["export"]["live"] and header["export"]["project_dir"] == str(project)
        assert live.parent == tmp_path / "live" and live.name == "board.kicad_pcb"  # stem kept
        assert live.read_text(encoding="utf-8") == board.contents
        assert (live.parent / "board.kicad_pro").is_file()  # project variables for kicad-cli
        assert header["appearance"]["saved_colors"]["F.Mask"][0] == 1.0
        board.contents = text("#00FF00FF")  # edited in Board Setup, not saved
        update = exchange(runtime, client, lambda f: any(h["type"] == "appearance" for h, _ in f))
        appearance = next(h for h, _ in update if h["type"] == "appearance")["appearance"]
        assert appearance["saved_colors"]["F.Mask"][:3] == [0.0, 1.0, 0.0]
        assert live.read_text(encoding="utf-8") == board.contents
        assert saved.read_text(encoding="utf-8") == "(kicad_pcb)"  # the user's file is untouched
    finally:
        client.close()
        runtime.close()
    assert not (tmp_path / "live").exists()  # the private copy is removed with the bridge


def test_live_copy_waits_for_edits_to_settle(tmp_path):
    from kileido_bridge.live_copy import LiveBoardCopy
    copy = LiveBoardCopy(tmp_path, debounce_s=0.5, recheck_s=4.0)
    copy.target("board.kicad_pcb", "")
    assert copy.due(0.0)
    assert copy.write("a", 0.0) and not copy.write("a", 0.1)  # unchanged text: no rewrite
    assert not copy.due(1.0)
    copy.changed(1.0)
    assert not copy.due(1.2) and copy.due(1.6)
    copy.write("b", 1.6)
    assert not copy.due(5.0) and copy.due(5.7)  # idle re-check for edits IPC cannot see


def test_bridge_sends_selection_changes():
    """KiCad's selection is polled (read only) and sent when it changes."""
    from types import SimpleNamespace
    fixture_snapshot = fixture()
    track = fixture_snapshot.tracks[0]
    snapshot = replace(fixture_snapshot, tracks=(replace(track, net="SIG_P"),
                                        replace(fixture_snapshot.tracks[1], net="SIG_N"), *fixture_snapshot.tracks[2:]))

    class FakeBoard:
        selected = []

        def get_selection(self, types=None):
            return [SimpleNamespace(id=SimpleNamespace(value=item)) for item in self.selected]

    reader = FakeReader(snapshot)
    reader.board = FakeBoard()
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        FakeBoard.selected = [track.id]
        frames = exchange(runtime, client, lambda f: any(h["type"] == "selection" and h["selected"] for h, _ in f))
        message = next(h for h, _ in frames if h["type"] == "selection" and h["selected"])
        assert track.id in message["selected"] and fixture_snapshot.tracks[1].id in message["pair"]
        assert not set(message["selected"]) & set(message["pair"])
        FakeBoard.selected = []
        frames = exchange(runtime, client, lambda f: any(h["type"] == "selection" for h, _ in f))
        assert next(h for h, _ in frames if h["type"] == "selection")["selected"] == []
    finally:
        client.close()
        runtime.close()


def test_a_selected_track_without_a_net_is_highlighted_alone():
    """A net-less track (drawn loose, or left over from a netlist change) has no net to
    follow; it still lights up while KiCad has it selected, and nothing else does."""
    from types import SimpleNamespace
    fixture_snapshot = fixture()
    loose = replace(fixture_snapshot.tracks[0], net="")
    snapshot = replace(fixture_snapshot, tracks=(loose, *(replace(t, net="") for t in fixture_snapshot.tracks[1:])))

    class FakeBoard:
        selected = []

        def get_selection(self, types=None):
            return [SimpleNamespace(id=SimpleNamespace(value=item)) for item in self.selected]

    reader = FakeReader(snapshot)
    reader.board = FakeBoard()
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        FakeBoard.selected = [loose.id]
        frames = exchange(runtime, client, lambda f: any(h["type"] == "selection" and h["selected"] for h, _ in f))
        message = next(h for h, _ in frames if h["type"] == "selection" and h["selected"])
        assert message["selected"] == [loose.id] and message["pair"] == []  # not the other net-less tracks
        FakeBoard.selected = []
        frames = exchange(runtime, client, lambda f: any(h["type"] == "selection" for h, _ in f))
        assert next(h for h, _ in frames if h["type"] == "selection")["selected"] == []
    finally:
        client.close()
        runtime.close()


def test_blender_click_selects_in_kicad_and_pads_select_their_footprint():
    """The one KiCad-changing call: a select request from Blender replaces KiCad's selection."""
    snapshot = fixture()
    pad = next(p for p in snapshot.pads if p.footprint_id)
    calls = []

    class FakeBoard:
        def clear_selection(self):
            calls.append(("clear",))

        def add_to_selection(self, items):
            calls.append(("add", [item.id.value for item in items]))

        def get_selection(self, types=None):
            return []

    class FakeClient:
        def send(self, request, _response_type):
            calls.append(("action", request.action))

    reader = FakeReader(snapshot)
    reader.board = FakeBoard()
    reader.board._kicad = FakeClient()
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        client.request_select([snapshot.tracks[0].id, pad.id])
        for _ in range(50):
            client.poll_io()
            runtime.step()
            if calls:
                break
            time.sleep(0.001)
        assert calls == [("clear",), ("add", [snapshot.tracks[0].id, pad.footprint_id])]
        calls.clear()
        client.request_select([snapshot.tracks[1].id], extend=True)
        for _ in range(50):
            client.poll_io()
            runtime.step()
            if calls:
                break
            time.sleep(0.001)
        assert calls == [("add", [snapshot.tracks[1].id])]  # Shift+click keeps the rest
        calls.clear()
        client.request_select([pad.id], center=True)  # "Center KiCad on click" ticked
        for _ in range(50):
            client.poll_io()
            runtime.step()
            if len(calls) == 3:
                break
            time.sleep(0.001)
        assert calls == [("clear",), ("add", [pad.footprint_id]), ("action", "common.Control.centerSelection")]
    finally:
        client.close()
        runtime.close()


def test_a_slow_kicad_reply_is_not_a_lost_kicad():
    """kipy turns a reply timeout into ConnectionError("... Timed out"): a few in a row
    keep the connection (KiCad stalls while its window is hidden); a real error drops it."""
    reader = FakeReader(fixture())
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    try:
        runtime.step()
        timed_out = ConnectionError("Error receiving reply from KiCad: Timed out")
        reader.events = [timed_out] * (BridgeRuntime.MAX_TIMEOUTS - 1)
        for _ in range(BridgeRuntime.MAX_TIMEOUTS - 1):
            runtime.step()
        assert runtime.reader is reader and not reader.closed
        runtime.step()  # a good poll resets the count
        reader.events = [ConnectionError("Failed to send command to KiCad: Closed")]
        runtime.step()
        assert runtime.reader is None
    finally:
        runtime.close()


def test_blender_click_highlights_before_a_slow_kicad_answers():
    """KiCad can take seconds to apply a selection (its window hidden behind Blender):
    the click's highlight goes out at once, and reads that time out do not clear it."""
    snapshot = fixture()
    track = snapshot.tracks[0]

    class SlowBoard:
        def clear_selection(self):
            raise ConnectionError("Error receiving reply from KiCad: Timed out")

        add_to_selection = get_selection = clear_selection

    reader = FakeReader(snapshot)
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        reader.board = SlowBoard()
        client.request_select([track.id])
        frames = exchange(runtime, client, lambda f: any(h["type"] == "selection" and h["selected"] for h, _ in f))
        assert track.id in next(h for h, _ in frames if h["type"] == "selection" and h["selected"])["selected"]
        later = []
        for _ in range(20):
            runtime.step()
            later += [header for header, _ in client.poll_io() if header["type"] == "selection"]
            time.sleep(0.001)
        assert later == []  # still highlighted
    finally:
        client.close()
        runtime.close()


def test_selection_reads_from_before_kicad_applied_a_click_are_ignored():
    """A stalled KiCad can answer a selection read (on another connection) before it
    applies the click: that old, or empty, selection must not undo the click's highlight."""
    snapshot = fixture()
    old, clicked = snapshot.tracks[0], snapshot.tracks[1]
    snapshot = replace(snapshot, tracks=(replace(old, net="A"), replace(clicked, net="B"), *snapshot.tracks[2:]))

    class StalledBoard:
        selected = [old.id]

        def clear_selection(self):
            pass  # applied later, like a stalled KiCad

        def add_to_selection(self, items):
            pass

        def get_selection(self, types=None):
            return [SimpleNamespace(id=SimpleNamespace(value=item)) for item in self.selected]

    reader = FakeReader(snapshot)
    reader.board = StalledBoard()
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        frames = exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        assert any(h["type"] == "selection" and old.id in h["selected"] for h, _ in frames)
        client.request_select([clicked.id])
        exchange(runtime, client, lambda f: any(h["type"] == "selection" and clicked.id in h["selected"] for h, _ in f))
        for stale in ([old.id], []):  # the old selection, then between clear and add
            StalledBoard.selected = stale
            for _ in range(10):
                runtime.step()
                time.sleep(0.001)
            assert clicked.id in runtime.highlight[0] and old.id not in runtime.highlight[0]  # the click's stays
            assert all(clicked.id in h["selected"] for h, _ in client.poll_io() if h["type"] == "selection")
        StalledBoard.selected = [clicked.id]  # KiCad applied it: confirmed, reads count again
        runtime.step()
        StalledBoard.selected = []  # and a later deselect in KiCad clears the highlight
        cleared = []
        for _ in range(50):
            runtime.step()
            cleared += [h for h, _ in client.poll_io() if h["type"] == "selection"]
            if cleared:
                break
            time.sleep(0.001)
        assert cleared and cleared[-1]["selected"] == []
    finally:
        client.close()
        runtime.close()


def test_every_click_is_highlighted_at_once_while_kicad_calls_stall():
    """After a selection change a hidden KiCad answers every call late (measured 1-9 s).
    With KiCad calls on the worker thread, not only the first click but each next one
    is highlighted at once, while the worker still waits on KiCad."""
    snapshot = fixture()
    tracks = tuple(replace(track, net=f"N{index}") for index, track in enumerate(snapshot.tracks[:3]))
    snapshot = replace(snapshot, tracks=(*tracks, *snapshot.tracks[3:]))

    class StallingBoard:
        calls = 0

        def stall(self, *args, **kwargs):
            StallingBoard.calls += 1
            time.sleep(1.0)
            return []

        clear_selection = add_to_selection = get_selection = stall

    reader = FakeReader(snapshot)
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        reader.board = StallingBoard()
        runtime.start()
        for track in tracks:
            started = time.perf_counter()
            client.request_select([track.id])
            highlighted = False
            while not highlighted and time.perf_counter() - started < 0.3:
                client.poll_io()  # sends the click
                runtime.step()
                runtime.wait(0.005)
                highlighted = any(h["type"] == "selection" and track.id in h["selected"] for h, _ in client.poll_io())
            took = time.perf_counter() - started
            assert highlighted and took < 0.3, f"click {tracks.index(track) + 1}: highlighted after {took:.2f} s"
        assert StallingBoard.calls  # the worker was talking to (stalled) KiCad meanwhile
    finally:
        client.close()
        runtime.close()


def test_highlight_survives_routing_and_clears_on_a_real_deselect():
    """KiCad is busy while routing and clears the selection: the nets stay
    highlighted and newly routed tracks join; a plain deselect clears them."""
    from types import SimpleNamespace
    snapshot = fixture()
    track = replace(snapshot.tracks[0], net="SIG")
    snapshot = replace(snapshot, tracks=(track, *snapshot.tracks[1:]))

    class FakeBoard:
        selected = []

        def get_selection(self, types=None):
            return [SimpleNamespace(id=SimpleNamespace(value=item)) for item in self.selected]

    reader = FakeReader(snapshot)
    reader.board = FakeBoard()
    server = BridgeServer(port=0, token="t")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")

    def last_selection(frames):
        return [h for h, _ in frames if h["type"] == "selection"][-1]

    try:
        runtime.step()
        client.connect()
        exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        FakeBoard.selected = [track.id]
        frames = exchange(runtime, client, lambda f: any(h["type"] == "selection" and h["selected"] for h, _ in f))
        assert track.id in last_selection(frames)["selected"]
        # Routing: KiCad busy, then the selection is empty and a new track exists on SIG.
        routed = replace(snapshot.tracks[1], id="new-segment", net="SIG")
        after = replace(snapshot, tracks=(*snapshot.tracks, routed))
        reader.events = [KiCadBusy("routing"), KiCadBusy("routing"),
                         (after, frozenset({(routed.layer, "tracks")}))]
        FakeBoard.selected = []
        frames = exchange(runtime, client, lambda f: any(
            h["type"] == "selection" and "new-segment" in h["selected"] for h, _ in f))
        assert track.id in last_selection(frames)["selected"]  # still highlighted
        for _ in range(5):  # stays while nothing new happens
            runtime.step()
            assert not any(h["type"] == "selection" for h, _ in client.poll_io())
        # A normal select then deselect (no busy period) clears the highlight.
        FakeBoard.selected = [track.id]
        for _ in range(3):
            runtime.step()
            client.poll_io()
        FakeBoard.selected = []
        frames = exchange(runtime, client, lambda f: any(
            h["type"] == "selection" and not h["selected"] for h, _ in f))
        assert last_selection(frames)["selected"] == []
    finally:
        client.close()
        runtime.close()


def test_routes_appear_while_kicad_is_busy_from_the_board_text():
    """KiCad's route tool keeps item reads busy; the board text still has the route."""
    from kileido_bridge.loop import BridgeRuntime as Runtime
    snapshot = fixture()
    track = snapshot.tracks[0]

    def segment(item):
        return (f'\t(segment\n\t\t(start {item.start[0] / 1e6} {item.start[1] / 1e6})\n'
                f'\t\t(end {item.end[0] / 1e6} {item.end[1] / 1e6})\n\t\t(width {item.width / 1e6})\n'
                f'\t\t(layer "{item.layer}")\n\t\t(net "{item.net}")\n\t\t(uuid "{item.id}")\n\t)\n')

    routed = replace(track, id="routed-while-busy", start=(1_000_000, 2_000_000))

    class FakeBoard:
        def get_as_string(self):
            parts = [segment(item) for item in (*snapshot.tracks, routed)]
            return "(kicad_pcb\n" + "".join(parts) + ")\n"

        def get_selection(self, types=None):
            return []

    reader = FakeReader(snapshot)
    reader.board = FakeBoard()
    server = BridgeServer(port=0, token="t")
    runtime = Runtime(server, connector=lambda: reader, poll_interval_s=0.0)
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    try:
        runtime.step()
        client.connect()
        exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f))
        reader.events = [KiCadBusy("route tool")] * 50
        frames = exchange(runtime, client, lambda f: any(
            h.get("kind") == "tracks" and "routed-while-busy" in h.get("ids", ()) for h, _ in f))
        header = next(h for h, _ in frames if h.get("kind") == "tracks")
        assert header["layer"] == track.layer and track.id in header["ids"]
        # The router closes and the route is undone before the next item read: the reader
        # sees its own last board again (no change for it), yet Blender must drop the route.
        reader.events = []
        frames = exchange(runtime, client, lambda f: any(h.get("kind") == "tracks" for h, _ in f))
        header = next(h for h, _ in frames if h.get("kind") == "tracks")
        assert "routed-while-busy" not in header["ids"] and track.id in header["ids"]
    finally:
        client.close()
        runtime.close()
