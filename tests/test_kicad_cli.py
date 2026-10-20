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


def test_model_files_as_kicad_resolves_them(tmp_path, monkeypatch):
    """Each model path's files as KiCad's resolver tries them: variables expanded (the
    environment over kicad_common.json), absolute or variable paths as they are,
    relative ones under the project and each ${KICADn_3DMODEL_DIR}; embedded models
    and repeats left out, the board's string escapes undone."""
    project, stock, saved_dir = tmp_path / "project", tmp_path / "stock", tmp_path / "saved"
    monkeypatch.setenv("KLS_TEST_3D", str(tmp_path / "env"))
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(stock))
    monkeypatch.delenv("KLS_UNSET_TEST_3D", raising=False)
    settings = _settings(tmp_path / "10.0", {"KLS_TEST_3D": "/ignored", "KLS_SAVED_3D": str(saved_dir),
                                             "KLS_BAD_3D": 3})
    absolute = tmp_path / "abs" / "a.step"
    written = str(absolute).replace("\\", "\\\\")  # as the board writes it (measured: kicad-cli unescapes)
    paths = ["${KIPRJMOD}/local.step", "${KLS_TEST_3D}/env.step", "$(KLS_SAVED_3D)/saved.step",
             "${KLS_UNSET_TEST_3D}/unset.step", written, "parts/rel.step", "kicad-embed://inside.step",
             "${KIPRJMOD}/local.step", "esc\\\\aped \\\"q\\\".step"]
    files = kicad_cli.model_files(paths, str(project), settings)
    assert files[:5] == [project / "local.step", tmp_path / "env" / "env.step", saved_dir / "saved.step",
                         Path("${KLS_UNSET_TEST_3D}/unset.step"), absolute]
    assert project / "parts" / "rel.step" in files and stock / "parts" / "rel.step" in files
    assert not any("inside" in str(path) for path in files)
    assert files.count(project / "local.step") == 1
    assert project / 'esc\\aped "q".step' in files


def test_file_stamps_follow_a_file_saved_again(tmp_path):
    model = tmp_path / "part.step"
    model.write_bytes(b"ISO-10303-21; old")
    before = kicad_cli.file_stamps([model, tmp_path / "absent.step"])
    assert before.endswith("absent.step|-")
    model.write_bytes(b"ISO-10303-21; new")
    os.utime(model, ns=(1, 1))
    assert kicad_cli.file_stamps([model, tmp_path / "absent.step"]) != before
    assert kicad_cli.file_stamps([]) == ""


def test_model_problems_from_kicad_cli_messages():
    """kicad-cli 10's GLB export messages (measured) for a model path not found and a
    VRML file its reader rejects; the rest of its output is not a problem."""
    output = ("Could not add 3D model for R1.\n"
              "File not found: ${MY_3D}/part.step\n"
              "Error in VrmlAPI_CafReader: NumericInputErroroccurred at line 4\n"
              "while reading VRML file 'C:\\models\\bad part.wrl'\n"
              "readVRML() failed on filename 'C:\\models\\bad part.wrl'.\n"
              "No model for filename 'C:\\models\\bad part.wrl'.\n"
              "Binary GLTF file 'C:\\out\\models.glb' created.\n")
    assert kicad_cli.model_problems(output) == ["${MY_3D}/part.step", "C:\\models\\bad part.wrl"]
    assert kicad_cli.model_problems("Binary GLTF file 'x.glb' created.\n") == []


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


def test_variant_arguments_name_the_selected_variant_only():
    """`--variant` goes to the GLB and position exports for a selected variant; the
    default variant passes no flag (older kicad-cli has none)."""
    assert kicad_cli.variant_arguments({"variant": "5V Output"}) == ["--variant", "5V Output"]
    assert kicad_cli.variant_arguments({"variant": ""}) == []
    assert kicad_cli.variant_arguments({}) == []
    assert kicad_cli.variant_arguments(None) == []
