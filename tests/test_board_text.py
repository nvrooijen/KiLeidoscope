"""Copper from the board text, used while KiCad answers busy (route tool active)."""

from kileido_bridge import model
from kileido_bridge.board_text import copper_items, variant_dnp, via_protection, via_rings, with_variant_dnp

TEXT = """(kicad_pcb
\t(segment
\t\t(start -11.9235 10.8164)
\t\t(end -12.3485 11.2414)
\t\t(width 0.16)
\t\t(layer "F.Cu")
\t\t(net "LDO_1V8")
\t\t(uuid "seg-1")
\t)
\t(arc
\t\t(start -8.19459 0.4514)
\t\t(mid -8.057181 0.508316)
\t\t(end -8.000265 0.645725)
\t\t(width 0.11735)
\t\t(layer "B.Cu")
\t\t(net "/MIPI SENSOR/CSI_D0_P")
\t\t(uuid "arc-1")
\t)
\t(via
\t\t(at -11.3485 9.4414)
\t\t(size 0.45)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "In2.Cu")
\t\t(net "")
\t\t(uuid "via-1")
\t)
\t(via
\t\t(at 4 0)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(tenting
\t\t\t(front no)
\t\t\t(back no)
\t\t)
\t\t(capping yes)
\t\t(covering
\t\t\t(front no)
\t\t\t(back none)
\t\t)
\t\t(plugging
\t\t\t(front yes)
\t\t\t(back no)
\t\t)
\t\t(filling yes)
\t\t(net "")
\t\t(uuid "via-2")
\t)
\t(footprint "R_0402"
\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
\t)
)
"""


def test_copper_items_match_the_ipc_records():
    tracks, arcs, vias = copper_items(TEXT)
    assert tracks == (model.Track("seg-1", "F.Cu", "LDO_1V8", (-11_923_500, 10_816_400),
                                  (-12_348_500, 11_241_400), 160_000),)
    assert arcs == (model.Arc("arc-1", "B.Cu", "/MIPI SENSOR/CSI_D0_P", (-8_194_590, 451_400),
                              (-8_057_181, 508_316), (-8_000_265, 645_725), 117_350),)
    assert vias == (model.Via("via-1", "", (-11_348_500, 9_441_400), 450_000, 300_000, "F.Cu", "In2.Cu"),
                    model.Via("via-2", "", (4_000_000, 0), 600_000, 300_000, "F.Cu", "B.Cu",
                              (0, 0, 0, -1, 1, 0, 1, 1)))  # as KiCad 10 writes a Type VII via; "none": the rules


def test_a_via_padstack_keeps_the_front_size():
    text = ('(kicad_pcb\n\t(via\n\t\t(at 0 0)\n\t\t(size 0.6)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n'
            '\t\t(padstack\n\t\t\t(mode front_inner_back)\n\t\t\t(layer "Inner"\n\t\t\t\t(size 0.4)\n\t\t\t)\n\t\t)\n'
            '\t\t(uuid "via-3")\n\t)\n)')
    assert copper_items(text)[2][0].diameter == 600_000  # as IPC's diameter: the front's


def test_kicad_9_names_the_tented_sides():
    assert via_protection("(tenting front)") == (1, 0, -1, -1, -1, -1, -1, -1)
    assert via_protection("(tenting none)", (1,) * 8) == (0, 0, 1, 1, 1, 1, 1, 1)


def test_annular_rings_as_kicad_writes_them():
    assert via_rings("(layers \"F.Cu\" \"B.Cu\")") == model.RINGS_ALL  # KiCad's default writes nothing
    assert via_rings("(remove_unused_layers yes)") == model.RINGS_CONNECTED
    assert via_rings("(remove_unused_layers yes) (keep_end_layers yes)") == model.RINGS_ENDS_AND_CONNECTED


VARIANT_TEXT = """(kicad_pcb
\t(variants
\t\t(variant
\t\t\t(name "5V Output")
\t\t)
\t\t(variant
\t\t\t(name "Lite")
\t\t)
\t)
\t(footprint "Resistor_SMD:R_0402_1005Metric"
\t\t(layer "F.Cu")
\t\t(uuid "fp-1")
\t\t(at 10 20)
\t\t(property "Reference" "R1"
\t\t\t(at 0 0 0)
\t\t\t(uuid "text-1")
\t\t)
\t\t(property "Note" "(variant (name \\"Lite\\") (dnp yes))"
\t\t\t(uuid "text-2")
\t\t)
\t\t(attr smd)
\t\t(variant
\t\t\t(name "5V Output")
\t\t\t(dnp yes)
\t\t)
\t\t(variant
\t\t\t(name "Li \\"te\\"")
\t\t\t(dnp no)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(uuid "pad-1")
\t\t)
\t)
\t(footprint "Resistor_SMD:R_0402_1005Metric"
\t\t(layer "B.Cu")
\t\t(uuid "fp-2")
\t\t(attr smd dnp)
\t\t(variant
\t\t\t(name "5V Output")
\t\t\t(dnp no)
\t\t)
\t)
\t(footprint "Fiducial:Fiducial_1mm"
\t\t(layer "F.Cu")
\t\t(uuid "fp-3")
\t)
)
"""


def test_per_variant_dnp_is_read_per_footprint():
    """KiCad 10 writes a footprint's per-variant DNP as `(variant (name "X") (dnp yes))`
    blocks inside the footprint; the first uuid in the block is the footprint's own."""
    found = variant_dnp(VARIANT_TEXT)
    assert found == {"5V Output": {"fp-1": True, "fp-2": False}, 'Li "te"': {"fp-1": False}}
    assert variant_dnp("(kicad_pcb\n\t(variants\n\t\t(variant\n\t\t\t(name \"A\")\n\t\t)\n\t)\n)\n") == {}
    assert variant_dnp("(kicad_pcb (footprint \"R\" (uuid \"u\")))") == {}  # nothing to parse: one search
    compact = ('(kicad_pcb\n\t(footprint "R"\n\t\t(uuid "fp-1")\n'
               '\t\t(variant (name "A") (dnp yes))\n\t\t(variant (name "B") (dnp no))\n\t)\n'
               '\t(variants\n\t\t(variant\n\t\t\t(name "A")\n\t\t\t(description "x")\n\t\t)\n\t)\n)\n')
    assert variant_dnp(compact) == {"A": {"fp-1": True}, "B": {"fp-1": False}}


def test_variant_dnp_overrides_the_footprint_flag():
    """The selected variant's DNP replaces KiCad's default flag per footprint; a
    footprint the variant does not mention keeps it. The default variant ("") changes nothing."""
    fitted = model.Footprint("fp-1", "R1", (0, 0), 0.0, "top", ())
    unfitted = model.Footprint("fp-2", "R2", (0, 0), 0.0, "bottom", (), dnp=True)
    bare = model.Footprint("fp-3", "FID1", (0, 0), 0.0, "top", ())
    footprints = (fitted, unfitted, bare)
    overrides = variant_dnp(VARIANT_TEXT)
    assert [f.dnp for f in with_variant_dnp(footprints, "5V Output", overrides)] == [True, False, False]
    assert [f.dnp for f in with_variant_dnp(footprints, 'Li "te"', overrides)] == [False, True, False]
    assert with_variant_dnp(footprints, "", overrides) == footprints
    assert with_variant_dnp(footprints, "Unknown", overrides) == footprints
    assert with_variant_dnp(footprints, "5V Output", {}) == footprints
