"""Tracks, arcs and vias from the open board's text, for when KiCad answers busy.

While KiCad's route tool is active (even idle between routes), every item read
(`get_tracks`, `get_vias`, ...) answers busy, but `SaveDocumentToString` still
works (measured on the reference board, KiCad 10.0.3). Committed routes are in
that text, so the bridge reads copper from it until KiCad accepts item reads
again. Same records as `kicad_reader`: integer nm in KiCad's frame, canonical
layer names, net names, KiCad UUIDs.
"""

import bisect
import re
from dataclasses import replace

from . import model

_ITEM = re.compile(r"\n\t\((segment|arc|via)\b(.*?)\n\t\)", re.S)  # board-level items only
_POINT = re.compile(r"\((start|mid|end|at) (-?[\d.]+) (-?[\d.]+)")
_NUMBER = re.compile(r"\((width|size|drill) ([\d.]+)\)")
_LAYER = re.compile(r'\(layer "([^"]+)"\)')
_LAYERS = re.compile(r'\(layers "([^"]+)" "([^"]+)"')
_NET = re.compile(r'\(net "((?:[^"\\]|\\.)*)"\)')
_UUID = re.compile(r'\(uuid "([^"]+)"\)')
_SIDED = re.compile(r"\((tenting|covering|plugging)\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_SIDE = re.compile(r"\((front|back)\s+(yes|no|none)\)")
_SINGLE = re.compile(r"\((capping|filling)\s+(yes|no|none)\)")
# Where each written field lands in `model.PROTECTION` (a sided one: its front; back is next).
_SIDED_INDEX = {"tenting": 0, "covering": 2, "plugging": 4}
_SINGLE_INDEX = {"capping": 6, "filling": 7}
_RINGS = re.compile(r"\((remove_unused_layers|keep_end_layers|start_end_only)\s+yes\)")
_FOOTPRINT = re.compile(r'\n\s*\(footprint\s+"')
# A footprint's per-variant settings (KiCad 10) or the board's variant list: the name opens the block
_VARIANT = re.compile(r'\(variant\s*\(name "((?:[^"\\]|\\.)*)"\)')
_DNP = re.compile(r"\(dnp\s+(yes|no)\)")


def via_rings(text: str) -> int:
    """A via's annular rings (`model.RINGS_*`) from its text: `(remove_unused_layers yes)`,
    with `(keep_end_layers yes)` for its ends too; nothing written is every layer."""
    found = set(_RINGS.findall(text))
    if "start_end_only" in found:  # assumed KiCad 10 spelling; not yet seen in a saved board
        return model.RINGS_ENDS
    if "remove_unused_layers" in found:
        return model.RINGS_ENDS_AND_CONNECTED if "keep_end_layers" in found else model.RINGS_CONNECTED
    return model.RINGS_ALL


def via_protection(text: str, fallback: tuple[int, ...] = model.FROM_RULES) -> tuple[int, ...]:
    """A via's (or the board setup's) protection features, per `model.PROTECTION`.

    KiCad 10 writes a via's own settings as `(tenting (front yes) (back no))`,
    `(covering ...)`, `(plugging ...)`, `(capping no)`, `(filling yes)`; a via without
    them follows the board's (the same fields in its setup). KiCad 9 wrote the sides
    as bare words, `(tenting front back)`. What is not written takes `fallback`.
    """
    found = list(fallback)
    for name, body in _SIDED.findall(text):
        sides = dict(_SIDE.findall(body))
        if not sides:  # KiCad 9: the sides named are the ones on
            words = body.split()
            sides = {side: "yes" if side in words else "no" for side in ("front", "back")}
        for offset, side in enumerate(("front", "back")):
            if sides.get(side, "none") != "none":
                found[_SIDED_INDEX[name] + offset] = 1 if sides[side] == "yes" else 0
    for name, value in _SINGLE.findall(text):
        if value != "none":
            found[_SINGLE_INDEX[name]] = 1 if value == "yes" else 0
    return tuple(found)


def _nm(value: str) -> int:
    return round(float(value) * 1_000_000)


def copper_items(text: str) -> tuple[tuple[model.Track, ...], tuple[model.Arc, ...], tuple[model.Via, ...]]:
    tracks, arcs, vias = [], [], []
    for match in _ITEM.finditer(text):
        kind, body = match.group(1), match.group(2)
        uuid = _UUID.search(body)
        if uuid is None:
            continue
        points = {name: (_nm(x), _nm(y)) for name, x, y in _POINT.findall(body)}
        numbers = {}  # the first of each: a via's front size (IPC's diameter), not its padstack's per-layer ones
        for name, value in _NUMBER.findall(body):
            numbers.setdefault(name, _nm(value))
        net_match = _NET.search(body)
        net = net_match.group(1).replace('\\"', '"').replace("\\\\", "\\") if net_match else ""
        if kind == "via":
            layers = _LAYERS.search(body)
            if "at" not in points or layers is None:
                continue
            vias.append(model.Via(uuid.group(1), net, points["at"], numbers.get("size", 0),
                                  numbers.get("drill", 0), layers.group(1), layers.group(2), via_protection(body),
                                  via_rings(body)))
            continue
        layer = _LAYER.search(body)
        if layer is None or "start" not in points or "end" not in points:
            continue
        if kind == "arc" and "mid" in points:
            arcs.append(model.Arc(uuid.group(1), layer.group(1), net, points["start"], points["mid"],
                                  points["end"], numbers.get("width", 0)))
        else:
            tracks.append(model.Track(uuid.group(1), layer.group(1), net, points["start"], points["end"],
                                      numbers.get("width", 0)))
    return tuple(tracks), tuple(arcs), tuple(vias)


def _unescaped(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def block(text: str, start: int) -> str:
    """The balanced (...) expression starting at `start` (quotes respected)."""
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("Unclosed KiCad board section")


def variant_dnp(text: str) -> dict[str, dict[str, bool]]:
    """Each assembly variant's DNP overrides, by footprint uuid, from the board text.

    KiCad 10 writes a footprint's per-variant settings inside the footprint as
    `(variant (name "X") (dnp yes))` (the API footprint message carries them only
    from KiCad 10.0.7). A footprint without a block for a variant keeps its own
    `(attr ... dnp)` in it. Each block belongs to the footprint opened last before
    it (layout-independent); the board's own `(variants ...)` list has no `dnp`.
    Runs on every live-copy write, so a board without variants costs one search.
    """
    found: dict[str, dict[str, bool]] = {}
    if "(variant" not in text:
        return found
    starts = [match.start() for match in _FOOTPRINT.finditer(text)]
    uuids: dict[int, str | None] = {}
    for match in _VARIANT.finditer(text):
        owner = bisect.bisect(starts, match.start()) - 1
        if owner < 0:
            continue  # the board's variant list comes before the footprints
        dnp = _DNP.search(block(text, match.start()))
        if dnp is None:
            continue
        if owner not in uuids:  # the footprint's own uuid: the first after its opening
            uuid = _UUID.search(text, starts[owner])
            uuids[owner] = uuid[1] if uuid is not None else None
        if uuids[owner] is not None:
            found.setdefault(_unescaped(match[1]), {})[uuids[owner]] = dnp[1] == "yes"
    return found


def with_variant_dnp(footprints: tuple[model.Footprint, ...], variant: str,
                     overrides: dict[str, dict[str, bool]]) -> tuple[model.Footprint, ...]:
    """The footprints with the selected variant's DNP flags in place of KiCad's defaults."""
    flags = overrides.get(variant) if variant else None
    if not flags:
        return footprints
    return tuple(replace(f, dnp=flags[f.id]) if f.id in flags and flags[f.id] != f.dnp else f
                 for f in footprints)
