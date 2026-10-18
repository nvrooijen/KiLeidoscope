"""Dynamic phase as findings (`kls-findings 1`): what the DRC column's Dynamic phase button lists.

Every pair is measured whole (phase.analyze_board, no per-poll budget) and what the Diff
pairs column marks becomes a finding, so it gets the DRC column's list, its markers on the
board, confirm and dismiss, and select-in-KiCad: an excursion (|Δt| above the marker
tolerance for longer than the marker length) is a warning with the stretch's copper lit and
a label at its start; a short an error; a loop a warning; an unfinished side, ends on
different parts, a branch and a pair without a tuning profile are info. Pure: no files, no KiCad. The loop writes the file
(kls_folder) and the findings watch reads it back like KiCad's DRC's, so every target is
resolved on the live board and the text shown as text.
"""

from __future__ import annotations

from . import phase
from .findings import FORMAT, MAX_TARGETS, MAX_TEXT

TOOL = "Dynamic phase"
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
LABELS = 3  # shorts labelled per pair


def convert(results, warnings, settings: phase.Settings, heights: dict[str, int], stamp: str) -> dict:
    """The findings file of `phase.analyze_board`'s (results, warnings), errors first.
    `heights`: copper layer -> z (nm), to name the layer a point lies on; `stamp`: the live
    board's, so the list says when the board changed since."""
    layer_at = {height: layer for layer, height in heights.items()}
    found, pairs = [], 0
    for result in results:
        if isinstance(result, phase.Shorted):
            found.append(_short(result))
            continue
        if isinstance(result, phase.Unprofiled):
            found.append(_finding("phase.unprofiled", "info", f"{result.channel.name}: {result.reason}",
                                  "KiCad has no delay for this pair, so dynamic phase does not measure it. Set a "
                                  "tuning profile (Board Setup > Tuning Profiles) on its netclass, with a delay for "
                                  "every layer it runs on.", {}, []))
            continue
        pairs += 1
        found += [_excursion(result, excursion, settings, layer_at) for excursion in result.excursions]
        found += _route_findings(result, layer_at)
    found += [_unmeasured(text) for text in warnings]
    found.sort(key=lambda finding: SEVERITY_ORDER[finding["severity"]])
    tool = (f"{TOOL}; {pairs} pair{'' if pairs == 1 else 's'} measured; markers above {settings.tolerance_ps:g} ps "
            f"for {settings.min_length_nm / 1e6:g} mm")
    return {"format": FORMAT, "run": "", "stamp": stamp, "tool": _cut(tool), "findings": found, "skipped": []}


def _excursion(result: phase.PairPhase, excursion: phase.Excursion, settings: phase.Settings, layer_at) -> dict:
    name = result.channel.name
    start = phase.end_label(result.p.start, result.n.start)
    at, length = excursion.at_nm / 1e6, excursion.length_nm / 1e6
    ahead = "N" if excursion.peak_ps > 0 else "P"
    ids = excursion.item_ids or ((excursion.item_id,) if excursion.item_id else ())
    draws = _highlight(ids) + [{"tool": "label", "at": _pt(excursion.pos, layer_at),
                                "text": f"{excursion.peak_ps:+.1f} ps over {length:.1f} mm"}]
    return _finding("phase.excursion", "warning",
                    f"{name}: P and N {abs(excursion.peak_ps):.1f} ps apart over {length:.1f} mm",
                    f"|Δt| stays above {settings.tolerance_ps:g} ps from {at:.1f} mm to {at + length:.1f} mm along "
                    f"the pair, walked from {start}; peak {excursion.peak_ps:+.1f} ps, {ahead} ahead. Part of the "
                    f"differential signal runs as common mode there. End-to-end skew {result.skew_ps:+.1f} ps.",
                    {"peak_ps": round(excursion.peak_ps, 2), "length_mm": round(length, 2), "at_mm": round(at, 2),
                     "skew_ps": round(result.skew_ps, 2)}, draws)


def _short(result: phase.Shorted) -> dict:
    name = result.channel.name
    contacts = result.contacts
    ids = tuple(dict.fromkeys(item for contact in contacts for item in (contact.p_id, contact.n_id) if item))
    draws = _highlight(ids) + [{"tool": "label", "at": _pt((contact.pos[0], contact.pos[1]), None, contact.layer),
                                "text": "P touches N"} for contact in contacts[:LABELS]]
    places = f"{len(contacts)} place{'' if len(contacts) == 1 else 's'}"
    return _finding("phase.short", "error", f"{name}: P and N are shorted",
                    f"P's copper touches N's in {places}: no differential signal, so the pair has no phase and no "
                    f"numbers. In the Diff pairs column its pulses run from the start up to the short.",
                    {"places": len(contacts)}, draws)


def _route_findings(result: phase.PairPhase, layer_at) -> list[dict]:
    """What the pair list warns about besides excursions: an unfinished side, a loop, a
    branch, ends on different parts."""
    name = result.channel.name
    found = []
    for side, route in (("P", result.p), ("N", result.n)):
        if not route.complete:
            found.append(_finding("phase.unrouted", "info", f"{name}: {side} side is not fully routed",
                                  f"Not every pad of the {side} nets is on the route's copper; the pair is measured "
                                  f"as far as the copper goes.", {}, _highlight(route.item_ids)))
        for loop in route.loops:
            found.append(_finding(
                "phase.loop", "warning", f"{name}: {loop.length_nm / 1e6:.1f} mm of copper loops beside the {side} route",
                "Copper that closes a loop with the route, such as an old route left beside a new one. The pair is "
                "measured along the shortest path; KiCad's length tuner counts both.",
                {"length_mm": round(loop.length_nm / 1e6, 2)},
                _highlight(loop.item_ids) + [{"tool": "label", "at": _pt(loop.pos, None, loop.layer),
                                              "text": f"{loop.length_nm / 1e6:.1f} mm loop"}]))
        for branch in route.branches:
            found.append(_finding(
                "phase.branch", "info", f"{name}: {side} side branches to {branch.terminal.label}",
                f"A branch {branch.length_nm / 1e6:.1f} mm long leaves the route {branch.at_nm / 1e6:.1f} mm from "
                f"the start (an ESD diode, a second receiver). It is not walked, and its loading is not modelled.",
                {"at_mm": round(branch.at_nm / 1e6, 2), "length_mm": round(branch.length_nm / 1e6, 2)},
                _highlight((branch.terminal.item_id,))))
    parts = [{terminal.label.partition(".")[0] for terminal in (route.start, route.end) if "." in terminal.label}
             for route in (result.p, result.n)]
    if len(parts[0]) == len(parts[1]) == 2 and parts[0] != parts[1]:
        found.append(_finding("phase.ends", "info", f"{name}: P and N end on different parts",
                              f"P runs {result.p.start.label} to {result.p.end.label}, N {result.n.start.label} to "
                              f"{result.n.end.label}; the two are matched only by being near each other.",
                              {}, _highlight(result.p.item_ids + result.n.item_ids)))
    return found


def _unmeasured(text: str) -> dict:
    return _finding("phase.unmeasured", "info", text,
                    "Dynamic phase could not measure this pair; the Diff pairs column's pair list says why.", {}, [])


def _finding(check: str, severity: str, title: str, message: str, values: dict, draws: list) -> dict:
    return {"check": check, "severity": severity, "confidence": "high", "title": _cut(title),
            "message": _cut(message), "values": values, "draw": draws}


def _highlight(ids) -> list[dict]:
    targets = [f"uuid:{item}" for item in dict.fromkeys(ids) if item][:MAX_TARGETS]
    return [{"tool": "highlight", "targets": targets}] if targets else []


def _pt(pos, layer_at, layer: str = "") -> str:
    """A point target (mm, KiCad's frame) on the copper layer nearest the point's z."""
    if not layer and layer_at:
        layer = layer_at[min(layer_at, key=lambda height: abs(height - pos[2]))]
    return f"pt:{_mm(pos[0])},{_mm(pos[1])}" + (f"@{layer}" if layer else "")


def _mm(nm: float) -> str:
    text = f"{nm / 1e6:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _cut(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
