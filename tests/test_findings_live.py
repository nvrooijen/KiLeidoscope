"""Findings over the live link: `.kileidoscope/` files in, `findings` frames out, requests back."""

import json
import sys
import time
import types
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import kileido_bridge
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
    runtime.findings = quick(watch or findings_watch.FindingsWatch(find_cli=lambda hint: ""))  # no kicad-cli here
    client = addon_client().SocketClient("127.0.0.1", server.port, "t")
    return runtime, reader, client, project


@contextmanager
def connected(runtime, client):
    """The runtime stepped once and the add-on connected; both closed after."""
    try:
        runtime.step()
        client.connect()
        yield
    finally:
        client.close()
        runtime.close()


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


def dangling(track, version):
    """KiCad's DRC report of one dangling track."""
    return {"kicad_version": version, "violations": [{
        "type": "track_dangling", "severity": "warning", "description": "Track has unconnected end",
        "items": [{"uuid": track.id, "description": f"Track [{track.net}] on {track.layer}",
                   "pos": {"x": track.start[0] / 1e6, "y": track.start[1] / 1e6}}]}]}


def file_rows(payload):
    return payload["sources"]["file"]["findings"]


def test_a_written_findings_file_reaches_blender_after_the_snapshot(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    with connected(runtime, client):
        frames = exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f)
                          and any(p["status"] == "none" for p in payloads(f)), limit=LIMIT)
        none = payloads(frames)[-1]
        assert none["folder"] == str(project / ".kileidoscope") and none["drc"]["state"] == "idle"
        assert set(none["sources"]) == {"drc", "phase", "file"}
        write(project, tool_file(track))
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        (row,) = file_rows(payload)
        assert row["title"] == "Stub on the clock" and row["state"] == "" and not row["faded"]
        source = payload["sources"]["file"]
        assert source["status"] == "ok" and source["connected"] and source["handshake"] == "outside"
        assert row["draws"][0]["ids"] == [track.id]  # another tool's file: drawn straight away
        assert payload["sources"]["drc"]["status"] == "none"
        types = [h["type"] for h, _ in frames]
        assert "findings" not in types[:types.index("snapshot_end")]  # never inside a snapshot

        (project / ".kileidoscope" / "board.kls-findings.json").write_text("{not json", encoding="utf-8")
        broken = until_payload(runtime, client, lambda p: p["sources"]["file"]["status"] == "error")
        assert broken["sources"]["file"]["error"].startswith("not JSON")
        assert [r["title"] for r in file_rows(broken)] == ["Stub on the clock"]  # the last good list stays


def test_dismiss_is_saved_and_sent_again(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    write(project, tool_file(reader.snapshot.tracks[0]))
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        key = file_rows(payload)[0]["key"]
        request(client, "dismiss", key, source="file")
        payload = until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["state"] == "dismissed")
        assert file_rows(payload)[0]["key"] == key
        state = project / ".kileidoscope" / "dismissed.json"
        for _ in range(LIMIT):  # written on the watch's thread, atomically: an open can land mid-replace
            runtime.step()
            try:
                saved = json.loads(state.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        else:
            raise AssertionError("dismissed.json was not written")
        entry = saved["findings"][findings_watch.state_key("file", key)]
        assert entry["state"] == "dismissed" and entry["check"] == "hs.stub"
        assert entry["title"] == "Stub on the clock" and entry["board"] == "board"
        request(client, "dismiss", key)  # a DRC finding by that key: there is none, nothing changes
        request(client, "reset", key, source="file")
        payload = until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["state"] == "")
        assert file_rows(payload)[0]["draws"][0]["ids"] == [reader.snapshot.tracks[0].id]


def test_a_resync_sends_the_findings_again_after_the_snapshot(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    write(project, tool_file(reader.snapshot.tracks[0]))
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "ok")
        client.request_resync()
        frames = exchange(runtime, client, lambda f: any(h["type"] == "snapshot_end" for h, _ in f)
                          and any(h["type"] == "findings" for h, _ in f), limit=LIMIT)
        types = [h["type"] for h, _ in frames]
        assert types.index("findings") > types.index("snapshot_end")
        assert file_rows(payloads(frames)[-1])[0]["title"] == "Stub on the clock"


def test_a_removed_track_fades_its_finding(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    write(project, tool_file(track))
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        assert not file_rows(payload)[0]["faded"]
        without = replace(reader.snapshot, tracks=reader.snapshot.tracks[1:])
        reader.events.append((without, {(track.layer, "tracks")}))
        payload = until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["faded"])
        assert file_rows(payload)[0]["title"] == "Stub on the clock"


def test_a_disconnect_keeps_the_folder_and_the_keys(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    write(project, tool_file(reader.snapshot.tracks[0]))
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        folder, key = runtime.findings.folder, file_rows(payload)[0]["key"]
        reader.events.append(ConnectionError("KiCad closed"))
        payload = until_payload(runtime, client, lambda p: not p["sources"]["file"]["connected"])
        assert runtime.reader is None and runtime.findings.folder is folder
        assert payload["status"] == "ok" and file_rows(payload)[0]["key"] == key  # not renamed without the board
        client.request_resync()  # a viewer that reconnects meanwhile gets the list too
        frames = exchange(runtime, client, lambda f: any(h["type"] == "findings" for h, _ in f), limit=LIMIT)
        assert file_rows(payloads(frames)[-1])[0]["key"] == key


def test_a_file_changed_while_kicad_is_gone_is_read_once_it_is_back(tmp_path, monkeypatch):
    """Keys name items by the board, so a file that changes while KiCad is disconnected
    stays unread (a dismiss meanwhile would be saved under a key that changes back); it is
    read as soon as KiCad is connected again."""
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    write(project, tool_file(track))
    with connected(runtime, client):
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


def test_an_unsaved_board_says_so(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch, saved=False)
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "unsaved")
        assert payload["folder"] == ""
        assert {source["status"] for source in payload["sources"].values()} == {"unsaved"}
        request(client, "run_drc")
        payload = until_payload(runtime, client, lambda p: p["drc"]["state"] == "failed")
        assert payload["drc"]["error"] == "Save the board first"
        assert not (project / ".kileidoscope").exists()


def test_a_board_saved_after_the_link_started_gets_its_folder(tmp_path, monkeypatch):
    """KiCad names a new project's board before its file exists: once the user saves it,
    the findings and the export's project folder move to it without a reconnect."""
    import kileido_bridge.loop as loop_module
    runtime, reader, client, project = live(tmp_path, monkeypatch, saved=False)
    board_file = project / "board.kicad_pcb"
    board_file.unlink()
    monkeypatch.setattr(loop_module, "saved_board_path", lambda _board: str(board_file) if board_file.is_file() else "")
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "unsaved")
        assert runtime.copy.project_dir == ""
        board_file.write_text(FakeBoard.contents, encoding="utf-8")  # the user saves
        frames = exchange(runtime, client, lambda f: any(p["status"] == "none" for p in payloads(f)), limit=LIMIT)
        payload = payloads(frames)[-1]
        assert payload["folder"] == str(project / ".kileidoscope")
        assert runtime.board_path == str(board_file) and runtime.copy.project_dir == str(project)
        exports = [header["export"] for header, _ in frames if header["type"] == "export"]
        assert exports and exports[-1]["project_dir"] == str(project)  # Blender learns the folder too


def test_run_drc_checks_a_frozen_copy_of_the_open_board(tmp_path, monkeypatch):
    snapshot = fixture()
    track = snapshot.tracks[0]
    calls = []

    def run_drc(cli, run_dir):
        calls.append((cli, (run_dir / "board.kicad_pcb").read_text(encoding="utf-8")))
        return dangling(track, "10.0.3")

    watch = findings_watch.FindingsWatch(run_drc=run_drc, find_cli=lambda hint: hint or "kicad-cli")
    runtime, reader, client, project = live(tmp_path, monkeypatch, watch=watch, snapshot=snapshot)
    with connected(runtime, client):
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
        assert drc["handshake"] == "known" and not drc["changed"] and drc["tool"] == "KiCad DRC 10.0.3"
        (row,) = drc["findings"]
        assert row["check"] == "drc.track_dangling" and row["draws"][0]["ids"] == [track.id]
        folder = project / ".kileidoscope"
        assert (folder / "runs" / "1" / "board.kicad_pcb").read_text(encoding="utf-8") == FakeBoard.contents
        written = json.loads((folder / "board.drc.kls-findings.json").read_text(encoding="utf-8"))
        assert written["stamp"] == kls_folder.stamp(FakeBoard.contents)  # the live board's stamp
        assert (folder / ".gitignore").read_text(encoding="utf-8") == "*\n"

        def broken(cli, run_dir):
            raise findings_watch.drc_findings.DrcFailed("Failed to load board")
        watch._run_drc = broken
        request(client, "run_drc")
        payload = until_payload(runtime, client, lambda p: p["drc"]["state"] == "failed")
        assert payload["drc"] == {"state": "failed", "error": "Failed to load board", "run": "2"}
        assert payload["sources"]["drc"]["findings"]  # the last run's findings stay listed


def test_dynamic_phase_from_the_drc_column_lists_its_excursions(tmp_path, monkeypatch):
    """The DRC column's Dynamic phase button: every pair measured now, its findings file
    written beside KiCad's DRC's and read back, so the excursion shows in the list."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import phase_boards
    runtime, reader, client, project = live(tmp_path, monkeypatch, snapshot=phase_boards.compensated_far())
    phase_boards.project_file(project)  # board.kicad_pro beside the board: its tuning profile
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "none")
        request(client, "run_phase", source="phase")
        payload = until_payload(runtime, client, lambda p: p["sources"]["phase"]["status"] == "ok")
        assert payload["phase"] == {"state": "idle", "error": ""}
        source = payload["sources"]["phase"]
        assert source["tool"].startswith("Dynamic phase; 1 pair measured")
        (row,) = source["findings"]
        assert row["check"] == "phase.excursion" and row["draws"][0]["ids"] and row["draws"][1]["tool"] == "label"
        written = json.loads((project / ".kileidoscope" / "board.phase.kls-findings.json").read_text(encoding="utf-8"))
        assert written["findings"][0]["check"] == "phase.excursion"
        assert payload["sources"]["drc"]["status"] == "none"  # KiCad's DRC was not run


def test_dynamic_phase_needs_a_saved_board(tmp_path, monkeypatch):
    runtime, reader, client, project = live(tmp_path, monkeypatch, saved=False)
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "unsaved")
        request(client, "run_phase", source="phase")
        payload = until_payload(runtime, client, lambda p: p["phase"]["state"] == "failed")
        assert payload["phase"]["error"] == findings_watch.SAVE_FIRST


Check = namedtuple("Check", "id group title instruction evidence draws v1 values", defaults=((),))
CATALOGUE = (Check("plane.gap_crossing", "plane", "Signal crossing a plane gap", "Find them", "net", ("highlight",),
                   True),
             Check("decap.distance", "decap", "Decoupling cap far from its pin", "Measure", "cap", ("distance",),
                   True),
             Check("hs.stub", "hs", "Stubs on fast nets", "Find stubs", "net", ("highlight",), False))


def stub_modules(monkeypatch, calls, fail=None, drcs=None, dismissals=None):
    """checks and prompt as their interfaces say; what KiCad's DRC gave each prompt goes
    into `drcs`, what the user dismissed into `dismissals`."""
    checks = types.ModuleType("kileido_bridge.checks")
    checks.CATALOGUE = CATALOGUE
    checks.GROUPS = ("plane", "decap", "hs")
    checks.GROUP_TITLES = {"plane": "Return paths and planes", "decap": "Decoupling", "hs": "High speed"}
    checks.by_id = lambda ids: tuple(check for check in CATALOGUE if check.id in set(ids))
    prompt = types.ModuleType("kileido_bridge.prompt")

    def make_run(folder, text, snapshot, chosen, notes="", drc=None, dismissed=()):
        if drcs is not None:
            drcs.append(drc)
        if dismissals is not None:
            dismissals.append(list(dismissed))
        if fail:
            raise fail
        run = folder.new_run(text, "prompt")
        folder._write(f"runs/{run.id}/request.md", b"# KiLeidoscope review request")
        calls.append((text, snapshot is not None, [check.id for check in chosen], notes))
        return run, f"Run: {run.id}   Stamp: {run.stamp}\nRead request.md in {run.directory} first."
    prompt.make_run = make_run
    for name, module in (("checks", checks), ("prompt", prompt)):
        monkeypatch.setitem(sys.modules, f"kileido_bridge.{name}", module)
        monkeypatch.setattr(kileido_bridge, name, module, raising=False)


def test_a_prompt_runs_kicads_drc_first_and_names_what_it_found(tmp_path, monkeypatch):
    calls, drcs, texts = [], [], []
    stub_modules(monkeypatch, calls, drcs=drcs)

    def run_drc(cli, run_dir):
        texts.append((run_dir / "board.kicad_pcb").read_text(encoding="utf-8"))
        return dangling(reader.snapshot.tracks[0], "10.0.6")

    watch = findings_watch.FindingsWatch(run_drc=run_drc, find_cli=lambda hint: hint or "kicad-cli")
    runtime, reader, client, project = live(tmp_path, monkeypatch, watch=watch)
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "none")
        client.request_findings("prompt", "", "file", checks=["decap.distance"])
        frames = exchange(runtime, client, lambda f: any(p["prompt"]["state"] == "ready" for p in payloads(f))
                          and any(p["sources"]["drc"]["status"] == "ok" for p in payloads(f)), limit=LIMIT)
        assert "running" in [p["drc"]["state"] for p in payloads(frames)]  # the DRC tab shows it running
        assert texts == [FakeBoard.contents]  # on the same board text as the prompt's run
        assert len(drcs) == 1 and sum(drcs[0].values()) == 1 and all(k.startswith("drc.") for k in drcs[0])
        payload = payloads(frames)[-1]
        assert payload["drc"] == {"state": "idle", "error": "", "run": "1"} and payload["prompt"]["run"] == "2"


def test_without_kicad_cli_a_prompt_says_why_drc_did_not_run(tmp_path, monkeypatch):
    calls, drcs = [], []
    stub_modules(monkeypatch, calls, drcs=drcs)
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "none")
        client.request_findings("prompt", "", "file", checks=["decap.distance"])
        payload = until_payload(runtime, client, lambda p: p["prompt"]["state"] == "ready")
        assert payload["prompt"]["run"] == "1" and "kicad-cli not found" in drcs[0]
        assert payload["drc"]["state"] == "failed"  # the DRC tab says so too


def test_copy_prompt_freezes_a_run_its_findings_echo(tmp_path, monkeypatch):
    calls = []
    stub_modules(monkeypatch, calls)
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "none")
        assert payload["prompt"] == {"state": "idle", "run": "", "text": "", "error": ""}
        assert payload["progress"] == {"state": "none", "run": "", "done": 0, "total": 0, "check": "", "note": "",
                                       "updated_unix": 0.0, "made_unix": 0.0}
        assert payload["catalogue"][0] == {"id": "plane.gap_crossing", "group": "plane",
                                           "title": "Signal crossing a plane gap", "v1": True, "measurable": False}
        assert [row["v1"] for row in payload["catalogue"]] == [True, True, False]
        assert [row["measurable"] for row in payload["catalogue"]] == [False, True, False]  # distance measures
        assert payload["groups"][1] == {"id": "decap", "title": "Decoupling"}
        client.request_findings("prompt", "", "file", checks=["decap.distance", "not.a_check", "hs.stub"])
        frames = exchange(runtime, client, lambda f: any(p["prompt"]["state"] == "ready" for p in payloads(f)),
                          limit=LIMIT)
        assert "making" in [p["prompt"]["state"] for p in payloads(frames)]
        prompt = payloads(frames)[-1]["prompt"]
        stamp = kls_folder.stamp(FakeBoard.contents)
        assert prompt["run"] == "1" and prompt["error"] == "" and prompt["text"].startswith(f"Run: 1   Stamp: {stamp}")
        assert calls == [(FakeBoard.contents, True, ["decap.distance", "hs.stub"], "")]  # unknown ids dropped
        run = project / ".kileidoscope" / "runs" / "1"
        assert (run / "request.md").is_file()
        assert (run / "board.kicad_pcb").read_text(encoding="utf-8") == FakeBoard.contents
        # The LLM's answer echoes the run and its stamp: a known run, drawn.
        write(project, {**tool_file(track), "run": "1", "stamp": stamp})
        payload = until_payload(runtime, client, lambda p: p["sources"]["file"]["status"] == "ok")
        source = payload["sources"]["file"]
        assert source["handshake"] == "known" and source["run"] == "1" and not source["changed"]
        assert file_rows(payload)[0]["draws"][0]["ids"] == [track.id]
        assert payload["prompt"]["state"] == "ready"  # kept until the next prompt
        # None of the ticked checks known: said, no run made.
        client.request_findings("prompt", "", "file", checks=["not.a_check"])
        payload = until_payload(runtime, client, lambda p: p["prompt"]["state"] == "failed")
        assert payload["prompt"]["error"] == findings_watch.NO_CHECKS and len(calls) == 1


def write_progress(project, data):
    write(project, data, "board.kls-progress.json")
    return project / ".kileidoscope" / "board.kls-progress.json"


def until_progress_read(runtime, client, test):
    """Step until the watch has read the progress file (`test` on what it parsed)."""
    exchange(runtime, client, lambda _frames: test(runtime.findings._progress[0]), limit=LIMIT)
    return runtime.findings.result()[1]["progress"]


def test_progress_waits_then_works_then_is_done(tmp_path, monkeypatch):
    calls = []
    stub_modules(monkeypatch, calls)
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    stamp = kls_folder.stamp(FakeBoard.contents)
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "none")
        request(client, "prompt", source="file", checks=["plane.gap_crossing"], notes="Mind J5")
        payload = until_payload(runtime, client, lambda p: p["progress"]["state"] == "waiting")
        progress = payload["progress"]
        assert payload["prompt"]["state"] == "ready" and calls[0][3] == "Mind J5"
        assert progress["run"] == "1" and progress["made_unix"] > 0 and progress["updated_unix"] == 0.0
        made = progress["made_unix"]
        # Another run's progress, this run with another stamp, and broken files are ignored.
        other = {"format": "kls-progress 1", "run": "7", "stamp": stamp, "done": 1, "total": 2}
        write_progress(project, {**other, "run": "1", "stamp": "deadbeef"})
        assert until_progress_read(runtime, client, lambda p: p and p["stamp"] == "deadbeef")["state"] == "waiting"
        for data in ("{not json", {**other, "format": "kls-progress 9", "run": "1"}, {**other, "run": 1},
                     {**other, "run": "1", "note": "x" * 5000}):
            write_progress(project, other)
            assert until_progress_read(runtime, client, lambda p: p and p["run"] == "7")["state"] == "waiting"
            path = project / ".kileidoscope" / "board.kls-progress.json"
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
            assert until_progress_read(runtime, client, lambda p: p is None)["state"] == "waiting"
        # A progress file for this run and stamp: working, never trusted.
        path = write_progress(project, {"format": "kls-progress 1", "run": "1", "stamp": stamp, "done": 10 ** 9,
                                        "total": -3, "check": "plane.gap_crossing", "note": "Tracing\tGND‮" * 20})
        payload = until_payload(runtime, client, lambda p: p["progress"]["state"] == "working")
        progress = payload["progress"]
        assert progress["run"] == "1" and progress["made_unix"] == made
        assert progress["done"] == findings_watch.MAX_PROGRESS_COUNT and progress["total"] == 0
        assert progress["check"] == "plane.gap_crossing" and progress["updated_unix"] == path.stat().st_mtime
        assert len(progress["note"]) == kls_folder.MAX_PROGRESS_NOTE and progress["note"].endswith("…")
        assert progress["note"].startswith("Tracing GND Tracing") and "\t" not in progress["note"]
        # The findings file for this run is in: done.
        write(project, {**tool_file(reader.snapshot.tracks[0]), "run": "1", "stamp": stamp})
        payload = until_payload(runtime, client, lambda p: p["progress"]["state"] == "done")
        answer = project / ".kileidoscope" / "board.kls-findings.json"
        assert payload["progress"]["run"] == "1" and payload["progress"]["made_unix"] == made
        assert payload["progress"]["updated_unix"] == answer.stat().st_mtime_ns / 1e9
        version = runtime.findings.result()[0]
        for _ in range(20):  # time passing alone publishes nothing
            runtime.step()
        assert runtime.findings.result()[0] == version


def test_progress_files_are_parsed_as_untrusted_data():
    good = {"format": "kls-progress 1", "run": "3", "stamp": "a1b2c3d4", "done": 2.7, "total": 5,
            "check": "decap.distance", "note": "line\nbreak"}
    assert findings_watch.parse_progress(json.dumps(good).encode()) == {
        "run": "3", "stamp": "a1b2c3d4", "done": 2, "total": 5, "check": "decap.distance", "note": "line break"}
    for done, total in ((True, "5"), (-1, float("nan")), (None, [5])):
        loose = findings_watch.parse_progress(json.dumps({**good, "done": done, "total": total, "check": 5,
                                                          "note": None}).encode())
        assert (loose["done"], loose["total"], loose["check"], loose["note"]) == (0, 0, "", "")
    for bad in (b"", b"[]", b"\xff", json.dumps({**good, "format": "kls-findings 1"}).encode(),
                json.dumps({**good, "stamp": None}).encode(), b"[" * 5000,
                json.dumps({**good, "note": "x" * kls_folder.MAX_PROGRESS_BYTES}).encode()):
        assert findings_watch.parse_progress(bad) is None


def test_a_prompt_that_cannot_be_made_says_why(tmp_path, monkeypatch):
    stub_modules(monkeypatch, [], fail=OSError("disk full"))
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "none")
        request(client, "prompt", source="file", checks=["plane.gap_crossing"])
        payload = until_payload(runtime, client, lambda p: p["prompt"]["state"] == "failed")
        assert payload["prompt"] == {"state": "failed", "run": "", "text": "", "error": "OSError: disk full"}


def test_an_unsaved_board_makes_no_prompt(tmp_path, monkeypatch):
    calls = []
    stub_modules(monkeypatch, calls)
    runtime, reader, client, project = live(tmp_path, monkeypatch, saved=False)
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "unsaved")
        assert [row["id"] for row in payload["catalogue"]] == [check.id for check in CATALOGUE]
        request(client, "prompt", source="file", checks=["plane.gap_crossing"])
        payload = until_payload(runtime, client, lambda p: p["prompt"]["state"] == "failed")
        assert payload["prompt"]["error"] == "Save the board first"
        assert not calls and not (project / ".kileidoscope").exists()


def test_without_board_text_no_prompt_is_made(tmp_path, monkeypatch):
    calls = []
    stub_modules(monkeypatch, calls)
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    with connected(runtime, client):
        until_payload(runtime, client, lambda p: p["status"] == "none")
        runtime.copy_enabled = False  # this KiCad cannot serialize boards
        request(client, "prompt", source="file", checks=["plane.gap_crossing"])
        payload = until_payload(runtime, client, lambda p: p["prompt"]["state"] == "failed")
        assert payload["prompt"]["error"] == findings_watch.NO_TEXT and not calls


def test_without_the_checks_module_the_catalogue_is_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "kileido_bridge.checks", None)  # an import of it fails
    monkeypatch.delattr(kileido_bridge, "checks", raising=False)
    assert findings_watch.catalogue() is None
    watch = findings_watch.FindingsWatch()
    watch.target("")
    watch.request("prompt", source="file", checks=["plane.gap_crossing"])
    _, payload = watch.result()
    assert payload["catalogue"] == [] and payload["groups"] == []
    assert payload["prompt"] == {"state": "failed", "run": "", "text": "", "error": "Save the board first"}


def test_the_server_queues_only_well_formed_findings_requests():
    server = BridgeServer(port=0, token="t")
    most_checks, most_notes = findings_watch.MAX_CHECKS, findings_watch.MAX_NOTES
    try:
        server._handle({"type": "findings", "action": "dismiss", "key": "3f2a"})
        server._handle({"type": "findings", "action": "confirm", "key": "3f2a", "source": "file"})
        server._handle({"type": "findings", "action": "run_drc"})
        server._handle({"type": "findings", "action": "prompt", "source": "file", "checks": ["plane.gap_crossing"]})
        server._handle({"type": "findings", "action": "prompt", "source": "file",
                        "checks": ["c" * 64] * most_checks, "notes": "n" * most_notes})
        server._handle({"type": "findings", "action": "prompt", "source": "file", "checks": ["plane.gap_crossing"],
                        "notes": "Mind J5,\nit is a test header"})
        for bad in ({"action": "delete", "key": ""}, {"action": ["run_drc"]}, {"action": "dismiss", "key": 5},
                    {"action": "dismiss", "key": "k" * 65}, {"action": "dismiss", "source": "../x"},
                    {"action": "dismiss", "source": ["drc"]}, {"action": "undo", "source": "file"},
                    {"action": "prompt", "checks": "plane.gap_crossing"},
                    {"action": "prompt", "checks": [5]}, {"action": "prompt", "checks": ["c" * 65]},
                    {"action": "prompt", "checks": ["c"] * (most_checks + 1)},
                    {"action": "prompt", "notes": 5}, {"action": "prompt", "notes": ["Mind J5"]},
                    {"action": "prompt", "notes": "n" * (most_notes + 1)}):
            server._handle({"type": "findings", **bad})
        requests = server.take_findings_requests()
        assert [request[:4] for request in requests[:4]] == [
            ("dismiss", "3f2a", "drc", ()), ("confirm", "3f2a", "file", ()), ("run_drc", "", "drc", ()),
            ("prompt", "", "file", ("plane.gap_crossing",))]
        assert len(requests) == 6 and len(requests[4].checks) == most_checks
        assert [request.notes for request in requests] == ["", "", "", "", "n" * most_notes,
                                                           "Mind J5,\nit is a test header"]
        assert server.take_findings_requests() == []
        for _ in range(MAX_FINDINGS_REQUESTS + 10):
            server._handle({"type": "findings", "action": "reset", "key": "k"})
        assert len(server.take_findings_requests()) == MAX_FINDINGS_REQUESTS
    finally:
        server.close()


def test_a_prompt_names_what_the_user_dismissed_on_this_board(tmp_path, monkeypatch):
    calls, dismissals = [], []
    stub_modules(monkeypatch, calls, dismissals=dismissals)
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    write(project, tool_file(reader.snapshot.tracks[0]))
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        request(client, "dismiss", file_rows(payload)[0]["key"], source="file")
        until_payload(runtime, client, lambda p: file_rows(p) and file_rows(p)[0]["state"] == "dismissed")
        request(client, "prompt", source="file", checks=["hs.stub"])
        until_payload(runtime, client, lambda p: p["prompt"]["state"] == "ready")
        assert dismissals == [[("hs.stub", "Stub on the clock")]]


def test_a_bug_in_one_job_never_ends_the_findings_thread(tmp_path):
    (tmp_path / "board.kicad_pcb").write_text(FakeBoard.contents, encoding="utf-8")
    watch = findings_watch.FindingsWatch(find_cli=lambda hint: "")
    watch.target(str(tmp_path / "board.kicad_pcb"))
    done, work = [], watch._work

    def flaky(*args):
        done.append(len(done))
        if len(done) == 1:
            raise RuntimeError("an odd snapshot")
        return work(*args)
    watch._work = flaky

    def job():
        with watch._lock:
            watch._job.save = True
            watch._kick()

    def until(test):
        for _ in range(LIMIT):
            if test():
                return
            time.sleep(0.001)
        raise AssertionError("not reached")
    job()
    until(lambda: watch.result()[1] is not None)
    payload = watch.result()[1]
    for source in ("drc", "file"):
        assert payload["sources"][source]["status"] == "error"
        assert payload["sources"][source]["error"] == ("could not read the findings (RuntimeError: an odd snapshot); "
                                                       "please report it")
    job()  # the thread still takes work
    until(lambda: len(done) == 2)
    until(lambda: (tmp_path / ".kileidoscope" / "dismissed.json").is_file())


def test_a_file_read_by_a_job_that_fails_later_is_kept(tmp_path):
    """The tick counts a file as read when it hands it over: a job that fails after reading
    it (a board index or run listing bug) must not lose it, or the next resolve would show
    the file before it."""
    (tmp_path / "board.kicad_pcb").write_text(FakeBoard.contents, encoding="utf-8")
    watch = findings_watch.FindingsWatch(find_cli=lambda hint: "")
    watch.target(str(tmp_path / "board.kicad_pcb"))
    folder, generation = watch.folder, watch._generation
    track = types.SimpleNamespace(id="11111111-2222-4333-8444-555555555555")
    old = tool_file(track)
    write(tmp_path, old)
    watch._work(findings_watch._Job(reload={"file"}), generation, folder, watch._board, watch._state)
    assert [r["title"] for r in file_rows(watch.result()[1])] == ["Stub on the clock"]
    new = {**old, "findings": [{**old["findings"][0], "title": "The newer answer"}]}
    write(tmp_path, new)
    runs = folder.runs
    folder.runs = lambda: (_ for _ in ()).throw(RuntimeError("a listing bug"))
    with pytest.raises(RuntimeError):
        watch._work(findings_watch._Job(reload={"file"}), generation, folder, watch._board, watch._state)
    folder.runs = runs
    watch._work(findings_watch._Job(resolve={"file"}), generation, folder, watch._board, watch._state)
    assert [r["title"] for r in file_rows(watch.result()[1])] == ["The newer answer"]


def test_a_drc_file_from_an_earlier_session_is_discarded(tmp_path, monkeypatch):
    """The bridge's own DRC file is not carried into a new session: DRC is rerun by hand, so
    what is drawn always comes from this bridge's converter. A tool's file stays."""
    runtime, reader, client, project = live(tmp_path, monkeypatch)
    track = reader.snapshot.tracks[0]
    write(project, tool_file(track), name="board.drc.kls-findings.json")
    write(project, tool_file(track))
    with connected(runtime, client):
        payload = until_payload(runtime, client, lambda p: p["status"] == "ok")
        assert not (project / ".kileidoscope" / "board.drc.kls-findings.json").exists()
        assert payload["sources"]["drc"]["status"] == "none"
        assert [r["title"] for r in file_rows(payload)] == ["Stub on the clock"]
