"""Target strings of a findings file, resolved on the board.

`U3` (part), `U3.4` (pad), `net:GND`, `net:GND@In1.Cu`, `net:USB_D+@F.Cu~42.10,18.75`
(the one item of that net nearest the point), `layer:In1.Cu`, `uuid:5a222dbd` (a uuid's
last 8 hex, or all of it), `pt:42.10,18.75@F.Cu` (snapped to copper within 0.5 mm) and
`edge`. Coordinates are millimetres in KiCad's board frame (y down), as in the board file.
A target that names nothing resolves with no items and a note saying why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import model
from .board_index import BoardIndex
from .model import Point

SNAP_NM = 500_000  # `pt:` snaps to copper this close
_NUMBER = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
_POINT = re.compile(rf"\s*({_NUMBER})\s*,\s*({_NUMBER})\s*")
# Net names can hold "@" and "~" ("/~{RST}"), so suffixes are only split off when they
# parse as a copper layer and a point; a name that is a net as written wins.
_SUFFIXES = re.compile(rf"^(?P<name>.*?)(?:\s*@\s*(?P<layer>(?:[FfBb]|[Ii][Nn]\d+)\.[Cc][Uu]))?"
                       rf"(?:\s*~\s*(?P<point>{_NUMBER}\s*,\s*{_NUMBER}))?\s*$")
_COPPER = re.compile(r"\s*(?:([FfBb])|[Ii][Nn](\d+))\.[Cc][Uu]\s*")

_KIND = {model.Track: "track", model.Arc: "arc", model.Via: "via", model.Pad: "pad",
         model.ZoneFill: "zone", model.CopperGraphic: "copper shape", model.Footprint: "part",
         model.Outline: "board edge"}


@dataclass(frozen=True)
class Resolved:
    target: str
    items: tuple = ()
    layer: str | None = None
    point: Point | None = None  # nm; the named point, when the target has one
    note: str = ""  # why nothing was found, or what was assumed

    @property
    def found(self) -> bool:
        return bool(self.items) or (self.point is not None and self.target.strip().lower().startswith("pt:"))


def kind(record) -> str:
    return _KIND.get(type(record), type(record).__name__.lower())


def _mm(nm: float) -> str:
    return f"{nm / 1e6:.2f} mm"


def _parse_point(text: str) -> Point | None:
    match = _POINT.fullmatch(text)
    if not match:
        return None
    return (round(float(match.group(1)) * 1e6), round(float(match.group(2)) * 1e6))


def copper_layer(text: str) -> str | None:
    """KiCad's spelling of a copper layer name in any case ("f.cu" -> "F.Cu"), else None."""
    match = _COPPER.fullmatch(text)
    if not match:
        return None
    return f"{match.group(1).upper()}.Cu" if match.group(1) else f"In{int(match.group(2))}.Cu"


def _split(rest: str, names) -> tuple[str, str | None, Point | None]:
    """(name, layer, point) of `name[@layer][~x,y]`; a known name is taken whole."""
    rest = rest.strip()
    if rest in names:
        return rest, None, None
    match = _SUFFIXES.match(rest)
    point = _parse_point(match.group("point")) if match.group("point") else None
    layer = copper_layer(match.group("layer")) if match.group("layer") else None
    return match.group("name").strip(), layer, point


def _pick(index: BoardIndex, target: str, items, layer, point, what: str) -> Resolved:
    """`items` as found, or the one nearest `point` when the target gives one."""
    if not items:
        return Resolved(target, (), layer, point, f"{what} not found on board")
    if point is None:
        return Resolved(target, tuple(items), layer)
    near = index.nearest(point, items, layer)
    if near is None:
        return Resolved(target, (), layer, point, f"{what} has no copper to measure")
    note = "" if near.gap.distance <= SNAP_NM else \
        f"nearest {kind(near.record)} of {what} is {_mm(near.gap.distance)} from the point"
    return Resolved(target, (near.record,), layer, point, note)


def resolve(index: BoardIndex, target: str) -> Resolved:
    text = target.strip()
    if text == "edge":
        return Resolved(target, (index.snapshot.outline,))
    head, colon, rest = text.partition(":")
    head = head.strip().lower()
    if colon and head == "net":
        name, layer, point = _split(rest, index.nets)
        return _pick(index, target, index.net_items(name, layer), layer, point,
                     f"net {name}" + (f" on {layer}" if layer else ""))
    if colon and head == "layer":
        name, _, point = _split(rest, ())
        layer = copper_layer(name)
        if layer is None:
            return Resolved(target, note=f"{name} is not a copper layer")
        return _pick(index, target, index.layer_items(layer), layer, point, f"copper on {layer}")
    if colon and head == "uuid":
        rest = rest.strip().strip("{}")
        ids = index.ids_ending(rest)
        if len(ids) > 1:
            return Resolved(target, note=f"{len(ids)} items end in {rest}; give more of the uuid")
        if not ids:
            return Resolved(target, note=f"no item with uuid {rest}")
        return Resolved(target, index.records(ids[0]))
    if colon and head == "pt":
        where, _, layer = rest.partition("@")
        point = _parse_point(where)
        if point is None:
            return Resolved(target, note="point is not x,y in mm")
        if layer.strip():
            name, layer = layer.strip(), copper_layer(layer)
            if layer is None:  # silkscreen, courtyard, mask: the point itself is the target
                return Resolved(target, (), name, point)
        else:
            layer = None
        candidates = index.layer_items(layer) if layer else index.copper
        near = index.nearest(point, candidates, layer, max_distance=SNAP_NM)
        if near is None:
            return Resolved(target, (), layer, point, f"no copper within {_mm(SNAP_NM)}")
        where = getattr(near.record, "layer", None) if layer is None else None
        note = (f"snapped to {kind(near.record)}" + (f" on {where}" if where else "") +
                f" {_mm(near.gap.distance)} away") if near.gap.distance or where else ""
        return Resolved(target, (near.record,), layer, point, note)
    footprints = index.footprints(text)
    if footprints:
        note = f"{len(footprints)} parts share reference {text}" if len(footprints) > 1 else ""
        return Resolved(target, footprints, note=note)
    reference, dot, number = text.partition(".")
    if dot:
        pads = index.pads(reference, number)
        if pads:
            return Resolved(target, pads)
        if index.footprints(reference):
            return Resolved(target, note=f"{reference} has no pad {number}")
    return Resolved(target, note=f"{text} not found on board")
