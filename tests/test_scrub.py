"""An LLM run's copies without what names the user (scrub.py), and the folder's link check."""

import json
import os

import pytest

from kileido_bridge import kls_folder, scrub

BOARD = """(kicad_pcb
\t(version 20250114)
\t(generator "pcbnew")
\t(title_block
\t\t(title "Secret board")
\t\t(company "Acme (Client) Ltd")
\t\t(comment 1 "drawn by Jane")
\t)
\t(property "CLIENT" "Acme")
\t(net 1 "GND")
\t(gr_text "a (title_block in quotes \\" stays"
\t\t(at 1 2)
\t)
\t(footprint "Resistor_SMD:R_0402"
\t\t(layer "F.Cu")
\t\t(property "Reference" "R1"
\t\t\t(at 0 0)
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 0 1)
\t\t)
\t\t(property "Sheetfile" "secret.kicad_sch"
\t\t\t(at 0 2)
\t\t)
\t\t(sheetname "/Power/")
\t\t(sheetfile "secret.kicad_sch")
\t\t(model "C:/Users/jane/Acme/3d/R_0402.step"
\t\t\t(offset (xyz 0 0 0))
\t\t)
\t\t(model "${KICAD9_3DMODEL_DIR}\\\\Resistor_SMD.3dshapes\\\\R_0603.wrl")
\t)
)
"""


def test_the_board_loses_what_names_the_user():
    clean = scrub.board(BOARD)
    for gone in ("title_block\n", "Secret board", "Acme (Client)", "Jane", '"CLIENT"', "secret.kicad_sch",
                 "/Power/", "C:/Users", "jane", "KICAD9_3DMODEL_DIR"):
        assert gone not in clean, gone
    assert clean.count("(") - clean.count(")") == BOARD.count("(") - BOARD.count(")")  # whole nodes went


def test_the_board_keeps_what_the_review_needs():
    clean = scrub.board(BOARD)
    for kept in ('(net 1 "GND")', '(property "Reference" "R1"', '(property "Value" "10k"',
                 '(model "R_0402.step"', '(model "R_0603.wrl")', '"a (title_block in quotes \\" stays"',
                 '(footprint "Resistor_SMD:R_0402"'):
        assert kept in clean, kept
    assert "\n\n" not in clean  # a removed node leaves no blank line


def test_a_board_with_nothing_to_remove_is_unchanged():
    text = '(kicad_pcb\r\n\t(net 1 "GND")\r\n\t(footprint "R" (property "Reference" "R1") (model "R.step"))\r\n)\r\n'
    assert scrub.board(text) == text


DATASHEETS = {  # a footprint's Datasheet as KiCad writes it -> what the LLM run's copy keeps
    "https://www.ti.com/lit/ds/symlink/lm317.pdf": "https://www.ti.com/lit/ds/symlink/lm317.pdf",
    "http://example.com/a.pdf": "http://example.com/a.pdf",
    "~": "~",
    "": "",
    "www.ti.com/lit/ds/lm317.pdf": "www.ti.com/lit/ds/lm317.pdf",
    "C:/Users/jane/Acme/datasheets/lm317.pdf": "",
    "C:\\\\Users\\\\jane\\\\lm317.pdf": "",
    "file:///C:/Users/jane/lm317.pdf": "",
    "FILE://server/share/lm317.pdf": "",
    "/home/jane/acme/lm317.pdf": "",
    "\\\\\\\\server\\\\share\\\\lm317.pdf": "",
    "~/datasheets/lm317.pdf": "",
    "../datasheets/lm317.pdf": "",
    "${ACME_DOCS}/lm317.pdf": "",
}


@pytest.mark.parametrize("given, kept", DATASHEETS.items())
def test_a_datasheet_that_is_a_local_file_is_emptied(given, kept):
    text = (f'(kicad_pcb\n\t(footprint "R"\n\t\t(property "Datasheet" "{given}"\n\t\t\t(at 0 0)\n\t\t)\n'
            f'\t\t(property "Reference" "C:/Users/jane"\n\t\t)\n\t)\n)\n')
    clean = scrub.board(text)
    assert f'(property "Datasheet" "{kept}"\n' in clean
    assert '(property "Reference" "C:/Users/jane"' in clean  # only the Datasheet is read so
    assert "jane" not in clean.replace('"C:/Users/jane"', "")


def test_the_project_keeps_only_the_review_sections():
    raw = {"meta": {"filename": "acme_secret.kicad_pro", "version": 3}, "board": {"design_settings": {}},
           "net_settings": {"classes": [{"name": "Default"}]}, "text_variables": {"COMPANY": "Acme"},
           "sheets": [["uuid", "Acme power"]], "pcbnew": {"last_paths": {"step": "C:/Users/jane/x.step"}},
           "schematic": {"page_layout_descr_file": "C:/Users/jane/acme.kicad_wks"}}
    clean = json.loads(scrub.project(json.dumps(raw).encode("utf-8")))
    assert set(clean) == {"meta", "board", "net_settings"}
    assert clean["meta"] == {"filename": "board.kicad_pro", "version": 3}
    assert "Acme" not in json.dumps(clean) and "jane" not in json.dumps(clean)


@pytest.mark.parametrize("data", [b"not json", b"[1, 2]", b"\xff\xfe"])
def test_a_project_that_is_not_a_json_object_is_left_out(data):
    assert scrub.project(data) is None
    assert scrub.clean(".kicad_pro", data) is None


def test_the_rules_file_is_copied_as_it_is():
    assert scrub.clean(".kicad_dru", b"(version 1)\n(rule a)") == b"(version 1)\n(rule a)"


def test_an_llm_run_writes_scrubbed_copies_with_the_raw_stamp(tmp_path):
    (tmp_path / "acme.kicad_pro").write_text(json.dumps({"meta": {"filename": "acme.kicad_pro"},
                                                         "text_variables": {"A": "Acme"}}), encoding="utf-8")
    (tmp_path / "acme.kicad_dru").write_text("(version 1)", encoding="utf-8")
    folder = kls_folder.KlsFolder(str(tmp_path / "acme.kicad_pcb"))
    run = folder.new_run(BOARD, "llm", clean=scrub.clean)
    assert run.stamp == kls_folder.stamp(BOARD)  # the raw text's, as KiCad's live board hashes
    assert (run.directory / "board.kicad_pcb").read_text(encoding="utf-8") == scrub.board(BOARD)
    assert "Acme" not in (run.directory / "board.kicad_pro").read_text(encoding="utf-8")
    assert (run.directory / "board.kicad_dru").read_text(encoding="utf-8") == "(version 1)"
    drc = folder.new_run(BOARD, "drc")  # KiCad's DRC keeps the raw files
    assert (drc.directory / "board.kicad_pcb").read_text(encoding="utf-8") == BOARD


def test_a_project_left_out_is_not_copied(tmp_path):
    (tmp_path / "acme.kicad_pro").write_text("not json", encoding="utf-8")
    folder = kls_folder.KlsFolder(str(tmp_path / "acme.kicad_pcb"))
    run = folder.new_run(BOARD, "llm", clean=scrub.clean)
    assert not (run.directory / "board.kicad_pro").exists()


def test_a_link_out_of_the_folder_is_not_read(tmp_path):
    folder = kls_folder.KlsFolder(str(tmp_path / "acme.kicad_pcb"))
    folder.ensure()
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    try:
        os.symlink(secret, folder.findings_path)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here (Windows without developer mode)")
    with pytest.raises(ValueError):
        folder.open_read(folder.findings_path)


def test_a_plain_file_of_the_folder_is_read(tmp_path):
    folder = kls_folder.KlsFolder(str(tmp_path / "acme.kicad_pcb"))
    folder.ensure()
    folder.findings_path.write_bytes(b"{}")
    with folder.open_read(folder.findings_path) as handle:
        assert handle.read() == b"{}"
    with pytest.raises(FileNotFoundError):
        folder.open_read(folder.progress_path)
