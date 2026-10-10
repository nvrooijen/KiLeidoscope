"""The `.kileidoscope` project folder: runs, findings files, dismissed state, write guard."""

import hashlib
import json

import pytest

from kileido_bridge.kls_folder import KEEP_RUNS, KlsFolder, NotAvailable, State, stamp

TEXT = "(kicad_pcb (version 20250101)\r\n  (gr_text \"µ\")\n)\n"


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "x.kicad_pcb").write_text("saved", encoding="utf-8")
    return KlsFolder(str(tmp_path / "x.kicad_pcb"))


def test_unsaved_board_has_no_folder():
    folder = KlsFolder("")
    assert not folder.available and folder.load_state() == State()
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
    for i in range(7):
        folder.new_run(f"{TEXT}{i}", "drc")
    runs = tmp_path / ".kileidoscope" / "runs"
    assert sorted(int(p.name) for p in runs.iterdir()) == [3, 4, 5, 6, 7] and KEEP_RUNS == 5
    (runs / "notes").mkdir()
    assert folder.new_run(TEXT, "drc").id == "8"
    assert sorted(p.name for p in runs.iterdir()) == ["4", "5", "6", "7", "8", "notes"]


def test_dismissed_state_round_trip(folder, tmp_path):
    assert folder.load_state() == State()
    state = State()
    state.mark("k1", "dismissed")
    state.mark("k2", "confirmed")
    state.mark("k3", "dismissed")
    state.mark("k3", "")
    folder.save_state(state)
    raw = json.loads((tmp_path / ".kileidoscope" / "dismissed.json").read_text(encoding="utf-8"))
    assert raw == {"version": 1, "findings": {"k1": "dismissed", "k2": "confirmed"}}
    loaded = folder.load_state()
    assert loaded == state
    assert loaded.states() == {"k1": "dismissed", "k2": "confirmed"}


def test_broken_state_files_load_empty_or_partly(folder, tmp_path):
    folder.ensure()
    path = tmp_path / ".kileidoscope" / "dismissed.json"
    path.write_text("{oops", encoding="utf-8")
    assert folder.load_state() == State()
    path.write_text(json.dumps({"version": 1, "findings": {"a": "dismissed", "b": "maybe", "c": 5,
                                                           "d": {"state": "dismissed"}}}), encoding="utf-8")
    assert list(folder.load_state().findings) == ["a"]
    path.write_text(json.dumps({"version": 2, "findings": {"a": "dismissed"}}), encoding="utf-8")
    assert folder.load_state() == State()


def test_findings_files_per_source(folder, tmp_path):
    root = tmp_path / ".kileidoscope"
    assert folder.findings_path == root / "x.kls-findings.json"
    assert folder.drc_findings_path == root / "x.drc.kls-findings.json"
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
