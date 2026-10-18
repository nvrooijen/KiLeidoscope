"""Board digest: content, ordering, names that resolve, size."""

import json
import math
import random
from dataclasses import replace
from pathlib import Path

import pytest

from kileido_bridge import digest, model
from kileido_bridge.board_index import BoardIndex
from kileido_bridge.kicad_reader import _convert_footprint
from kileido_bridge.targets import resolve
from test_board_index import MM, board, square
from test_phase1 import footprint as fake_footprint

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"


def fixture_snapshot():
    return model.snapshot_from_jsonable(json.loads(FIXTURE.read_text(encoding="utf-8")))


def parsed(snapshot, run="7", stamp="0123abcd"):
    """The digest as read back from its text, rows turned into dicts by its `columns`."""
    result = json.loads(digest.dumps(digest.build(snapshot, run, stamp)))
    columns = result["columns"]
    for key in ("nets", "vias", "tracks"):
        result[key] = [dict(zip(columns[key], row)) for row in result[key]]
    for part in result["footprints"]:
        part["pads"] = [dict(zip(columns["pads"], row)) for row in part["pads"]]
    return result


def named(snapshot):
    """board() with values, libraries and a DNP part."""
    values = {"U3": ("STM32G0", "Package_DFN_QFN:QFN-20"), "C12": ("100n", "Capacitor_SMD:C_0402_1005Metric")}
    footprints = tuple(replace(f, value=values.get(f.reference, ("10k", ""))[0],
                               lib_id=values.get(f.reference, ("", "Resistor_SMD:R_0402_1005Metric"))[1],
                               dnp=f.id == "f-r1b") for f in snapshot.footprints)
    return replace(snapshot, footprints=footprints)


# reading KiCad

def test_reader_takes_value_and_library_id():
    record, _ = _convert_footprint(fake_footprint())
    assert (record.value, record.lib_id) == ("SMA", "Connector_Coaxial:SMA_Edge")
    bare = fake_footprint()
    bare.definition.id.library = ""
    assert _convert_footprint(bare)[0].lib_id == "SMA_Edge"


def test_old_dumps_without_value_or_library_still_load():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert all("value" not in f and "lib_id" not in f for f in data["footprints"])
    footprints = model.snapshot_from_jsonable(data).footprints
    assert {(f.value, f.lib_id) for f in footprints} == {("", "")}
    round_trip = model.snapshot_from_jsonable(model.to_jsonable(named(board())))
    assert {f.reference: f.value for f in round_trip.footprints}["C12"] == "100n"


# content

def test_header_and_outline_box():
    result = parsed(board())
    assert [result[key] for key in ("digest", "run", "stamp", "board", "units")] == \
        [1, "7", "0123abcd", "board.kicad_pcb", "mm"]
    assert result["bbox"] == [-5, -5, 45, 45]
    assert "board.kicad_pcb" in digest.dumps(digest.build(board(), "1", "x"))


def test_footprints_carry_value_library_side_dnp_and_pads():
    parts = {f["ref"] + f.get("uuid", ""): f for f in parsed(named(board()))["footprints"]}
    c12 = parts["C12"]
    assert (c12["value"], c12["lib_id"], c12["side"], c12["pos"], c12["rot"], c12["dnp"]) == \
        ("100n", "Capacitor_SMD:C_0402_1005Metric", "top", [20, 10], 0, False)
    (pad,) = c12["pads"]
    assert pad == {"number": "1", "net": "SIG", "x": 20, "y": 10, "w": 1, "h": 1, "type": "smd", "layers": ["F.Cu"],
                   "drill": 0, "pin": "", "pin_type": ""}
    assert [p["number"] for p in parts["U3"]["pads"]] == ["4", "EP", "EP"]


def test_pads_carry_their_schematic_pin():
    """What a pin does comes from the board, never from memory of the part (U2 pin 13 is PA7, not VDD)."""
    pads = tuple(replace(p, pin_name="PA7_13", pin_type="bidirectional") if p.number == "4" else p
                 for p in board().pads)
    snapshot = replace(board(), pads=pads)
    (pin,) = [p for p in parsed(snapshot)["footprints"][-1]["pads"] if p["number"] == "4"]
    assert (pin["pin"], pin["pin_type"]) == ("PA7_13", "bidirectional")
    round_trip = model.snapshot_from_jsonable(model.to_jsonable(snapshot))
    assert {(p.pin_name, p.pin_type) for p in round_trip.pads if p.number == "4"} == {("PA7_13", "bidirectional")}


def test_reader_takes_the_schematic_pin():
    from types import SimpleNamespace

    from kipy.proto.board.board_types_pb2 import Pad
    from kipy.proto.common.types.base_types_pb2 import ElectricalPinType

    from kileido_bridge.kicad_reader import _symbol_pin
    proto = Pad()
    assert _symbol_pin(SimpleNamespace(_proto=proto)) == ("", "")
    proto.symbol_pin.name, proto.symbol_pin.type = "VDD_3", ElectricalPinType.EPT_POWER_INPUT
    assert _symbol_pin(SimpleNamespace(_proto=proto)) == ("VDD_3", "power_in")
    proto.symbol_pin.type, proto.symbol_pin.no_connect = ElectricalPinType.EPT_BIDIRECTIONAL, True
    assert _symbol_pin(SimpleNamespace(_proto=proto)) == ("VDD_3", "bidirectional+no_connect")


def test_the_pad_to_die_delay_is_read_in_ps():
    """KiCad sends a pad's pad-to-die delay as attoseconds; kipy 0.7 has no accessor."""
    from types import SimpleNamespace

    from kipy.proto.board.board_types_pb2 import Pad

    from kileido_bridge.kicad_reader import _die_delay

    proto = Pad()
    assert _die_delay(SimpleNamespace(_proto=proto)) == 0.0 and _die_delay(SimpleNamespace()) == 0.0
    proto.pad_to_die_delay.value_as = 2_500_000
    assert _die_delay(SimpleNamespace(_proto=proto)) == 2.5
    r1 = [f for f in parsed(named(board()))["footprints"] if f["ref"] == "R1"]
    assert [f["dnp"] for f in r1] == [False, True] and all("uuid" in f for f in r1)  # same reference twice


def test_through_hole_pad_from_the_drill():
    pad = parsed(fixture_snapshot())["footprints"][0]["pads"][0]  # J1: 8 x 4 mm, 2 x 1 mm slot, all copper
    assert (pad["type"], pad["layers"], pad["w"], pad["h"], pad["drill"]) == ("th", ["*.Cu"], 8, 4, [2, 1])
    hole = model.Pad("h", "f-u3", "", "", (0, 0), (MM, MM), {})
    snapshot = replace(board(), pads=board().pads + (hole,))
    (entry,) = [p for p in parsed(snapshot)["footprints"][-1]["pads"] if p["number"] == ""]
    assert (entry["type"], entry["layers"], entry["w"], entry["h"], entry["drill"]) == ("npth", [], 1, 1, 1)


def test_rotation_and_side_match_kicad():
    parts = {f["ref"]: f for f in parsed(fixture_snapshot())["footprints"]}
    assert (parts["J3"]["rot"], parts["J3"]["side"], parts["J4"]["rot"], parts["J4"]["side"]) == \
        (90, "top", 90, "bottom")
    assert (parts["J3"]["pads"][0]["x"], parts["J3"]["pads"][0]["y"]) == (29, 27)


def test_nets_count_pads_vias_and_length_per_layer():
    nets = {n["name"]: n for n in parsed(board())["nets"]}
    assert set(nets) == {"SIG", "GND", "/~{RST}"}  # names exactly as KiCad has them
    assert (nets["SIG"]["pads"], nets["SIG"]["vias"]) == (2, 1)
    assert nets["SIG"]["length"] == {"F.Cu": 10, "B.Cu": round(math.pi, 4)}  # 2 mm wide half circle
    assert (nets["GND"]["pads"], nets["GND"]["vias"], nets["GND"]["length"]) == (2, 1, {})


def test_arc_length_follows_the_mid_point():
    assert digest.arc_length((0, 0), (MM, MM), (2 * MM, 0)) == pytest.approx(math.pi * MM)
    assert digest.arc_length((0, 0), (MM, -MM), (2 * MM, 0)) == pytest.approx(math.pi * MM)
    three_quarters = digest.arc_length((MM, 0), (-MM, 0), (0, MM))  # the long way round
    assert three_quarters == pytest.approx(1.5 * math.pi * MM)
    assert digest.arc_length((0, 0), (MM, 0), (2 * MM, 0)) == 2 * MM  # collinear
    tracks = parsed(fixture_snapshot())["tracks"]
    assert tracks[0] == {"net": "RF_A", "layer": "F.Cu", "segments": 4, "arcs": 1,
                         "length": round(15 + 7 + 5 + 5 * math.pi, 4)}  # straight runs and a 5 mm radius half circle


def test_tracks_are_counts_and_lengths_only():
    tracks = parsed(board())["tracks"]
    assert tracks == [{"net": "/~{RST}", "layer": "F.Cu", "segments": 1, "arcs": 0, "length": 10},
                      {"net": "SIG", "layer": "F.Cu", "segments": 1, "arcs": 0, "length": 10},
                      {"net": "SIG", "layer": "B.Cu", "segments": 1, "arcs": 1, "length": round(math.pi, 4)}]


def test_zones_and_plane_cover():
    result = parsed(board())
    assert [(z["net"], z["layer"], z["bbox"], z["area"]) for z in result["zones"]] == \
        [("GND", "In1.Cu", [0, 0, 40, 40], 1500), ("GND", "In2.Cu", [0, 0, 40, 40], 1500)]
    cover = {layer["name"]: layer["zone_cover"] for layer in result["stackup"]}
    assert cover["In1.Cu"] == [{"net": "GND", "cover": 0.6, "area": 1500}]  # of a 50 x 50 mm board
    assert cover["F.Cu"] == [] and list(cover) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]  # no stackup: copper only


def test_fixture_stackup_in_order_with_thickness():
    stackup = parsed(fixture_snapshot())["stackup"]
    assert [layer["name"] for layer in stackup] == \
        ["F.Cu", "Dielectric 2", "In1.Cu", "Dielectric 4", "In2.Cu", "Dielectric 6", "B.Cu"]
    assert [layer["thickness"] for layer in stackup] == [0.035, 0.2, 0.035, 0.4, 0.035, 0.8, 0.035]
    assert stackup[2]["zone_cover"][0]["net"] == "GND" and 0.6 < stackup[2]["zone_cover"][0]["cover"] < 0.73


def test_outline_area_counts_a_separate_cutout_as_a_hole():
    outline = model.Outline((square(0, 0, 10 * MM, 10 * MM), square(2 * MM, 2 * MM, 4 * MM, 4 * MM)))
    assert digest.outline_area(outline) == pytest.approx(96 * MM * MM)


# names the LLM copies back

def test_short_uuids_resolve_to_the_same_item():
    snapshot = board()
    index = BoardIndex(snapshot)
    result = parsed(snapshot)
    for entry in (*result["vias"], *result["zones"]):
        ids = {item.id.lower() for item in resolve(index, f"uuid:{entry['uuid']}").items}
        assert len(ids) == 1 and ids.pop().endswith(entry["uuid"])
    # both vias end in 5a222dbd: the short form would name neither, so the full uuid is given
    assert sorted(v["uuid"] for v in result["vias"]) == ["00000000-0000-0000-0000-00005a222dbd",
                                                         "11111111-0000-0000-0000-00005a222dbd"]
    assert parsed(fixture_snapshot())["vias"][0]["uuid"] == "44444444"


def test_references_and_pads_resolve():
    snapshot = named(board())
    index = BoardIndex(snapshot)
    for part in parsed(snapshot)["footprints"]:
        assert resolve(index, part["ref"]).items
        for pad in part["pads"]:
            if pad["number"]:
                assert resolve(index, f"{part['ref']}.{pad['number']}").items
    for net in parsed(snapshot)["nets"]:
        assert resolve(index, f"net:{net['name']}").items


# ordering and text

def test_ordering_is_deterministic_and_natural():
    snapshot = board()
    shuffled = replace(snapshot, footprints=snapshot.footprints[::-1], pads=snapshot.pads[::-1],
                       vias=snapshot.vias[::-1], tracks=snapshot.tracks[::-1], zones=snapshot.zones[::-1])
    assert digest.dumps(digest.build(snapshot, "1", "s")) == digest.dumps(digest.build(shuffled, "1", "s"))
    parts = [model.Footprint(f"f{i}", ref, (0, 0), 0.0, "top", ()) for i, ref in enumerate(("C10", "C2", "R1", "C1"))]
    assert [f["ref"] for f in parsed(replace(snapshot, footprints=tuple(parts)))["footprints"]] == \
        ["C1", "C2", "C10", "R1"]


def test_text_is_valid_json_with_short_lines():
    text = digest.dumps(digest.build(big_board(100), "1", "s"))  # up to a 100-pin part
    assert json.loads(text)["counts"]["footprints"] == 100
    assert max(len(line) for line in text.splitlines()) < 400
    assert text.startswith('{\n "digest":1,\n "run":"1",')


def test_no_fill_polygons_and_four_decimals():
    text = digest.dumps(digest.build(replace(board(), vias=(
        model.Via("v", "SIG", (1_234_567, 7_654_321), 600_000, 300_000, "F.Cu", "B.Cu"),)), "1", "s"))
    assert '["v","SIG",1.2346,7.6543,"F.Cu","B.Cu",0.3,0.6]' in text
    assert "polygons" not in text


# size

def big_board(parts: int, seed: int = 1, grid: int = 25_000, via_every: int = 4) -> model.BoardSnapshot:
    """A synthetic board: mostly 2-pad passives, some SOT-23s, SOIC-8s, QFP-48s and a few
    100-pin parts (~4.5 pads per part), hierarchical net names, a via and a track on every
    `via_every`-th pad. Parts sit on a `grid` (nm); vias anywhere."""
    rng = random.Random(seed)
    layers = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    nets = ["GND", "+3V3", "+1V8", "+5V"] + [f"/Sheet{n // 50}/SIG_{n:04d}" for n in range(parts)]
    mix = ([(2, "Capacitor_SMD:C_0402_1005Metric", "100n")] * 70 + [(3, "Package_TO_SOT_SMD:SOT-23", "BSS138")] * 20 +
           [(8, "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "TLV9062")] * 7 +
           [(48, "Package_QFP:LQFP-48_7x7mm_P0.5mm", "STM32F0")] * 2 + [(100, "Package_QFP:LQFP-100_14x14mm_P0.5mm", "STM32F4")])
    footprints, pads, vias, tracks = [], [], [], []
    for i in range(parts):
        count, lib, value = mix[i % len(mix)]
        x, y = rng.randrange(200 * MM // grid) * grid, rng.randrange(150 * MM // grid) * grid
        side = "top" if i % 4 else "bottom"
        layer = "F.Cu" if side == "top" else "B.Cu"
        fid = f"{rng.getrandbits(128):032x}"
        ref = {2: "C", 3: "Q", 8: "U", 48: "U", 100: "U"}[count] + str(i + 1)
        footprints.append(model.Footprint(fid, ref, (x, y), math.radians(90 * (i % 4)), side, (), value=value, lib_id=lib))
        for n in range(count):
            px, py = x + n * 500_000, y
            net = rng.choice(nets[:4]) if n % 3 == 0 else nets[4 + rng.randrange(parts)]
            pads.append(model.Pad(f"{rng.getrandbits(128):032x}", fid, str(n + 1), net, (px, py), None,
                                  {layer: (square(px - 300_000, py - 125_000, px + 300_000, py + 125_000),),
                                   layer.replace("Cu", "Paste"): (square(px - 250_000, py - 100_000, px + 250_000, py + 100_000),)}))
            if n % via_every == 0:
                vx, vy = px + rng.randrange(-MM, MM), py + rng.randrange(-MM, MM)
                vias.append(model.Via(f"{rng.getrandbits(128):032x}", net, (vx, vy), 450_000, 200_000, "F.Cu", "B.Cu"))
                tracks.append(model.Track(f"{rng.getrandbits(128):032x}", rng.choice(layers), net,
                                          (px, py), (vx, vy), 150_000))
    stackup = model.Stackup(tuple(model.StackupLayer(name, "copper", 35_000, "copper", None, None) for name in layers))
    outline = model.Outline((square(0, 0, 200 * MM, 150 * MM),))
    return model.BoardSnapshot("big", {}, tuple(tracks), (), tuple(vias), tuple(pads), tuple(footprints), (),
                               outline, stackup, (), {})


def test_a_2000_part_board_stays_near_a_megabyte():
    snapshot = big_board(2000)
    size = len(digest.dumps(digest.build(snapshot, "1", "s")).encode("utf-8"))
    assert 4 < len(snapshot.pads) / 2000 < 5 and 1 < len(snapshot.vias) / 2000 < 1.6
    assert size < 1_250_000, size  # ~600 bytes per part: 4.5 pads (pin columns empty), 1.5 vias, ~1 net each
