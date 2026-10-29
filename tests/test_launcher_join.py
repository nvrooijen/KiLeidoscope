"""A second Open in Blender joins the open KiLeidoscope Blender (multi-board): the launcher's
side of the join files, the bridge's lifetime, the Windows pipe file and the logs."""

import json
import os
import time
from types import SimpleNamespace

import pytest

from kileido_bridge import launcher


@pytest.fixture(autouse=True)
def _contained(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(launcher, "_stop_on_signals", lambda: None)
    monkeypatch.setattr(launcher, "_tell_user", lambda message: None)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def _viewer(folder, pid, protocol=launcher.JOIN_PROTOCOL):
    (folder / f"viewer-{pid}.json").write_text(json.dumps({"pid": pid, "protocol": protocol}), encoding="utf-8")


def test_open_viewers_lists_running_blenders_and_drops_closed_ones(monkeypatch):
    folder = launcher.viewer_folder()
    for pid in (11, 22, 33):
        _viewer(folder, pid)
    _viewer(folder, 44, protocol=99)  # another version's: left alone
    monkeypatch.setattr(launcher, "process_alive", lambda pid: pid != 22)
    assert sorted(launcher.open_viewers(folder)) == [11, 33]
    assert not (folder / "viewer-22.json").exists() and (folder / "viewer-44.json").exists()


def test_process_alive_knows_this_process():
    assert launcher.process_alive(os.getpid())


def _answering(folder, answer, after=0.0, clock=None):
    """A wait() that plays the viewer: takes the request at once, answers `after` s later."""
    state = {"taken": None}

    def wait(seconds):
        clock.wait(seconds)
        requests = list(folder.glob("join-*.json"))
        if requests and state["taken"] is None:
            request = requests[0]
            state["taken"] = (clock(), request.stem.rsplit("-", 1)[-1], json.loads(request.read_text()))
            request.unlink()
        if state["taken"] and answer is not None and clock() - state["taken"][0] >= after:
            (folder / f"reply-{state['taken'][1]}.json").write_text(json.dumps(answer), encoding="utf-8")
            state["taken"] = (float("inf"), *state["taken"][1:])
    return wait, state


def test_join_answer_comes_back_and_the_files_are_cleaned_up():
    folder, clock = launcher.viewer_folder(), Clock()
    wait, state = _answering(folder, {"accepted": True, "slot": 2, "reason": "free"}, clock=clock)
    offer = {"port": 1234, "token": "t", "board_name": "B.kicad_pcb", "board_path": ""}
    answer = launcher.ask_to_join(folder, 77, offer, wait, clock)
    assert answer == {"accepted": True, "slot": 2, "reason": "free"}
    assert state["taken"][2]["port"] == 1234 and state["taken"][2]["protocol"] == launcher.JOIN_PROTOCOL
    assert not list(folder.glob("join-*")) and not list(folder.glob("reply-*"))


def test_a_user_may_take_a_minute_to_answer():
    folder, clock = launcher.viewer_folder(), Clock()
    wait, _ = _answering(folder, {"accepted": False, "slot": None, "reason": "own window"}, after=60.0, clock=clock)
    assert launcher.ask_to_join(folder, 77, {}, wait, clock)["reason"] == "own window"


def test_an_untaken_request_is_withdrawn_and_an_unanswered_one_gives_up():
    folder, clock = launcher.viewer_folder(), Clock()
    assert launcher.ask_to_join(folder, 77, {}, clock.wait, clock) is None  # nobody took it
    assert not list(folder.glob("join-*")) and clock() >= launcher.REPLY_WAIT_S
    clock = Clock()
    wait, _ = _answering(folder, None, clock=clock)  # taken, never answered
    assert launcher.ask_to_join(folder, 77, {}, wait, clock) is None
    assert launcher.ANSWER_WAIT_S <= clock() < launcher.ANSWER_WAIT_S + 1


def test_abandoned_after_the_viewer_left_or_never_came():
    server, clock = SimpleNamespace(authenticated=False), Clock()
    abandoned = launcher.Abandoned(server, clock)
    clock.now = launcher.Abandoned.START_S - 1
    assert not abandoned()
    server.authenticated = True
    assert not abandoned()  # connected
    server.authenticated = False
    clock.now += launcher.Abandoned.ABANDON_S - 1
    assert not abandoned()  # a viewer reconnecting after a hiccup
    clock.now += 2
    assert abandoned()  # its slot took a newer bridge
    never = launcher.Abandoned(SimpleNamespace(authenticated=False), Clock())
    never.clock.now = launcher.Abandoned.START_S + 1
    assert never()


def _launch_parts(monkeypatch, authenticated=True):
    monkeypatch.setenv("KICAD_API_SOCKET", "pipe-b")
    server = SimpleNamespace(port=2345, token="tok", authenticated=authenticated)
    runtime = SimpleNamespace(started=0, steps=0, closed=0, snapshot=SimpleNamespace(board_name="B.kicad_pcb"),
                              board_path="/boards/B.kicad_pcb")
    runtime.start = lambda: setattr(runtime, "started", runtime.started + 1)
    runtime.step = lambda: setattr(runtime, "steps", runtime.steps + 1)
    runtime.wait = lambda timeout: None
    runtime.close = lambda: setattr(runtime, "closed", runtime.closed + 1)
    monkeypatch.setattr(launcher, "BridgeServer", lambda port: server)
    monkeypatch.setattr(launcher, "BridgeRuntime", lambda s: runtime)
    return server, runtime


def test_launch_joins_the_open_blender_instead_of_starting_one(monkeypatch, tmp_path):
    server, runtime = _launch_parts(monkeypatch)
    _viewer(launcher.viewer_folder(), 5150)
    asked = []
    monkeypatch.setattr(launcher, "ask_to_join",
                        lambda folder, pid, offer, wait: asked.append((pid, offer)) or {"accepted": True, "slot": 2})
    alive = iter((True, True, True, False))
    monkeypatch.setattr(launcher, "process_alive", lambda pid: next(alive) if pid == 5150 else False)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("started a Blender"))
    monkeypatch.setattr(launcher, "find_blender", lambda: pytest.fail("looked for Blender"))
    assert launcher.launch(tmp_path) == 0
    assert asked == [(5150, {"port": 2345, "token": "tok", "board_name": "B.kicad_pcb",
                             "board_path": "/boards/B.kicad_pcb"})]
    assert runtime.started == 1 and runtime.steps >= 1 and runtime.closed == 1
    assert launcher.viewer_pid("pipe-b") is None  # removed once that Blender let go


def test_launch_opens_its_own_window_when_asked_to(monkeypatch, tmp_path):
    server, runtime = _launch_parts(monkeypatch)
    _viewer(launcher.viewer_folder(), 5150)
    monkeypatch.setattr(launcher, "process_alive", lambda pid: True)
    monkeypatch.setattr(launcher, "ask_to_join",
                        lambda folder, pid, offer, wait: {"accepted": False, "slot": None, "reason": "own window"})
    monkeypatch.setattr(launcher, "find_blender", lambda: "blender")
    started = {}
    outcomes = iter((None, 0))

    def popen(command, **kwargs):
        started.update(command=command, log=kwargs["stdout"].name)
        return SimpleNamespace(poll=lambda: next(outcomes), returncode=0, pid=6000)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert launcher.launch(tmp_path) == 0
    assert started["log"].endswith(f"blender-{os.getpid()}.log")  # the open Blender keeps blender.log
    assert runtime.started == 1  # started once, for the request, not again


def test_a_question_answered_late_does_not_cost_the_new_blender_its_bridge(monkeypatch, tmp_path):
    """The user takes minutes over the open Blender's question, then it opens in its own
    window: the bridge serves that Blender (its START_S counts from when it was started)."""
    server, runtime = _launch_parts(monkeypatch, authenticated=False)
    _viewer(launcher.viewer_folder(), 5150)
    monkeypatch.setattr(launcher, "process_alive", lambda pid: True)
    clock = Clock()
    monkeypatch.setattr(launcher.time, "monotonic", clock)
    runtime.wait = lambda timeout: clock.wait(timeout)

    def declined_late(folder, pid, offer, wait):
        wait(launcher.REPLY_WAIT_S + launcher.ANSWER_WAIT_S)  # "Open in its own window", after two minutes
        return {"accepted": False, "slot": None, "reason": "own window"}

    monkeypatch.setattr(launcher, "ask_to_join", declined_late)
    monkeypatch.setattr(launcher, "find_blender", lambda: "blender")
    outcomes = iter((None, None, None, 0))
    steps_at_start = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: steps_at_start.append(runtime.steps) or
                        SimpleNamespace(poll=lambda: next(outcomes), returncode=0, pid=6002))
    assert launcher.launch(tmp_path) == 0
    assert runtime.steps - steps_at_start[0] >= 3, "the bridge gave up on the Blender it had just started"


def test_a_bridge_left_by_its_viewer_ends_while_blender_runs_on(monkeypatch, tmp_path):
    server, runtime = _launch_parts(monkeypatch, authenticated=False)
    monkeypatch.setattr(launcher, "find_blender", lambda: "blender")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: SimpleNamespace(poll=lambda: None, pid=6001))
    clock = Clock()
    monkeypatch.setattr(launcher.time, "monotonic", clock)
    runtime.wait = lambda timeout: clock.wait(1.0)
    assert launcher.launch(tmp_path) == 0  # did not wait for Blender to close
    assert clock() > launcher.Abandoned.START_S


def test_windows_pipe_file_made_once_and_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.tempfile, "gettempdir", lambda: str(tmp_path))
    launcher.keep_kicad_connections_apart()
    marker = tmp_path / "kicad" / "api.sock"
    assert marker.is_file() and marker.stat().st_size == 0
    marker.write_text("kept")  # one another tool made: left as it is
    launcher.keep_kicad_connections_apart()
    assert marker.read_text() == "kept"
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    marker.unlink()
    launcher.keep_kicad_connections_apart()
    assert not marker.exists()  # Linux gives each KiCad its own socket already


def test_numbered_logs_are_cleaned_after_a_week(tmp_path):
    root = launcher.cache_root()
    root.mkdir(parents=True)
    old, recent = root / "blender-1.log", root / "blender-2.log"
    old.write_text("x")
    recent.write_text("y")
    week_ago = time.time() - launcher.LOG_KEEP_S - 60
    os.utime(old, (week_ago, week_ago))
    assert launcher.log_path(shared=False) == root / "blender.log"
    assert launcher.log_path(shared=True) == root / f"blender-{os.getpid()}.log"
    assert not old.exists() and recent.exists()
