"""Return-path check on the synthetic split board (tests/split_board.py), and its frame."""

from dataclasses import replace

import split_board
from kileido_bridge import model, protocol
from kileido_bridge.return_path import RETURN_VIA_RADIUS_NM, ReturnPathCheck, checked_nets
from split_board import MM


def issues(snapshot, selected=frozenset()):
    return ReturnPathCheck().check(snapshot, checked_nets(snapshot, selected))


def by_item(found):
    return {issue.item: issue for issue in found}


def test_diff_pairs_are_checked_and_selected_nets_join_them():
    board = split_board.board()
    pairs = {"USB_D+", "USB_D-", "ETH_P", "ETH_N", "SATA_P", "SATA_N", "LVDS_P", "LVDS_N"}
    assert checked_nets(board) == pairs
    assert checked_nets(board, {"CLK", "missing"}) == pairs | {"CLK"}


def test_split_under_a_diff_pair_is_flagged():
    found = by_item(issues(split_board.board()))
    for item in ("usb-0", "usb-1"):
        issue = found[item]
        assert (issue.kind, issue.layer, issue.reference) == ("split", "F.Cu", "In1.Cu")
        assert abs(issue.at[0] - 50 * MM) <= 100_000  # the middle of the split
        assert "+3V3 | GND" in issue.message
        ((layer, path, width),) = issue.marks
        assert layer == "F.Cu" and width == split_board.WIDTH
        assert path[0][0] < 49_750_000 < 50_250_000 < path[-1][0]
    assert "clk" not in found  # not a pair, not selected
    assert by_item(issues(split_board.board(), {"CLK"}))["clk"].kind == "split"


def test_the_broken_part_of_the_plane_is_marked():
    """Gaps and splits carry the holes in the reference plane that cause them, whole."""
    found = by_item(issues(split_board.board()))
    ((layer, rects),) = found["sata-0"].areas
    assert layer == "In1.Cu" and rects == ((25 * MM, 34 * MM, 26 * MM, 36 * MM),)  # the void
    ((layer, rects),) = found["usb-0"].areas
    left, top, right, bottom = rects[0]
    assert len(rects) == 1 and (left, right) == (49_750_000, 50_250_000)  # the split channel
    assert top < 10 * MM - 2 * MM and bottom > 10 * MM + 2 * MM  # clipped near the pair, not the board
    assert all(not issue.areas for issue in found.values() if issue.kind not in ("gap", "split"))


def test_void_in_the_plane_is_a_gap_and_a_solid_plane_is_clean():
    found = by_item(issues(split_board.board()))
    assert found["sata-0"].kind == "gap" and found["sata-0"].reference == "In1.Cu"
    assert 2 * MM < found["sata-0"].length_nm < 2.4 * MM  # 1 mm void plus 0.6 mm each side
    assert not {"eth-0", "eth-1"} & found.keys()
    assert not {"lvds-f-0", "lvds-f-1", "lvds-b-0", "lvds-b-1"} & found.keys()  # own antipads excused


def test_layer_change_needs_a_nearby_return_via():
    board = split_board.board()
    found = by_item(issues(board))
    for item in ("via-lvds-0", "via-lvds-1"):
        assert found[item].kind == "no_return_via"
        assert found[item].reference == "In1.Cu and In2.Cu"
        assert {layer for layer, _, _ in found[item].marks} == {"F.Cu", "B.Cu"}
    found = by_item(issues(split_board.with_return_via(board, 0.5 * MM)))
    assert "via-lvds-0" not in found and "via-lvds-1" not in found
    far = by_item(issues(split_board.with_return_via(board, RETURN_VIA_RADIUS_NM + MM)))
    assert "via-lvds-0" in far


def test_reference_change_between_nets_needs_a_capacitor():
    board = split_board.board()
    zones = tuple(replace(zone, net="+1V8") if zone.layer == "In2.Cu" else zone for zone in board.zones)
    found = by_item(issues(replace(board, zones=zones)))
    assert found["via-lvds-0"].kind == "reference_change"
    assert "In1.Cu (GND)" in found["via-lvds-0"].message and "In2.Cu (+1V8)" in found["via-lvds-0"].message


def test_stripline_gaps_are_flagged_on_both_planes():
    """Tracks on In1.Cu between a F.Cu ground pour with a void and the solid In2.Cu
    plane: the void is flagged although In2.Cu covers the whole track."""
    board = split_board.board()
    void = split_board.rect(25 * MM, 14 * MM, 26 * MM, 16 * MM)
    pour = model.ZoneFill("pour", "GND", "F.Cu", ((split_board.rect(MM, 12 * MM, 99 * MM, 18 * MM), void),))
    strip = split_board.pair("STRIP_P", "STRIP_N", "In1.Cu", 10 * MM, 40 * MM, 15 * MM, "strip")
    board = replace(board, zones=(pour, board.zones[2]), tracks=strip, vias=())
    found = by_item(issues(board))
    assert found.keys() == {"strip-0", "strip-1"}
    assert found["strip-0"].kind == "gap" and found["strip-0"].reference == "F.Cu"


def test_no_plane_at_all():
    found = issues(replace(split_board.board(), zones=()))
    assert {issue.kind for issue in found} == {"no_reference"}
    assert by_item(found)["usb-0"].length_nm == 80 * MM


def test_check_is_cached_until_something_changes():
    board = split_board.board()
    check = ReturnPathCheck()
    nets = checked_nets(board)
    first = check.check(board, nets)
    computed = check.planes.stats["computed"]
    assert check.check(replace(board), nets) is first  # a new snapshot of the same parts
    assert check.planes.stats["computed"] == computed


def test_frame_carries_issues_and_marks():
    board = split_board.board()
    found = issues(board)
    header, arrays = protocol.FrameDecoder().feed(
        protocol.return_path_message(checked_nets(board), found, revision=7, elapsed_ms=1.5))[0]
    assert header["type"] == "return_path" and header["revision"] == 7
    assert [issue["item"] for issue in header["issues"]] == [issue.item for issue in found]
    assert header["layers"] == ["B.Cu", "F.Cu", "In1.Cu"]
    marks = sum(max(1, len(points) - 1) for issue in found for _, points, _ in issue.marks)
    assert arrays["mark"].shape == (marks, 5)
    via = next(index for index, issue in enumerate(found) if issue.kind == "no_return_via")
    rows = arrays["mark"][arrays["mark_issue"] == via]
    assert (rows[:, 0] == rows[:, 2]).all() and (rows[:, 4] == 600_000).all()  # a disc on each layer
    areas = sum(len(rects) for issue in found for _, rects in issue.areas)
    assert arrays["area"].shape == (areas, 4) and len(arrays["area_issue"]) == len(arrays["area_layer"]) == areas
    assert {header["layers"][index] for index in arrays["area_layer"].tolist()} == {"In1.Cu"}


def test_check_command_prints_the_issues(monkeypatch, capsys):
    from kileido_bridge import cli
    monkeypatch.setattr(cli, "connect_board", lambda timeout_ms: None)
    monkeypatch.setattr(cli, "read_snapshot", lambda board: split_board.board())
    assert cli.main(["check", "--net", "CLK"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("split_board.kicad_pcb: ") and "CLK" in lines[0]
    assert any(line.startswith("split ") and "CLK on F.Cu" in line for line in lines[1:])
