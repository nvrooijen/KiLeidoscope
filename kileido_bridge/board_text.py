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
from .board_specs import _block  # the one balanced-parenthesis reader

_ITEM = re.compile(r"\n\t\((segment|arc|via)\b(.*?)\n\t\)", re.S)  # board-level items only
_POINT = re.compile(r"\((start|mid|end|at) (-?[\d.]+) (-?[\d.]+)")
_NUMBER = re.compile(r"\((width|size|drill) ([\d.]+)\)")
_LAYER = re.compile(r'\(layer "([^"]+)"\)')
_LAYERS = re.compile(r'\(layers "([^"]+)" "([^"]+)"')
_NET = re.compile(r'\(net "((?:[^"\\]|\\.)*)"\)')
_UUID = re.compile(r'\(uuid "([^"]+)"\)')


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
        numbers = {name: _nm(value) for name, value in _NUMBER.findall(body)}
        net_match = _NET.search(body)
        net = net_match.group(1).replace('\\"', '"').replace("\\\\", "\\") if net_match else ""
        if kind == "via":
            layers = _LAYERS.search(body)
            if "at" not in points or layers is None:
                continue
            vias.append(model.Via(uuid.group(1), net, points["at"], numbers.get("size", 0),
                                  numbers.get("drill", 0), layers.group(1), layers.group(2)))
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
