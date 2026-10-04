"""Flex mode's fold (`blender_addon/kileido/foldmath.py`), without Blender."""

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from kileido_bridge import flex_checks
from test_flex import STACK, bend, board, mm, zone

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "foldmath.py"
_SPEC = importlib.util.spec_from_file_location("kileido_foldmath", _PATH)
foldmath = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(foldmath)

MM = 1e-3
ORIGIN = (50_000_000, 15_000_000)  # world (0, 0): KiCad (50, 15) mm
HEIGHTS = {"F.Cu": 1.3e-3, "In1.Cu": 0.7e-3, "In2.Cu": 0.632e-3, "B.Cu": 0.0}
THICKNESS = {"F.Cu": 35e-6, "In1.Cu": 18e-6, "In2.Cu": 18e-6, "B.Cu": 35e-6}


def make_plan(*bends):
    report = flex_checks.report(board(zone(), *bends), STACK)
    return foldmath.plan(report, ORIGIN, HEIGHTS, THICKNESS), report


def world(x, y, z=None):
    """KiCad mm to world metres; z on the flex's middle plane by default."""
    return np.array(((x - 50) * MM, -(y - 15) * MM, (0.614e-3 + 0.7e-3) / 2 if z is None else z))


def test_plan_puts_bends_in_world_metres():
    plan, report = make_plan(*bend(42, "90° R2"), *bend(58, "-90° R0.5 #2"))
    first, second = plan.bends
    assert first.origin == pytest.approx(world(42, 15)) and first.axis == pytest.approx((0, -1, 0))
    assert first.normal == pytest.approx((1, 0, 0))  # its child, x > 42, is the rest of the board to the right
    assert first.width == pytest.approx(report["bends"][0]["width_nm"] * 1e-9)
    assert (first.target, second.target) == pytest.approx((math.pi / 2, -math.pi / 2))
    assert (first.step, second.step) == (1, 2)
    assert plan.order == [1, 0]  # the deeper bend first
    assert plan.z_mid == pytest.approx((0.614e-3 + 0.7e-3) / 2)
    assert plan.z_range == pytest.approx((0.614e-3 - 50e-6, 0.7e-3 + 50e-6))
    assert sorted(map(sorted, plan.transitions)) == sorted(
        map(sorted, ([tuple(world(30, 8)[:2]), tuple(world(30, 22)[:2])],
                     [tuple(world(70, 22)[:2]), tuple(world(70, 8)[:2])])))


def test_tags_mark_each_point_with_the_bends_it_hangs_off():
    plan, _ = make_plan(*bend(42, "90° R2"), *bend(58, "90° R2"))
    points = np.array([world(10, 10), world(42, 15), world(50, 15), world(58.5, 15), world(90, 15)])
    mask, zone_index, _ = foldmath.tags(points, plan)
    assert list(zone_index) == [-1, 0, -1, 1, -1]
    assert list(mask) == [0, 0, 1, 1, 3]  # a strip point hangs off its parent side only


def test_a_quarter_fold_lifts_the_child_by_the_radius_plus_the_distance():
    plan, _ = make_plan(*bend(42, "90° R2"))
    folded = plan.bends[0]
    radius = folded.width / (math.pi / 2)
    beyond = world(42, 15) + folded.normal * (folded.width / 2 + 5 * MM)
    mask, zone_index, _ = foldmath.tags(beyond[None], plan)
    moved = foldmath.fold(beyond[None], mask, zone_index, [math.pi / 2], plan)[0]
    expected = world(42, 15) + folded.normal * (-folded.width / 2 + radius) + np.array((0, 0, radius + 5 * MM))
    assert moved == pytest.approx(expected, abs=1e-9)
    assert foldmath.fold(beyond[None], mask, zone_index, [0.0], plan)[0] == pytest.approx(beyond, abs=1e-9)


def test_the_strip_keeps_its_length_and_meets_the_rigid_part():
    plan, _ = make_plan(*bend(42, "90° R2"))
    folded = plan.bends[0]
    for angle in (math.pi / 2, -math.pi, 0.3):
        for h in (-0.2 * MM, 0.0, 0.2 * MM):
            line = np.array([world(42, 15) + folded.normal * v + np.array((0, 0, h))
                             for v in np.linspace(-folded.width / 2, folded.width / 2, 41)])
            rolled = foldmath.strip(folded, line, angle)
            edge = foldmath.rigid(folded, line[-1:], angle)[0]
            assert rolled[-1] == pytest.approx(edge, abs=1e-9)
            if h == 0.0:  # the middle plane neither stretches nor shrinks
                steps = np.linalg.norm(np.diff(rolled, axis=0), axis=1)
                assert steps.sum() == pytest.approx(folded.width, rel=1e-3)


def test_a_u_fold_lays_the_far_end_upside_down_over_the_board():
    plan, _ = make_plan(*bend(42, "90° R2"), *bend(58, "90° R2"))
    angles = [math.pi / 2, math.pi / 2]
    matrix = foldmath.region_matrix(plan.bends[1].child, angles, plan)
    assert matrix[:3, 2] == pytest.approx((0, 0, -1), abs=1e-9)  # its up now points down
    assert matrix[:3, 0] == pytest.approx((-1, 0, 0), abs=1e-9)  # and it runs back over the board
    points = np.array([world(80, 12), world(95, 18, 0.65e-3)])
    mask, zone_index, _ = foldmath.tags(points, plan)
    moved = foldmath.fold(points, mask, zone_index, angles, plan)
    by_matrix = (matrix @ np.c_[points, np.ones(2)].T).T[:, :3]
    assert moved == pytest.approx(by_matrix, abs=1e-9)
    assert (moved[:, 2] > plan.z_mid).all()


def test_steps_fold_one_after_another():
    plan, _ = make_plan(*bend(42, "90° R2 #1"), *bend(58, "-90° R2 #2"))
    assert foldmath.angles_at(plan, 0.0) == pytest.approx([0, 0])
    assert foldmath.angles_at(plan, 0.25) == pytest.approx([math.pi / 4, 0])
    assert foldmath.angles_at(plan, 0.75) == pytest.approx([math.pi / 2, -math.pi / 4])
    assert foldmath.angles_at(plan, 1.0) == pytest.approx([math.pi / 2, -math.pi / 2])
    together, _ = make_plan(*bend(42, "90° R2"), *bend(58, "90° R2"))
    assert foldmath.angles_at(together, 0.5) == pytest.approx([math.pi / 4, math.pi / 4])


def test_the_flex_is_thin_and_ramps_down_from_the_rigid_board():
    plan, _ = make_plan(*bend(42, "90° R2"))
    body = (0.0, 1.335e-3)
    low, high = plan.z_range
    points = np.array([world(50, 15, body[1]), world(50, 15, body[1] + 10e-6), world(50, 15, 0.0),
                       world(50, 15, 1.0e-3), world(20, 15, body[1]), world(30, 15, body[1]),
                       world(30.025, 15, body[1]), world(50, 8, body[1])])
    thinned = foldmath.thin(points, plan, body)[:, 2]
    assert thinned[:4] == pytest.approx([high, high + 10e-6, low, high])  # top, mask on it, bottom, clamped
    assert thinned[4] == pytest.approx(body[1]) and thinned[5] == pytest.approx(body[1])  # rigid, and its edge
    assert thinned[6] == pytest.approx((body[1] + high) / 2)  # half way down the ramp
    assert thinned[7] == pytest.approx(high)  # the flex's own board edge is thin too


def test_cut_planes_cross_each_strip_and_line_the_transitions():
    plan, _ = make_plan(*bend(42, "90° R2"))
    planes = foldmath.cut_planes(plan)
    assert len(planes) == 10 + 2 * 3  # 9 slices of 10 degrees, and 3 planes at each of the 2 transitions
    across = planes[:10]
    assert all(normal == pytest.approx(plan.bends[0].normal) for _, normal in across)
    assert across[0][0][0] == pytest.approx(world(42, 15)[0] - plan.bends[0].width / 2)
    assert across[-1][0][0] == pytest.approx(world(42, 15)[0] + plan.bends[0].width / 2)


def test_board_past_the_rigid_edge_is_flex_even_outside_the_drawn_zone():
    plan, _ = make_plan(*bend(50, "90° R2"))
    points = np.array([world(30.5, 7.5), world(29.5, 7.5), world(29.5, 15), world(30.01, 15), world(31, 5.5)])
    share = foldmath.flexness(points[:, :2], plan)
    assert list(share) == pytest.approx([1.0, 0.0, 0.0, 0.2, 0.0])
    # (30.5, 7.5): a rounded inside corner's web, past the edge at x = 30: flex. (29.5, *): rigid.
    # (30.01, 15): on the ramp. (31, 5.5): past the edge but 2.5 mm from the zone: not flex.


def test_stiffener_materials_are_drawn_as_their_kicad_text_says():
    assert foldmath.stiffener_look("Stainless steel") == ("metal", True)
    assert foldmath.stiffener_look("SUS304") == ("metal", True)
    assert foldmath.stiffener_look("Aluminium") == ("metal", True)
    assert foldmath.stiffener_look("Polyimide") == ("polyimide", True)
    assert foldmath.stiffener_look("PI") == ("polyimide", True)
    assert foldmath.stiffener_look("FR-4") == ("fr4", True)
    assert foldmath.stiffener_look("Kevlar") == ("fr4", False)  # unknown: FR4, and the panel says so
    assert foldmath.stiffener_look("") == ("fr4", False)


def test_a_twist_turns_the_tail_about_its_line_evenly_along_it():
    twist_line = (("User.2", "line", mm((40, 15), (60, 15))), ("User.2", "text", mm((45, 16)), "twist 90°"))
    plan, _ = make_plan(*twist_line)
    (twist,) = plan.bends
    assert twist.kind == "twist" and twist.width == pytest.approx(20 * MM)
    assert twist.pivot == pytest.approx(world(50, 15)) and twist.normal == pytest.approx((1, 0, 0))
    edge = [world(x, 20) for x in (35, 40, 50, 60, 65)]  # 5 mm off the axis, along the tail
    mask, zone_index, _ = foldmath.tags(np.array(edge), plan)
    assert list(zone_index) == [-1, 0, 0, 0, -1] and list(mask) == [0, 0, 0, 0, 1]
    moved = foldmath.fold(np.array(edge), mask, zone_index, [math.pi / 2], plan)
    off = moved - np.array([world(x, 15) for x in (35, 40, 50, 60, 65)])
    assert off[0] == pytest.approx((0, -5 * MM, 0), abs=1e-9)  # before it: untouched
    assert off[1] == pytest.approx((0, -5 * MM, 0), abs=1e-9)  # its start: not turned yet
    half = 5 * MM / math.sqrt(2)  # half way: 45 degrees
    assert off[2] == pytest.approx((0, -half, -half), abs=1e-9)
    assert off[3] == pytest.approx((0, 0, -5 * MM), abs=1e-9)  # its end: a quarter turn
    assert off[4] == pytest.approx(off[3], abs=1e-9)  # past it: turned with its end
    assert np.linalg.norm(off, axis=1) == pytest.approx(5 * MM)  # the same distance from the axis


def test_a_cone_rolls_its_wedge_without_stretching_and_folds_by_its_angle():
    area = ("User.2", "closed", mm((40, 7), (44, 7), (42, 23), (41, 23)))  # sides meet at (41.33, 28.33)
    plan, report = make_plan(area, ("User.2", "text", mm((42, 15)), "90°"))
    (cone,) = plan.bends
    assert cone.kind == "cone" and cone.target == pytest.approx(report["bends"][0]["psi"] - cone.alpha)
    points = np.array([world(x, y) for x, y in ((30, 15), (40.5, 12), (42, 15), (41.5, 20), (60, 15), (55, 10))])
    mask, zone_index, _ = foldmath.tags(points, plan)
    assert list(zone_index) == [-1, 0, 0, 0, -1, -1]
    flat = foldmath.fold(points, mask, zone_index, [0.0], plan)
    assert flat == pytest.approx(points, abs=1e-12)  # angle 0: flat
    moved = foldmath.fold(points, mask, zone_index, [cone.target], plan)
    tip = cone.pivot
    assert np.linalg.norm(moved - tip, axis=1) == pytest.approx(np.linalg.norm(points - tip, axis=1))  # no stretch
    # The far part's face turns by the KiCad angle, 90 degrees, towards the top.
    matrix = foldmath.region_matrix(cone.child, [cone.target], plan)
    assert math.degrees(math.acos(matrix[2, 2])) == pytest.approx(90, abs=1e-6)
    # The strip meets the far part: a point on its second side, (44, 7)-(42, 23) at y = 15, rolled
    # with the strip, is where the far part's turn puts it.
    edge = np.array([world(44 - 2 * 8 / 16, 15)])
    rolled = foldmath.strip(cone, edge, cone.target)
    turned = foldmath.rigid(cone, edge, cone.target)
    assert rolled == pytest.approx(turned, abs=1e-9)


def test_steps_pieces_and_what_may_touch():
    plan, _ = make_plan(*bend(42, "90° R2 #2"), *bend(58, "90° R2 #1"))
    assert foldmath.steps(plan) == [(1, [1]), (2, [0])]
    points = np.array([world(10, 10), world(42, 15), world(50, 15), world(90, 15)])
    mask, zone_index, _ = foldmath.tags(points, plan)
    board_piece, strip, middle, far = foldmath.pieces(plan, points, zone_index)
    assert strip == ("strip", 0) and board_piece[0] == middle[0] == far[0] == "region"
    assert not foldmath.apart(plan, strip, board_piece) and not foldmath.apart(plan, strip, middle)
    assert foldmath.apart(plan, board_piece, middle)  # only through the strip: a folded-back flap hits it
    assert foldmath.apart(plan, board_piece, far)
    assert [foldmath.piece_name(plan, piece) for piece in (board_piece, strip, far)] == [
        "the board", "bend 1", "the part past bend 2"]
