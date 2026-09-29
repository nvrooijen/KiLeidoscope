"""kicad-cli helpers and the export cache (`blender_addon/kileido/kicad_cli.py`), without Blender."""

import importlib.util
import json
import os
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "kicad_cli.py"
_SPEC = importlib.util.spec_from_file_location("kileido_kicad_cli", _PATH)
kicad_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(kicad_cli)


def test_identity_follows_an_upgrade_in_place(tmp_path):
    cli = tmp_path / "kicad-cli"
    cli.write_bytes(b"9")
    before = kicad_cli.identity(cli)
    cli.write_bytes(b"10.0")
    os.utime(cli, ns=(1, 1))
    assert kicad_cli.identity(cli) != before
    assert str(cli) in before


def test_board_copy_keeps_the_stem_and_the_project(tmp_path):
    source = tmp_path / "live" / "my.board.kicad_pcb"
    source.parent.mkdir()
    source.write_text("rewritten meanwhile", encoding="utf-8")
    source.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    copy = kicad_cli.board_copy(source, b"(kicad_pcb hashed)", scratch)
    assert copy.name == "my.board.kicad_pcb"
    assert copy.read_bytes() == b"(kicad_pcb hashed)"
    assert copy.with_suffix(".kicad_pro").read_text(encoding="utf-8") == "{}"


def test_board_copy_without_a_project(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    source.write_bytes(b"x")
    copy = kicad_cli.board_copy(source, b"x", tmp_path)
    assert copy.is_file() and not copy.with_suffix(".kicad_pro").exists()


def test_project_dir_prefers_the_saved_board():
    assert kicad_cli.project_dir({"project_dir": "/work/project"}, "/tmp/live/b.kicad_pcb") == "/work/project"
    assert kicad_cli.project_dir({"project_dir": ""}, Path("/work/b.kicad_pcb")) == str(Path("/work"))
    assert kicad_cli.defines("/work") == ["--define-var", "KIPRJMOD=/work"]


def _settings(folder, variables):
    folder.mkdir(exist_ok=True)
    (folder / "kicad_common.json").write_text(json.dumps({"environment": {"vars": variables}}), encoding="utf-8")
    return str(folder)


def test_path_variables_follow_configure_paths(tmp_path, monkeypatch):
    """A path variable changed in KiCad, saved or handed down through the environment,
    makes a new cache key; variables the model paths do not use and KIPRJMOD do not."""
    monkeypatch.delenv("KLS_TEST_3D", raising=False)
    paths = ["${KLS_TEST_3D}/part.step", "$(KLS_TEST_3D)/other.wrl", "${KIPRJMOD}/local.step", "C:/abs.step"]
    settings = _settings(tmp_path / "10.0", {"KLS_TEST_3D": "/models/a", "UNUSED": "/x"})
    saved = kicad_cli.path_variables(paths, settings)
    assert "KLS_TEST_3D" in saved and "/models/a" in saved
    assert "UNUSED" not in saved and "KIPRJMOD" not in saved
    _settings(tmp_path / "10.0", {"KLS_TEST_3D": "/models/b"})
    assert kicad_cli.path_variables(paths, settings) != saved
    monkeypatch.setenv("KLS_TEST_3D", "/models/b")
    assert kicad_cli.path_variables(paths, settings) not in (saved, "")
    _settings(tmp_path / "10.0", {"KLS_TEST_3D": "/models/a", "UNUSED": "/y"})
    monkeypatch.delenv("KLS_TEST_3D")
    assert kicad_cli.path_variables(paths, settings) == saved


def test_path_variables_without_settings(tmp_path, monkeypatch):
    """No settings folder, no file, or a file KiCad never gave variables: the
    environment alone; no variables used: nothing to add."""
    monkeypatch.setenv("KLS_TEST_3D", "/models")
    for settings in ("", str(tmp_path / "absent"), _settings(tmp_path / "empty", None)):
        assert kicad_cli.path_variables(["${KLS_TEST_3D}/p.step"], settings) == "KLS_TEST_3D='/models'|None"
    assert kicad_cli.path_variables(["${KIPRJMOD}/p.step", "C:/p.step"], str(tmp_path)) == ""


def test_store_and_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(kicad_cli, "cache_root", lambda: tmp_path / "cache")
    output = tmp_path / "result.txt"
    output.write_text("plot", encoding="utf-8")
    entry = kicad_cli.store("overlays", "key", {"result.txt": output})
    assert entry is not None and (entry / "result.txt").read_text(encoding="utf-8") == "plot"
    assert kicad_cli.lookup("overlays", "key") == entry


def test_store_failure_falls_back_to_the_outputs(tmp_path, monkeypatch):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("", encoding="utf-8")  # the cache root cannot be created under a file
    monkeypatch.setattr(kicad_cli, "cache_root", lambda: blocker / "cache")
    output = tmp_path / "result.txt"
    output.write_text("plot", encoding="utf-8")
    assert kicad_cli.store("overlays", "key", {"result.txt": output}) is None
    assert kicad_cli.lookup("overlays", "key") is None


def test_run_never_fails_on_undecodable_output():
    result = kicad_cli.run([sys.executable, "-c",
                            "import sys; sys.stdout.buffer.write(b'File not found: \\x90\\xff ok')"], 30)
    assert result.returncode == 0
    assert result.stdout.startswith("File not found:") and result.stdout.endswith("ok")
