"""Via protection as KiCad sets it (`blender_addon/kileido/protection.py`), without Blender."""

import importlib.util
from pathlib import Path

import numpy as np

from kileido_bridge import model

_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "protection.py"
_SPEC = importlib.util.spec_from_file_location("kileido_protection", _PATH)
protection = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(protection)

R = protection.FROM_RULES
NONE = (0, 0, 0, 0, 0, 0, 0, 0)
UNTENTED_RULES = NONE


def resolve(*codes, rules=UNTENTED_RULES, hole=0.3e-3, top=True, bottom=True, max_tent=0.3e-3):
    count = len(codes)
    return protection.resolve(protection.pack(codes), rules, np.full(count, hole),
                              np.full(count, top), np.full(count, bottom), max_tent)


def test_fields_match_the_bridge():
    assert protection.FIELDS == model.PROTECTION


def test_pack_round_trips():
    codes = np.array([(1, 0, R, 1, 0, R, 1, 0), (R,) * 8, NONE])
    assert (protection.unpack(protection.pack(codes)) == codes).all()


def test_only_an_unprotected_through_via_is_open():
    found = resolve(NONE, (1, 0, 0, 0, 0, 0, 0, 0), (0, 0, 0, 1, 0, 0, 0, 0))  # none, I-a, II-a's cover
    assert found["open"].tolist() == [True, False, False]
    assert found["drilled"].all()  # all three are holes: rings, the tent closing the drill
    assert found["tent_top"].tolist() == [False, True, False] and found["tent_bottom"].tolist() == [False, False, True]
    assert found["finished"].tolist() == [True, False, False]  # a tent keeps the finish out of the barrel
    blind = resolve(NONE, (1, 1, 0, 0, 0, 0, 0, 0), top=True, bottom=False)
    assert blind["drilled"].all() and blind["side"].tolist() == [protection.TOP] * 2  # a hole from the top only
    assert blind["tent_top"].tolist() == [False, True] and not blind["tent_bottom"].any()
    assert blind["finished"].tolist() == [True, False]  # its open end is finished


def test_a_buried_via_is_resin_filled_whatever_kicad_says():
    found = resolve(NONE, (1, 1, 1, 1, 1, 1, 1, 0), (0, 0, 0, 0, 0, 0, 0, 1), top=False, bottom=False)
    assert found["core_top"].all() and found["core_bottom"].all()  # pressed full of prepreg resin
    assert found["filled"].tolist() == [False, False, True]  # resin, or KiCad's filled: the fill material
    assert not (found["drilled"] | found["capped"] | found["tent_top"] | found["finished"]).any()


def test_a_capped_via_is_no_hole():
    found = resolve((0, 0, 0, 0, 0, 0, 1, 1), (0, 0, 0, 0, 1, 1, 0, 0))  # VII, III-b
    assert found["drilled"].tolist() == [False, True]  # the cap is copper across; a plug shows in a ring
    assert not found["finished"].any() and not found["open"].any()


def test_from_rules_follows_the_board():
    assert not resolve((R,) * 8, rules=protection.KICAD_DEFAULT)["open"][0]  # KiCad's default: tented
    assert resolve((R,) * 8, rules=NONE)["open"][0]
    assert not resolve((R,) * 8, rules=None)["open"][0]  # no rules known: KiCad's own defaults


def test_type_vii_is_filled_and_capped():
    found = resolve((0, 0, 0, 0, 0, 0, 1, 1))
    assert found["filled"][0] and found["capped"][0] and found["core_top"][0] and found["core_bottom"][0]
    assert resolve((0, 0, 0, 0, 0, 0, 1, 0))["filled"][0]  # a cap needs a fill under it


def test_a_plug_fills_half_from_its_side_and_all_from_both():
    found = resolve((0, 0, 0, 0, 1, 0, 0, 0), (0, 0, 0, 0, 1, 1, 0, 0))
    assert found["core_top"].tolist() == [True, True] and found["core_bottom"].tolist() == [False, True]
    assert not found["filled"].any() and found["plugged"].all()  # plugged, not filled: mask ink
    blind = resolve((0, 0, 0, 0, 1, 0, 0, 0), top=True, bottom=False)  # plugged from its only open end
    assert blind["core_top"][0] and blind["core_bottom"][0]


def test_a_tent_does_not_span_a_large_empty_drill():
    found = resolve((1, 1, 0, 0, 0, 0, 0, 0), hole=0.5e-3)
    assert found["open"][0] and found["too_big"][0]
    filled = resolve((1, 1, 0, 0, 0, 0, 0, 1), hole=0.5e-3)  # VI-b: the fill carries the mask
    assert not filled["open"][0] and not filled["too_big"][0]
    half = resolve((1, 1, 0, 0, 1, 0, 0, 0), hole=0.5e-3)  # IV-a: the plug carries the top tent only
    assert not half["open"][0] and half["too_big"][0]
    assert not resolve((1, 1, 0, 0, 0, 0, 0, 0), hole=0.5e-3, max_tent=0.6e-3)["too_big"][0]
