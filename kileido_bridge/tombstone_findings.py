"""The tombstone check as findings (`kls-findings 1`): what the DRC column's Tombstone button lists.

Every 01005, 0201 and 0402 on the board is checked at once (tombstone.check) and each part
at MEDIUM or HIGH risk becomes a finding, so it gets the DRC column's list, its markers on
the board, confirm and dismiss, and select-in-KiCad: HIGH a warning, MEDIUM info, its two
pads lit and a label at the part naming the end that would lift. Where an end lifts, the
part is drawn lifted off its pads as it would stand if it tombstoned (a `lift` draw: turned
about the holding pad's outer edge, steeper at HIGH); a part no end of which is singled
out is lit instead. Pure: no files, no KiCad. The loop writes the file (kls_folder) and the
findings watch reads it back like KiCad's DRC's, so every target is resolved on the live
board.
"""

from __future__ import annotations

from . import tombstone
from .findings import FORMAT, MAX_TEXT

TOOL = "Tombstone check"
SEVERITY = {"HIGH": "warning", "MEDIUM": "info"}
LIFT_DEG = {"HIGH": 45, "MEDIUM": 25}  # how far the part is drawn lifted: a picture of the risk, not a prediction
# Per factor (tombstone.factors): the check id, how sure its threshold is (the science
# notes' [S]: high, [G]: medium) and what to do about it
CHECKS = {
    "F1": ("tombstone.via_in_pad", "high", "Fill or cap the via, or move it out of the pad."),
    "F2": ("tombstone.heat", "medium",
           "Give the pad in the plane a thermal relief, or tie the other pad into more copper, so both ends melt "
           "together."),
    "F4": ("tombstone.paste", "medium", "Give both pads the same paste aperture."),
    "F5": ("tombstone.area_ratio", "high", "Open the aperture up, or use a thinner stencil."),
    "F7": ("tombstone.pad_size", "high", "Use the library footprint's nominal pads."),
    "F8": ("tombstone.pad_gap", "high", "Move the pads closer together, so each metal end sits over its pad."),
}


def convert(checked: int, found: list[tombstone.Finding], stencil_nm: int, stamp: str) -> dict:
    """The findings file of `tombstone.check`'s (checked, findings), HIGH first as given.
    `stamp`: the live board's, so the list says when the board changed since."""
    parts = f"{checked} part{'' if checked == 1 else 's'}"
    tool = f"{TOOL}; {parts} checked (01005, 0201, 0402); stencil {stencil_nm / 1e6:g} mm"
    return {"format": FORMAT, "run": "", "stamp": stamp, "tool": _cut(tool),
            "findings": [_finding(finding) for finding in found], "skipped": []}


def _finding(found: tombstone.Finding) -> dict:
    check, confidence, fix = CHECKS[found.keys[0]]
    raised = [factor.partition(" ")[2] for factor in found.factors]  # "HIGH: ...", the key dropped
    if len(raised) == 1:
        message = f"{found.package} at {found.level} tombstone risk: {raised[0].partition(': ')[2]}."
    else:
        message = f"{found.package} at {found.level} tombstone risk, {len(raised)} factors raised: {'; '.join(raised)}."
    lifts = found.pad_names[found.lifts] if found.lifts >= 0 else ""
    if lifts:
        message += f" The end on {lifts} would lift."
    label = f"tombstone {found.level}" + (f": {lifts} lifts" if lifts else "")
    lifted = found.pivot is not None and found.rises is not None
    lit = found.pads if lifted else (*found.items, *found.pads)  # a lifted part stands clear of its box
    draws = [{"tool": "highlight", "targets": [f"uuid:{item}" for item in lit if item]},
             {"tool": "label", "at": _pt(found.where, found.layer), "text": _cut(label)}]
    if lifted:
        draws.append({"tool": "lift", "targets": [f"uuid:{item}" for item in found.items],
                      "about": _pt(found.pivot, found.layer), "toward": _pt(found.rises, found.layer),
                      "angle_deg": LIFT_DEG[found.level]})
    return {"check": check, "severity": SEVERITY[found.level], "confidence": confidence,
            "title": _cut(found.message), "message": _cut(message), "fix": fix,
            "values": {"level": found.level, "package": found.package, "lifts": lifts or "either end"},
            "draw": draws}


def _pt(point, layer: str) -> str:
    return f"pt:{_mm(point[0])},{_mm(point[1])}@{layer}"


def _mm(nm: float) -> str:
    text = f"{nm / 1e6:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _cut(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
