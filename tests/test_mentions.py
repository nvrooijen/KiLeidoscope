"""Names a finding's text mentions: the chips under its message."""

from dataclasses import replace

from kileido_bridge import mentions
from kileido_bridge.board_index import BoardIndex
from test_board_index import board


def found(text, snapshot=None):
    return [(m["text"], m["kind"], m["found"]) for m in mentions.find(BoardIndex(snapshot or board()), text)]


def test_parts_pads_and_nets_in_order():
    pads = tuple(replace(p, pin_name="PA7_13") if p.id == "p-u3-4" else p for p in board().pads)
    index = BoardIndex(replace(board(), pads=pads))
    text = "U3 pin 4 relies on C12 over 4 mm away; SIG and GND."
    result = mentions.find(index, text)
    assert [(m["text"], m["kind"]) for m in result] == [("U3.4", "pad"), ("C12", "part"), ("SIG", "net"),
                                                         ("GND", "net")]
    assert result[0]["what"] == "pad, PA7_13, SIG" and result[0]["ids"] == ["p-u3-4"]


def test_a_pad_written_with_a_dot_and_the_net_inside_it_once():
    assert found("Put it near U3.EP, U3.4 and on net:SIG.") == [("U3.EP", "pad", True), ("U3.4", "pad", True),
                                                                ("SIG", "net", True)]


def test_uuid_codes_found_ambiguous_or_made_up():
    assert found("the track t001? uuid:00000000t001 no; 5a222dbd and 420d87bb") == \
        [("5a222dbd", "", False), ("420d87bb", "", False)]  # two vias end in 5a222dbd; 420d87bb is no item
    full = "00000000-0000-0000-0000-00005a222dbd"
    assert found(f"via {full}") == [(full, "via", True)]


def test_words_that_look_like_names_are_left_out():
    assert found("An STM32 PA7 pin, 0402 at 12345678 mm, R9 and a defaced net") == []
    assert found("U3 and U3 again, then (SIG)") == [("U3", "part", True), ("SIG", "net", True)]


def test_a_net_with_odd_characters_is_whole_only():
    assert found("reset is /~{RST}, not x/~{RST}2") == [("/~{RST}", "net", True)]


def test_no_board_no_mentions():
    assert mentions.find(None, "U3") == []
