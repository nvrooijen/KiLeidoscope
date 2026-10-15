"""The check catalogue of the LLM Visualizer: what a review prompt can ask for.

Our own ids (`group.name`), independent of any analysis tool. `instruction` and `evidence`
go into `request.md` word for word (`prompt.request_md`), so they are written for the
LLM: plain language, what to look for, what to point at. `draws` are the tools of
`findings.TOOLS` (marks, measured geometry, planes) that show the finding, best first; the
prompt offers only the ones Blender draws (`findings.DRAWN`). `later` is a note for us: a
tool not built yet that would show it better; it is never offered. A check with no `draws`
is board-wide: the finding is listed, not drawn. `values` are the value keys a finding of
the check should give (unit in the suffix, `findings.UNITS`); `request.md` lists them, since
analysis tools often state numbers only in their text. `v1` marks the quick checks, ticked
in Blender's prompt box until the user ticks others.

Kept as Python, not a data file: the add-on ships as one KiCad PCM zip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Check:
    id: str
    group: str
    title: str  # short, for the panel's tick list
    instruction: str  # for the LLM
    evidence: str  # what the finding should name or give
    draws: tuple[str, ...]  # tools of findings.TOOLS, best first
    v1: bool = False
    later: tuple[str, ...] = ()  # a note for us: tools not built yet that would draw it better; never read
    values: tuple[str, ...] = ()  # value keys a finding should give, unit in the suffix (findings.UNITS)
    rules: tuple[str, ...] = ()  # findings.RULES the bridge applies to its findings; the prompt says so


GROUPS = ("plane", "decap", "power", "dc", "hs", "dp", "emc", "analog", "thermal", "fab")
GROUP_TITLES = {
    "plane": "Return paths and planes",
    "decap": "Decoupling",
    "power": "Switching regulators",
    "dc": "DC and current",
    "hs": "High speed",
    "dp": "Differential pairs",
    "emc": "Connectors, ESD, EMC",
    "analog": "Analog and clocks",
    "thermal": "Thermal",
    "fab": "Fabrication and assembly",
}

CATALOGUE: tuple[Check, ...] = (
    # Return paths and planes
    Check("plane.gap_crossing", "plane", "Plane gap crossing",
          "Find signal tracks that cross a split, slot or void in their reference plane (the plane "
          "on the next layer). Draw plane_gap with the plane as net@layer and the track as crossing "
          "(net@layer~x,y at the crossing itself); KiLeidoscope finds where it has no plane under it. "
          "A track on an inner layer between two planes (a stripline) returns on both, mostly on the "
          "nearer one: name the nearer plane (the digest's stackup gives the distances); KiLeidoscope "
          "shows whether the other one is there. "
          "If you only know how much of the track has plane under it, not where it crosses, give "
          "coverage_pct (the share with plane under it) and length_mm (the whole track) in values and "
          "draw return_path for the net and plane. To suggest a "
          "route that stays over the plane, add a ghost track (path, layer, net); KiLeidoscope checks it fits.",
          "net, track as net@layer~x,y at the crossing, plane as net@layer, gap_mm, or coverage_pct and "
          "length_mm",
          ("plane_gap", "return_path", "highlight", "area", "label", "ghost"), v1=True,
          values=("gap_mm", "coverage_pct", "length_mm")),
    Check("plane.layer_change_no_return", "plane", "Layer change without return via",
          "Find signal vias that move a signal to a layer with a different reference plane and have no "
          "return or stitching via of the plane nets within a radius. Rule of thumb for the radius: "
          "twice the dielectric height to the plane, at least 1 mm (about 3 mm on a 1.5 mm two-layer "
          "core), or lambda/20 at the edge rate if you know it. Draw layer_change for the signal via "
          "with both planes and the radius you used as limit; KiLeidoscope finds the nearest return "
          "via and measures it. If you suggest where a return via should go, add a ghost via there "
          "with its net; KiLeidoscope checks it fits.",
          "signal via uuid, nearest return via uuid and distance_mm, both plane nets and layers",
          ("layer_change", "highlight", "distance", "ghost"), v1=True,
          values=("distance_mm",), rules=("signal_via",)),
    Check("plane.edge_run", "plane", "Track past plane edge",
          "Find signal tracks that run along or past the edge of their reference plane, with no plane "
          "copper under part of their length. Draw return_path for the net and its plane. "
          "A track on an inner layer between two planes (a stripline) returns on both, mostly on the "
          "nearer one: name the nearer plane (the digest's stackup gives the distances); KiLeidoscope "
          "shows whether the other one is there.",
          "net, track as net@layer~x,y, plane as net@layer, length_mm without plane",
          ("return_path", "highlight", "area", "label"),
          values=("length_mm",)),
    Check("plane.fragmented", "plane", "Fragmented plane",
          "Find ground or power planes cut into islands or narrow peninsulas by routing, vias or cutouts. "
          "Draw plane_islands for the net and layer.",
          "plane net and layer, zone uuids of the pieces",
          ("plane_islands", "highlight", "area", "label")),
    Check("plane.coverage", "plane", "Ground plane coverage",
          "Find boards with no ground plane, and ground pours with a low fill ratio: much of the pour "
          "cut away by routing, thermal relief or voids (rule of thumb: under about 60 % of its outline "
          "filled). Draw plane_islands for the net and layer, or mark the pour with an area on its "
          "layer (targets net@layer) and label its fill. With no ground plane at all, highlight the "
          "layer that should carry one (layer:B.Cu).",
          "plane net and layer as net@layer, fill share in fill_pct",
          ("plane_islands", "area", "highlight", "label"),
          values=("fill_pct",)),
    Check("plane.weak_tie", "plane", "Weak plane tie",
          "Find plane pieces joined to the rest of the plane only by a single via or a thin neck of copper. "
          "Draw plane_islands for the net and layer, and highlight the via or the neck.",
          "plane net and layer, the via uuid or the neck point, width_mm of a neck",
          ("plane_islands", "highlight", "label"), later=("neck",),
          values=("width_mm",)),
    Check("plane.stackup", "plane", "Stackup without reference",
          "Check the stackup for two signal layers next to each other with no plane between them, or a "
          "signal layer far from any plane. Skip 2-layer boards: F.Cu next to B.Cu is all they have, "
          "so do not report it there.",
          "the layer pair, dielectric thickness in distance_mm",
          ("layers", "highlight"),
          values=("distance_mm",)),
    Check("plane.plane_spacing", "plane", "Plane pair far apart",
          "Check power and ground plane pairs for a thick dielectric between them, which gives little "
          "interplane capacitance (rule of thumb: over about 0.2 mm). Board-wide: no drawing needed.",
          "the plane layer pair, dielectric thickness in distance_mm",
          (),
          values=("distance_mm",)),
    Check("plane.stitching_sparse", "plane", "Sparse ground stitching",
          "Find areas where ground stitching vias between plane layers are too far apart for the "
          "board's fastest edges. Draw stitching along the pour (net@layer) with the spacing you "
          "want and the largest you accept as max_mm; KiLeidoscope finds the vias along its outline "
          "and marks every stretch wider than that. Inside a pour, draw the gap between two "
          "neighbouring vias as a distance (limit_is max). Suggest new vias as ghost vias with "
          "their net, or a line of them as one ghost via_row (from, to, pitch, net); KiLeidoscope checks "
          "each fits.",
          "layer pair, via uuids, spacing in distance_mm or density in via_density_per_cm2",
          ("stitching", "distance", "ghost", "highlight"),
          values=("distance_mm", "via_density_per_cm2")),
    Check("plane.floating_copper", "plane", "Floating copper",
          "Find copper islands left unconnected by the pour; they can act as antennas.",
          "zone uuid, layer",
          ("highlight",)),
    Check("plane.edge_unstitched", "plane", "Pour edge not stitched",
          "Find pours that reach the board edge with no ground stitching vias along the rim. Draw "
          "stitching along edge with the ground net and the spacing you want; KiLeidoscope finds the "
          "vias along the edge and marks every stretch wider than that; suggest new vias along the rim "
          "as a ghost via_row (from, to, pitch, net), which KiLeidoscope checks via by via. edge_gap "
          "shows how close the pour comes to the edge.",
          "layer, the stretch of edge, via spacing in distance_mm",
          ("stitching", "highlight", "ghost", "edge_gap", "label"),
          values=("distance_mm",)),
    # Decoupling
    Check("decap.distance", "decap", "Decoupling cap distance",
          "Find decoupling caps far from the power pin they serve, or with a long loop to the plane. "
          "Draw a distance from the cap pad to the IC power pin with a limit (rule of thumb about 2 mm "
          "for small caps, limit_is max) and highlight the cap. If the cap could sit closer, suggest "
          "the move as a ghost part (part, at, rotation); KiLeidoscope checks it fits.",
          "cap reference, IC pad, distance_mm, loop length_mm or area_mm2 if measured",
          ("distance", "highlight", "ghost"), v1=True, later=("decap_loop",),
          values=("distance_mm", "length_mm", "area_mm2"), rules=("decap_pair",)),
    Check("decap.long_return", "decap", "Long cap return",
          "Find decoupling caps that reach the plane through a long track or a via shared with others.",
          "cap reference, via uuids, track length_mm",
          ("highlight", "distance", "label"), later=("decap_loop",),
          values=("length_mm",), rules=("decap_pair",)),
    Check("decap.missing", "decap", "Missing decoupling",
          "Find IC power pins with no decoupling cap nearby. Ghost a cap where one should be.",
          "IC pad",
          ("ghost", "highlight"), later=("decap_missing",)),
    Check("decap.via_first", "decap", "Via before the cap",
          "Find power pins whose track reaches the plane via before the cap, so the cap hangs off the "
          "side instead of sitting in the current path.",
          "IC pad, cap reference, via uuid",
          ("highlight", "arrow", "label"), later=("decap_loop",), rules=("decap_pair",)),
    Check("decap.shared_via", "decap", "Caps sharing a via",
          "Find several decoupling caps sharing one ground via.",
          "cap references, via uuid",
          ("highlight", "arrow"), later=("decap_link",), rules=("decap_pair",)),
    Check("decap.no_bulk", "decap", "No bulk capacitor",
          "Find regulator inputs or outputs with no bulk capacitor near them. Ghost a cap where one is missing.",
          "regulator reference and pad",
          ("ghost", "highlight")),
    Check("decap.pdn_impedance", "decap", "PDN impedance",
          "Estimate each power rail's impedance from its regulator and decoupling caps against a target "
          "impedance (allowed ripple over the transient current; state both) and report anti-resonance "
          "peaks above the target. Board-wide: no drawing needed.",
          "rail net, regulator reference, target and peak impedance with their frequencies in the "
          "message, transient current_a",
          (),
          values=("current_a",)),
    # Switching regulators
    Check("power.hot_loop", "power", "Large hot loop",
          "Find switching regulators whose input hot loop (input cap, switch, low side switch or diode, "
          "ground return) encloses a large area. Mark the loop with an area.",
          "references and pads in the loop, area_mm2",
          ("area", "highlight", "label"), later=("decap_loop",),
          values=("area_mm2",)),
    Check("power.switch_node", "power", "Switch node copper",
          "Find switch node copper that is too large (radiates) or too small (runs hot).",
          "switch node net, area_mm2",
          ("area", "highlight"),
          values=("area_mm2",)),
    Check("power.fb_near_noise", "power", "Feedback near noise",
          "Find feedback tracks routed under or beside the inductor or the switch node. Draw the gap "
          "as a clearance between the feedback track and the aggressor, with required if you have a rule.",
          "feedback net, aggressor net or reference, gap in distance_mm",
          ("clearance", "highlight"),
          values=("distance_mm",)),
    Check("power.under_inductor", "power", "Under the inductor",
          "Find sensitive tracks under a power inductor, or the plane cut away under it.",
          "inductor reference, nets and layers under it",
          ("isolate", "highlight", "layers")),
    Check("power.boot_cap_far", "power", "Bootstrap cap far",
          "Find bootstrap or IC supply caps placed far from their pins (distance, limit_is max). A "
          "better place for the cap is a ghost part (part, at).",
          "cap reference, IC pad, distance_mm",
          ("distance", "highlight", "ghost"),
          values=("distance_mm",), rules=("decap_pair",)),
    # DC and current
    Check("dc.trace_width", "dc", "Power track width",
          "Find power tracks narrower than their current needs (state the current you assume) or than "
          "their net class. Draw the track (or the net on its layer) with width and required in mm.",
          "track as net@layer~x,y, width_mm, required width_mm, current_a",
          ("width", "highlight", "label"), later=("neck",),
          values=("width_mm", "current_a")),
    Check("dc.bottleneck", "dc", "Pour bottleneck",
          "Find power pours that neck down between holes, vias or around a cutout.",
          "the neck as net@layer~x,y, width_mm",
          ("highlight", "label"), later=("neck",),
          values=("width_mm",)),
    Check("dc.via_current", "dc", "Too few vias for current",
          "Find layer changes that carry a high current through too few vias. Ghost the vias that are missing.",
          "via uuids, current_a, via count",
          ("highlight", "ghost", "label"), later=("via_count",),
          values=("current_a",)),
    Check("dc.voltage_drop", "dc", "Voltage drop",
          "Estimate the voltage drop from regulator to load on the power nets and report paths above "
          "budget (state the current and budget you assume).",
          "source pad, load pads, drop_mv, current_a",
          ("highlight", "arrow", "label"), later=("current_path", "voltage_drop"),
          values=("drop_mv", "current_a")),
    Check("dc.thermal_relief", "dc", "Relief on a power pad",
          "Find thermal relief spokes on pads that carry a high current.",
          "pad, zone as net@layer",
          ("highlight", "label"), later=("neck",)),
    Check("dc.sense_point", "dc", "Sense point at the source",
          "Find sense or feedback lines picked up at the regulator instead of at the load. Draw an "
          "arrow from where it is picked up to where it should be; routed_length shows the sense path.",
          "sense net, pads",
          ("arrow", "routed_length", "highlight")),
    # High speed
    Check("hs.clock_outer", "hs", "Clock on outer layer",
          "Find clock or fast-edge nets routed on an outer layer, or routed long. For a long route, "
          "draw routed_length from the driver pad to the load pad.",
          "net, layer, length_mm (driver pad to load pad)",
          ("highlight", "routed_length", "label"),
          values=("length_mm",)),
    Check("hs.stub", "hs", "Stubs on fast nets",
          "Find via stubs or track stubs on fast nets. Draw stub for a via (or a plated pad) with a "
          "stub; KiLeidoscope finds the layers its net uses and the unused barrel's length from the "
          "stackup. Highlight a track stub and label its length.",
          "via uuid or track, via stub length in stub_mm, track stub length_mm",
          ("stub", "highlight", "label"),
          values=("stub_mm", "length_mm")),
    Check("hs.impedance_step", "hs", "Impedance step",
          "Find width or reference plane changes along a controlled-impedance track, including necking "
          "under a BGA. Draw discontinuity for the net with the points where it changes; KiLeidoscope "
          "finds what changes at each (width, layer, reference plane). Draw the net with width and its "
          "nominal width as required to show every segment.",
          "net, the points where it changes, width_mm",
          ("discontinuity", "width", "highlight", "label"),
          values=("width_mm",)),
    Check("hs.crosstalk", "hs", "Crosstalk",
          "Find long parallel runs of fast nets closer than about 3 times their height above the plane. "
          "Draw the gap as a clearance between the two tracks, with required set to 3 times the height.",
          "both nets, gap in distance_mm, parallel length_mm",
          ("clearance", "highlight"),
          values=("distance_mm", "length_mm")),
    Check("hs.termination_far", "hs", "Termination far from driver",
          "Find series termination resistors placed far from their driver pin (distance, limit_is max). "
          "A better place for the resistor is a ghost part (part, at).",
          "resistor reference, driver pad, distance_mm",
          ("distance", "highlight", "ghost"),
          values=("distance_mm",), rules=("same_net",)),
    Check("hs.bus_match", "hs", "Bus length match",
          "Find bus members that are not length matched within tolerance (state the tolerance). Draw "
          "the lengths as bars (unit mm, limit on each: the longest allowed) next to the driver.",
          "nets, length_mm of each, tolerance",
          ("bars", "highlight", "label"),
          values=("length_mm",)),
    Check("hs.edge_trace", "hs", "Track near board edge",
          "Find fast or signal tracks close to the board edge. Draw edge_gap from the track, with "
          "min_mm the distance it should keep. distance_mm is from the track's copper edge (not its "
          "centre line) to the board edge.",
          "track as net@layer~x,y, distance_mm",
          ("edge_gap", "highlight"),
          values=("distance_mm",)),
    Check("hs.under_noisy_part", "hs", "Under a noisy part",
          "Find fast tracks routed under a crystal, inductor or connector shell.",
          "net, part reference",
          ("isolate", "highlight")),
    # Differential pairs
    Check("dp.skew", "dp", "Pair skew",
          "Find differential pairs whose two nets differ in length by more than the tolerance (state it). "
          "Draw the two lengths as bars (unit mm, or ps for skew).",
          "p and n nets, mismatch in length_mm, skew_ps if known",
          ("bars", "highlight", "label"), later=("skew",),
          values=("length_mm", "skew_ps"), rules=("diff_pair",)),
    Check("dp.uncoupled", "dp", "Uncoupled sections",
          "Find sections where a pair splits apart (around vias, pads or obstacles), often on one side only.",
          "p and n nets, where as net@layer~x,y",
          ("highlight", "label"), later=("pair", "pair_gap"), rules=("diff_pair",)),
    Check("dp.gap_change", "dp", "Pair gap change",
          "Find places where the spacing within a pair changes along the route.",
          "p and n nets, where, nominal and actual gap in distance_mm",
          ("highlight", "label", "distance"), later=("pair_gap",),
          values=("distance_mm",), rules=("diff_pair",)),
    Check("dp.via_asymmetry", "dp", "Pair via asymmetry",
          "Find pair vias placed asymmetrically, or without return vias next to them at a layer change.",
          "via uuids",
          ("highlight", "distance", "ghost"), later=("pair_vias",), rules=("diff_pair",)),
    Check("dp.ref_change", "dp", "Pair reference change",
          "Find pairs that cross a plane split or switch reference plane. Draw plane_gap with the "
          "plane and each net of the pair as crossing.",
          "pair nets, vias, both planes as net@layer",
          ("plane_gap", "highlight", "area", "label"), later=("pair_vias",), rules=("diff_pair",)),
    Check("dp.outer_layer", "dp", "Pair on outer layer",
          "Find fast differential pairs routed mostly on an outer layer when an inner (stripline) layer "
          "is available. Highlight the pair on its outer layer (net@layer) and label the share routed "
          "outside.",
          "p and n nets as net@layer, share of length on outer layers in outer_pct",
          ("highlight", "label"),
          values=("outer_pct",), rules=("diff_pair",)),
    # Connectors, ESD, EMC
    Check("emc.esd_far", "emc", "ESD part far",
          "Find ESD protection placed far from its connector, or after the track has fanned out. Draw "
          "routed_length along the signal net from the connector pin to the ESD pin (or a distance, "
          "limit_is max).",
          "connector reference, ESD reference, signal net, length_mm or distance_mm",
          ("routed_length", "distance", "highlight"),
          values=("length_mm", "distance_mm"), rules=("same_net",)),
    Check("emc.esd_ground", "emc", "ESD ground path",
          "Find ESD parts whose ground return is thin or long. Draw the ground tracks with width, or "
          "routed_length to the ground pin when the return is a track (it does not follow pours).",
          "ESD reference, ground path, width_mm, length_mm",
          ("width", "routed_length", "highlight", "label"),
          values=("width_mm", "length_mm")),
    Check("emc.io_unfiltered", "emc", "Unfiltered I/O",
          "Find signals leaving the board through a connector with no filtering.",
          "connector reference, net",
          ("highlight", "label")),
    Check("emc.io_ground", "emc", "Connector ground",
          "Find connectors with too few ground pins or a far ground return.",
          "connector reference",
          ("highlight", "label")),
    Check("emc.chassis_tie", "emc", "Chassis tie placement",
          "Find a chassis to signal ground tie in the wrong place. Draw an arrow from where it is to "
          "where it should be.",
          "tie point, references",
          ("arrow", "highlight", "label")),
    Check("emc.antenna_keepout", "emc", "Antenna keep-out",
          "Find copper under or near an antenna keep-out.",
          "antenna reference, zone or items",
          ("area", "isolate", "highlight")),
    Check("emc.shield_ground", "emc", "Shield not grounded",
          "Find cable shields not tied to ground at the connector.",
          "connector reference, shield pads",
          ("highlight", "label")),
    Check("emc.emission_estimate", "emc", "Emission estimate",
          "Estimate where the board is likely to emit: cavity resonances of the board's plane pair "
          "(from its size and dielectric) and the harmonic envelope of switching regulators and clocks "
          "against the emissions test band. Board-wide: no drawing needed.",
          "board size, the frequencies and the parts behind them, in the message",
          ()),
    # Analog and clocks
    Check("analog.crossing", "analog", "Digital in analog area",
          "Find digital tracks crossing the analog area, or analog and digital returns sharing a path. "
          "Mark the area; return_path shows a digital net over the analog plane.",
          "nets, the area, the plane as net@layer",
          ("return_path", "area", "highlight")),
    Check("analog.crystal", "analog", "Crystal placement",
          "Find crystals far from their IC, with long tracks, no ground ring or tracks beneath them.",
          "crystal reference, IC pads, distance_mm",
          ("distance", "isolate", "highlight"),
          values=("distance_mm",), rules=("same_net",)),
    Check("analog.sensitive_near_noise", "analog", "Sensitive node near noise",
          "Find sensitive nodes (ADC reference, op-amp inputs) routed beside a noisy track. Draw the "
          "gap as a clearance between the two, with required if you have a rule.",
          "both nets, gap in distance_mm",
          ("clearance", "highlight"),
          values=("distance_mm",)),
    # Thermal
    Check("thermal.pad_vias", "thermal", "Thermal pad vias",
          "Find exposed or thermal pads with too few vias, or vias so large the solder wicks away. "
          "Ghost the vias that are missing.",
          "pad, via count, drill in width_mm",
          ("highlight", "ghost", "label"), later=("thermal_vias",),
          values=("width_mm",)),
    Check("thermal.no_spread", "thermal", "No heat spreading",
          "Find power devices with no copper area to spread their heat.",
          "reference, pad, copper area_mm2",
          ("area", "highlight"),
          values=("area_mm2",)),
    Check("thermal.hot_neighbour", "thermal", "Hot neighbour",
          "Find hot parts placed against temperature-sensitive ones (distance, limit_is min).",
          "both references, distance_mm",
          ("distance", "highlight"),
          values=("distance_mm",)),
    # Fabrication and assembly
    Check("fab.clearance", "fab", "Copper near edge or hole",
          "Find copper too close to the board edge, a slot or a mounting hole, beyond what DRC checks. "
          "Draw edge_gap (min_mm) for the board edge, clearance (required) for a slot or hole.",
          "both items, gap in distance_mm",
          ("clearance", "edge_gap", "highlight"),
          values=("distance_mm",)),
    Check("fab.via_in_pad", "fab", "Via in pad",
          "Find vias in pads that are not plugged or filled.",
          "pad, via uuid",
          ("highlight",)),
    Check("fab.acid_trap", "fab", "Acid trap",
          "Find acute angles between a track and a pad.",
          "track as net@layer~x,y, pad",
          ("highlight", "label")),
    Check("fab.silk_on_pad", "fab", "Silkscreen on pad",
          "Find silkscreen printed over a pad.",
          "pad, silkscreen item uuid",
          ("highlight",)),
    Check("fab.copper_balance", "fab", "Copper balance",
          "Compare copper density between layer pairs and report large differences (warp risk). "
          "Board-wide: no drawing needed.",
          "copper share per layer as values such as f_cu_pct",
          (), later=("copper_balance",),
          values=("f_cu_pct", "b_cu_pct")),
    Check("fab.part_spacing", "fab", "Part spacing",
          "Find parts too close together for pick-and-place or rework. Draw a clearance between the "
          "two parts with required.",
          "both references, gap in distance_mm",
          ("clearance", "highlight"),
          values=("distance_mm",)),
    Check("fab.panel_edge", "fab", "Parts over panel edge",
          "Find parts over a V-score or mouse-bite line. Mark the line with an area; a clearance from "
          "the part to a point on the line (pt:x,y) gives the gap.",
          "reference, the stretch of edge",
          ("area", "clearance", "highlight")),
    Check("fab.fiducials", "fab", "Fiducials and test points",
          "Report missing fiducials or test points. Ghost one where it should go.",
          "where",
          ("ghost", "label")),
)


def by_id(ids: Iterable[str]) -> tuple[Check, ...]:
    """The checks with these ids, in catalogue order, once each; unknown ids dropped."""
    wanted = {ids} if isinstance(ids, str) else set(ids)
    return tuple(check for check in CATALOGUE if check.id in wanted)
