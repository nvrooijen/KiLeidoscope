"""Propagation delay per length of copper, by layer: KiCad's tuning profile numbers.

`DelayModel` is the whole interface the phase analysis uses: one delay per layer,
charged to every track of the net, as KiCad's own time-domain tuning does. It comes
from KiCad 10's tuning profile on the net's netclass (Board Setup > Tuning Profiles,
stored in the project file `.kicad_pro` under "tuning_profiles"; the netclass names its
profile in "net_settings"). Its unit delays are attoseconds per mm (KiCad's
`IU_PER_PS_PER_MM` = 1e6); a via takes an override for its layers, else the profile's
via delay per mm of stackup height it crosses.

Without a profile, or on a layer the profile has no delay for, there is no number:
KiCad has none either, and the pair is not measured (phase.Unprofiled).
"""

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

AS_PER_PS = 1_000_000  # KiCad's delay unit: attoseconds
PROFILE = "tuning profile"


@dataclass(frozen=True)
class UnitDelay:
    ps_per_mm: float
    source: str  # "tuning profile 'USB'"


class DelayModel:
    """Delay of track and via copper; None where there is no number."""

    def profile(self, net: str) -> str:
        """The name of the net's tuning profile, "" for none."""
        return ""

    def track(self, net: str, layer: str, width_nm: int) -> UnitDelay | None:
        return None

    def barrel(self, net: str, from_layer: str, to_layer: str, span: tuple[str, str],
               length_nm: float) -> tuple[float, str] | None:
        """(ps, source) of a via or plated hole carrying the signal between two layers."""
        return None


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


class ProfileDelays(DelayModel):
    """The nets' tuning profiles: `profiles` by name, `net_profiles` net -> profile name."""

    def __init__(self, profiles: dict[str, TuningProfile] | None = None, net_profiles: dict[str, str] | None = None):
        self.profiles = profiles or {}
        self.net_profiles = {} if net_profiles is None else net_profiles  # net -> profile name ("" for none)

    def _profile(self, net: str) -> TuningProfile | None:
        return self.profiles.get(self.net_profiles.get(net, ""))

    def profile(self, net: str) -> str:
        found = self._profile(net)
        return found.name if found is not None else ""

    def track(self, net: str, layer: str, width_nm: int) -> UnitDelay | None:
        profile = self._profile(net)
        if profile is None or layer not in profile.layers:
            return None
        return UnitDelay(profile.layers[layer], f"{PROFILE} '{profile.name}'")

    def barrel(self, net: str, from_layer: str, to_layer: str, span: tuple[str, str],
               length_nm: float) -> tuple[float, str] | None:
        profile = self._profile(net)
        if profile is None:
            return None
        for signal, via, ps in profile.via_overrides:
            if signal == {from_layer, to_layer} and via == set(span):
                return ps, f"{PROFILE} '{profile.name}'"
        return profile.via_ps_per_mm * length_nm / 1e6, f"{PROFILE} '{profile.name}'"


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
