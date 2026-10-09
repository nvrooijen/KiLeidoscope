"""Immutable board records. Coordinates and physical lengths are integer nanometres."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Any

Point = tuple[int, int]
Ring = tuple[Point, ...]
Polygon = tuple[Ring, ...]  # outer ring, then holes


@dataclass(frozen=True)
class Track:
    id: str
    layer: str
    net: str
    start: Point
    end: Point
    width: int


@dataclass(frozen=True)
class Arc:
    id: str
    layer: str
    net: str
    start: Point
    mid: Point
    end: Point
    width: int


# A via's protection features (IPC-4761), in this order. Each is 1 (yes), 0 (no) or
# -1 (KiCad's "From design rules": the board's setup decides, `board_specs.via_rules`).
PROTECTION = ("tent_front", "tent_back", "cover_front", "cover_back", "plug_front", "plug_back", "cap", "fill")
FROM_RULES = (-1,) * len(PROTECTION)
# Which copper layers a via has an annular ring on (KiCad's "Annular rings", its padstack's
# unconnected layer removal, numbered as KiCad's IPC `UnconnectedLayerRemoval`): every layer
# it spans (KiCad's default), only those it connects to, those plus its start and end
# layers, or its start and end layers only.
RINGS_ALL, RINGS_CONNECTED, RINGS_ENDS_AND_CONNECTED, RINGS_ENDS = 1, 2, 3, 4


@dataclass(frozen=True)
class Via:
    id: str
    net: str
    pos: Point
    diameter: int
    drill: int
    layer_top: str
    layer_bottom: str
    protection: tuple[int, ...] = FROM_RULES  # per PROTECTION
    rings: int = RINGS_ALL


PASTE_LAYERS = ("F.Paste", "B.Paste")  # stencil apertures, as KiCad reports them per pad


@dataclass(frozen=True)
class Pad:
    id: str
    footprint_id: str
    number: str
    net: str
    pos: Point
    drill: Point | None
    polygons: dict[str, tuple[Polygon, ...]]
    drill_shape: str | None = None  # "round" or "oval"; None without a drill
    drill_angle_rad: float = 0.0

    @property
    def layers(self) -> tuple[str, ...]:
        """Copper layers whose `pads` group carries this pad.

        A non-plated hole (np_thru_hole, e.g. Tag-Connect alignment holes) has no
        copper anywhere; it rides on the outer layers so its drill opening is drawn.
        Any drilled pad rides on F.Cu, whose frame carries every pad drill: copper
        only on other layers (a pad KiCad still answers for as the SMD pad it was,
        see kicad_reader._pad_polygons) must not lose its hole.
        """
        copper = tuple(layer for layer in self.polygons if layer not in PASTE_LAYERS)
        if not (self.drill and min(self.drill) > 0):
            return copper
        if not copper:
            return ("F.Cu", "B.Cu")
        return copper if "F.Cu" in copper else ("F.Cu", *copper)

    @property
    def groups(self) -> frozenset[tuple[str, str]]:
        """Display groups: copper under `pads`, stencil apertures under `paste`."""
        return frozenset({(layer, "pads") for layer in self.layers} |
                         {(layer, "paste") for layer in self.polygons if layer in PASTE_LAYERS})


@dataclass(frozen=True)
class Footprint:
    id: str
    reference: str
    pos: Point
    rotation_rad: float
    side: str
    model_paths: tuple[str, ...]
    bbox_nm: tuple[int, int, int, int] | None = None  # KiCad board-space x, y, width, height
    model_visible: tuple[bool, ...] = ()
    dnp: bool = False  # KiCad's "Do not populate"


@dataclass(frozen=True)
class ZoneFill:
    id: str
    net: str
    layer: str
    polygons: tuple[Polygon, ...]


@dataclass(frozen=True)
class CopperGraphic:
    """Copper drawn as a graphic (a board or footprint shape on a copper layer, e.g. a
    net tie's bridge or a ring around a hole), as filled polygons: one display item each."""
    id: str
    net: str
    layer: str
    polygons: tuple[Polygon, ...]


@dataclass(frozen=True)
class Outline:
    polygons: tuple[Polygon, ...]
    # Only while the outline is malformed: every Edge.Cuts line (open chains included)
    # and the problem locations, so the viewer can show where it is wrong.
    strokes: tuple[tuple[Point, ...], ...] = ()
    problems: tuple[Point, ...] = ()


@dataclass(frozen=True)
class Drawing:
    """A drawing on one of flex mode's user layers (`flex.LAYER_NAMES`): a closed outline
    ("closed"), an open line ("line", arcs sampled) or a text at `points[0]` ("text")."""
    id: str
    layer: str
    kind: str
    points: tuple[Point, ...]
    text: str = ""


@dataclass(frozen=True)
class StackupLayer:
    name: str
    type: str
    thickness_nm: int | None
    material: str | None
    epsilon_r: float | None
    loss_tangent: float | None


@dataclass(frozen=True)
class Stackup:
    layers: tuple[StackupLayer, ...]  # top to bottom


@dataclass(frozen=True)
class BoardSnapshot:
    board_name: str
    layer_display_names: dict[str, str]
    tracks: tuple[Track, ...]
    arcs: tuple[Arc, ...]
    vias: tuple[Via, ...]
    pads: tuple[Pad, ...]
    footprints: tuple[Footprint, ...]
    zones: tuple[ZoneFill, ...]
    outline: Outline
    stackup: Stackup
    warnings: tuple[str, ...]
    read_timings_ms: dict[str, float]
    graphics: tuple[CopperGraphic, ...] = ()
    drawings: tuple[Drawing, ...] = ()
    variant: str = ""  # KiCad's selected assembly variant; "" is the default


def to_jsonable(value: Any) -> Any:
    """Convert records without making tuples or nanometres into floating point values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    return value


def snapshot_from_jsonable(data: dict) -> BoardSnapshot:
    """Inverse of to_jsonable for a BoardSnapshot (used for dump files and fixtures)."""
    def point(value):
        return tuple(int(c) for c in value)

    def polygon(value):
        return tuple(tuple(point(p) for p in ring) for ring in value)

    return BoardSnapshot(
        data["board_name"], dict(data["layer_display_names"]),
        tuple(Track(t["id"], t["layer"], t["net"], point(t["start"]), point(t["end"]), t["width"])
              for t in data["tracks"]),
        tuple(Arc(a["id"], a["layer"], a["net"], point(a["start"]), point(a["mid"]), point(a["end"]), a["width"])
              for a in data["arcs"]),
        tuple(Via(v["id"], v["net"], point(v["pos"]), v["diameter"], v["drill"], v["layer_top"], v["layer_bottom"],
                  tuple(v.get("protection", FROM_RULES)), v.get("rings", RINGS_ALL))  # older dumps: neither
              for v in data["vias"]),
        tuple(Pad(p["id"], p["footprint_id"], p["number"], p["net"], point(p["pos"]),
                  point(p["drill"]) if p["drill"] is not None else None,
                  {layer: tuple(polygon(poly) for poly in polys) for layer, polys in p["polygons"].items()},
                  p["drill_shape"], float(p["drill_angle_rad"]))
              for p in data["pads"]),
        tuple(Footprint(f["id"], f["reference"], point(f["pos"]), f["rotation_rad"], f["side"],
                        tuple(f["model_paths"]),
                        tuple(int(value) for value in f["bbox_nm"]) if f["bbox_nm"] is not None else None,
                        tuple(f["model_visible"]), bool(f.get("dnp", False)))  # older dumps: no dnp
              for f in data["footprints"]),
        tuple(ZoneFill(z["id"], z["net"], z["layer"], tuple(polygon(poly) for poly in z["polygons"]))
              for z in data["zones"]),
        Outline(tuple(polygon(poly) for poly in data["outline"]["polygons"]),
                tuple(tuple(point(p) for p in line) for line in data["outline"].get("strokes", ())),
                tuple(point(p) for p in data["outline"].get("problems", ()))),
        Stackup(tuple(StackupLayer(**layer) for layer in data["stackup"]["layers"])),
        tuple(data["warnings"]), dict(data["read_timings_ms"]),
        tuple(CopperGraphic(g["id"], g["net"], g["layer"], tuple(polygon(poly) for poly in g["polygons"]))
              for g in data.get("graphics", ())),  # dumps from before copper graphics have none
        tuple(Drawing(d["id"], d["layer"], d["kind"], tuple(point(p) for p in d["points"]), d["text"])
              for d in data.get("drawings", ())),  # dumps from before flex mode have none
        variant=data.get("variant", ""),  # dumps from before variants have none
    )
