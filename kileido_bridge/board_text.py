"""Tracks, arcs and vias from the open board's text, for when KiCad answers busy.

While KiCad's route tool is active (even idle between routes), every item read
(`get_tracks`, `get_vias`, ...) answers busy, but `SaveDocumentToString` still
works (measured on the reference board, KiCad 10.0.3). Committed routes are in
that text, so the bridge reads copper from it until KiCad accepts item reads
again. Same records as `kicad_reader`: integer nm in KiCad's frame, canonical
layer names, net names, KiCad UUIDs.

The same text also gives the stackup's dielectric constants, which the IPC
stackup lacks (`stackup_layers`, for the dynamic-phase delay estimate).
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
# Where each written field lands in `model.PROTECTION` (a sided one: its front; back is next).
_SIDED_INDEX = {"tenting": 0, "covering": 2, "plugging": 4}
_SINGLE_INDEX = {"capping": 6, "filling": 7}
_RINGS = re.compile(r"\((remove_unused_layers|keep_end_layers|start_end_only)\s+yes\)")


def _block(text: str, start: int) -> str:
    """The balanced-parenthesis section starting at `start` (board_specs reads with it too)."""
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


_STACKUP = re.compile(r"\(stackup\b")
_STACKUP_LAYER = re.compile(r'\(layer "([^"]+)"')
_TYPE = re.compile(r'\(type "([^"]*)"\)')
_MATERIAL = re.compile(r'\(material "([^"]*)"\)')
_SUBLAYER_VALUES = re.compile(r"\((thickness|epsilon_r|loss_tangent) ([\d.eE+-]+)")


def stackup_layers(text: str) -> tuple[model.StackupLayer, ...]:
    """The board's stackup, top to bottom, with the dielectric constants KiCad 10.0.3's
    IPC stackup leaves out (kicad_reader._stackup). A dielectric of several sublayers
    (`addsublayer`) becomes one layer: summed thickness, thickness-weighted epsilon_r
    and loss tangent. Empty without a stackup section."""
    marker = _STACKUP.search(text)
    if marker is None:
        return ()
    try:
        stackup = _block(text, marker.start())
    except ValueError:  # a truncated file (read while KiCad saves it)
        return ()
    layers, position = [], len("(stackup")
    while (match := _STACKUP_LAYER.search(stackup, position)) is not None:
        body = _block(stackup, match.start())
        position = match.start() + len(body)
        kind = _TYPE.search(body)
        material = _MATERIAL.search(body)
        thickness, weighted = 0, {"epsilon_r": 0.0, "loss_tangent": 0.0}
        known = {"epsilon_r": True, "loss_tangent": True}
        for part in re.split(r"\baddsublayer\b", body):
            values = {name: float(value) for name, value in _SUBLAYER_VALUES.findall(part)}
            part_nm = round(values.get("thickness", 0.0) * 1_000_000)
            thickness += part_nm
            for name in weighted:
                if name in values:
                    weighted[name] += values[name] * part_nm
                else:
                    known[name] = False
        layer_type = kind[1].lower() if kind else ""
        dielectric = layer_type in ("core", "prepreg", "dielectric")
        layers.append(model.StackupLayer(
            match[1], "dielectric" if dielectric else layer_type, thickness or None,
            material[1] if material else None,
            *(weighted[name] / thickness if dielectric and known[name] and thickness else None
              for name in ("epsilon_r", "loss_tangent"))))
    return tuple(layers)


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
