"""DC resistance and IR drop of one power net: the setup, and the board as the solver's input.

The solver is Fill Resistance by Janik Oltmanns / B4L
(https://git.b4l.co.th/B4L/kicad-zone-resistance, GPL-3.0-or-later), vendored in
`dcsolve`: a steady-current (DC) finite-difference solve of the net's copper, a
sheet per copper layer joined by via and plated-hole barrels. Supplies are ideal
voltage sources, loads draw a set current (their PDN mode).

The setup is one power net with its supplies and loads, each a list of parts: pads
and vias clicked in Blender. It is saved beside the board as
`<board stem>.kileidoscope-dc.json` in Fill Resistance's own config format
(fill_res_config.json, PDN mode, schema version 1), so their plugin reads it when the
file is copied to `<board stem>.fill_res_config.json`. A board with no such file
starts from their config if one lies beside it. It is the only file KiLeidoscope
writes into a project folder, and only when the user changes the setup.

Parts use their grammar: "U7.3" (pad 3 of U7), "U7" (every pad of U7 on the net),
{"via_mm": [x, y]} (the net's via nearest to x, y within 1 mm).

`build_problem` turns a snapshot and a setup into the solver's `Problem`, like
Fill Resistance's board_io.build_problem does from KiCad: the net's zone fills,
tracks, arcs, pads and copper graphics per layer, its vias and plated pads as
barrels, the parts as terminal contacts. Heights come from the stackup; the via
plating thickness, which KiCad does not store, is a setting.
"""

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import model, protocol
from .dcsolve import config as solver_config
from .dcsolve.errors import UserFacingError
from .dcsolve.geometry import (Electrode, LayerFill, Polygon, Problem, Rect, Terminal, TrackSeg, ViaLink,
                               contact_solder_buildups, tht_joint_buildups)
from .dcsolve.skin import parse_engineering
from .dcworker import MAX_DISPLAY_CELLS

RHO_CU_OHM_M = solver_config.RHO_CU_OHM_M  # copper at 20 degC
PLATING_UM = solver_config.VIA_PLATING_UM  # via barrel plating: KiCad does not store it
FALLBACK_COPPER_NM = int(solver_config.FALLBACK_THICKNESS_UM * 1000)
VIA_SEARCH_NM = 1_000_000  # {"via_mm": ...} finds the net's via this close
SUFFIX = ".kileidoscope-dc.json"
THEIR_NAMES = ("{stem}.fill_res_config.json", "fill_res_config.default.json", "fill_res_config.json")
DEFAULT_LOAD_A = 1.0
DEFAULT_SUPPLY_V = 3.3


class SetupError(UserFacingError):
    """The setup does not fit the board (a part that is not on the net, ...)."""


# --- The setup -------------------------------------------------------------------------

@dataclass
class DcTerminal:
    name: str
    role: str  # "supply" (ideal source of `value` volts) or "load" (draws `value` amperes)
    parts: list = field(default_factory=list)  # part references (module docstring)
    value: float = 0.0
    bonded: bool = False  # all parts one lug (a package's internal metal)
    guessed: bool = False  # auto_terminals made it and its value is still the placeholder

    def key(self):
        return (self.name, self.role, tuple(json.dumps(p, sort_keys=True) for p in self.parts), self.value,
                self.bonded)


@dataclass
class DcSetup:
    net: str = ""  # the net shown in the panel's table (and the only one solved in scope "net")
    terminals: list = field(default_factory=list)
    plating_um: float = PLATING_UM
    cell_um: float | None = None  # solver grid; None: Fill Resistance's automatic size
    vias_capped: bool = False  # filled and capped vias: a thin copper cap over their outer mouths
    scope: str = "net"  # "all": every power net (all_nets), each with its own terminals
    saved: dict = field(default_factory=dict)  # net -> terminals, for the nets not in the table now

    def key(self):
        return (self.net, tuple(t.key() for t in self.terminals), self.plating_um, self.cell_um,
                self.vias_capped)

    def for_net(self, net: str, terminals: list) -> "DcSetup":
        """This setup's settings with one net and its terminals (what one solve takes)."""
        return DcSetup(net, terminals, self.plating_um, self.cell_um, self.vias_capped)

    def switch(self, net: str) -> None:
        """Show `net` in the table; the old net's terminals are kept for later."""
        if net == self.net:
            return
        if self.net and self.terminals:
            self.saved[self.net] = self.terminals
        self.net, self.terminals = net, self.saved.pop(net, [])

    def ready(self) -> str:
        """"" when there is something to solve, else what is missing."""
        if not self.net:
            return "Choose the power net"
        active = [t for t in self.terminals if t.parts]
        if not any(t.role == "supply" for t in active):
            return "Mark a supply: a pad or via where the voltage enters"
        if not any(t.role == "load" for t in active):
            return "Mark a load: a pad or via that draws current"
        return ""


def setup_path(board_path: str) -> Path | None:
    return Path(board_path).with_name(Path(board_path).stem + SUFFIX) if board_path else None


def load_setup(board_path: str) -> tuple[DcSetup, str]:
    """(setup, where it came from): ours beside the board, else Fill Resistance's."""
    if not board_path:
        return DcSetup(), ""
    board = Path(board_path)
    names = [board.stem + SUFFIX] + [name.format(stem=board.stem) for name in THEIR_NAMES]
    for name in names:
        path = board.with_name(name)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            setup = setup_from_config(_json_without_comments(text))
        except (ValueError, KeyError, TypeError) as exc:
            raise SetupError(f"{path.name}: {exc}") from exc
        if setup.terminals or name.endswith(SUFFIX):
            return setup, str(path)
    return DcSetup(), ""


def save_setup(setup: DcSetup, board_path: str) -> str:
    """Write the setup beside the board; "" when the board has no file yet."""
    path = setup_path(board_path)
    if path is None:
        return ""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(config_from_setup(setup), indent=2) + "\n", encoding="utf-8")
    partial.replace(path)
    return str(path)


def _json_without_comments(text: str) -> dict:
    """Their files may carry whole-line // comments."""
    return json.loads("\n".join("" if line.lstrip().startswith("//") else line for line in text.splitlines()))


def _number(value) -> float:
    return parse_engineering(value) if isinstance(value, str) else float(value)


def setup_from_config(data: dict) -> DcSetup:
    """A Fill Resistance config (schema 1): its PDN terminals whose parts are pads or vias."""
    if int(data.get("version", 0)) != 1:
        raise ValueError(f"unsupported config version {data.get('version')!r}")
    run, physics = data.get("run", {}) or {}, data.get("physics", {}) or {}
    setup = DcSetup(net=str(run.get("net") or ""), vias_capped=bool(run.get("vias_capped", False)))
    if run.get("cell_um") is not None:
        setup.cell_um = _number(run["cell_um"])
    if physics.get("via_plating_um") is not None:
        setup.plating_um = _number(physics["via_plating_um"])
    setup.terminals = [_terminal_from_config(entry) for entry in data.get("terminals", ())]
    ours = data.get("_kileidoscope", {}) or {}  # their loader skips "_" keys
    setup.scope = "all" if ours.get("scope") == "all" else "net"
    setup.saved = {str(net): [_terminal_from_config(entry) for entry in entries]
                   for net, entries in (ours.get("nets", {}) or {}).items() if net != setup.net}
    return setup


def _terminal_from_config(entry: dict) -> DcTerminal:
    role = entry["role"]
    if role not in ("supply", "load"):
        raise ValueError(f"terminal {entry.get('name')!r}: unknown role {role!r}")
    parts = [part for part in entry.get("parts", ()) if _part_kind(part)]
    value = entry.get("v_oc" if role == "supply" else "i_draw_a")
    default = DEFAULT_SUPPLY_V if role == "supply" else DEFAULT_LOAD_A
    return DcTerminal(str(entry["name"]), role, parts, default if value is None else _number(value),
                      bool(entry.get("bonded")), bool(entry.get("_guessed")))


def _terminal_config(terminal: DcTerminal) -> dict:
    entry = {"name": terminal.name, "role": terminal.role, "parts": list(terminal.parts),
             "active": bool(terminal.parts)}  # their loader needs parts on active terminals
    if terminal.role == "supply":
        entry.update(r_out_ohm=0.0, v_oc=terminal.value)
    else:
        entry["i_draw_a"] = terminal.value
    if terminal.bonded:
        entry["bonded"] = True
    if terminal.guessed:
        entry["_guessed"] = True
    return entry


def config_from_setup(setup: DcSetup) -> dict:
    """The net in the table as their config; the other nets and the scope in our section."""
    config = {"_comment": "KiLeidoscope's DC analysis setup, in Fill Resistance's fill_res_config format "
                          "(https://git.b4l.co.th/B4L/kicad-zone-resistance)",
              "version": 1, "mode": "pdn",
              "run": {"net": setup.net, "vias_capped": setup.vias_capped, "cell_um": setup.cell_um},
              "physics": {"via_plating_um": setup.plating_um},
              "terminals": [_terminal_config(terminal) for terminal in setup.terminals]}
    if setup.scope == "all" or setup.saved:
        config["_kileidoscope"] = {
            "_comment": "The other nets' supplies and loads; scope \"all\" solves every power net",
            "scope": setup.scope,
            "nets": {net: [_terminal_config(terminal) for terminal in terminals]
                     for net, terminals in sorted(setup.saved.items()) if terminals}}
    return config


# --- Parts -----------------------------------------------------------------------------

def _part_kind(part) -> str:
    """"pad", "footprint", "via" or "" (a part this viewer does not mark, e.g. a rectangle)."""
    if isinstance(part, dict):
        return "via" if "via_mm" in part else ""
    if not isinstance(part, str) or not part or part.startswith("rect:"):
        return ""
    return "pad" if "." in part else "footprint"


def part_for(snapshot: model.BoardSnapshot, item_id: str, whole_component: bool = False):
    """The part reference of a clicked pad or via id, and the net it is on; None for other items."""
    for via in snapshot.vias:
        if via.id == item_id:
            return {"via_mm": [via.pos[0] / 1e6, via.pos[1] / 1e6]}, via.net
    references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
    for pad in snapshot.pads:
        if pad.id == item_id:
            reference = references.get(pad.footprint_id, "")
            if not reference:
                return None
            return (reference if whole_component else f"{reference}.{pad.number}"), pad.net
    return None


def net_of(snapshot: model.BoardSnapshot, item_id: str) -> str:
    """The net of a clicked track, arc, via, zone or pad ("" if none)."""
    for item in (*snapshot.tracks, *snapshot.arcs, *snapshot.vias, *snapshot.zones, *snapshot.pads,
                 *snapshot.graphics):
        if item.id == item_id:
            return item.net
    return ""


def resolve_part(snapshot: model.BoardSnapshot, part, net: str) -> list:
    """The pads or via a part reference names on `net` ([] when none)."""
    kind = _part_kind(part)
    if kind == "via":
        x, y = (round(float(value) * 1e6) for value in part["via_mm"])
        vias = [via for via in snapshot.vias if via.net == net and math.dist(via.pos, (x, y)) <= VIA_SEARCH_NM]
        return [min(vias, key=lambda via: math.dist(via.pos, (x, y)))] if vias else []
    if not kind:
        return []
    reference, _, number = part.partition(".")
    owners = {footprint.id for footprint in snapshot.footprints if footprint.reference == reference}
    return [pad for pad in snapshot.pads if pad.footprint_id in owners and pad.net == net
            and (kind == "footprint" or pad.number == number)]


def describe_part(part) -> str:
    if _part_kind(part) == "via":
        x, y = part["via_mm"]
        return f"via ({float(x):.2f}, {float(y):.2f})"
    return str(part)


AUTO_LOAD_A = 0.1  # what auto-detected loads draw until the user types their currents
REGULATOR_PADS = 16  # an IC this small with another power net on it: a regulator (13 with its pad and vias)
_VOLTS = re.compile(r"\+?\d+(\.\d+)?V\d*|V\d+(V\d*)?")  # 3V3, +5V, 1.8V, V12
_RAILS = ("VCC", "VDD", "VBAT", "VBUS")  # anywhere in a word: AVDD, VDDIO, U3RXVDDQ
_RAIL_STARTS = ("VIN", "VOUT", "VSYS", "VCORE", "VREG", "VPP", "VEE")
_GROUNDS = {"GND", "AGND", "DGND", "PGND", "SGND", "GNDA", "GNDD", "VSS", "VSSA", "0V"}
_SIGNALS = {"GOOD", "PG", "PGOOD", "EN", "ENABLE", "FB", "SENSE", "SNS", "SW", "NR", "SS", "RESET", "CLK"}


def is_power(net: str) -> bool:
    """A supply rail's net by its name: a voltage (3V3, +5V), VCC, VDD, VBUS, ...
    word; not ground, not a signal about power (PWR_GOOD_1V2, EN_3V3), not one of
    KiCad's unnamed nets."""
    if not net or net.startswith("Net-("):
        return False
    name = net.rsplit("/", 1)[-1]  # a hierarchical sheet path may say GND
    words = [word for word in re.split(r"[^A-Z0-9.+]+", name.upper()) if word]
    if not words or any(word in _GROUNDS or word in _SIGNALS for word in words):
        return False
    return name.startswith("+") or any(
        _VOLTS.fullmatch(word) or any(rail in word for rail in _RAILS) or word.startswith(_RAIL_STARTS)
        for word in words)


def _prefix(reference: str) -> str:
    return re.match(r"[A-Za-z]*", reference)[0].upper()


def auto_terminals(snapshot: model.BoardSnapshot, net: str) -> tuple[list, str]:
    """Supplies and loads guessed from the components on `net`, and what was done.

    The supply, best first: a switcher's output inductor (one pad on the net, the
    other on a switch node), a small IC that also touches another power net (a
    regulator), a ferrite, inductor or resistor from another power net, a
    connector. The loads: every other IC on the net (all its pads on the net one
    conductor) and every other ferrite, inductor or resistor to another power net
    (it feeds that rail), each drawing AUTO_LOAD_A until the user types its
    current. Capacitors and the rest are left out: at DC a capacitor carries no
    current, and pull-ups and dividers draw next to nothing."""
    references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
    pads_of: dict = {}
    for pad in snapshot.pads:
        pads_of.setdefault(pad.footprint_id, []).append(pad)
    found = []  # (score, reference, pads on the net, regulator-like, feeds another rail)
    for footprint_id, pads in pads_of.items():
        reference = references.get(footprint_id, "")
        mine = [pad for pad in pads if pad.net == net]
        if not reference or not mine:
            continue
        prefix = _prefix(reference)
        other_power = any(is_power(pad.net) for pad in pads if pad.net and pad.net != net)
        regulator = prefix in ("U", "IC", "VR", "PS") and len(pads) <= REGULATOR_PADS and other_power
        series = prefix in ("L", "FB", "R") and len(pads) == 2 and len(mine) == 1 and other_power
        if prefix == "L" and len(pads) == 2 and len(mine) == 1 and not other_power:
            score = 10
        elif regulator:
            score = 8
        elif series:
            score = 5 if prefix != "R" else 4
        elif prefix in ("J", "P", "CN", "CON", "X"):
            score = 3
        else:
            score = 0
        found.append((score, reference, mine, regulator, series))
    supplies = [entry for entry in found if entry[0] > 0]
    if not supplies:
        raise SetupError(f"No inductor, regulator or connector on {net} to feed it: mark the supply by hand")
    _, source, mine, _, _ = max(supplies, key=lambda entry: (entry[0], -len(entry[2])))
    terminals = [DcTerminal(source, "supply", [source if len(mine) > 1 else f"{source}.{mine[0].number}"],
                            guess_voltage(net), guessed=True)]
    skipped = []
    for _, reference, mine, regulator, series in sorted(found, key=lambda entry: entry[1]):
        if reference == source or not (series or _prefix(reference) in ("U", "IC")):
            continue
        if regulator:
            skipped.append(reference)  # a regulator's feedback or sense pin, not a load
            continue
        terminals.append(DcTerminal(reference, "load", [reference], AUTO_LOAD_A, bonded=len(mine) > 1, guessed=True))
    loads = len(terminals) - 1
    message = (f"Supply {source}; {loads} load{'s' * (loads != 1)} at {AUTO_LOAD_A:g} A"
               f"{' each' * (loads > 1)} (ICs and parts feeding other rails): type the real currents. "
               "Capacitors draw no DC current")
    if skipped:
        message += f"; left out {', '.join(skipped)} (regulator)"
    return terminals, message


def guess_voltage(net: str) -> float:
    """A supply's first value from its net name: +3V3 -> 3.3, 1V8 -> 1.8, +5V -> 5, VCC_12V -> 12."""
    match = re.search(r"(\d+)V(\d+)", net, re.IGNORECASE) or re.search(r"(\d+(?:\.\d+)?)V", net, re.IGNORECASE)
    if match is None:
        return DEFAULT_SUPPLY_V
    value = float(f"{match[1]}.{match[2]}") if match.re.groups == 2 else float(match[1])
    return value if 0 < value < 1000 else DEFAULT_SUPPLY_V


# --- The solver's input ----------------------------------------------------------------

@dataclass(frozen=True)
class Stack:
    """Copper layers top first, their thickness and the depth of their centre (nm)."""
    names: tuple[str, ...]
    thickness_nm: dict
    z_nm: dict
    bottom_nm: int
    warnings: tuple[str, ...] = ()


def copper_stack(snapshot: model.BoardSnapshot) -> Stack:
    """Depths from the stackup, walking down from the top like Fill Resistance's
    get_stackup_info; missing thicknesses fall back to the viewer's even spacing
    (reference.copper_order) and 35 um copper, with a warning."""
    from .reference import copper_order
    names, gaps, exact = copper_order(snapshot)
    by_name = {entry.name: entry for entry in snapshot.stackup.layers}
    warnings = [] if exact else ["DC: the stackup has no complete thicknesses; layers evenly spaced over 1.6 mm"]
    thickness = {}
    for name in names:
        entry = by_name.get(name)
        thickness[name] = entry.thickness_nm if entry is not None and entry.thickness_nm else FALLBACK_COPPER_NM
        if entry is None or not entry.thickness_nm:
            warnings.append(f"DC: no copper thickness for {name}; assuming {FALLBACK_COPPER_NM / 1000:g} um")
    z, depth = {}, 0
    for index, name in enumerate(names):
        z[name] = depth + thickness[name] // 2
        depth += thickness[name] + (gaps[index] if index < len(gaps) else 0)
    return Stack(names, thickness, z, depth, tuple(warnings))


def _polygon(polygon: model.Polygon) -> Polygon:
    return Polygon(outline=np.asarray(polygon[0], np.int64),
                   holes=[np.asarray(ring, np.int64) for ring in polygon[1:]])


def _points(polygons) -> np.ndarray:
    return np.array([p for polygon in polygons for p in polygon[0]], np.float64).reshape(-1, 2)


def _pad_sizes(pad: model.Pad, polygons) -> tuple[int, int]:
    """(largest, smallest) pad dimension around its centre: the circumscribed and
    inscribed diameters of its copper (their padstack size, for rotated shapes too)."""
    points = _points(polygons)
    if not len(points):
        return 0, 0
    centre = np.asarray(pad.pos, np.float64)
    outer = 2 * float(np.hypot(*(points - centre).T).max())
    inner = outer
    for polygon in polygons:
        ring = np.asarray(polygon[0], np.float64)
        a, b = ring, np.roll(ring, -1, axis=0)
        span = b - a
        t = np.clip(((centre - a) * span).sum(axis=1) / np.maximum((span * span).sum(axis=1), 1e-9), 0, 1)
        inner = min(inner, 2 * float(np.hypot(*(a + t[:, None] * span - centre).T).min()))
    return round(outer), round(inner)


def _drill(pad: model.Pad) -> tuple[int, int, int]:
    """(width, slot_dx, slot_dy) like Fill Resistance's _drill_info: an oval hole's
    narrow width and the offset from its centre to each end-cap centre."""
    dx, dy = pad.drill or (0, 0)
    if dx <= 0 or dy <= 0 or dx == dy or pad.drill_shape != "oval":
        return max(min(dx, dy), 0), 0, 0
    half = (max(dx, dy) - min(dx, dy)) / 2
    ux, uy = (1.0, 0.0) if dx > dy else (0.0, 1.0)
    angle = pad.drill_angle_rad
    return (min(dx, dy), round(half * (ux * math.cos(angle) + uy * math.sin(angle))),
            round(half * (uy * math.cos(angle) - ux * math.sin(angle))))


def _plated(pad: model.Pad) -> bool:
    return bool(pad.drill and min(pad.drill) > 0 and any(layer not in model.PASTE_LAYERS for layer in pad.polygons))


def _solder_side(pad: model.Pad, sides: dict) -> str:
    """Where a through-hole lead sticks out: opposite its component (B.Cu when unknown)."""
    return "F.Cu" if sides.get(pad.footprint_id) == "bottom" else "B.Cu"


def _pad_shape(pad: model.Pad, layers) -> tuple[str, tuple]:
    """(layer, polygons) of the first of `layers` the pad has copper on."""
    for layer in layers:
        if pad.polygons.get(layer):
            return layer, pad.polygons[layer]
    return "", ()


def _rect(points: np.ndarray, label: str) -> Rect:
    return Rect.normalized(int(points[:, 0].min()), int(points[:, 1].min()), int(points[:, 0].max()),
                           int(points[:, 1].max()), label)


def _pad_electrode(pad: model.Pad, reference: str, sides: dict) -> Electrode:
    """A pad as a contact, like Fill Resistance's _to_electrode: an SMD pad on its
    layer; a through-hole pad at its barrel, as a soldered joint."""
    copper = [layer for layer in pad.polygons if layer not in model.PASTE_LAYERS]
    drill, slot_dx, slot_dy = _drill(pad) if _plated(pad) else (0, 0, 0)
    contact = copper[0] if len(copper) == 1 and not drill else "all"
    side = _solder_side(pad, sides) if drill else None
    order = [contact] if contact != "all" else [side, "F.Cu", "B.Cu", *copper]
    _, polygons = _pad_shape(pad, order)
    if not polygons:
        raise SetupError(f"Pad {reference}.{pad.number} has no copper shape")
    largest, smallest = _pad_sizes(pad, polygons)
    return Electrode(rect=_rect(_points(polygons), "pad"), contact=contact,
                     polygons=[_polygon(polygon) for polygon in polygons],
                     label=f"pad {reference}.{pad.number}@{pad.net}", drill_nm=drill, pad_nm=largest,
                     pad_min_nm=smallest, slot_dx_nm=slot_dx, slot_dy_nm=slot_dy, center=pad.pos,
                     solder=drill > 0, protrusion_side=side)


def _via_span(via: model.Via, stack: Stack) -> tuple[int, int]:
    """(z_top, z_bottom) of a via's barrel, as Fill Resistance's _padstack_span."""
    zs = [stack.z_nm[name] for name in (via.layer_top, via.layer_bottom) if name in stack.z_nm]
    if len(zs) == 2:
        return min(zs) - 1, max(zs) + 1
    return -1, stack.bottom_nm + 1


def _via_electrode(via: model.Via, stack: Stack) -> Electrode:
    radius = max(via.diameter, via.drill) // 2
    x, y = via.pos
    return Electrode(rect=Rect.normalized(x - radius, y - radius, x + radius, y + radius, "via"), contact="all",
                     label=f"via({x / 1e6:.1f},{y / 1e6:.1f})", drill_nm=via.drill, pad_nm=via.diameter,
                     center=via.pos, barrel_z=_via_span(via, stack))


@dataclass
class Built:
    """The solver's input and what the viewer needs to map results back."""
    problem: Problem
    barrel_ids: list  # KiCad id of each problem.vias entry (via or plated pad)
    barrel_layers: list  # (top, bottom) copper layer of each barrel
    warnings: list
    optional: list = field(default_factory=list)  # loads the solver may leave out (guessed ones)


def build_problem(snapshot: model.BoardSnapshot, setup: DcSetup, board_path: str = "") -> Built:
    """The net's copper, barrels and terminals as a Fill Resistance `Problem` (PDN mode)."""
    net = setup.net
    stack = copper_stack(snapshot)
    warnings = list(stack.warnings)
    copper = {name: [] for name in stack.names}
    tracks = {name: [] for name in stack.names}
    for zone in snapshot.zones:
        if zone.net == net and zone.layer in copper:
            copper[zone.layer] += [_polygon(polygon) for polygon in zone.polygons]
    for graphic in snapshot.graphics:
        if graphic.net == net and graphic.layer in copper:
            copper[graphic.layer] += [_polygon(polygon) for polygon in graphic.polygons]
    for track in snapshot.tracks:
        if track.net == net and track.layer in tracks:
            tracks[track.layer].append(TrackSeg(track.layer, np.array([track.start, track.end], np.int64),
                                                track.width))
    for arc in snapshot.arcs:
        if arc.net == net and arc.layer in tracks:
            tracks[arc.layer].append(TrackSeg(arc.layer, np.array([arc.start, arc.mid, arc.end], np.int64),
                                              arc.width))
    pads = [pad for pad in snapshot.pads if pad.net == net]
    for pad in pads:  # every pad's exact copper per layer, SMD and through-hole alike
        for layer, polygons in pad.polygons.items():
            if layer in copper:
                copper[layer] += [_polygon(polygon) for polygon in polygons]
    layers = [LayerFill(name, stack.thickness_nm[name], stack.z_nm[name], copper[name])
              for name in stack.names if copper[name] or tracks[name]]
    if not layers:
        raise SetupError(f"Net {net} has no copper on this board")
    segments = [segment for layer in layers for segment in tracks[layer.layer_name]]

    sides = {footprint.id: footprint.side for footprint in snapshot.footprints}
    barrels, ids, spans = [], [], []
    for via in snapshot.vias:
        if via.net == net and via.drill > 0:
            top, bottom = _via_span(via, stack)
            barrels.append(ViaLink(x=via.pos[0], y=via.pos[1], drill_nm=via.drill, z_top_nm=top, z_bot_nm=bottom,
                                   kind="via", pad_nm=via.diameter))
            ids.append(via.id)
            spans.append((via.layer_top, via.layer_bottom))
    shapes = {}
    for pad in pads:
        if not _plated(pad):
            continue
        drill, slot_dx, slot_dy = _drill(pad)
        side = _solder_side(pad, sides)
        _, polygons = _pad_shape(pad, (side, "F.Cu", "B.Cu", *pad.polygons))
        largest, smallest = _pad_sizes(pad, polygons)
        # Populated through-hole pads, as Fill Resistance assumes for a pad without
        # footprint data: lead and solder in the hole, coat and cone on the solder side.
        barrels.append(ViaLink(x=pad.pos[0], y=pad.pos[1], drill_nm=drill, z_top_nm=-1,
                               z_bot_nm=stack.bottom_nm + 1, kind="pad", pad_nm=largest, pad_min_nm=smallest,
                               slot_dx_nm=slot_dx, slot_dy_nm=slot_dy, solder_filled=True,
                               protrusion_side=side))
        ids.append(pad.id)
        spans.append((stack.names[0], stack.names[-1]))
        if polygons:
            shapes[pad.pos] = [_polygon(polygon) for polygon in polygons]

    terminals = _terminals(snapshot, setup, stack, sides)
    problem = Problem(
        board_path=board_path or snapshot.board_name, net_name=net, rho_ohm_m=RHO_CU_OHM_M,
        plating_nm=round(setup.plating_um * 1000), layers=layers, vias=barrels, electrodes1=[], electrodes2=[],
        terminals=terminals, thickness_source="stackup",
        solder_thickness_nm=int(solver_config.SOLDER_THICKNESS_UM * 1000),
        solder_rho_ohm_m=solver_config.SOLDER_RHO_OHM_M, tracks=segments, vias_capped=setup.vias_capped,
        cap_plating_nm=int(solver_config.CAP_PLATING_UM * 1000),
        cap_max_drill_nm=int(solver_config.CAP_MAX_DRILL_MM * 1e6),
        tht_protrusion_nm=int(solver_config.THT_LEAD_PROTRUSION_MM * 1e6),
        tht_lead_clearance_nm=int(solver_config.THT_LEAD_CLEARANCE_MM * 1e6),
        tht_lead_rho_ohm_m=solver_config.THT_LEAD_RHO_OHM_M)
    contact_solder_buildups(problem)
    tht_joint_buildups(problem, shapes)
    return Built(problem, ids, spans, warnings)


def _terminals(snapshot, setup: DcSetup, stack: Stack, sides: dict) -> list:
    references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
    terminals = []
    for terminal in setup.terminals:
        if not terminal.parts:
            continue
        electrodes = []
        for part in terminal.parts:
            found = resolve_part(snapshot, part, setup.net)
            if not found:
                raise SetupError(f"{terminal.name}: {describe_part(part)} is not on {setup.net} "
                                 "(moved, renamed or deleted?)")
            for item in found:
                electrodes.append(_via_electrode(item, stack) if isinstance(item, model.Via)
                                  else _pad_electrode(item, references.get(item.footprint_id, ""), sides))
        if terminal.role == "supply":
            terminals.append(Terminal("supply", electrodes, terminal.name, r_out_ohm=0.0, v_oc=terminal.value,
                                      bonded=terminal.bonded))
        else:
            terminals.append(Terminal("load", electrodes, terminal.name, i_draw_a=max(0.0, terminal.value),
                                      bonded=terminal.bonded))
    return terminals


def inputs_key(snapshot: model.BoardSnapshot, setup: DcSetup):
    """Everything a solve depends on: the net's copper, vias and pads, the stackup,
    the terminals' footprints and the setup. Equal keys give equal results. The
    reader keeps unchanged records, so comparing keys is quick."""
    net = setup.net
    on_net = lambda items: tuple(item for item in items if item.net == net)  # noqa: E731
    pads = on_net(snapshot.pads)
    owners = {pad.footprint_id for pad in pads}
    return (setup.key(), on_net(snapshot.tracks), on_net(snapshot.arcs), on_net(snapshot.vias),
            on_net(snapshot.zones), on_net(snapshot.graphics), pads, snapshot.stackup,
            tuple((f.id, f.reference, f.side) for f in snapshot.footprints if f.id in owners))


# --- The live analysis -----------------------------------------------------------------

MAX_NAME = 40


def power_nets(snapshot: model.BoardSnapshot) -> list:
    """The nets named like a supply rail (is_power) that reach a pad, sorted."""
    return sorted({pad.net for pad in snapshot.pads if is_power(pad.net)})


class DcAnalysis:
    """The setup of the open board and its solves, for the live loop.

    `handle` applies the viewer's edits (protocol: "dc" requests) and saves the
    setup. `step`, called every poll, makes the plan: the nets to solve now, each
    with its supplies and loads (scope "net": the table's net; scope "all": every
    power net, with the terminals the user gave it or auto_terminals' guess).
    A net is solved again when what its solve depends on (`inputs_key`) has
    changed and the board then stayed the same for `settle_s`, like the kicad-cli
    exports after edits; a change while a solve runs cancels it. Solves run in a
    worker process (dcworker.SolverProcess), all due nets in one job, one result
    frame per net. Both return frames to send.
    """

    SETTLE_S = 1.5
    STATUS_INTERVAL_S = 1.0  # the elapsed time of a running solve, at most this often
    DISPLAY_CELLS_EACH = 300_000  # at least this many display pixels per net when many are solved

    def __init__(self, clock=time.monotonic, solver_factory=None, settle_s: float = SETTLE_S):
        self.clock = clock
        self.solver_factory = solver_factory or _default_solver
        self.solver = None
        self.settle_s = settle_s
        self.lock = threading.RLock()
        self.board_path = ""
        self.board_name = None
        self.setup = DcSetup()
        self.file = ""  # where the setup was read or saved
        self.message = ""  # feedback on the last edit
        self.active = -1  # the terminal clicks add parts to
        self.keys = None  # net -> inputs_key of the plan at the last step
        self.skipped = {}  # net -> why scope "all" leaves it out
        self.changed_at = 0.0
        self.solved = {}  # net -> the inputs_key its shown result (or error) belongs to
        self.results = {}  # net -> its last result frame, for a viewer that reconnects
        self.errors = {}  # net -> why its last solve failed
        self.force = False
        self.job = 0
        self.running = None  # (job id, {net: key}, started, build seconds, {net: guessed load names})
        self.guesses = ({}, None)  # (net -> auto_terminals, the pads and footprints they were made from)
        self.notes = []  # the solver's notes and warnings for the running job
        self.status = {"state": "idle", "message": "", "elapsed_s": None, "notes": []}
        self.status_sent = (None, 0.0)
        self.setup_frame = None
        self.outbox = []  # frames for the next step (a new board's cleared results)
        self.revision = 0

    # --- Board and setup --------------------------------------------------------------

    def target(self, board_name: str, board_path: str) -> None:
        """A (new) board is open: its saved setup, or none."""
        with self.lock:
            if (board_name, board_path) == (self.board_name, self.board_path):
                return
            self._cancel()
            self.board_name, self.board_path = board_name, board_path
            try:
                self.setup, self.file = load_setup(board_path)
                self.message = f"Setup read from {Path(self.file).name}" if self.file else ""
            except (OSError, SetupError) as exc:
                self.setup, self.file, self.message = DcSetup(), "", f"Setup not read: {exc}"
            self.active = len(self.setup.terminals) - 1
            self.keys = None  # the next step sends the new board's setup
            self.solved, self.results, self.errors, self.setup_frame = {}, {}, {}, None
            self.outbox = [protocol.dc_result_message(None, self.revision)]

    def handle(self, request: dict, snapshot, selected_nets=frozenset()) -> list[bytes]:
        """One edit from the viewer; the frames that show it."""
        with self.lock:
            self.message = ""
            try:
                self._edit(request, snapshot, selected_nets)
            except (SetupError, ValueError, TypeError, KeyError, IndexError) as exc:
                self.message = str(exc)
                return [self._setup_message(snapshot, force=True)]
            if request.get("op") not in ("solve", "activate"):
                self._save()
            if snapshot is not None:
                self._plan(snapshot)  # the nets scope "all" leaves out, for the setup frame
            return [self._setup_message(snapshot, force=True)] + self.step(snapshot)

    def _edit(self, request, snapshot, selected_nets):
        op, setup = request.get("op"), self.setup
        terminals = setup.terminals
        if op == "net":
            self._show_net(self._chosen_net(request, snapshot, selected_nets), snapshot)
        elif op == "scope":
            setup.scope = "all" if request.get("scope") == "all" else "net"
            if setup.scope == "all" and setup.net and not setup.terminals and snapshot is not None:
                self._show_net(setup.net, snapshot, refill=True)
        elif op == "add":
            role = request["role"]
            if role not in ("supply", "load"):
                raise ValueError(f"unknown role {role!r}")
            prefix = "S" if role == "supply" else "L"
            names = {terminal.name for terminal in terminals}
            name = next(f"{prefix}{n}" for n in range(1, len(terminals) + 2) if f"{prefix}{n}" not in names)
            value = guess_voltage(setup.net) if role == "supply" else DEFAULT_LOAD_A
            terminals.append(DcTerminal(name, role, [], value))
            self.active = len(terminals) - 1
        elif op == "auto":
            if not setup.net:
                raise SetupError("Choose the power net first")
            if snapshot is None:
                raise SetupError("No board yet")
            setup.terminals, message = auto_terminals(snapshot, setup.net)
            self.active = 0
            self.message = message
        elif op == "remove":
            del terminals[int(request["index"])]
            self.active = min(self.active, len(terminals) - 1)
        elif op == "activate":
            self.active = max(-1, min(int(request["index"]), len(terminals) - 1))
        elif op == "mark":
            self._mark(request, snapshot)
        elif op == "edit":
            self._edit_terminal(terminals[int(request["index"])], request)
        elif op == "settings":
            self._settings(request)
        elif op == "solve":
            self.force = True
            self.solved = {}
        else:
            raise ValueError(f"unknown DC request {op!r}")

    def _show_net(self, net, snapshot, refill=False):
        """The table shows `net`. In scope "all", a net without supplies and loads of
        its own starts from the guess it was solved with."""
        setup = self.setup
        setup.switch(net)
        if setup.scope == "all" and snapshot is not None and (refill or not setup.terminals):
            try:
                setup.terminals, self.message = auto_terminals(snapshot, net)
            except SetupError as exc:
                self.message = str(exc)
        self.active = len(setup.terminals) - 1 if setup.terminals else -1

    def _edit_terminal(self, terminal, request):
        if "name" in request:
            name = str(request["name"]).strip()[:MAX_NAME]
            if not name or any(other is not terminal and other.name == name for other in self.setup.terminals):
                raise ValueError(f"A terminal needs a unique name (not {name!r})")
            terminal.name = name
        if "value" in request:
            value = float(request["value"])
            if not math.isfinite(value) or value < 0 or (terminal.role == "supply" and value <= 0):
                raise ValueError("Supplies need a voltage above 0, loads a current of 0 or more")
            terminal.value, terminal.guessed = value, False
        if "bonded" in request:
            terminal.bonded = bool(request["bonded"])

    def _settings(self, request):
        setup = self.setup
        if "plating_um" in request:
            value = float(request["plating_um"])
            if not 1 <= value <= 200:
                raise ValueError("Via plating: 1 to 200 um")
            setup.plating_um = value
        if "cell_um" in request:
            value = request["cell_um"]
            value = None if value in (None, 0) else float(value)
            if value is not None and not 10 <= value <= 2000:
                raise ValueError("Grid cell: 10 to 2000 um, or 0 for automatic")
            setup.cell_um = value
        if "vias_capped" in request:
            setup.vias_capped = bool(request["vias_capped"])

    def _chosen_net(self, request, snapshot, selected_nets) -> str:
        if "item" in request:
            net = net_of(snapshot, str(request["item"])) if snapshot is not None else ""
            if not net:
                raise SetupError("That item has no net: click the copper of the power net")
            return net
        if request.get("from") == "selection":
            nets = sorted(selected_nets)
            if not nets:
                raise SetupError("Nothing with a net is selected in KiCad")
            if len(nets) > 1:
                self.message = f"{len(nets)} nets selected; using {nets[0]}"
            return nets[0]
        return str(request.get("net", ""))[:200]

    def _mark(self, request, snapshot):
        """A click on a pad or via: add it to the active terminal, or take it off
        again. Shift+click takes every pad of that component on the net. In scope
        "all", a click on another power net's part moves the table to that net."""
        if snapshot is None:
            raise SetupError("No board yet")
        found = part_for(snapshot, str(request["item"]), bool(request.get("whole")))
        if found is None:
            raise SetupError("Click a pad or a via to mark it")
        part, net = found
        setup = self.setup
        if not setup.net or (net != setup.net and setup.scope == "all" and is_power(net)):
            self._show_net(net, snapshot)
            self.active = -1
        elif net != setup.net:
            raise SetupError(f"{describe_part(part)} is on {net or 'no net'}, not {setup.net}")
        if not 0 <= self.active < len(setup.terminals):
            role = "load" if any(t.role == "supply" for t in setup.terminals) else "supply"
            self._edit({"op": "add", "role": role}, snapshot, frozenset())
        terminal = setup.terminals[self.active]
        items = {item.id for item in resolve_part(snapshot, part, net)}
        for other in setup.terminals:  # one part, one terminal
            before = len(other.parts)
            other.parts = [p for p in other.parts if not items & {i.id for i in resolve_part(snapshot, p, net)}]
            if other is terminal and len(other.parts) < before:
                self.message = f"{describe_part(part)} taken off {terminal.name}"
                return
        terminal.parts.append(part)
        self.message = f"{describe_part(part)} added to {terminal.name}"

    def _save(self):
        if not self.board_path:
            self.message = self.message or "Save the board in KiCad to keep this setup"
            return
        try:
            self.file = save_setup(self.setup, self.board_path)
        except OSError as exc:
            self.message = f"Setup not saved: {exc}"

    def setup_state(self) -> dict:
        setup = self.setup
        return {"net": setup.net, "active": self.active, "file": self.file, "message": self.message,
                "scope": setup.scope, "skipped": dict(self.skipped),
                "settings": {"plating_um": setup.plating_um, "cell_um": setup.cell_um or 0,
                             "vias_capped": setup.vias_capped, "rho_ohm_m": RHO_CU_OHM_M},
                "terminals": [{"name": t.name, "role": t.role, "value": t.value, "bonded": t.bonded,
                               "guessed": t.guessed, "parts": [describe_part(part) for part in t.parts]}
                              for t in setup.terminals]}

    def _markers(self, snapshot) -> list:
        """(x, y, size, terminal, side) of every pad and via the table's terminals name."""
        rows = []
        if snapshot is None:
            return rows
        for number, terminal in enumerate(self.setup.terminals):
            for part in terminal.parts:
                for item in resolve_part(snapshot, part, self.setup.net):
                    if isinstance(item, model.Via):
                        rows.append((*item.pos, item.diameter, number, 0))
                        continue
                    copper = [layer for layer in item.polygons if layer not in model.PASTE_LAYERS]
                    points = _points(tuple(p for layer in copper for p in item.polygons[layer]))
                    size = int(np.ptp(points, axis=0).max()) if len(points) else 0
                    side = 0 if _plated(item) or len(copper) != 1 else (-1 if copper[0] == "B.Cu" else 1)
                    rows.append((*item.pos, size, number, side))
        return rows

    def _setup_message(self, snapshot, force=False) -> bytes | None:
        frame = protocol.dc_setup_message(self.setup_state(), self._markers(snapshot), self.revision)
        if frame == self.setup_frame and not force:
            return None
        self.setup_frame = frame
        return frame

    # --- The plan ---------------------------------------------------------------------

    def _guess(self, snapshot, net):
        """auto_terminals for `net`, kept while the board's pads and footprints are."""
        guesses, source = self.guesses
        if source != (snapshot.pads, snapshot.footprints):
            guesses = {}
            self.guesses = (guesses, (snapshot.pads, snapshot.footprints))
        if net not in guesses:
            try:
                guesses[net] = auto_terminals(snapshot, net)[0]
            except SetupError as exc:
                guesses[net] = str(exc)
        return guesses[net]

    def _plan(self, snapshot) -> tuple[dict, str]:
        """(net -> the setup its solve takes, what is missing when nothing is due)."""
        setup = self.setup
        if setup.scope != "all":
            missing = setup.ready()
            self.skipped = {}
            return ({} if missing else {setup.net: setup.for_net(setup.net, setup.terminals)}), missing
        plan, self.skipped = {}, {}
        nets = power_nets(snapshot)
        for net in sorted(nets, key=lambda net: net != setup.net):  # the table's net first
            own = setup.for_net(net, setup.terminals if net == setup.net else setup.saved.get(net, []))
            if own.ready():
                guess = self._guess(snapshot, net)
                own = setup.for_net(net, [] if isinstance(guess, str) else guess)
                if own.ready():
                    self.skipped[net] = guess if isinstance(guess, str) else f"{net}: no load found"
                    continue
            plan[net] = own
        return plan, ("" if plan else "No power net with a supply to solve")

    # --- Solving ----------------------------------------------------------------------

    def step(self, snapshot, revision: int | None = None) -> list[bytes]:
        """Start, supersede or finish solves; frames to send (usually none)."""
        with self.lock:
            if revision is not None:
                self.revision = revision
            if snapshot is None:
                return []
            frames, self.outbox = self.outbox, []
            now = self.clock()
            plan, missing = self._plan(snapshot)
            keys = {net: inputs_key(snapshot, one) for net, one in plan.items()}
            if keys != self.keys:
                self.keys, self.changed_at = keys, now
                if (frame := self._setup_message(snapshot)) is not None:
                    frames.append(frame)  # the markers follow moved pads
                if self.running is not None and any(keys.get(net) != key for net, key in self.running[1].items()):
                    self._cancel()
                for net in [net for net in self.results if net not in keys]:
                    frames.append(protocol.dc_result_message(None, self.revision, net))  # no longer solved
                    del self.results[net]
                for net in [net for net in self.solved if net not in keys]:
                    del self.solved[net]
                    self.errors.pop(net, None)
            frames += self._replies(now)
            due = [net for net, key in keys.items() if self.solved.get(net) != key]
            if not keys:
                self._set_status(state="idle", message=missing)
            elif self.running is None and due:
                if self.force or now - self.changed_at >= self.settle_s:
                    self._launch(snapshot, plan, keys, due, now)
                else:
                    self._set_status(state="waiting", message="Edits settling; solving shortly")
            return frames + self._status_frames(now)

    def _launch(self, snapshot, plan, keys, due, now):
        self.force = False
        started = time.perf_counter()
        items, guessed = [], {}
        for net in due:
            try:
                items.append((net, build_problem(snapshot, plan[net], self.board_path)))
            except UserFacingError as exc:
                self.solved[net], self.errors[net] = keys[net], str(exc)  # retried once it changes
                continue
            guessed[net] = [t.name for t in plan[net].terminals if t.guessed and t.role == "load"]
            items[-1][1].optional = guessed[net]
        if not items:
            self._finish(0.0)
            return
        if self.solver is None:
            self.solver = self.solver_factory()
        self.job += 1
        self.notes = [note for _, built in items for note in built.warnings]
        cells = max(self.DISPLAY_CELLS_EACH, MAX_DISPLAY_CELLS // len(items))
        self.solver.submit(self.job, [(net, built) for net, built in items], cell_um=self.setup.cell_um,
                           display_cells=cells)
        self.running = (self.job, {net: keys[net] for net, _ in items}, now, time.perf_counter() - started, guessed)
        self._set_status(state="solving", message="Starting the solver", elapsed_s=0.0)

    def _replies(self, now) -> list[bytes]:
        if self.solver is None:
            return []
        frames = []
        for kind, job, payload in self.solver.poll():
            running = self.running
            if kind == "log" and running is not None:
                self.notes = (self.notes + [payload])[-20:]
            if running is None or (job is not None and job != running[0]):
                continue
            nets = running[1]
            if kind == "progress":
                text, elapsed, index, count, net = payload
                where = f"{net} ({index + 1}/{count}): " if count > 1 else ""
                self._set_status(state="solving", message=where + text, elapsed_s=round(elapsed, 1))
            elif kind == "result":
                net = payload["net"]
                payload["timings_s"].update(build=running[3], total=now - running[2])
                payload["guessed"] = [name for name in running[4].get(net, []) if name not in payload["left_out"]]
                payload["notes"] = [note for note in self.notes if not note.startswith("adaptive grid")]
                self.results[net] = protocol.dc_result_message(payload, self.revision)
                frames.append(self.results[net])
                self.solved[net] = nets[net]
                self.errors.pop(net, None)
            elif kind == "error":
                net, message = payload
                self.solved[net], self.errors[net] = nets[net], message
                if self.results.pop(net, None) is not None:
                    frames.append(protocol.dc_result_message(None, self.revision, net))
            elif kind in ("done", "exit"):
                self.running = None
                for net, key in nets.items():  # a process that died leaves its nets unsolved: say so
                    if self.solved.get(net) != key:
                        self.solved[net], self.errors[net] = key, "The DC solver stopped unexpectedly"
                self._finish(now - running[2])
        return frames

    def _finish(self, elapsed):
        """The status after a job: done, or the errors of the nets that failed."""
        failed = {net: message for net, message in self.errors.items() if net in (self.keys or {})}
        if self.setup.scope != "all":
            if failed:
                self._set_status(state="error", message=next(iter(failed.values())))
            else:
                self._set_status(state="done", message=f"Solved in {elapsed:.1f} s", elapsed_s=round(elapsed, 2))
            return
        solved = len(self.results)
        message = f"Solved {solved} net{'s' * (solved != 1)} in {elapsed:.1f} s"
        if failed:
            message += f"; {len(failed)} failed"
        self._set_status(state="done" if solved else "error", message=message, elapsed_s=round(elapsed, 2),
                         errors=failed)

    def _cancel(self):
        if self.running is not None and self.solver is not None:
            self.solver.cancel()
        self.running = None

    def _set_status(self, **status):
        self.status = {"state": status["state"], "message": status.get("message", ""),
                       "elapsed_s": status.get("elapsed_s"), "notes": list(self.notes)[-6:]}
        if status.get("errors"):
            self.status["errors"] = dict(status["errors"])

    def _status_frames(self, now) -> list[bytes]:
        sent, at = self.status_sent
        shown = {key: value for key, value in self.status.items() if key != "elapsed_s"}
        if shown == sent and not (self.status["state"] == "solving" and now - at >= self.STATUS_INTERVAL_S):
            return []
        self.status_sent = (shown, now)
        return [protocol.dc_status_message(self.status, self.revision)]

    def resync_frames(self, snapshot) -> list[bytes]:
        """Everything for a viewer that (re)connects."""
        with self.lock:
            return [self._setup_message(snapshot, force=True), protocol.dc_status_message(self.status, self.revision),
                    protocol.dc_result_message(None, self.revision), *self.results.values()]

    def close(self):
        with self.lock:
            self.running = None
            if self.solver is not None:
                self.solver.close()
                self.solver = None


def _default_solver():
    from .dcworker import SolverProcess
    from .launcher import cache_root
    return SolverProcess(log_path=cache_root() / "dc_worker.log")
