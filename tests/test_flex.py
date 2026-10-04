"""Flex mode's model (`kileido_bridge/flex.py`): zones, bends, regions and stiffeners from
user-layer drawings, the stackup's flexible part, and the reader's drawings."""

import math

import pytest
from types import SimpleNamespace as NS

from kipy.proto.board.board_types_pb2 import BoardLayer

from kileido_bridge import flex, flex_checks, model
from kileido_bridge.board_specs import flex_stack
from kileido_bridge.kicad_reader import read_snapshot
from test_phase1 import FakeBoard, item_id, point

MM = 1_000_000
NAMES = {"User.1": "Flex", "User.2": "Bend", "User.3": "Stiffener"}
STACK = {"layers": ["In1.Cu", "In2.Cu"], "thickness_nm": 86_000}


def mm(*points):
    return tuple((round(x * MM), round(y * MM)) for x, y in points)


def rect(x0, y0, x1, y1):
    return mm((x0, y0), (x1, y0), (x1, y1), (x0, y1))


# The rigid-flex test board's shape: rigid ends x 0..30 (taller, so it is the root) and
# 70..100, joined by flex x 30..70, y 8..22.
OUTLINE = mm((0, -5), (30, -5), (30, 8), (70, 8), (70, 0), (100, 0), (100, 30), (70, 30), (70, 22),
             (30, 22), (30, 35), (0, 35))


def board(*drawings, outline=OUTLINE, extra_rings=()):
    counter = iter(range(1000))

    def make(layer, kind, points, text=""):
        return model.Drawing(f"d{next(counter)}", layer, kind, points, text)

    return model.BoardSnapshot("flex.kicad_pcb", dict(NAMES), (), (), (), (), (), (),
                               model.Outline(((outline,), *((ring,) for ring in extra_rings))),
                               model.Stackup(()), (), {},
                               drawings=tuple(make(*drawing) for drawing in drawings))


def zone():
    return ("User.1", "closed", rect(30, 8, 70, 22))


def bend(x, label="90° R1.5", y0=8, y1=22):
    line = ("User.2", "line", mm((x, y0), (x, y1)))
    return (line,) if label is None else (line, ("User.2", "text", mm((x + 0.5, y0 - 1)), label))


def test_bend_and_stiffener_texts():
    assert flex.parse_bend("90° R1.5") == (90.0, 1_500_000)
    assert flex.parse_bend("-45 deg R 2 mm") == (-45.0, 2_000_000)
    assert flex.parse_bend("R0,5 90°") == (90.0, 500_000)
    assert flex.parse_bend("BEND HERE") is None
    assert flex.parse_bend("180° R1 #2") == (180.0, 1_000_000) and flex.parse_step("180° R1 #2") == 2
    assert flex.parse_step("90° R1.5") == 1
    # Forgiving: any order and spelling; what it leaves out is None.
    assert flex.parse_bend("-90 deg r2mm") == (-90.0, 2_000_000)
    assert flex.parse_bend("R2 90") == (90.0, 2_000_000)
    assert flex.parse_bend("angle 45 radius 3") == (45.0, 3_000_000)
    assert flex.parse_bend("R500um 90°") == (90.0, 500_000)
    assert flex.parse_bend("90°") == (90.0, None) and flex.parse_bend("R2 #3") == (None, 2_000_000)
    assert flex.parse_bend("Bend 2") is None  # a lone number is only the angle beside a radius
    assert flex.parse_stiffener("SUS 0.3mm") == ("SUS", 300_000, None)
    assert flex.parse_stiffener("steel 0.3 top") == ("steel", 300_000, "top")
    assert flex.parse_coverlay(["Coverlay black"]) == ("BLACK", None)
    assert flex.parse_coverlay(["COVERLAY: White"]) == ("WHITE", None)
    assert flex.parse_coverlay(["coverlay purple"]) == ("AMBER", "coverlay purple")
    assert flex.parse_coverlay(["Rigid-flex, 4 layers"]) == ("AMBER", None)
    assert flex.parse_stiffener("Polyimide 0.2 mm bottom") == ("Polyimide", 200_000, "bottom")
    assert flex.parse_stiffener("FR4, 300um, top") == ("FR4", 300_000, "top")
    assert flex.parse_stiffener("steel") == ("steel", None, None)


def test_what_a_bend_text_leaves_out_is_assumed_and_said():
    cases = {None: ("angle and radius", ""), "ninety": ("angle and radius", ' "ninety"'),
             "45°": ("radius", ' "45°"'), "R3": ("angle", ' "R3"')}
    for label, (missing, said) in cases.items():
        result = flex.build(board(zone(), *bend(42, label=label)), STACK)
        (folded,) = result.bends
        angle = 45.0 if label == "45°" else 90.0
        radius = 3_000_000 if label == "R3" else 1_900_000  # 186 um of flex at 10x, rounded up to 0.1 mm
        assert (folded.angle_deg, folded.radius_nm) == (angle, radius), label
        assert [(p.message, p.level) for p in result.problems] == [
            (f"Bend 1: {missing} not in its text{said}, so {flex.bend_note(angle, radius)} is assumed. Copy its "
             f"text to keep it, or write your own", "note")], label
    assert flex.default_radius_nm(186_000, 1) == 1_200_000 and flex.default_radius_nm(400_000, 4) == 8_000_000


def test_a_board_without_flex_has_no_model():
    assert flex.build(board(), {}) is None


def test_two_bends_split_the_board_into_three_regions_hanging_off_the_largest():
    result = flex.build(board(zone(), *bend(42), *bend(58, "-90° R0.5")), STACK)
    assert not result.problems
    assert result.layers == ("In1.Cu", "In2.Cu") and result.thickness_nm == 86_000
    assert [(b.start, b.end, b.angle_deg, b.radius_nm) for b in result.bends] == [
        (mm((42, 8))[0], mm((42, 22))[0], 90.0, 1_500_000), (mm((58, 8))[0], mm((58, 22))[0], -90.0, 500_000)]
    left, middle, right = (result.region_at(p) for p in mm((10, 10), (50, 15), (90, 15)))
    assert len(result.regions) == 3 and len({left, middle, right}) == 3
    assert result.regions[left].parent is None
    assert (result.regions[middle].parent, result.regions[middle].bend) == (left, 0)
    assert (result.regions[right].parent, result.regions[right].bend) == (middle, 1)
    assert result.region_at(mm((50, 30))[0]) is None  # off the board


def test_a_short_bend_line_still_folds_edge_to_edge_but_says_so():
    result = flex.build(board(zone(), *bend(42, y0=10, y1=22)), STACK)
    assert [(b.start, b.end) for b in result.bends] == [(mm((42, 8))[0], mm((42, 22))[0])]
    assert [p.message for p in result.problems] == ["A bend line should run edge to edge of the flex"]
    assert result.problems[0].where == mm((42, 8))[0]


def test_bends_that_cannot_fold_are_left_out_with_a_reason():
    cases = {
        "crosses rigid board": (zone(), *bend(20)),
        "one straight line": (zone(), ("User.2", "line", mm((42, 8), (44, 15), (42, 22))),
                              ("User.2", "text", mm((42.5, 7)), "90° R1")),
        "Two bends cross": (zone(), *bend(42), ("User.2", "line", mm((35, 8), (55, 22))),
                            ("User.2", "text", mm((56, 23)), "90° R1")),
    }
    for reason, drawings in cases.items():
        result = flex.build(board(*drawings), STACK)
        assert len(result.bends) == (1 if reason == "Two bends cross" else 0), reason
        assert any(reason in p.message for p in result.problems), (reason, result.problems)


def test_a_bend_into_a_cutout_is_left_out():
    for hole in (rect(48, 12, 52, 18), rect(48, 17, 52, 20)):  # across its middle, or near an end
        result = flex.build(board(zone(), *bend(50), extra_rings=(hole,)), STACK)
        assert not result.bends and "cutout" in result.problems[0].message, hole


def test_a_flex_zone_drawn_past_the_board_edge_is_still_edge_to_edge():
    result = flex.build(board(("User.1", "closed", rect(30, 6, 70, 24)), *bend(42)), STACK)
    assert len(result.bends) == 1 and not result.problems


def test_flex_drawn_without_polyimide_in_the_stackup_is_a_problem():
    result = flex.build(board(zone(), *bend(42)), {})
    assert result.layers == () and "no Polyimide" in result.problems[0].message


def test_stiffeners_take_the_text_inside_or_nearest():
    result = flex.build(board(zone(), ("User.3", "closed", rect(60, 8, 70, 22)),
                              ("User.3", "text", mm((61, 15)), "Polyimide 0.2 mm bottom"),
                              ("User.3", "closed", rect(30, 8, 34, 22)),
                              ("User.3", "text", mm((31, 24)), "FR4 0.3 mm")), STACK)
    assert [(s.material, s.thickness_nm, s.side) for s in result.stiffeners] == [
        ("Polyimide", 200_000, "bottom"), ("FR4", 300_000, "bottom")]
    assert [(p.message, p.level) for p in result.problems] == [
        ('Stiffener 2: side not in its text, so "FR4 0.3 mm bottom" is assumed. Copy its text to keep it, '
         'or write your own', "note")]


def test_flex_stack_is_the_copper_either_side_of_the_polyimide():
    def layer(name, kind, thickness, material=None):
        extra = f' (material "{material}")' if material else ""
        return f'(layer "{name}" (type "{kind}") (thickness {thickness}){extra})'
    text = ("(kicad_pcb (setup (stackup " + " ".join((
        layer("F.Mask", "Top Solder Mask", 0.01), layer("F.Cu", "copper", 0.035),
        layer("dielectric 1", "prepreg", 0.6, "FR4"), layer("In1.Cu", "copper", 0.018),
        layer("dielectric 2", "core", 0.05, "Polyimide"), layer("In2.Cu", "copper", 0.018),
        layer("dielectric 3", "prepreg", 0.6, "FR4"), layer("B.Cu", "copper", 0.035))) + ")))")
    assert flex_stack(text) == {"layers": ["In1.Cu", "In2.Cu"], "thickness_nm": 86_000}
    assert flex_stack(text.replace("Polyimide", "FR4")) == {}


def test_reader_keeps_drawings_on_layers_named_for_flex_only():
    fake = FakeBoard()
    fake.layers += [BoardLayer.BL_User_1, BoardLayer.BL_User_2, BoardLayer.BL_User_3]
    fake.layer_names = {BoardLayer.BL_User_1: "Flex", BoardLayer.BL_User_2: "Bend", BoardLayer.BL_User_3: "Notes"}
    fake.shapes = [
        NS(id=item_id("zone"), layer=BoardLayer.BL_User_1, top_left=point(0, 0), bottom_right=point(10, 5)),
        NS(id=item_id("bend"), layer=BoardLayer.BL_User_2, start=point(5, 0), end=point(5, 5)),
        NS(id=item_id("note"), layer=BoardLayer.BL_User_3, start=point(0, 0), end=point(1, 1)),
    ]
    fake.texts = [NS(id=item_id("label"), layer=BoardLayer.BL_User_2, position=point(6, -1), value="90° R1")]
    drawings = read_snapshot(fake).drawings
    assert [(d.id, d.layer, d.kind, d.points, d.text) for d in drawings] == [
        ("zone", "User.1", "closed", ((0, 0), (10, 0), (10, 5), (0, 5)), ""),
        ("bend", "User.2", "line", ((5, 0), (5, 5)), ""),
        ("label", "User.2", "text", ((6, -1),), "90° R1"),
    ]
    assert model.snapshot_from_jsonable(model.to_jsonable(read_snapshot(fake))).drawings == drawings


# --- Checks (`kileido_bridge/flex_checks.py`) ------------------------------------------------

STACKUP = model.Stackup(tuple(model.StackupLayer(name, "copper", 35_000, None, None, None)
                              for name in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")))


def track(uid, layer, net, *points, width=0.15):
    pts = mm(*points)
    return tuple(model.Track(f"{uid}{k}", layer, net, a, b, round(width * MM)) for k, (a, b) in
                 enumerate(zip(pts, pts[1:])))


def checked(*, tracks=(), vias=(), footprints=(), pads=(), zones=(), drawings=(), outline=OUTLINE):
    snapshot = board(zone(), *bend(42, "90° R2"), *bend(58, "90° R0.5"), *drawings, outline=outline)
    snapshot = model.BoardSnapshot(snapshot.board_name, snapshot.layer_display_names, tuple(tracks), (),
                                   tuple(vias), tuple(pads), tuple(footprints), tuple(zones), snapshot.outline,
                                   STACKUP, (), {}, drawings=snapshot.drawings)
    result = flex.build(snapshot, STACK)
    assert not result.problems, result.problems
    return [f for f in flex_checks.check(snapshot, result) if "R0.5" not in f.message and "Sharp" not in f.message
            and "Bend 1:" not in f.message]


def messages(findings):
    return [f.message for f in findings]


def test_clean_flex_routing_passes():
    assert checked(tracks=track("s", "In1.Cu", "SIG0", (22, 11), (78, 11))) == []


def test_a_trace_crossing_a_bend_at_an_angle():
    findings = checked(tracks=track("s", "In1.Cu", "SIG5", (22, 16), (40, 16), (44, 19), (78, 19)))
    assert messages(findings) == ["SIG5 crosses bend 1 at 37° off square; cross it square"]
    assert findings[0].items == ("s1",) and findings[0].where == mm((42, 17.5))[0]


def test_vias_and_parts_on_bends():
    vias = (model.Via("v1", "SIG2", mm((58, 13))[0], 450_000, 200_000, "In1.Cu", "In2.Cu"),
            model.Via("v2", "SIG3", mm((50, 13))[0], 450_000, 200_000, "In1.Cu", "In2.Cu"))  # between bends: fine
    part = model.Footprint("fp", "U1", mm((42, 15))[0], 0.0, "top", (), bbox_nm=(41 * MM, 14 * MM, 2 * MM, 2 * MM))
    findings = checked(vias=vias, footprints=(part,))
    assert messages(findings) == ["Via (SIG2) on bend 2", "U1 is on bend 1"]
    assert [f.items for f in findings] == [("v1",), ("fp",)]
    # The panel folds findings of one kind into one row: these carry its name.
    assert [f.group for f in findings] == ["Vias on bend 2", "Parts on bend 1"]


def test_copper_on_layers_the_flex_does_not_have():
    through = model.Via("v3", "SIG4", mm((50, 20))[0], 600_000, 300_000, "F.Cu", "B.Cu")
    pour = model.ZoneFill("z1", "GND", "B.Cu", ((rect(28, 9, 33, 12),),))
    findings = checked(tracks=track("l", "F.Cu", "LED", (12, 20), (36, 20)), vias=(through,), zones=(pour,))
    assert messages(findings) == ["LED runs on F.Cu into the flex; F.Cu does not continue there",
                                  "Via (SIG4) in the flex reaches F.Cu", "GND pour on B.Cu reaches into the flex"]
    assert findings[0].where == mm((30, 20))[0]


def test_stacked_traces_on_the_two_flex_layers():
    findings = checked(tracks=track("a", "In1.Cu", "SIG0", (22, 11), (78, 11))
                       + track("b", "In2.Cu", "SIG6", (25, 11), (75, 11)))
    assert messages(findings) == ["SIG6 (In2.Cu) runs under SIG0 (In1.Cu) through the flex; stagger them"]
    beside = checked(tracks=track("a", "In1.Cu", "SIG0", (22, 11), (78, 11))
                     + track("b", "In2.Cu", "SIG6", (25, 11.5), (75, 11.5)))
    assert beside == []


def test_solid_pours_across_a_bend_but_not_hatched_ones():
    solid = model.ZoneFill("z1", "GND", "In2.Cu", ((rect(29.5, 8.5, 70.5, 21.5),),))
    holes = tuple(rect(x, y, x + 0.5, y + 0.5) for x in [30 + 0.8 * k for k in range(50)]
                  for y in [8.5 + 0.8 * k for k in range(17)])
    hatched = model.ZoneFill("z2", "GND", "In2.Cu", ((rect(29.5, 8.5, 70.5, 21.5), *holes),))
    assert messages(checked(zones=(solid,))) == ["Solid GND pour on In2.Cu across bend 1; hatch it in the flex",
                                                 "Solid GND pour on In2.Cu across bend 2; hatch it in the flex"]
    assert checked(zones=(hatched,)) == []


def test_a_stiffener_reaching_into_a_bend():
    stiffener = (("User.3", "closed", rect(56, 8, 70, 22)), ("User.3", "text", mm((63, 15)), "FR4 0.3 mm bottom"))
    assert messages(checked(drawings=stiffener)) == ["A stiffener reaches into bend 2"]


def test_bend_radius_against_the_flex_thickness():
    result = flex.build(board(zone(), *bend(42, "90° R2"), *bend(58, "90° R0.5")), STACK)
    found = [(f.message, f.use) for f in flex_checks.check(board(), result) if f.message.startswith("Bend")]
    assert found == [
        ("Bend 1: R2 mm is 10.8× the flex's 186 µm; dynamic flex needs 150× (27.90 mm)", "dynamic"),
        ("Bend 2: R0.5 mm is 2.7× the flex's 186 µm; static flex needs 10× (1.86 mm)", "static"),
        ("Bend 2: R0.5 mm is 2.7× the flex's 186 µm; dynamic flex needs 150× (27.90 mm)", "dynamic")]
    assert flex_checks.limits(1) == {"static": 6, "dynamic": 100}
    assert flex_checks.limits(4) == {"static": 20, "dynamic": None}


def test_sharp_inside_corners_at_the_flex_and_not_rounded_ones():
    def corners(outline):
        snapshot = board(zone(), outline=outline)
        return [f.where for f in flex_checks.check(snapshot, flex.build(snapshot, STACK)) if "Sharp" in f.message]
    assert sorted(corners(OUTLINE)) == sorted(mm((30, 8), (70, 8), (70, 22), (30, 22)))
    points = list(OUTLINE)  # (30, 8) rounded: R1 around (31, 7), sampled every 10 degrees
    k = points.index(mm((30, 8))[0])
    points[k:k + 1] = mm(*[(31 - math.cos(math.radians(a)), 7 + math.sin(math.radians(a))) for a in range(0, 91, 10)])
    assert sorted(corners(tuple(points))) == sorted(mm((70, 8), (70, 22), (30, 22)))


def test_the_report_reaches_blender_in_every_snapshot():
    from kileido_bridge.protocol import FrameDecoder, snapshot_frames
    snapshot = board(zone(), *bend(42, "90° R2"))
    report = flex_checks.report(snapshot, STACK)
    assert report["total_nm"] == 186_000 and report["limits"] == {"static": 10, "dynamic": 150}
    assert [bend["radius_nm"] for bend in report["bends"]] == [2_000_000] and len(report["regions"]) == 2
    bend_report = report["bends"][0]  # chord (42, 8) to (42, 22): the child, x > 42, is on its -dy side
    assert (bend_report["side"], bend_report["step"], bend_report["parent"], bend_report["child"]) == (-1, 1, 1, 0)
    assert bend_report["width_nm"] == round((2_000_000 + 93_000) * math.pi / 2)
    headers = [header for header, _ in FrameDecoder().feed(b"".join(snapshot_frames(snapshot, flex=report)))]
    assert [h["type"] for h in headers][-2:] == ["flex", "snapshot_end"] and headers[-2]["flex"] == report
    assert flex_checks.report(board(), {}) is None


def test_a_pure_flex_board_is_flex_all_over_without_a_zone():
    stackup = model.Stackup((model.StackupLayer("F.Cu", "copper", 18_000, None, None, None),
                             model.StackupLayer("core", "dielectric", 50_000, None, None, None),
                             model.StackupLayer("B.Cu", "copper", 18_000, None, None, None)))
    snapshot = board(*bend(42, "90° R1"))
    snapshot = model.BoardSnapshot(snapshot.board_name, snapshot.layer_display_names, (), (), (), (), (), (),
                                   snapshot.outline, stackup, (), {}, drawings=snapshot.drawings)
    result = flex.build(snapshot, {"layers": ["F.Cu", "B.Cu"], "thickness_nm": 86_000})
    assert result.zones == (OUTLINE,) and len(result.bends) == 1 and not result.problems
    rigid_flex = flex.build(snapshot, {"layers": ["F.Cu"], "thickness_nm": 86_000})  # B.Cu stays rigid
    assert rigid_flex.zones == () and not rigid_flex.bends


def test_a_twist_is_a_line_along_the_tail_saying_twist():
    assert flex.parse_twist("twist 90°") == 90.0 and flex.parse_twist("Twist -45 #2") == -45.0
    assert flex.parse_twist("twist") is None
    line = ("User.2", "line", mm((40, 15), (60, 15)))  # along the flex, x 40..60
    result = flex.build(board(zone(), line, ("User.2", "text", mm((45, 16)), "twist 90° #1")), STACK)
    (twist,) = result.bends
    assert (twist.kind, twist.angle_deg, twist.length_nm, twist.pivot, twist.step) == (
        "twist", 90.0, 20_000_000, mm((50, 15))[0], 1)
    assert (twist.start, twist.end) in ((mm((50, 8))[0], mm((50, 22))[0]), (mm((50, 22))[0], mm((50, 8))[0]))
    assert not result.problems and len(result.regions) == 2
    report = flex_checks.report(board(zone(), line, ("User.2", "text", mm((45, 16)), "twist")), STACK)
    assert report["bends"][0]["width_nm"] == 20_000_000 and report["bends"][0]["note"] == "twist 90°"
    assert [p["message"] for p in report["problems"]] == [
        'Twist 1: angle not in its text "twist", so twist 90° is assumed. Copy its text to keep it, '
        'or write your own']
    assert not [f for f in report["findings"] if f["message"].startswith(("Twist 1", "Bend 1"))]  # no radius check
    rigid = flex.build(board(zone(), ("User.2", "line", mm((20, 15), (40, 15))),
                             ("User.2", "text", mm((25, 16)), "twist 90°")), STACK)
    assert not rigid.bends and "reaches rigid board" in rigid.problems[0].message


def test_a_bend_drawn_as_its_area_takes_its_radius_from_its_width():
    area = ("User.2", "closed", mm((40, 7), (44, 7), (44, 23), (40, 23)))  # past the board edges, y 8..22
    result = flex.build(board(zone(), area, ("User.2", "text", mm((41, 15)), "90° #1")), STACK)
    (folded,) = result.bends
    assert folded.kind == "bend" and folded.area is not None and not result.problems
    assert (folded.start[0], folded.end[0]) == (42_000_000, 42_000_000)  # half way between its sides
    assert folded.radius_nm == round(4_000_000 / (math.pi / 2) - 93_000)  # its width rolled through 90°


def test_converging_sides_make_a_cone():
    area = ("User.2", "closed", mm((40, 7), (44, 7), (42, 23), (41, 23)))  # its sides meet at (41.33, 28.33)
    snapshot = board(zone(), area, ("User.2", "text", mm((42, 15)), "90°"))
    result = flex.build(snapshot, STACK)
    (cone,) = result.bends
    assert cone.kind == "cone" and cone.name == "Cone" and not result.problems
    assert cone.apex == pytest.approx(mm((41 + 1 / 3, 28 + 1 / 3))[0], abs=2)
    assert cone.psi > cone.alpha > 0 and 0 < cone.radius_nm < cone.radius_max_nm
    assert len(result.regions) == 2
    report = flex_checks.report(snapshot, STACK)
    first, second = report["bends"][0]["sides"]
    child = result.regions[report["bends"][0]["child"]]
    assert flex._inside(flex._rounded(flex._lerp(*second, 0.5)), child.ring)  # the child's side second
    assert report["bends"][0]["note"] == "90°"


def test_a_cone_turns_so_its_wedge_keeps_its_angle():
    alpha = math.radians(15)
    assert flex.cone_turn(alpha, 0.0)[0] == pytest.approx(alpha)  # flat: the axis upright
    for fold in (0.3, math.pi / 2, 2.5):
        psi, beta = flex.cone_turn(alpha, fold)
        assert psi * math.sin(beta) == pytest.approx(alpha)
        assert math.acos(math.cos(psi) + math.sin(beta) ** 2 * (1 - math.cos(psi))) == pytest.approx(fold)
