"""Findings over the live link: `.kileidoscope/` files in, `findings` frames out, requests back."""

import json
from dataclasses import replace

from kileido_bridge import findings_watch, kls_folder
from kileido_bridge.live_copy import LiveBoardCopy
from kileido_bridge.loop import BridgeRuntime
from kileido_bridge.protocol import encode_frame
from kileido_bridge.server import MAX_FINDINGS_REQUESTS, BridgeServer
from test_live_link import FakeReader, addon_client, exchange, fixture

LIMIT = 3000  # the watch works on threads of its own: allow it a few seconds


class FakeBoard:
    contents = "(kicad_pcb (version 20250101))\n"

    def get_as_string(self):
        return self.contents


def quick(watch):
    watch.CHECK_S = watch.SETTLE_S = watch.RESOLVE_DEBOUNCE_S = 0.0
    return watch


def live(tmp_path, monkeypatch, saved=True, watch=None, snapshot=None):
    """A runtime on the fixture board, saved as tmp_path/project/board.kicad_pcb (or unsaved)."""
    import kileido_bridge.loop as loop_module
    project = tmp_path / "project"
    project.mkdir()
    board_file = project / "board.kicad_pcb"
    board_file.write_text(FakeBoard.contents, encoding="utf-8")
    monkeypatch.setattr(loop_module, "saved_board_path", lambda _board: str(board_file) if saved else "")
    reader = FakeReader(snapshot or fixture())
    reader.board = FakeBoard()
    server = BridgeServer(port=0, token="t")
    copy = LiveBoardCopy(tmp_path / "live", debounce_s=0.0, recheck_s=0.0)
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0, reconnect_interval_s=60.0,
                            live_copy=copy)
    runtime.findings = quick(watch or findings_watch.FindingsWatch())
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    return runtime, reader, client, project


def payloads(frames):
    return [header["findings"] for header, _ in frames if header["type"] == "findings"]


def until_payload(runtime, client, test):
    frames = exchange(runtime, client, lambda f: any(test(p) for p in payloads(f)), limit=LIMIT)
    return next(p for p in reversed(payloads(frames)) if test(p))


def write(project, data, name="board.kls-findings.json"):
    folder = project / ".kileidoscope"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


def request(client, action, key="", **extra):
    client.outgoing.extend(encode_frame({"type": "findings", "action": action, "key": key, **extra}))


def tool_file(track):
    return {"format": "kls-findings 1", "run": "", "tool": "test tool", "findings": [
        {"check": "hs.stub", "severity": "warning", "title": "Stub on the clock",
         "draw": [{"tool": "highlight", "targets": [f"uuid:{track.id}"]}]}]}


def file_rows(payload):
    return payload["sources"]["file"]["findings"]


def test_a_written_findings_file_reaches_blender_after_the_snapshot(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    try:
        runtime.step()
        client.connect()
        frames = exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f)
                          and any(p["status"] == "none" for p in payloads(f)), limit=LIMIT)
        none = payloads(frames)[-1]
        assert none["folder"] == str(project / ".kileidoscope") and none["drc"]["state"] == "idle"
        assert set(none["sources"]) == {"drc", "file"}
        write(project, tool_file(track))
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        (row,) = file_rows(payload)
        assert row["title"] == "Stub on the clock" and row["state"] == "" and not row["faded"]
        source = payload["sources"]["file"]
        assert source["status"] == "ok" and source["connected"]
        assert source["path"] == str(project / ".kileidoscope" / "board.kls-findings.json")
        assert payload["sources"]["drc"]["status"] == "none"
        types = [h["type"] for h, _ in frames]
        assert "findings" not in types[:types.index("snapshot_end")]  # never inside a snapshot

        (project / ".kileidoscope" / "board.kls-findings.json").write_text("{not json", encoding="utf-8")
        broken = until_payload(runtime, client, lambda p: p["sources"]["file"]["status"] == "error")
        assert broken["sources"]["file"]["error"].startswith("not JSON")
        assert [r["title"] for r in file_rows(broken)] == ["Stub on the clock"]  # the last good list stays
    finally:
        client.close()
        runtime.close()


def test_dismiss_is_saved_and_sent_again(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    write(project, tool_file(reader.snapshot.tracks[0]))
    try:
        runtime.step()
        client.connect()
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        key = file_rows(payload)[0]["key"]
        request(client, "dismiss", key, source="file")
        payload = until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["state"] == "dismissed")
        assert file_rows(payload)[0]["key"] == key
        state = project / ".kileidoscope" / "dismissed.json"
        for _ in range(LIMIT):  # written on the watch's thread
            runtime.step()
            if state.is_file():
                break
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["findings"] == {findings_watch.state_key("file", key): "dismissed"}
        request(client, "dismiss", key)  # a DRC finding by that key: there is none, nothing changes
        request(client, "reset", key, source="file")
        payload = until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["state"] == "")
        assert file_rows(payload)[0]["draws"][0]["ids"] == [reader.snapshot.tracks[0].id]
    finally:
        client.close()
        runtime.close()


def test_a_resync_sends_the_findings_again_after_the_snapshot(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    write(project, tool_file(reader.snapshot.tracks[0]))
    try:
        runtime.step()
        client.connect()
        until_payload(runtime, client, lambda p: p["status"] == "ok")
        client.request_resync()
        frames = exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f)
                          and any(h["type"] == "findings" for h, _ in f), limit=LIMIT)
        types = [h["type"] for h, _ in frames]
        assert types.index("findings") > types.index("snapshot_end")
        assert file_rows(payloads(frames)[-1])[0]["title"] == "Stub on the clock"
    finally:
        client.close()
        runtime.close()


def test_a_removed_track_fades_its_finding(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    write(project, tool_file(track))
    try:
        runtime.step()
        client.connect()
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        assert not file_rows(payload)[0]["faded"]
        without = replace(reader.snapshot, tracks=reader.snapshot.tracks[1:])
        reader.events.append((without, {(track.layer, "tracks")}))
        payload = until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["faded"])
        assert file_rows(payload)[0]["title"] == "Stub on the clock"
    finally:
        client.close()
        runtime.close()


def test_a_disconnect_keeps_the_folder_and_the_keys(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    write(project, tool_file(reader.snapshot.tracks[0]))
    try:
        runtime.step()
        client.connect()
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        folder, key = runtime.findings.folder, file_rows(payload)[0]["key"]
        reader.events.append(ConnectionError("KiCad closed"))
        payload = until_payload(runtime, client, lambda p: not p["sources"]["file"]["connected"])
        assert runtime.reader is None and runtime.findings.folder is folder
        assert payload["status"] == "ok" and file_rows(payload)[0]["key"] == key  # not renamed without the board
        client.request_resync()  # a viewer that reconnects meanwhile gets the list too
        frames = exchange(runtime, client, lambda f: any(h["type"] == "findings" for h, _ in f), limit=LIMIT)
        assert file_rows(payloads(frames)[-1])[0]["key"] == key
    finally:
        client.close()
        runtime.close()


def test_a_file_changed_while_kicad_is_gone_is_read_once_it_is_back(tmp_path, monkeypatch):
    """Keys name items by the board, so a file that changes while KiCad is disconnected
    stays unread (a dismiss meanwhile would be saved under a key that changes back); it is
    read as soon as KiCad is connected again."""
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    write(project, tool_file(track))
    try:
        runtime.step()
        client.connect()
        until_payload(runtime, client, lambda p: p["status"] == "ok")
        reader.events.append(ConnectionError("KiCad closed"))
        until_payload(runtime, client, lambda p: not p["sources"]["file"]["connected"])
        renamed = tool_file(track)
        renamed["findings"][0]["title"] = "Stub, renamed"
        write(project, renamed)
        seen = []
        for _ in range(50):  # the file is seen and settled, and left alone
            runtime.step()
            seen.extend(client.poll_io())
        assert all([r["title"] for r in file_rows(p)] == ["Stub on the clock"] for p in payloads(seen))
        runtime.next_connect_at = 0.0  # KiCad is back
        payload = until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["title"] == "Stub, renamed")
        assert payload["sources"]["file"]["connected"]
    finally:
        client.close()
        runtime.close()


def test_an_unsaved_board_says_so(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch, saved=False)
    try:
        runtime.step()
        client.connect()
        payload = until_payload(runtime, client, lambda p: p["status"] == "unsaved")
        assert payload["folder"] == ""
        assert {source["status"] for source in payload["sources"].values()} == {"unsaved"}
        request(client, "run_drc")
        payload = until_payload(runtime, client, lambda p: p["drc"]["state"] == "failed")
        assert payload["drc"]["error"] == "Save the board first"
        assert not (project / ".kileidoscope").exists()
    finally:
        client.close()
        runtime.close()


def test_a_board_saved_after_the_link_started_gets_its_folder(tmp_path, monkeypatch):
    """KiCad names a new project's board before its file exists: once the user saves it,
    the findings and the export's project folder move to it without a reconnect."""
    import kileido_bridge.loop as loop_module
    runtime, reader, client, project = live(tmp_path, monkeypatch, saved=False)
    board_file = project / "board.kicad_pcb"
    board_file.unlink()
    monkeypatch.setattr(loop_module, "saved_board_path", lambda _board: str(board_file) if board_file.is_file() else "")
    try:
        runtime.step()
        client.connect()
        until_payload(runtime, client, lambda p: p["status"] == "unsaved")
        assert runtime.copy.project_dir == ""
        board_file.write_text(FakeBoard.contents, encoding="utf-8")  # the user saves
        frames = exchange(runtime, client, lambda f: any(p["status"] == "none" for p in payloads(f)), limit=LIMIT)
        payload = payloads(frames)[-1]
        assert payload["folder"] == str(project / ".kileidoscope")
        assert runtime.board_path == str(board_file) and runtime.copy.project_dir == str(project)
        exports = [header["export"] for header, _ in frames if header["type"] == "export"]
        assert exports and exports[-1]["project_dir"] == str(project)  # Blender learns the folder too
    finally:
        client.close()
        runtime.close()


def test_run_drc_checks_a_frozen_copy_of_the_open_board(tmp_path, monkeypatch):
    snapshot = fixture()
    track = snapshot.tracks[0]
    calls = []

    def run_drc(cli, run_dir):
        calls.append((cli, (run_dir / "board.kicad_pcb").read_text(encoding="utf-8")))
        return {"kicad_version": "10.0.3", "violations": [{
            "type": "track_dangling", "severity": "warning", "description": "Track has unconnected end",
            "items": [{"uuid": track.id, "description": f"Track [{track.net}] on {track.layer}",
                       "pos": {"x": track.start[0] / 1e6, "y": track.start[1] / 1e6}}]}]}

    watch = findings_watch.FindingsWatch(run_drc=run_drc, find_cli=lambda hint: hint or "kicad-cli")
    runtime, reader, client, project = live(tmp_path, monkeypatch, watch=watch, snapshot=snapshot)
    try:
        runtime.step()
        client.connect()
        until_payload(runtime, client, lambda p: p["status"] == "none")
        request(client, "run_drc")
        frames = exchange(runtime, client, lambda f: any(p["sources"]["drc"]["status"] == "ok"
                                                         for p in payloads(f)), limit=LIMIT)
        states = [p["drc"]["state"] for p in payloads(frames)]
        assert "running" in states and states[-1] == "idle"
        payload = payloads(frames)[-1]
        assert payload["drc"] == {"state": "idle", "error": "", "run": "1"}
        assert calls == [("kicad-cli", FakeBoard.contents)]  # the raw board text, unsaved edits included
        drc = payload["sources"]["drc"]
        assert drc["stamp"] == kls_folder.stamp(FakeBoard.contents) == drc["live_stamp"]
        assert not drc["changed"] and drc["tool"] == "KiCad DRC 10.0.3"
        (row,) = drc["findings"]
        assert row["check"] == "drc.track_dangling" and row["draws"][0]["ids"] == [track.id]
        folder = project / ".kileidoscope"
        assert (folder / "runs" / "1" / "board.kicad_pcb").read_text(encoding="utf-8") == FakeBoard.contents
        assert (folder / "board.drc.kls-findings.json").is_file()
        assert (folder / ".gitignore").read_text(encoding="utf-8") == "*\n"

        def broken(cli, run_dir):
            raise findings_watch.drc_findings.DrcFailed("Failed to load board")
        watch._run_drc = broken
        request(client, "run_drc")
        payload = until_payload(runtime, client, lambda p: p["drc"]["state"] == "failed")
        assert payload["drc"] == {"state": "failed", "error": "Failed to load board", "run": "2"}
        assert payload["sources"]["drc"]["findings"]  # the last run's findings stay listed
    finally:
        client.close()
        runtime.close()


def test_the_server_queues_only_well_formed_findings_requests():
    server = BridgeServer(port=0, token="t")
    try:
        server._handle({"type": "findings", "action": "dismiss", "key": "3f2a"})
        server._handle({"type": "findings", "action": "confirm", "key": "3f2a", "source": "file"})
        server._handle({"type": "findings", "action": "run_drc"})
        for bad in ({"action": "delete", "key": ""}, {"action": ["run_drc"]}, {"action": "dismiss", "key": 5},
                    {"action": "dismiss", "key": "k" * 65}, {"action": "dismiss", "source": "../x"},
                    {"action": "dismiss", "source": ["drc"]}, {"action": "undo", "source": "file"},
                    {"action": "accept", "source": "file"}):
            server._handle({"type": "findings", **bad})
        requests = server.take_findings_requests()
        assert requests == [("dismiss", "3f2a", "drc"), ("confirm", "3f2a", "file"), ("run_drc", "", "drc")]
        assert server.take_findings_requests() == []
        for _ in range(MAX_FINDINGS_REQUESTS + 10):
            server._handle({"type": "findings", "action": "reset", "key": "k"})
        assert len(server.take_findings_requests()) == MAX_FINDINGS_REQUESTS
    finally:
        server.close()


def test_a_drc_file_from_an_earlier_session_is_discarded(tmp_path, monkeypatch):
    """The bridge's own DRC file is not carried into a new session: DRC is rerun by hand, so
    what is drawn always comes from this bridge's converter. A tool's file stays."""
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    write(project, tool_file(track), name="board.drc.kls-findings.json")
    write(project, tool_file(track))
    try:
        runtime.step()
        client.connect()
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        assert not (project / ".kileidoscope" / "board.drc.kls-findings.json").exists()
        assert payload["sources"]["drc"]["status"] == "none"
        assert [r["title"] for r in file_rows(payload)] == ["Stub on the clock"]
    finally:
        client.close()
        runtime.close()
