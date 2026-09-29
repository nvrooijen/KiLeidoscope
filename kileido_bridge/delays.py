"""Propagation delay per length of copper, by layer: what turns a route into time.

`DelayModel` is the whole interface the phase analysis uses, so a later
impedance or εeff module can replace `StackupDelays`. Sources, in order:

1. KiCad 10's tuning profile on the net's netclass (Board Setup > Tuning Profiles,
   stored in the project file `.kicad_pro` under "tuning_profiles"; the netclass
   names its profile in "net_settings"). Its unit delays are attoseconds per mm
   (KiCad's `IU_PER_PS_PER_MM` = 1e6); a via takes an override for its layers,
   else the profile's via delay per mm of stackup height it crosses.
2. εeff estimated from the stackup: microstrip (Hammerstad) on the outer layers,
   stripline inside, with epsilon_r from the board file (KiCad 10.0.3's IPC
   stackup has none, see kicad_reader._stackup).
3. FR-4, εr 4.2, when the stackup gives no dielectric constant: clearly a guess,
   and named as the source.
"""

import fnmatch
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

    def track(self, net: str, layer: str, width_nm: int) -> UnitDelay:
        raise NotImplementedError

    def barrel(self, net: str, from_layer: str, to_layer: str, span: tuple[str, str],
               length_nm: float) -> tuple[float, str]:
        """(ps, source) of a via or plated hole carrying the signal between two layers."""
        raise NotImplementedError


@dataclass(frozen=True)
class TuningProfile:
    name: str
    layers: dict = field(default_factory=dict)  # signal layer -> ps/mm
    via_ps_per_mm: float = 0.0  # of stackup height crossed
    via_overrides: tuple = ()  # ({signal layers}, {via layers}, ps)


def tuning_profiles(project: dict) -> dict[str, TuningProfile]:
    """The project's tuning profiles by name (KiCad 10, project/tuning_profiles.cpp)."""
    profiles = {}
    section = project.get("tuning_profiles") or {}
    for entry in section.get("tuning_profiles_impedance_geometric") or ():
        try:
            layers = {row["signal_layer"]: float(row["delay"]) / AS_PER_PS
                      for row in entry.get("layer_entries") or () if float(row.get("delay", 0)) > 0}
            overrides = tuple((frozenset((row["signal_layer_from"], row["signal_layer_to"])),
                               frozenset((row["via_layer_from"], row["via_layer_to"])),
                               float(row["delay"]) / AS_PER_PS) for row in entry.get("via_overrides") or ())
            profiles[entry["profile_name"]] = TuningProfile(
                entry["profile_name"], layers, float(entry.get("via_prop_delay", 0)) / AS_PER_PS, overrides)
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


def microstrip_epsilon(epsilon_r: float, width_nm: float, height_nm: float) -> float:
    """Hammerstad's εeff of a microstrip (no solder mask, zero thickness)."""
    u = max(width_nm, 1.0) / max(height_nm, 1.0)
    shape = (1 + 12 / u) ** -0.5 + (0.04 * (1 - u) ** 2 if u < 1 else 0.0)
    return (epsilon_r + 1) / 2 + (epsilon_r - 1) / 2 * shape


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

    def track(self, net: str, layer: str, width_nm: int) -> UnitDelay:
        profile = self._profile(net)
        if profile is not None and layer in profile.layers:
            return UnitDelay(profile.layers[layer], f"{PROFILE} '{profile.name}'")
        beside = self._dielectric.get(layer, [])
        epsilon_r, source = self._epsilon(beside)
        if len(beside) == 1 or layer in ("F.Cu", "B.Cu"):  # an outer layer: microstrip over one dielectric
            height = (beside[0].thickness_nm if beside else None) or width_nm or 1
            effective = microstrip_epsilon(epsilon_r, width_nm or height, height)
        else:
            effective = epsilon_r  # stripline: the field is all in the dielectric
        return UnitDelay(PS_PER_MM_VACUUM * math.sqrt(effective), source)

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
        if netclass is None:
            netclass = next((name for pattern, name in patterns
                             if fnmatch.fnmatchcase(net, pattern) or fnmatch.fnmatchcase(net.lstrip("/"), pattern)),
                            "Default")
        # A net in several netclasses reports them joined ("USB,Default"): the first with a profile.
        found[net] = next((classes[name] for name in netclass.split(",") if classes.get(name)), "")
    return found
