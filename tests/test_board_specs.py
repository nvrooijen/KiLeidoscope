"""Read-only KiCad appearance parsing; all inputs are synthetic."""

import json
from types import SimpleNamespace

from kileido_bridge.board_specs import read_appearance
from kileido_bridge.kicad_reader import saved_board_path
import pytest


@pytest.mark.parametrize("finish", ["None", "ENIG", "HASL lead-free", "Custom finish", None])
def test_saved_finish_preserves_explicit_none(tmp_path, finish):
    board = tmp_path / "finish.kicad_pcb"
    field = f'(copper_finish "{finish}")' if finish is not None else ""
    board.write_text(f'(kicad_pcb (setup (stackup {field})))')
    assert read_appearance(str(board))["copper_finish"] == finish


def test_saved_colors_and_editor_theme(tmp_path, monkeypatch):
    board = tmp_path / "sample.kicad_pcb"
    board.write_text('''(kicad_pcb (setup (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (color "FR4 natural"))
      (layer "F.Mask" (type "soldermask") (color "#4B7CB6B3"))
      (layer "B.Mask" (type "soldermask") (color "#123456FF")))))''')
    settings = tmp_path / "kicad" / "10.0"
    (settings / "colors").mkdir(parents=True)
    (settings / "pcbnew.json").write_text(json.dumps({"appearance": {"color_theme": "test"}}))
    (settings / "colors" / "test.json").write_text(json.dumps({
        "3d_viewer": {"board": "rgb(109, 116, 75)", "copper": "rgb(179, 156, 0)"},
        "board": {"copper": {"f": "rgb(200, 52, 52)", "b": "rgb(77, 127, 196)"},
                  "f_mask": "rgba(216, 100, 255, 0.4)",
                  "pad_plated_hole": "rgb(194, 194, 0)"}}))
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "kicad"))  # any OS
    appearance = read_appearance(str(board))
    assert appearance["saved_colors"]["F.Mask"] == [75/255, 124/255, 182/255, 179/255]
    # One "FR4 natural" dielectric (a stackup name, resolved): KiCad's alpha 0.83 + (1 - 0.83) * 0.83 / 2.
    assert appearance["viewer"]["core"] == pytest.approx([109/255, 116/255, 75/255, 0.90055])
    assert appearance["editor_copper"]["F.Cu"] == [200/255, 52/255, 52/255, 1]
    assert appearance["editor_mask_top"] == [216/255, 100/255, 1, 0.4]


def test_saved_board_path_from_document_specifier(tmp_path):
    project = tmp_path / "sample.kicad_pro"
    board = tmp_path / "sample.kicad_pcb"
    board.write_text("(kicad_pcb)")
    source = SimpleNamespace(_doc=SimpleNamespace(
        board_filename=board.name, project=SimpleNamespace(path=str(project))))
    assert saved_board_path(source) == str(board.resolve())
    source._doc.board_filename = "missing.kicad_pcb"
    assert saved_board_path(source) == ""


@pytest.mark.parametrize("folder", ["ecc83v1.2", "rev.b", "plain"])
def test_saved_board_path_in_a_project_folder_with_a_dot_in_its_name(tmp_path, folder):
    """KiCad gives the project as its folder or as its .kicad_pro file. A folder named
    "ecc83v1.2" is still a folder: its ".2" is not a file extension."""
    project = tmp_path / folder
    project.mkdir()
    board = project / "amp.kicad_pcb"
    board.write_text("(kicad_pcb)")
    for given in (project, project / "amp.kicad_pro"):  # the folder; the project file (saved or not)
        source = SimpleNamespace(_doc=SimpleNamespace(
            board_filename=board.name, project=SimpleNamespace(path=str(given))))
        assert saved_board_path(source) == str(board.resolve()), given
    (project / "amp.kicad_pro").write_text("{}")
    assert saved_board_path(source) == str(board.resolve())


def _viewer_setup(tmp_path, monkeypatch, stackup_colors=None):
    board = tmp_path / "viewer.kicad_pcb"
    board.write_text('''(kicad_pcb (setup (stackup
      (layer "F.SilkS" (type "Top Silk Screen") (color "White"))
      (layer "F.Mask" (type "Top Solder Mask") (color "#4B7CB6B3"))
      (layer "dielectric 1" (type "core") (color "FR4 natural"))
      (layer "dielectric 2" (type "prepreg") (color "FR4 natural"))
      (copper_finish "ENIG"))))''')
    settings = tmp_path / "kicad" / "10.0"
    (settings / "colors").mkdir(parents=True)
    # The 3D viewer reads the "user" theme, whatever the PCB Editor theme is.
    (settings / "colors" / "user.json").write_text(json.dumps({"3d_viewer": {
        "silkscreen_top": "rgba(45, 94, 182, 0.702)", "copper": "rgb(179, 156, 0)"}}))
    if stackup_colors is not None:
        (settings / "3d_viewer.json").write_text(json.dumps({"use_stackup_colors": stackup_colors}))
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "kicad"))  # any OS
    return read_appearance(str(board))["viewer"]


def test_viewer_colors_resolve_stackup_names_like_kicad(tmp_path, monkeypatch):
    viewer = _viewer_setup(tmp_path, monkeypatch)
    assert viewer["silkscreen_top"] == [245/255, 245/255, 245/255, 1.0]  # "White", not the blue theme
    assert viewer["soldermask_top"] == pytest.approx([75/255, 124/255, 182/255, 179/255])
    assert viewer["copper"] == [178/255, 156/255, 0, 1.0]  # ENIG -> KiCad "Gold"
    assert viewer["core"][:3] == pytest.approx([109/255, 116/255, 75/255])
    assert viewer["soldermask_bottom"] == [0.08, 0.20, 0.14, 0.83]  # KiCad default


def test_viewer_colors_follow_theme_without_stackup_colors(tmp_path, monkeypatch):
    viewer = _viewer_setup(tmp_path, monkeypatch, stackup_colors=False)
    assert viewer["silkscreen_top"] == pytest.approx([45/255, 94/255, 182/255, 0.702])
    assert viewer["copper"] == [179/255, 156/255, 0, 1.0]
