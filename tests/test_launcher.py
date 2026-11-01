"""The plugin launches one isolated viewer and passes credentials without UI."""
from types import SimpleNamespace

import pytest

from kileido_bridge import launcher


@pytest.fixture(autouse=True)
def _contained(monkeypatch, tmp_path):
    """No log in the real cache folder, no signal handlers or notifications in pytest."""
    monkeypatch.setattr(launcher, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(launcher, "_stop_on_signals", lambda: None)
    monkeypatch.setattr(launcher, "_tell_user", lambda message: None)


def test_plugin_lifecycle_and_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_API_SOCKET", "test-editor-pipe")
    monkeypatch.setenv("KICAD_API_TOKEN", "test-editor-token")
    monkeypatch.setattr(launcher, "find_blender", lambda: "blender")
    captured = {}
    closed = []
    steps = []
    server = SimpleNamespace(port=23456, token="test-bridge-token", pump=lambda wait: None)
    monkeypatch.setattr(launcher, "BridgeServer", lambda port: server if port == 0 else None)
    monkeypatch.setattr(launcher, "BridgeRuntime", lambda s: SimpleNamespace(
        start=lambda: None, step=lambda: steps.append(1), wait=lambda timeout: None, close=lambda: closed.append(1)))
    outcomes = iter((None, 0))

    def popen(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(poll=lambda: next(outcomes), returncode=0, pid=4321)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert launcher.launch(tmp_path) == 0
    assert len(steps) == 1 and closed == [1]
    assert launcher.viewer_pid("test-editor-pipe") is None  # removed with the viewer
    assert captured["env"]["KICAD_API_SOCKET"] == "test-editor-pipe"
    assert captured["env"]["KILEIDO_BRIDGE_TOKEN"] == server.token
    assert captured["env"]["KILEIDO_BRIDGE_PORT"] == str(server.port)
    assert server.token not in str(captured["command"])


def test_plugin_requires_target_editor(monkeypatch):
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    with pytest.raises(RuntimeError, match="KiCad PCB Editor"):
        launcher.launch()


def test_launch_failure_closes_bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_API_SOCKET", "test")
    monkeypatch.setattr(launcher, "find_blender", lambda: "blender")
    closed = []
    monkeypatch.setattr(launcher, "BridgeRuntime", lambda s: SimpleNamespace(close=lambda: (s.close(), closed.append(1))))
    def failed(*args, **kwargs):
        raise OSError("launch failed")
    monkeypatch.setattr(launcher.subprocess, "Popen", failed)
    with pytest.raises(OSError, match="launch failed"):
        launcher.launch(tmp_path)
    assert closed == [1]


def test_one_viewer_per_kicad(monkeypatch, tmp_path):
    """Repeated clicks in one KiCad start no second Blender; another KiCad gets its own."""
    held = launcher.viewer_lock("kicad-a")
    assert held is not None and launcher.viewer_lock("kicad-a") is None
    other = launcher.viewer_lock("kicad-b")
    assert other is not None
    told = []
    monkeypatch.setenv("KICAD_API_SOCKET", "kicad-a")
    monkeypatch.setattr(launcher, "_tell_user", told.append)
    monkeypatch.setattr(launcher, "find_blender", lambda: pytest.fail("looked for Blender"))
    monkeypatch.setattr(launcher, "FOLLOW_WAIT_S", 0.1)  # the holder here answers no follow request
    assert launcher.launch(tmp_path) == 0
    assert "already shows" in told[0] and "Blender, process" not in told[0]  # none recorded
    launcher._viewer_file("kicad-a", ".pid").write_text("4242", encoding="ascii")
    assert launcher.launch(tmp_path) == 0
    assert "Blender, process 4242" in told[1]  # so a viewer whose window is lost can be ended
    held.close()  # the viewer closed
    again = launcher.viewer_lock("kicad-a")
    assert again is not None
    again.close()
    other.close()


def test_find_blender_skips_versions_older_than_5_1(monkeypatch):
    monkeypatch.delenv("KILEIDO_BLENDER", raising=False)
    versions = {"/usr/bin/blender": (4, 5), "/home/u/blender-5.1.0-linux-x64/blender": (5, 1)}
    monkeypatch.setattr(launcher, "blender_candidates", lambda: list(versions))
    monkeypatch.setattr(launcher, "blender_version", versions.get)
    assert launcher.find_blender() == "/home/u/blender-5.1.0-linux-x64/blender"
    del versions["/home/u/blender-5.1.0-linux-x64/blender"]
    with pytest.raises(RuntimeError, match=r"found only /usr/bin/blender \(4.5\)"):
        launcher.find_blender()


def test_blender_version_parses_the_banner(monkeypatch, tmp_path):
    assert launcher.blender_version(tmp_path / "missing") is None  # does not run
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **kwargs: SimpleNamespace(
        stdout=b"Blender 5.1.0\n\tbuild date: 2026-03-17\n"))
    assert launcher.blender_version("blender") == (5, 1)


def test_tarball_folders_sort_newest_first():
    from pathlib import Path
    paths = [Path("/opt/blender-4.5.3-linux-x64/blender"), Path("/opt/blender-5.1.0-linux-x64/blender"),
             Path("/opt/blender/blender")]
    assert sorted(paths, key=launcher._version_key, reverse=True)[0].parent.name == "blender-5.1.0-linux-x64"


def test_a_kicad_reopened_on_the_socket_is_followed_by_the_bridge_holding_it(monkeypatch):
    """Linux: a KiCad reopened after the first one closed gets its socket again, whose lock the
    board's old bridge holds. Open in Blender there asks that bridge to follow the new KiCad
    (as Resync with new KiCad does), and says "already shows" only when it has its KiCad."""
    held = launcher.viewer_lock("kicad-a")
    followed = []
    runtime = SimpleNamespace(reader=None)
    runtime.follow_kicad = lambda: followed.append(1) or runtime.reader is None

    def holder_serves(seconds):  # the holding launcher's serve loop, meanwhile
        launcher.answer_follow("kicad-a", runtime)
    assert launcher.ask_to_follow("kicad-a", wait=holder_serves)  # its KiCad was gone: it follows
    runtime.reader = object()  # a repeated click in the KiCad it has
    assert not launcher.ask_to_follow("kicad-a", wait=holder_serves)
    assert followed == [1, 1]
    assert not launcher._viewer_file("kicad-a", ".follow").exists()
    assert not launcher._viewer_file("kicad-a", ".followed").exists()
    monkeypatch.setattr(launcher, "FOLLOW_WAIT_S", 0.1)
    assert not launcher.ask_to_follow("kicad-a")  # nobody answers: withdrawn
    assert not launcher._viewer_file("kicad-a", ".follow").exists()
    held.close()
