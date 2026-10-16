"""Propagation delay per length of copper, by layer: what turns a route into time.

`DelayModel` is the whole interface the phase analysis uses, so a later
impedance or εeff module can replace `StackupDelays`. Sources, in order:

1. KiCad 10's tuning profile on the net's netclass (Board Setup > Tuning Profiles,
   stored in the project file `.kicad_pro` under "tuning_profiles"; the netclass
   names its profile in "net_settings"). Its unit delays are attoseconds per mm
   (KiCad's `IU_PER_PS_PER_MM` = 1e6); a via takes an override for its layers,
   else the profile's via delay per mm of stackup height it crosses. KiCad charges a
   layer's one delay to every track of the net, but its calculator worked it out for
   the pair at the entry's own width and gap: so that number holds where the pair
   runs at that gap, and where the two tracks part (a lone bump, a breakout) the
   track is scaled by the estimate's ratio between the gap there and the profile's.
   KiCad's own length tuner says as much: its pair tuning only works on coupled
   stretches, the rest is for the single-track tuner.
2. εeff estimated from the stackup, with epsilon_r from the board file (KiCad
   10.0.3's IPC stackup has none, see kicad_reader._stackup): stripline inside;
   microstrip on the outer layers, alone (Hammerstad and Jensen) or, given the gap
   to its partner, the pair's differential (odd) mode (Kirschning and Jansen), which
   runs faster where the pair is coupled; given the distance to a copper pour on the
   same layer (grounded coplanar waveguide), faster again, by the ratio a conformal
   mapping of the cross-section gives with and without that pour.
3. FR-4, εr 4.2, when the stackup gives no dielectric constant: clearly a guess,
   and named as the source.
"""

import fnmatch
import functools
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from . import model

PS_PER_MM_VACUUM = 1e9 / 299_792_458  # 1 / c: 3.3356 ps per mm
AS_PER_PS = 1_000_000  # KiCad's delay unit: attoseconds
FR4_EPSILON_R = 4.2
PROFILE = "tuning profile"
STACKUP = "stackup estimate"
DEFAULT = "FR-4 default (εr 4.2)"


@dataclass(frozen=True)
class UnitDelay:
    ps_per_mm: float
    source: str  # "tuning profile 'USB'", "stackup estimate", "FR-4 default (εr 4.2)"


class DelayModel:
    """Delay of track and via copper; override both methods."""

    def track(self, net: str, layer: str, width_nm: int, gap_nm: float | None = None,
              ground_nm: float | None = None) -> UnitDelay:
        """`gap_nm`: to the pair's other trace there (edge to edge); None: alone.
        `ground_nm`: to copper of another net on the same layer (a pour); None: none."""
        raise NotImplementedError

    def barrel(self, net: str, from_layer: str, to_layer: str, span: tuple[str, str],
               length_nm: float) -> tuple[float, str]:
        """(ps, source) of a via or plated hole carrying the signal between two layers."""
        raise NotImplementedError

    def structure(self, layer: str) -> tuple[str, float]:
        """("microstrip" or "stripline", dielectric height in nm) of a trace on `layer`;
        ("", 0) when unknown."""
        return "", 0.0


@dataclass(frozen=True)
class TuningProfile:
    name: str
    layers: dict = field(default_factory=dict)  # signal layer -> ps/mm
    via_ps_per_mm: float = 0.0  # of stackup height crossed
    via_overrides: tuple = ()  # ({signal layers}, {via layers}, ps)
    geometry: dict = field(default_factory=dict)  # signal layer -> (width nm, pair gap nm) the delay was computed for


def tuning_profiles(project: dict) -> dict[str, TuningProfile]:
    """The project's tuning profiles by name (KiCad 10, project/tuning_profiles.cpp).
    An entry's width and diff_pair_gap are the geometry KiCad's calculator gave the delay
    for (nm); a gap of 0 is a single-ended entry."""
    profiles = {}
    section = project.get("tuning_profiles") or {}
    for entry in section.get("tuning_profiles_impedance_geometric") or ():
        try:
            rows = [row for row in entry.get("layer_entries") or () if float(row.get("delay", 0)) > 0]
            layers = {row["signal_layer"]: float(row["delay"]) / AS_PER_PS for row in rows}
            geometry = {row["signal_layer"]: (max(0, int(row.get("width") or 0)), max(0, int(row.get("diff_pair_gap") or 0)))
                        for row in rows}
            overrides = tuple((frozenset((row["signal_layer_from"], row["signal_layer_to"])),
                               frozenset((row["via_layer_from"], row["via_layer_to"])),
                               float(row["delay"]) / AS_PER_PS) for row in entry.get("via_overrides") or ())
            profiles[entry["profile_name"]] = TuningProfile(
                entry["profile_name"], layers, float(entry.get("via_prop_delay", 0)) / AS_PER_PS, overrides, geometry)
        except (KeyError, TypeError, ValueError):
            continue  # a profile KiCad would not load either
    return profiles


def netclass_profiles(project: dict) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """(netclass -> tuning profile name, [(net pattern, netclass)]) from the project.
    The patterns only stand in when KiCad cannot say which netclass a net has."""
    settings = project.get("net_settings") or {}
    classes = {entry.get("name", ""): entry.get("tuning_profile") or "" for entry in settings.get("classes") or ()}
    patterns = [(entry.get("pattern", ""), entry.get("netclass", ""))
                for entry in settings.get("netclass_patterns") or () if isinstance(entry, dict)]
    return classes, patterns


def read_project(path: str) -> dict:
    """The project file as JSON, or {} (no project, unreadable, being written)."""
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _single_microstrip(epsilon_r: float, u: float) -> float:
    """Static εeff of a lone microstrip of width/height `u` (Hammerstad and Jensen 1980)."""
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((epsilon_r - 0.9) / (epsilon_r + 3)) ** 0.053
    return (epsilon_r + 1) / 2 + (epsilon_r - 1) / 2 * (1 + 10 / u) ** (-a * b)


LONE_GAP_H = 40.0  # a trace without a partner, for the ground's effect: one this far away
FAR_GROUND_H = 10.0  # a pour farther than this many dielectric heights changes nothing


@functools.lru_cache(maxsize=65_536)  # the same few geometries, pair after pair and poll after poll
def microstrip_epsilon(epsilon_r: float, width_nm: float, height_nm: float, gap_nm: float | None = None,
                       ground_nm: float | None = None) -> float:
    """εeff of a microstrip: alone, or `gap_nm` from its partner for the pair's odd
    (differential) mode, which has more of its field in the air between the two and so
    runs faster (Kirschning and Jansen 1984); it tends to the lone line as the gap
    widens. `ground_nm` from a pour on the same layer, it runs faster again: by the
    ratio of `_coplanar_epsilon` with and without that pour (within 1 % of a field
    solver, tests/field_solver.py). Static (no dispersion), zero thickness, no solder mask."""
    h = max(height_nm, 1.0)
    u = max(width_nm, 1.0) / h
    single = _single_microstrip(epsilon_r, u)
    effective = single
    if gap_nm is not None:
        g = max(gap_nm, 1.0) / h
        a = 0.7287 * (single - (epsilon_r + 1) / 2) * (1 - math.exp(-0.179 * u))
        b = 0.747 * epsilon_r / (0.15 + epsilon_r)
        c = b - (b - 0.207) * math.exp(-0.414 * u)
        d = 0.593 + 0.694 * math.exp(-0.562 * u)
        effective = ((epsilon_r + 1) / 2 + a - single) * math.exp(-c * g ** d) + single
    if ground_nm is not None and ground_nm / h < FAR_GROUND_H:
        g = max(gap_nm, 1.0) / h if gap_nm is not None else LONE_GAP_H
        s = max(ground_nm, 1.0) / h
        effective *= _coplanar_epsilon(epsilon_r, u, g, s) / _coplanar_epsilon(epsilon_r, u, g, None)
    return effective


def _k_ratio(k2: float) -> float:
    """K(k) / K(k') of the complete elliptic integral of the first kind, for k squared
    (by the arithmetic-geometric mean: K(k) = pi / (2 AGM(1, k')))."""
    k2 = min(max(k2, 1e-300), 1 - 1e-16)

    def agm(a, b):
        for _ in range(60):
            if abs(a - b) <= 1e-15 * a:
                break
            a, b = (a + b) / 2, math.sqrt(a * b)
        return a
    return agm(1.0, math.sqrt(k2)) / agm(1.0, math.sqrt(1 - k2))


def _strip_between(a: float | None, b: float, c: float, d: float | None) -> float:
    """Capacitance / eps0 of a strip [b, c] between grounds (-inf, a] and [d, inf) along
    a line bounding a half-plane (None: no ground on that side): K(k)/K(k'), with k
    squared the cross ratio of the four edges."""
    if a is None:
        k2 = (c - b) / (d - b)
    elif d is None:
        k2 = (c - b) / (c - a)
    else:
        k2 = (c - b) * (d - a) / ((d - b) * (c - a))
    return _k_ratio(k2)


def _coplanar_epsilon(epsilon_r: float, u: float, g: float, s: float | None) -> float:
    """Partial-capacitance εeff (Ghione and Naldi's method) of one trace of a pair in its
    odd mode, lengths in dielectric heights: width u, the partner g away (the midline an
    electric wall), a coplanar ground s from its outer edge (None: none), a plane below.
    z squared folds the air's quarter-plane, cosh(pi z) the dielectric's half-strip, each
    into a half-plane with the wall on its edge; the strip's capacitance there is then
    exact for zero-thickness copper. It leaves out the air's fringing to the plane below,
    so only its ratio with and without the pour is used (microstrip_epsilon)."""
    inner, outer = g / 2, g / 2 + u
    air = _strip_between(0.0, inner ** 2, outer ** 2, None if s is None else (outer + s) ** 2)
    below = _strip_between(None if s is None else -math.cosh(math.pi * (outer + s)), -math.cosh(math.pi * outer),
                           -math.cosh(math.pi * inner), -1.0)
    return (air + epsilon_r * below) / (air + below)


class StackupDelays(DelayModel):
    """Tuning profile first, then the stackup estimate, then FR-4."""

    def __init__(self, stackup: tuple[model.StackupLayer, ...], heights: dict[str, int],
                 profiles: dict[str, TuningProfile] | None = None, net_profiles: dict[str, str] | None = None):
        self.stackup = tuple(stackup)
        self.heights = heights
        self.profiles = profiles or {}
        self.net_profiles = net_profiles or {}  # net -> profile name ("" for none)
        self._dielectric = self._neighbours()

    def _neighbours(self) -> dict[str, list[model.StackupLayer]]:
        """Copper layer -> the dielectrics right above and below it."""
        found = {}
        layers = self.stackup
        for index, entry in enumerate(layers):
            if entry.type != "copper":
                continue
            beside = []
            for step in (-1, 1):
                other = index + step
                while 0 <= other < len(layers) and layers[other].type not in ("dielectric", "copper"):
                    other += step  # mask and silkscreen are not the signal's dielectric
                if 0 <= other < len(layers) and layers[other].type == "dielectric":
                    beside.append(layers[other])
            found[entry.name] = beside
        return found

    def _profile(self, net: str) -> TuningProfile | None:
        return self.profiles.get(self.net_profiles.get(net, ""))

    def _epsilon(self, dielectrics) -> tuple[float, str]:
        known = [(entry.epsilon_r, entry.thickness_nm or 1) for entry in dielectrics if entry.epsilon_r]
        if not known:
            return FR4_EPSILON_R, DEFAULT
        return sum(e * t for e, t in known) / sum(t for _, t in known), STACKUP

    def track(self, net: str, layer: str, width_nm: int, gap_nm: float | None = None,
              ground_nm: float | None = None) -> UnitDelay:
        profile = self._profile(net)
        if profile is not None and layer in profile.layers:
            scale = self._coupling(layer, width_nm, gap_nm, ground_nm, profile.geometry.get(layer))
            return UnitDelay(profile.layers[layer] * scale, f"{PROFILE} '{profile.name}'")
        beside = self._dielectric.get(layer, [])
        epsilon_r, source = self._epsilon(beside)
        if self._outer(layer):
            height = (beside[0].thickness_nm if beside else None) or width_nm or 1
            effective = microstrip_epsilon(epsilon_r, width_nm or height, height, gap_nm, ground_nm)
        else:
            effective = epsilon_r  # stripline: the field is all in the dielectric
        return UnitDelay(PS_PER_MM_VACUUM * math.sqrt(effective), source)

    def _outer(self, layer: str) -> bool:
        """An outer layer: microstrip over one dielectric."""
        return len(self._dielectric.get(layer, [])) == 1 or layer in ("F.Cu", "B.Cu")

    def _coupling(self, layer: str, width_nm: int, gap_nm: float | None, ground_nm: float | None,
                  geometry: tuple[int, int] | None) -> float:
        """How much slower or faster than its tuning profile's number a track runs here.
        The profile's delay is the pair's at the entry's own width and gap (KiCad's
        calculator: the odd mode there); away from that gap, alone, or beside a pour, the
        estimate's εeff ratio scales it, so a stretch where P and N part runs at the lone
        line's speed. 1 for a single-ended entry, on an inner layer (coupled or not, the
        field is all in the dielectric), or without a dielectric height to measure the gap by."""
        if not geometry or geometry[1] <= 0 or not self._outer(layer):
            return 1.0
        beside = self._dielectric.get(layer, [])
        height = beside[0].thickness_nm if beside else 0
        if not height:
            return 1.0
        epsilon_r, _ = self._epsilon(beside)
        anchor = microstrip_epsilon(epsilon_r, geometry[0] or width_nm or height, height, geometry[1])
        here = microstrip_epsilon(epsilon_r, width_nm or geometry[0] or height, height, gap_nm, ground_nm)
        return math.sqrt(here / anchor)

    def structure(self, layer: str) -> tuple[str, float]:
        beside = self._dielectric.get(layer, [])
        heights = [entry.thickness_nm for entry in beside if entry.thickness_nm]
        if self._outer(layer):
            return "microstrip", float(heights[0]) if heights else 0.0
        return "stripline", float(min(heights)) if heights else 0.0

    def barrel(self, net: str, from_layer: str, to_layer: str, span: tuple[str, str],
               length_nm: float) -> tuple[float, str]:
        profile = self._profile(net)
        if profile is not None:
            for signal, via, ps in profile.via_overrides:
                if signal == {from_layer, to_layer} and via == set(span):
                    return ps, f"{PROFILE} '{profile.name}'"
            if profile.via_ps_per_mm > 0:
                return profile.via_ps_per_mm * length_nm / 1e6, f"{PROFILE} '{profile.name}'"
        low, high = sorted((self.heights.get(from_layer, 0), self.heights.get(to_layer, 0)))
        epsilon_r, source = self._epsilon(self._dielectrics_between(low, high))
        return PS_PER_MM_VACUUM * math.sqrt(epsilon_r) * length_nm / 1e6, source

    def _dielectrics_between(self, low: int, high: int) -> list[model.StackupLayer]:
        """The dielectrics a barrel from height `low` to `high` passes through."""
        found, above, pending = [], None, []
        for entry in self.stackup:  # top to bottom
            if entry.type == "copper" and entry.name in self.heights:
                height = self.heights[entry.name]
                if above is not None and low <= height and above <= high:
                    found += pending
                above, pending = height, []
            elif entry.type == "dielectric":
                pending.append(entry)
        return found


def net_profiles_from_project(project: dict, nets, netclasses: dict[str, str] | None = None) -> dict[str, str]:
    """Net -> tuning profile name. `netclasses` (net -> netclass, from KiCad) wins;
    else the project's netclass patterns, else the Default netclass."""
    classes, patterns = netclass_profiles(project)
    found = {}
    for net in nets:
        netclass = (netclasses or {}).get(net)
        if netclass is None or not any(name in classes for name in netclass.split(",")):
            # Unknown to KiCad, or a merged netclass KiCad names "Effective for net: ..."
            # (several netclasses apply): the project's patterns say which it is.
            netclass = next((name for pattern, name in patterns
                             if fnmatch.fnmatchcase(net, pattern) or fnmatch.fnmatchcase(net.lstrip("/"), pattern)),
                            "Default")
        # A net in several netclasses reports them joined ("USB,Default"): the first with a profile.
        found[net] = next((classes[name] for name in netclass.split(",") if classes.get(name)), "")
    return found
