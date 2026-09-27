# Vendored from Fill Resistance 1.4.2, fill_resistance/skin.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Unchanged apart from this header.
"""Skin-effect corrections: frequency-dependent effective sheet
resistance of a copper foil and via-barrel wall.

1D diffusion through the foil thickness (exact): with tau = (1+j)/delta,
the internal impedance per square of a foil of thickness t is

    one-sided field (plane over a return plane):  Zs = tau*rho * coth(tau*t)
    two-sided field (isolated foil):              Zs = tau*rho/2 * coth(tau*t/2)

Both reduce to rho/t at DC and to rho/delta (resp. rho/(2*delta)) at
high frequency. R_AC = Re(Zs) is used as the effective sheet resistance.

HONESTY NOTE (also in the README): only the through-thickness current
crowding is modeled. Lateral redistribution (proximity effect - AC
current following the minimum-inductance path) needs a magneto-
quasistatic solve and is NOT captured; since the resistance-driven
distribution is the minimum-dissipation one, the reported AC resistance
is a rigorous LOWER BOUND at the given frequency.
"""
from __future__ import annotations

import cmath
import math
import re

MU0 = 4e-7 * math.pi


def skin_depth_m(freq_hz: float, rho_ohm_m: float) -> float:
    return math.sqrt(2.0 * rho_ohm_m / (2.0 * math.pi * freq_hz * MU0))


def _coth(x: complex) -> complex:
    return 1.0 / cmath.tanh(x)


def sheet_resistance_ac(thickness_m: float, freq_hz: float,
                        rho_ohm_m: float, sides: int = 1) -> float:
    """Effective sheet resistance [ohm/sq] of a foil at freq_hz.
    sides=1: field on one side (plane facing a return plane, conservative);
    sides=2: symmetric field on both sides (isolated foil)."""
    if freq_hz <= 0.0:
        return rho_ohm_m / thickness_m
    delta = skin_depth_m(freq_hz, rho_ohm_m)
    tau = (1.0 + 1.0j) / delta
    if sides == 2:
        zs = tau * rho_ohm_m / 2.0 * _coth(tau * thickness_m / 2.0)
    else:
        zs = tau * rho_ohm_m * _coth(tau * thickness_m)
    return zs.real


def resistance_factor(thickness_m: float, freq_hz: float,
                      rho_ohm_m: float, sides: int = 1) -> float:
    """R_AC / R_DC of a foil (or barrel wall) of the given thickness."""
    if freq_hz <= 0.0:
        return 1.0
    return (sheet_resistance_ac(thickness_m, freq_hz, rho_ohm_m, sides)
            / (rho_ohm_m / thickness_m))


def normalize_decimal(text: str) -> str:
    """Accept a European decimal comma ('1,5' -> '1.5'); reject
    thousands-separator commas ('1,500' would silently become 1.5,
    a 1000x error that propagates unnoticed into the result)."""
    if "," in text:
        if "." in text or text.count(",") > 1 \
                or re.search(r",\d{3}(?=\D|$)", text):
            raise ValueError(
                f"ambiguous comma in '{text}': use '.' as the decimal "
                "separator and no thousands separators")
        text = text.replace(",", ".")
    return text


_SI_SUFFIXES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
                "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def parse_engineering(text: str) -> float:
    """General value entry: '50m' -> 0.05, '4.7k' -> 4700, '2M' ->
    2e6, '3,3' -> 3.3, '10' -> 10. A trailing SI suffix scales the
    number - CASE decides between m (milli) and M (mega), unlike
    parse_frequency, where a lone m can only mean MHz. Raises
    ValueError on garbage or an ambiguous comma (normalize_decimal's
    rules)."""
    t = normalize_decimal(text.strip())
    mult = 1.0
    if t and t[-1] in _SI_SUFFIXES:
        mult = _SI_SUFFIXES[t[-1]]
        t = t[:-1].strip()               # allow '50 m'
    return float(t) * mult               # ValueError on garbage


def parse_frequency(text: str) -> float:
    """'0', '100k', '1.5M', '142500' -> Hz; empty -> 0 (DC).
    Raises ValueError on unparseable, ambiguous or negative input (a
    typo silently becoming DC would mislabel the result)."""
    t = normalize_decimal(text.strip().lower()).removesuffix("hz").strip()
    if not t:
        return 0.0
    mult = 1.0
    if t.endswith("meg"):
        mult, t = 1e6, t[:-3]
    elif t.endswith("m"):
        mult, t = 1e6, t[:-1]
    elif t.endswith("k"):
        mult, t = 1e3, t[:-1]
    elif t.endswith("g"):
        mult, t = 1e9, t[:-1]
    value = float(t) * mult          # ValueError on garbage
    if value < 0:
        raise ValueError(f"negative frequency: {text!r}")
    return value
