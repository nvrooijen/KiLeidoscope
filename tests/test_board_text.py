"""Copper from the board text, used while KiCad answers busy (route tool active)."""

from kileido_bridge import model
from kileido_bridge.board_text import copper_items, via_protection, via_rings

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
