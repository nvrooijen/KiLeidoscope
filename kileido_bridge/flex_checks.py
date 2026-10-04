"""Flex mode's design checks on a rigid-flex board, after IPC-2223's guidance.

Each finding says what is wrong, where (KiCad nm), and which items to select in KiCad.
Bends are numbered from 1 in `FlexModel.bends` order, as the panel lists them.

- a trace crossing a bend at an angle instead of square to it
- a via, pad or part on a bend (the bend's curved area and a margin either side)
- copper on a layer that does not continue into the flex
- a bend radius too tight for the flex's thickness (static and dynamic use differ)
- a sharp inside corner of the outline at the flex
- traces stacked on two flex layers (they stiffen the flex and crack)
- a solid pour across a bend (hatch it there)
- a stiffener reaching into a bend
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import flex
from .flex import COVERLAY_NM, DYNAMIC_RATIO, STATIC_RATIO, STATIC_RATIO_MULTI  # noqa: F401 (shared with flex.py)
from .model import BoardSnapshot, Point, Polygon, to_jsonable

BEND_MARGIN_NM = 500_000  # kept clear either side of a bend's curved area
SQUARE_DEG = 10.0  # a trace may cross a bend this far off square
PARALLEL_DEG = 5.0
STACKED_NM = 1_000_000  # traces on two flex layers overlapping for this long are stacked
SHARP_DEG = 30.0  # an outline corner turning this much at once is sharp (sampled arcs turn less)
NEAR_FLEX_NM = 200_000
SOLID = 0.85  # a pour covering this share of a bend's area is solid, not hatched


@dataclass(frozen=True)
class Finding:
    message: str
    where: Point
    items: tuple[str, ...] = ()  # KiCad ids to select
    use: str = ""  # "static" or "dynamic": only for that use; "" for both
    group: str = ""  # findings of one kind, as the panel folds them up: "Parts on dome 2"


def limits(copper_layers: int) -> dict:
    """{"static": ratio, "dynamic": ratio or None} for a flex of `copper_layers` layers."""
    static = STATIC_RATIO.get(copper_layers, STATIC_RATIO_MULTI)
    return {"static": static, "dynamic": DYNAMIC_RATIO.get(copper_layers)}


def total_thickness_nm(model: flex.FlexModel) -> int:
    return model.thickness_nm + 2 * COVERLAY_NM


def check(snapshot: BoardSnapshot, model: flex.FlexModel) -> tuple[Finding, ...]:
    bands = _bands(model, total_thickness_nm(model))
    references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
    findings: list[Finding] = []
    findings += _angled(snapshot, model, bands)
    findings += _on_bends(snapshot, bands, references)
    findings += _off_flex_layers(snapshot, model, references)
    findings += _radii(model)
    findings += _sharp_corners(snapshot, model)
    findings += _stacked(snapshot, model)
    findings += _solid_pours(snapshot, model, bands)
    findings += _stiffeners(model, bands)
    return tuple(dict.fromkeys(findings))  # a part across two fingers of a dome: once


def _bands(model: flex.FlexModel, thickness_nm: int) -> list[tuple[int, "_Band"]]:
    """(bend index, band) for every bend; a dome has one per finger."""
    bands = []
    for index, bend in enumerate(model.bends):
        if bend.kind != "dome":
            bands.append((index, _Band(bend, thickness_nm)))
            continue
        for finger in bend.fingers:
            # A finger curls from its chord to its tip: the band is centred half way.
            curl = flex.Bend(bend.id, finger.start, finger.end, finger.angle_deg, finger.radius_nm, kind="dome")
            band = _Band(curl, thickness_nm)
            shift = finger.side * band.half
            band.start, band.end = band.point(0, shift), band.point(band.length, shift)
            bands.append((index, band))
    return bands


class _Band:
    """A bend's chord with its curved area, (R + t / 2) * angle long, and margins."""

    def __init__(self, bend: flex.Bend, thickness_nm: int):
        self.start, self.end = bend.start, bend.end
        dx, dy = bend.end[0] - bend.start[0], bend.end[1] - bend.start[1]
        self.length = math.hypot(dx, dy) or 1.0
        self.along = (dx / self.length, dy / self.length)
        self.normal = (-self.along[1], self.along[0])
        self.name = bend.name  # "Bend", "Twist", "Cone" or "Dome", for the messages
        self.area = bend.area  # drawn as its area: that is where it curves
        self.closed = bend.closed  # a wrap: no chord
        if bend.kind == "twist":
            self.half = bend.length_nm / 2
        elif bend.area is not None:
            self.half = 0.0  # `holds` and `meets_box` use the area
        else:
            self.half = (bend.radius_nm + thickness_nm / 2) * math.radians(abs(bend.angle_deg)) / 2

    def local(self, point) -> tuple[float, float]:
        x, y = point[0] - self.start[0], point[1] - self.start[1]
        return x * self.along[0] + y * self.along[1], x * self.normal[0] + y * self.normal[1]

    def point(self, u: float, v: float) -> tuple[float, float]:
        return (self.start[0] + u * self.along[0] + v * self.normal[0],
                self.start[1] + u * self.along[1] + v * self.normal[1])

    def holds(self, point, margin: float = BEND_MARGIN_NM) -> bool:
        if self.area is not None:
            return flex._inside(point, self.area) or min(
                flex._segment_distance(point, p, q) for p, q in flex._edges(self.area)) <= margin
        u, v = self.local(point)
        return 0 <= u <= self.length and abs(v) <= self.half + margin

    def meets_box(self, corners, margin: float = BEND_MARGIN_NM) -> bool:
        if self.area is not None:
            return any(self.holds(corner, margin) for corner in corners) or any(
                flex._inside(p, tuple(corners)) for p in self.area)
        local = [self.local(corner) for corner in corners]
        us, vs = [u for u, _ in local], [v for _, v in local]
        return max(us) >= 0 and min(us) <= self.length and max(vs) >= -self.half - margin and \
            min(vs) <= self.half + margin


def _segments(snapshot: BoardSnapshot):
    """Tracks, and arcs as their chords: (id, layer, net, start, end, width)."""
    for item in snapshot.tracks:
        yield item.id, item.layer, item.net, item.start, item.end, item.width
    for item in snapshot.arcs:
        yield item.id, item.layer, item.net, item.start, item.mid, item.width
        yield item.id, item.layer, item.net, item.mid, item.end, item.width


def _crossing(a, b, c, d) -> tuple[float, float] | None:
    """Where segments ab and cd cross, or None."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    denominator = r[0] * s[1] - r[1] * s[0]
    if denominator == 0:
        return None
    w = (c[0] - a[0], c[1] - a[1])
    t = (w[0] * s[1] - w[1] * s[0]) / denominator
    u = (w[0] * r[1] - w[1] * r[0]) / denominator
    if 0 <= t <= 1 and 0 <= u <= 1:
        return a[0] + t * r[0], a[1] + t * r[1]
    return None


def _point(value) -> Point:
    return round(value[0]), round(value[1])


def _in_zone(point, model: flex.FlexModel) -> bool:
    return any(flex._inside(point, zone) for zone in model.zones)


def _touches_zone(a, b, model: flex.FlexModel):
    """The first point of segment ab inside a flex zone, or None."""
    if _in_zone(a, model):
        return a
    for zone in model.zones:
        for p, q in flex._edges(zone):
            if (where := _crossing(a, b, p, q)) is not None:
                return where
    return b if _in_zone(b, model) else None


# --- The checks -----------------------------------------------------------------------------

def _angled(snapshot, model, bands) -> list[Finding]:
    found: dict[tuple[str, int], tuple[float, tuple, list[str]]] = {}
    for item_id, layer, net, a, b, _ in _segments(snapshot):
        if layer not in model.layers or a == b:
            continue
        for index, band in bands:
            if band.name not in ("Bend", "Dome"):
                continue  # a twist turns along the traces; a cone's generators fan out
            where = _crossing(a, b, band.start, band.end)
            if where is None:
                continue
            length = math.dist(a, b)
            cosine = abs((b[0] - a[0]) * band.normal[0] + (b[1] - a[1]) * band.normal[1]) / length
            off = math.degrees(math.acos(min(1.0, cosine)))
            if off > SQUARE_DEG:
                worst, at, ids = found.get((net, index), (0.0, where, []))
                found[(net, index)] = (max(worst, off), at, ids + [item_id])
    return [Finding(f"{net or 'A trace'} crosses bend {index + 1} at {off:.0f}° off square; cross it square",
                    _point(at), tuple(dict.fromkeys(ids)), group=f"Traces crossing bend {index + 1} at an angle")
            for (net, index), (off, at, ids) in found.items()]


def _on_bends(snapshot, bands, references) -> list[Finding]:
    findings = []
    for via in snapshot.vias:
        for index, band in bands:
            if band.holds(via.pos):
                findings.append(Finding(f"Via{f' ({via.net})' if via.net else ''} on {band.name.lower()} {index + 1}",
                                        via.pos, (via.id,), group=f"Vias on {band.name.lower()} {index + 1}"))
    pads_by_footprint: dict[str, list] = {}
    for pad in snapshot.pads:
        pads_by_footprint.setdefault(pad.footprint_id, []).append(pad)
    for footprint in snapshot.footprints:
        pads = pads_by_footprint.get(footprint.id, [])
        if footprint.bbox_nm is not None:
            x, y, w, h = footprint.bbox_nm
            corners = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
        else:
            corners = tuple(pad.pos for pad in pads) or (footprint.pos,)
        for index, band in bands:
            if band.meets_box(corners):
                findings.append(Finding(f"{footprint.reference} is on {band.name.lower()} {index + 1}", footprint.pos,
                                        (footprint.id,), group=f"Parts on {band.name.lower()} {index + 1}"))
    for pad in snapshot.pads:
        if pad.footprint_id in references:
            continue  # its footprint answers for it
        for index, band in bands:
            if band.holds(pad.pos):
                findings.append(Finding(f"Pad {pad.number} on {band.name.lower()} {index + 1}", pad.pos, (pad.id,),
                                        group=f"Pads on {band.name.lower()} {index + 1}"))
    return findings


def _off_flex_layers(snapshot, model, references) -> list[Finding]:
    if not model.layers or not model.zones:
        return []
    rigid_only = [entry.name for entry in snapshot.stackup.layers
                  if entry.type == "copper" and entry.name not in model.layers]
    rigid = set(rigid_only)
    found: dict[tuple[str, str], tuple[tuple, list[str]]] = {}
    for item_id, layer, net, a, b, _ in _segments(snapshot):
        if layer in rigid and (where := _touches_zone(a, b, model)) is not None:
            at, ids = found.get((net, layer), (where, []))
            found[(net, layer)] = (at, ids + [item_id])
    rigid_copper = "Copper on layers the flex does not have"
    findings = [Finding(f"{net or 'A trace'} runs on {layer} into the flex; {layer} does not continue there",
                        _point(at), tuple(dict.fromkeys(ids)), group=rigid_copper)
                for (net, layer), (at, ids) in found.items()]
    order = [entry.name for entry in snapshot.stackup.layers if entry.type == "copper"]
    for via in snapshot.vias:
        if via.layer_top not in order or via.layer_bottom not in order or not _in_zone(via.pos, model):
            continue
        span = order[order.index(via.layer_top):order.index(via.layer_bottom) + 1]
        reached = [layer for layer in span if layer in rigid]
        if reached:
            findings.append(Finding(f"Via{f' ({via.net})' if via.net else ''} in the flex reaches {reached[0]}",
                                    via.pos, (via.id,), group=rigid_copper))
    for pad in snapshot.pads:
        layers = [layer for layer in pad.layers if layer in rigid]
        if layers and _in_zone(pad.pos, model):
            owner = references.get(pad.footprint_id)
            name = f"{owner}.{pad.number}" if owner else pad.number
            findings.append(Finding(f"Pad {name} on {layers[0]} is in the flex", pad.pos,
                                    (pad.footprint_id or pad.id,), group=rigid_copper))
    for fill in snapshot.zones:
        if fill.layer not in rigid:
            continue
        where = next((p for polygon in fill.polygons for p in polygon[0] if _in_zone(p, model)), None)
        if where is not None:
            findings.append(Finding(f"{fill.net or 'A'} pour on {fill.layer} reaches into the flex", where,
                                    (fill.id,), group=rigid_copper))
    return findings


def _radii(model) -> list[Finding]:
    thickness = total_thickness_nm(model)
    count = len(model.layers)
    found = limits(count)
    findings = []
    if model.bends and found["dynamic"] is None and count:
        findings.append(Finding(f"Dynamic flex should have 1 or 2 copper layers; this one has {count}",
                                model.bends[0].start, use="dynamic"))
    for index, bend in enumerate(model.bends):
        if bend.kind == "twist":
            continue  # no radius: it turns, it does not roll
        ratio = bend.radius_nm / thickness
        for use in ("static", "dynamic"):
            need = found[use]
            if need is not None and ratio < need:
                findings.append(Finding(
                    f"{bend.name} {index + 1}: R{round(bend.radius_nm / 1e6, 2):g} mm"
                    f"{' at its narrow end' if bend.kind == 'cone' else ''} is {ratio:.1f}× the flex's "
                    f"{thickness / 1000:g} µm; {use} flex needs {need}× ({need * thickness / 1e6:.2f} mm)",
                    _point(flex._lerp(bend.start, bend.end, 0.5)), use=use))
    return findings


def _sharp_corners(snapshot, model) -> list[Finding]:
    if not model.zones:
        return []
    rings = [ring for polygon in snapshot.outline.polygons for ring in polygon[:1]]
    findings = []
    for ring in rings:
        depth = sum(flex._inside(ring[0], other) for other in rings if other is not ring)
        sign = (1 if flex._area(ring) > 0 else -1) * (1 if depth % 2 == 0 else -1)  # material on the left: +1
        count = len(ring)
        for index, corner in enumerate(ring):
            before, after = ring[index - 1], ring[(index + 1) % count]
            first = (corner[0] - before[0], corner[1] - before[1])
            second = (after[0] - corner[0], after[1] - corner[1])
            cross = first[0] * second[1] - first[1] * second[0]
            dot = first[0] * second[0] + first[1] * second[1]
            turn = math.degrees(math.atan2(abs(cross), dot))
            if cross * sign >= 0 or turn < SHARP_DEG:  # turns the material's way (a convex corner), or gently
                continue
            if _in_zone(corner, model) or any(flex._segment_distance(corner, p, q) <= NEAR_FLEX_NM
                                              for zone in model.zones for p, q in flex._edges(zone)):
                findings.append(Finding("Sharp inside corner at the flex; give it a radius so it cannot tear",
                                        corner, group="Sharp inside corners at the flex"))
    return findings


def _stacked(snapshot, model) -> list[Finding]:
    if len(model.layers) < 2:
        return []
    order = {name: index for index, name in enumerate(model.layers)}
    segments = [s for s in _segments(snapshot) if s[1] in order and s[3] != s[4]]
    found: dict[tuple, tuple] = {}
    for i, (id_a, layer_a, net_a, a0, a1, width_a) in enumerate(segments):
        length = math.dist(a0, a1)
        along = ((a1[0] - a0[0]) / length, (a1[1] - a0[1]) / length)
        for id_b, layer_b, net_b, b0, b1, width_b in segments[i + 1:]:
            if layer_b == layer_a:
                continue
            other = math.dist(b0, b1)
            cosine = abs((b1[0] - b0[0]) * along[0] + (b1[1] - b0[1]) * along[1]) / other
            if math.degrees(math.acos(min(1.0, cosine))) > PARALLEL_DEG:
                continue
            offsets = [(p[0] - a0[0]) * -along[1] + (p[1] - a0[1]) * along[0] for p in (b0, b1)]
            if min(abs(v) for v in offsets) > (width_a + width_b) / 2:
                continue
            spans = sorted((p[0] - a0[0]) * along[0] + (p[1] - a0[1]) * along[1] for p in (b0, b1))
            low, high = max(0.0, spans[0]), min(length, spans[1])
            middle = (a0[0] + along[0] * (low + high) / 2, a0[1] + along[1] * (low + high) / 2)
            if high - low < STACKED_NM or not _in_zone(middle, model):
                continue
            upper, lower = sorted(((layer_a, net_a, id_a), (layer_b, net_b, id_b)), key=lambda e: order[e[0]])
            key = (upper[:2], lower[:2])
            found.setdefault(key, (middle, []))[1].extend([upper[2], lower[2]])
    return [Finding(f"{lower[1] or 'A trace'} ({lower[0]}) runs under {upper[1] or 'a trace'} ({upper[0]}) "
                    f"through the flex; stagger them", _point(middle), tuple(dict.fromkeys(ids)),
                    group="Traces stacked on two flex layers")
            for (upper, lower), (middle, ids) in found.items()]


def _covers(point, polygons: tuple[Polygon, ...]) -> bool:
    return any(flex._inside(point, polygon[0]) and not any(flex._inside(point, hole) for hole in polygon[1:])
               for polygon in polygons)


def _solid_pours(snapshot, model, bands) -> list[Finding]:
    findings = []
    for index, band in bands:
        if band.closed:
            continue  # a wrap curves gently all over; no chord to sample along
        samples = [band.point(band.length * (i + 0.5) / 24, band.half * (2 * (j + 0.5) / 8 - 1))
                   for i in range(24) for j in range(8)]
        samples = [p for p in samples if _in_zone(p, model)]
        if not samples:
            continue
        for fill in snapshot.zones:
            if fill.layer not in model.layers:
                continue
            covered = sum(_covers(p, fill.polygons) for p in samples) / len(samples)
            if covered >= SOLID:
                pour = f"{fill.net} pour" if fill.net else "pour"
                findings.append(Finding(f"Solid {pour} on {fill.layer} across {band.name.lower()} {index + 1}; "
                                        f"hatch it in the flex",
                                        _point(band.point(band.length / 2, 0)), (fill.id,),
                                        group=f"Solid pours across {band.name.lower()} {index + 1}"))
    return findings


def _stiffeners(model, bands) -> list[Finding]:
    findings = []
    for stiffener in model.stiffeners:
        for index, band in bands:
            corners = stiffener.ring
            if any(band.holds(p) for p in corners) or any(
                    _crossing(band.point(0, side * (band.half + BEND_MARGIN_NM)),
                              band.point(band.length, side * (band.half + BEND_MARGIN_NM)), p, q)
                    for side in (-1, 1) for p, q in flex._edges(corners)):
                findings.append(Finding(f"A stiffener reaches into {band.name.lower()} {index + 1}", corners[0],
                                        (stiffener.id,), group="Stiffeners reaching into bends"))
    return findings


def _fold_frame(model: flex.FlexModel, index: int, thickness_nm: int) -> dict:
    """What folding a bend needs: the regions it joins, which side of its chord the child
    is on (+1: towards (-dy, dx) of start to end, in KiCad's coordinates; -1: away), and the
    width of its curved strip, (R + t / 2) * angle on the flex's middle plane."""
    bend = model.bends[index]
    if bend.closed:
        return {"parent": _wrapped(model, bend), "child": None, "side": 0, "width_nm": 0}
    if bend.kind == "dome":
        fingers = []
        for finger in bend.fingers:
            probe = flex.Bend("", finger.start, finger.end, 0.0, 0)
            child = model.region_at(flex._rounded(flex._beside(probe, finger.side)))
            width = (finger.radius_nm + thickness_nm / 2) * math.radians(abs(finger.angle_deg))
            fingers.append(dict(to_jsonable(finger), child=child, width_nm=round(width)))
        parent = next((model.regions[f["child"]].parent for f in fingers if f["child"] is not None), None)
        return {"parent": parent, "child": None, "side": 0, "width_nm": 0, "fingers": fingers}
    child = next((k for k, region in enumerate(model.regions) if region.bend == index), None)
    parent = model.regions[child].parent if child is not None else None
    side = 0
    if child is not None:
        side = next((s for s in (1, -1) if model.region_at(flex._rounded(flex._beside(bend, s))) == child), 0)
    if bend.kind == "twist":
        width = bend.length_nm  # it turns along its whole line
    elif bend.kind == "cone":
        width = 0  # its sides say where it curves (foldmath)
    else:  # a 0° bend (to be set) curves as a 90° one would when its handle turns
        width = (bend.radius_nm + thickness_nm / 2) * math.radians(abs(bend.angle_deg) or flex.DEFAULT_ANGLE)
    return {"parent": parent, "child": child, "side": side, "width_nm": round(width)}


def _wrapped(model: flex.FlexModel, bend: flex.Bend) -> int | None:
    """The region a wrap bends: of those with board in its wedge, the nearest the root
    (then the largest)."""
    def depth(k):
        count = 0
        while model.regions[k].parent is not None:
            k, count = model.regions[k].parent, count + 1
        return count
    inside = [k for k, region in enumerate(model.regions) if any(flex._inside(p, bend.area) for p in region.ring)]
    return min(inside, key=lambda k: (depth(k), -abs(flex._area(model.regions[k].ring))), default=None)


def _cone_sides(model: flex.FlexModel, index: int) -> dict:
    """A cone's sides in fold order: the one on its parent's side first (foldmath turns
    the strip from it to the other)."""
    bend = model.bends[index]
    if bend.kind != "cone":
        return {}
    child = next((k for k, region in enumerate(model.regions) if region.bend == index), None)
    first, second = bend.sides
    if child is not None and model.region_at(flex._rounded(flex._lerp(*first, 0.5))) == child:
        first, second = second, first
    return {"sides": [list(map(list, first)), list(map(list, second))]}


def report(snapshot: BoardSnapshot, stack: dict) -> dict | None:
    """What Blender's panel shows (JSON-ready): the flex stack, the bends with their ratio
    limits, the setup problems and the findings. None for a board without flex."""
    model = flex.build(snapshot, stack)
    if model is None:
        return None
    thickness = total_thickness_nm(model)
    numbered = len({bend.step for bend in model.bends}) > 1  # steps matter only with more than one
    return {"layers": list(model.layers), "thickness_nm": model.thickness_nm, "coverlay_nm": COVERLAY_NM,
            "total_nm": thickness, "limits": limits(len(model.layers)),
            "zones": [to_jsonable(zone) for zone in model.zones],
            "regions": [to_jsonable(region) for region in model.regions],
            "bends": [dict(to_jsonable(bend), **_fold_frame(model, index, thickness),
                           note=flex.bend_note(bend.angle_deg, bend.radius_nm, bend.step if numbered else None,
                                               bend.kind, bend.area is not None, bend.closed),
                           **_cone_sides(model, index))
                      for index, bend in enumerate(model.bends)],
            "coverlay": model.coverlay,
            "stiffeners": [dict(to_jsonable(stiffener), note=flex.stiffener_note(
                stiffener.material, stiffener.thickness_nm, stiffener.side)) for stiffener in model.stiffeners],
            "problems": [to_jsonable(problem) for problem in model.problems],
            "findings": [to_jsonable(finding) for finding in check(snapshot, model)]}
