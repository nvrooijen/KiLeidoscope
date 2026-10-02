"""Tracks, arcs and vias from the open board's text, for when KiCad answers busy.

While KiCad's route tool is active (even idle between routes), every item read
(`get_tracks`, `get_vias`, ...) answers busy, but `SaveDocumentToString` still
works (measured on the reference board, KiCad 10.0.3). Committed routes are in
that text, so the bridge reads copper from it until KiCad accepts item reads
again. Same records as `kicad_reader`: integer nm in KiCad's frame, canonical
layer names, net names, KiCad UUIDs.
"""

import re

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


def via_protection(text: str, fallback: tuple[int, ...] = model.FROM_RULES) -> tuple[int, ...]:
    """A via's (or the board setup's) protection features, per `model.PROTECTION`.

    KiCad 10 writes a via's own settings as `(tenting (front yes) (back no))`,
    `(covering ...)`, `(plugging ...)`, `(capping no)`, `(filling yes)`; a via without
    them follows the board's (the same fields in its setup). KiCad 9 wrote the sides
    as bare words, `(tenting front back)`. What is not written takes `fallback`.
    """
    found = list(fallback)
    base = {"tenting": 0, "covering": 2, "plugging": 4}
    for name, body in _SIDED.findall(text):
        sides = dict(_SIDE.findall(body))
        if not sides:  # KiCad 9: the sides named are the ones on
            words = body.split()
            sides = {side: "yes" if side in words else "no" for side in ("front", "back")}
        for offset, side in enumerate(("front", "back")):
            if side in sides and sides[side] != "none":
                found[base[name] + offset] = 1 if sides[side] == "yes" else 0
    for name, value in _SINGLE.findall(text):
        if value != "none":
            found[6 if name == "capping" else 7] = 1 if value == "yes" else 0
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
        numbers = {name: _nm(value) for name, value in _NUMBER.findall(body)}
        net_match = _NET.search(body)
        net = net_match.group(1).replace('\\"', '"').replace("\\\\", "\\") if net_match else ""
        if kind == "via":
            layers = _LAYERS.search(body)
            if "at" not in points or layers is None:
                continue
            vias.append(model.Via(uuid.group(1), net, points["at"], numbers.get("size", 0),
                                  numbers.get("drill", 0), layers.group(1), layers.group(2), via_protection(body)))
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
