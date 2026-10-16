"""The `.kileidoscope` project folder: runs, findings files, dismissed state, write and read guards."""

import hashlib
import json
import os

import pytest

from kileido_bridge.ghost_drc import RUN_KIND
from kileido_bridge.kls_folder import KEEP_RUNS, KlsFolder, NotAvailable, Run, State, stamp

TEXT = "(kicad_pcb (version 20250101)\r\n  (gr_text \"µ\")\n)\n"


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "x.kicad_pcb").write_text("saved", encoding="utf-8")
    return KlsFolder(str(tmp_path / "x.kicad_pcb"))


def states(state):
    """Finding key -> its state alone."""
    return {key: entry["state"] for key, entry in state.findings.items()}


def test_unsaved_board_has_no_folder():
    folder = KlsFolder("")
    assert not folder.available and folder.runs() == {} and folder.load_state() == State()
    with pytest.raises(NotAvailable):
        folder.new_run(TEXT, "drc")
    with pytest.raises(NotAvailable):
        folder.write_findings({}, "drc")


def test_ensure_writes_a_gitignore_for_everything(folder, tmp_path):
    folder.ensure()
    assert (tmp_path / ".kileidoscope" / ".gitignore").read_bytes() == b"*\n"
    folder.ensure()
    assert sorted(p.name for p in (tmp_path / ".kileidoscope").iterdir()) == [".gitignore"]


def test_run_freezes_the_text_with_its_stamp(folder, tmp_path):
    run = folder.new_run(TEXT, "drc")
    assert run.id == "1" and run.directory == tmp_path / ".kileidoscope" / "runs" / "1"
    data = (run.directory / "board.kicad_pcb").read_bytes()
    assert run.stamp == stamp(TEXT) == hashlib.sha256(data).hexdigest()[:8]
    info = json.loads((run.directory / "run.json").read_text(encoding="utf-8"))
    assert {k: info[k] for k in ("run", "stamp", "board", "kind")} == {
        "run": "1", "stamp": run.stamp, "board": "x.kicad_pcb", "kind": "drc"}
    assert info["made"]
    assert not list(run.directory.glob("*.partial"))


def test_project_and_rule_files_are_copied_when_present(folder, tmp_path):
    (tmp_path / "x.kicad_pro").write_text('{"net_settings": {}}', encoding="utf-8")
    run = folder.new_run(TEXT, "drc")
    assert (run.directory / "board.kicad_pro").read_text(encoding="utf-8") == '{"net_settings": {}}'
    assert not (run.directory / "board.kicad_dru").exists()
    (tmp_path / "x.kicad_dru").write_text("(version 1)", encoding="utf-8")
    assert (folder.new_run(TEXT, "drc").directory / "board.kicad_dru").read_text(encoding="utf-8") == "(version 1)"


def test_runs_count_up_and_prune_to_the_last_five(folder, tmp_path):
    stamps = [folder.new_run(f"{TEXT}{i}", "drc").stamp for i in range(7)]
    runs = tmp_path / ".kileidoscope" / "runs"
    assert sorted(int(p.name) for p in runs.iterdir()) == [3, 4, 5, 6, 7]
    assert folder.runs() == {str(i + 1): stamps[i] for i in range(2, 7)} and KEEP_RUNS == 5
    (runs / "notes").mkdir()
    (runs / "6" / "run.json").write_text("broken", encoding="utf-8")
    assert list(folder.runs()) == ["3", "4", "5", "7"]
    assert folder.new_run(TEXT, "llm").id == "8"
    assert sorted(p.name for p in runs.iterdir()) == ["4", "5", "6", "7", "8", "notes"]


def test_ghost_runs_keep_their_own_count(folder):
    prompt = folder.new_run("(kicad_pcb)", "llm")
    made = [folder.new_run(f"(kicad_pcb {i})", RUN_KIND).id for i in range(6)]
    assert sorted(int(p.name) for p in folder.runs_dir.iterdir()) == [1, int(made[-2]), int(made[-1])]
    assert folder.runs() == {prompt.id: prompt.stamp}
    for i in range(5):
        folder.new_run(f"(kicad_pcb drc {i})", "drc")
    names = sorted(int(p.name) for p in folder.runs_dir.iterdir())
    assert int(made[-1]) in names and 1 not in names and len(names) == 2 + KEEP_RUNS


def test_run_files_land_in_the_run_and_spare_the_frozen_board(folder):
    run = folder.new_run(TEXT, "llm")
    path = folder.write_run_file(run, "request.md", "# µ\n")
    assert path == run.directory / "request.md" and path.read_bytes() == "# µ\n".encode("utf-8")
    for name in ("board.kicad_pcb", "run.json", "../x.md", "a/b.md", ".hidden", "", "x" * 65):
        with pytest.raises(ValueError):
            folder.write_run_file(run, name, "x")
    with pytest.raises(ValueError):
        folder.write_run_file(Run("../1", run.stamp, run.directory), "request.md", "x")
    with pytest.raises(ValueError):
        folder.write_run_file(Run("99", run.stamp, run.directory), "request.md", "x")
    assert stamp((run.directory / "board.kicad_pcb").read_bytes().decode("utf-8")) == run.stamp


def test_dismissed_state_round_trip(folder, tmp_path):
    assert folder.load_state() == State()
    state = State()
    state.mark("k1", "dismissed", "drc.clearance", "Clearance", "x")
    state.mark("k2", "confirmed")
    state.mark("k3", "dismissed")
    state.mark("k3", "")
    folder.save_state(state)
    raw = json.loads((tmp_path / ".kileidoscope" / "dismissed.json").read_text(encoding="utf-8"))
    assert set(raw) == {"version", "findings"} and raw["version"] == 1
    assert set(raw["findings"]["k1"]) == {"state", "check", "title", "board", "at"} and raw["findings"]["k1"]["at"]
    loaded = folder.load_state()
    assert loaded == state
    assert states(loaded) == {"k1": "dismissed", "k2": "confirmed"}


def test_a_state_file_from_the_first_drc_release_keeps_its_marks(folder, tmp_path):
    """It saved each entry as the state alone, with the same version."""
    folder.ensure()
    path = tmp_path / ".kileidoscope" / "dismissed.json"
    path.write_text(json.dumps({"version": 1, "findings": {"a1": "dismissed", "b2": "confirmed", "c3": "maybe",
                                                           "d4": {"state": "dismissed", "check": "x.y"}}}),
                    encoding="utf-8")
    loaded = folder.load_state()
    assert states(loaded) == {"a1": "dismissed", "b2": "confirmed", "d4": "dismissed"}
    assert loaded.findings["a1"] == {"state": "dismissed", "check": "", "title": "", "board": "", "at": ""}
    folder.save_state(loaded)  # saved again in today's form, nothing lost
    assert folder.load_state() == loaded


def test_dismissed_lists_this_boards_dismissals_latest_first():
    state = State({"old": {"state": "dismissed", "check": "plane.gap", "title": "Old gap", "board": "x",
                           "at": "2026-10-01T10:00:00+02:00"},
                   "new": {"state": "dismissed", "check": "hs.stub", "title": "Stub", "board": "x",
                           "at": "2026-10-11T10:00:00+02:00"},
                   "kept": {"state": "confirmed", "check": "a.b", "title": "Real", "board": "x", "at": ""},
                   "other": {"state": "dismissed", "check": "a.b", "title": "Elsewhere", "board": "y", "at": ""},
                   "bare": {"state": "dismissed", "check": "", "title": "", "board": "x", "at": ""}})
    assert state.dismissed("x") == [("hs.stub", "Stub"), ("plane.gap", "Old gap")]
    assert state.dismissed("z") == []


def test_an_old_state_file_with_accepted_runs_loads(folder, tmp_path):
    folder.ensure()
    path = tmp_path / ".kileidoscope" / "dismissed.json"
    path.write_text(json.dumps({"version": 1, "findings": {"a": {"state": "dismissed", "check": "x.y"}},
                                "accepted_runs": ["4|a1b2c3d4"]}), encoding="utf-8")
    loaded = folder.load_state()
    assert states(loaded) == {"a": "dismissed"} and loaded.findings["a"]["check"] == "x.y"
    folder.save_state(loaded)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"version", "findings"} and list(raw["findings"]) == ["a"]


def test_broken_state_files_load_empty_or_partly(folder, tmp_path):
    folder.ensure()
    path = tmp_path / ".kileidoscope" / "dismissed.json"
    path.write_text("{oops", encoding="utf-8")
    assert folder.load_state() == State()
    path.write_text(json.dumps({"version": 1, "findings": {"a": {"state": "dismissed"}, "b": {"state": "maybe"},
                                                           "c": "x", "d": 5, "e" * 65: "dismissed"}}), encoding="utf-8")
    loaded = folder.load_state()
    assert list(loaded.findings) == ["a"]
    path.write_text(json.dumps({"version": 2, "findings": {"a": {"state": "dismissed"}}}), encoding="utf-8")
    assert folder.load_state() == State()


def test_findings_files_per_source(folder, tmp_path):
    root = tmp_path / ".kileidoscope"
    assert folder.findings_path == root / "x.kls-findings.json"
    assert folder.drc_findings_path == root / "x.drc.kls-findings.json"
    assert folder.progress_path == root / "x.kls-progress.json"  # an LLM's, beside its answer; never written here
    path = folder.write_findings({"format": "kls-findings 1", "tool": "µ"}, "drc")
    assert path == folder.drc_findings_path and not folder.findings_path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["tool"] == "µ"
    folder.write_findings({"format": "kls-findings 1"}, "file")
    assert folder.findings_path.exists() and not list(root.glob("*.partial"))
    with pytest.raises(ValueError):
        folder.write_findings({"x": float("nan")}, "drc")
    with pytest.raises(ValueError):
        folder.write_findings({}, "elsewhere")


@pytest.mark.parametrize("relative", ["../escape.txt", "runs/../../escape.txt", "/escape.txt", ".", ""])
def test_writes_outside_the_folder_are_refused(folder, tmp_path, relative):
    folder.ensure()
    with pytest.raises(ValueError):
        folder._write(relative, b"x")
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path.parent / "escape.txt").exists()
    with pytest.raises(ValueError):
        folder._remove(relative)
    assert (tmp_path / "x.kicad_pcb").exists()


def test_discard_removes_only_our_drc_file(folder):
    folder.write_findings({"format": "kls-findings 1", "findings": []}, "drc")
    folder.findings_path.write_text("{}", encoding="utf-8")
    folder.discard("drc")
    assert not folder.drc_findings_path.exists() and folder.findings_path.exists()
    folder.discard("drc")  # already gone: nothing happens
    with pytest.raises(ValueError):
        folder.discard("file")
    KlsFolder("").discard("drc")  # an unsaved board: nothing to remove


def test_discard_tolerates_a_locked_file(folder, monkeypatch):
    from pathlib import Path

    def locked(self, missing_ok=False):
        raise PermissionError("in use")

    folder.write_findings({"format": "kls-findings 1", "findings": []}, "drc")
    monkeypatch.setattr(Path, "unlink", locked)
    folder.discard("drc")  # no error: the next DRC run overwrites the file
    assert folder.drc_findings_path.exists()


def test_a_link_out_of_the_folder_is_not_read(folder, tmp_path):
    folder.ensure()
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    try:
        os.symlink(secret, folder.findings_path)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here (Windows without developer mode)")
    with pytest.raises(ValueError):
        folder.open_read(folder.findings_path)


def test_a_plain_file_of_the_folder_is_read(folder):
    folder.ensure()
    folder.findings_path.write_bytes(b"{}")
    with folder.open_read(folder.findings_path) as handle:
        assert handle.read() == b"{}"
    with pytest.raises(FileNotFoundError):
        folder.open_read(folder.progress_path)
