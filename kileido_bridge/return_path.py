"""Return-path check: where a signal's return current loses its reference plane.

Built on the reference-plane lookup (reference.py). Checked are the nets of
differential pairs (selection.diff_pair_partner) and the nets highlighted from
KiCad's selection, with their partners. Issue kinds:

- "gap": a reference plane has a void, slot or edge under the track.
- "split": the gap lies between planes of different nets (a split plane).
- "no_reference": no copper at all above or below the track.
- "no_return_via": a via moves the signal to another reference layer of the same
  net, with no via or plated hole of that net within `RETURN_VIA_RADIUS_NM` that
  joins both reference layers.
- "reference_change": a via moves the signal between reference planes of different
  nets; the return current then needs a stitching capacitor, which is not checked.

Every side of a track with solid copper along it is a reference
(reference.SegmentReference.references): both planes of a stripline are checked.
"""

import math
from dataclasses import dataclass

from . import model, reference
from .reference import ReferencePlanes, SegmentReference
from .selection import diff_pair_partner

RETURN_VIA_RADIUS_NM = 2_000_000  # a stitching via this close to a signal via carries its return current
MIN_GAP_NM = 100_000  # shorter gaps are raster steps along a plane edge, not voids

Mark = tuple[str, tuple[model.Point, ...], int]  # (layer, polyline to highlight, width); one point: a disc
Area = tuple[str, tuple[reference.Rect, ...]]  # (reference layer, rects of the plane's holes to highlight)


@dataclass(frozen=True)
class Issue:
    kind: str
    net: str
    item: str  # track, arc or via id
    layer: str  # signal layer; for a via, the first layer it connects
    reference: str  # reference layer ("" when there is none)
    at: model.Point  # where a marker goes
    marks: tuple[Mark, ...]
    length_nm: int  # of the uncovered track; 0 for a via
    message: str
    areas: tuple[Area, ...] = ()  # where the reference plane is broken (gaps and splits)


def checked_nets(snapshot: model.BoardSnapshot, selected=frozenset()) -> frozenset[str]:
    """Nets of differential pairs, and the selected nets with their partners."""
    nets = {item.net for item in (*snapshot.tracks, *snapshot.arcs, *snapshot.vias) if item.net}
    chosen = {net for net in selected if net in nets}
    chosen |= {partner for net in chosen if (partner := diff_pair_partner(net, nets))}
    return frozenset(chosen | {net for net in nets if diff_pair_partner(net, nets)})


def _mm(nm: float) -> str:
    return f"{nm / 1e6:.2f} mm"


class ReturnPathCheck:
    """The check over successive snapshots; the lookup caches keep it incremental."""

    def __init__(self, planes: ReferencePlanes | None = None):
        self.planes = planes or ReferencePlanes()
        self._last = (None, ())
        self._issues = {}  # track id -> (SegmentReference, its issues): kept while the lookup is

    def check(self, snapshot: model.BoardSnapshot, nets: frozenset[str]) -> tuple[Issue, ...]:
        # The reader builds a new snapshot every poll but keeps unchanged parts: compare those.
        key = (nets, snapshot.tracks, snapshot.arcs, snapshot.vias, snapshot.pads, snapshot.zones,
               snapshot.outline, snapshot.stackup)
        if key == self._last[0]:
            return self._last[1]
        self.planes.update(snapshot)
        items = sorted((item for item in (*snapshot.tracks, *snapshot.arcs) if item.net in nets),
                       key=lambda item: (item.net, item.layer, item.id))
        found = self.planes.segments([item.id for item in items])
        segments = [found[item.id] for item in items if item.id in found]
        known, self._issues = self._issues, {}
        for segment in segments:
            cached = known.get(segment.id)
            self._issues[segment.id] = (segment, cached[1] if cached and cached[0] is segment
                                        else self._segment_issues(segment))
        issues = [issue for segment in segments for issue in self._issues[segment.id][1]]
        issues += self._via_issues(snapshot, nets, segments)
        issues = tuple(issues)
        self._last = (key, issues)
        return issues

    def _segment_issues(self, segment: SegmentReference) -> list[Issue]:
        references = segment.references
        whole = ((segment.layer, segment.path, segment.width),)
        if not references:
            nearest = min(segment.covers, key=lambda cover: cover.distance_nm, default=None)
            return [Issue("no_reference", segment.net, segment.id, segment.layer,
                          nearest.layer if nearest else "", segment.point_at(segment.length_nm / 2), whole,
                          segment.length_nm,
                          f"{segment.net} on {segment.layer}: no reference plane above or below "
                          f"({_mm(segment.length_nm)} of track)")]
        issues = []
        for cover in references:
            for start, end in cover.gaps:
                if end - start < MIN_GAP_NM:
                    continue
                path = segment.subpath(start, end)
                reach = segment.width / 2 + cover.margin_nm
                around = self.planes.nets_near(cover.layer, path, reach, cover.margin_nm)
                holes = self.planes.plane_breaks(cover.layer, path, reach, around)
                split = len(around) > 1
                where = " | ".join(net or "no net" for net in around)
                message = (f"{segment.net} on {segment.layer}: crosses a split in {cover.layer} ({where})" if split
                           else f"{segment.net} on {segment.layer}: gap in {cover.layer} under {_mm(end - start)}")
                issues.append(Issue("split" if split else "gap", segment.net, segment.id, segment.layer,
                                    cover.layer, segment.point_at((start + end) / 2),
                                    ((segment.layer, path, segment.width),), end - start, message,
                                    ((cover.layer, holes),) if holes else ()))
        return issues

    def _via_issues(self, snapshot, nets, segments: list[SegmentReference]) -> list[Issue]:
        """Vias where the route changes reference layer (module docstring)."""
        by_net: dict[str, list[SegmentReference]] = {}
        for segment in segments:
            by_net.setdefault(segment.net, []).append(segment)
        order = {name: index for index, name in enumerate(self.planes.order)}
        issues = []
        for via in sorted((via for via in snapshot.vias if via.net in nets), key=lambda via: via.id):
            references, layers = set(), []
            for segment in by_net.get(via.net, ()):
                reach = via.diameter / 2 + segment.width / 2
                for end, plane_index in ((segment.path[0], 0), (segment.path[-1], -1)):
                    if math.dist(end, via.pos) > reach:
                        continue
                    layers.append(segment.layer)
                    references |= {(cover.layer, cover.planes[plane_index][2])
                                   for cover in segment.references if cover.planes}
            reference_layers = {layer for layer, _ in references}
            if len(set(layers)) < 2 or len(reference_layers) < 2:
                continue
            marks = tuple((layer, (via.pos,), via.diameter) for layer in dict.fromkeys(layers))
            names = " and ".join(sorted(reference_layers, key=lambda layer: order.get(layer, 0)))
            plane_nets = {net for _, net in references}
            if len(plane_nets) > 1:
                where = ", ".join(f"{layer} ({net or 'no net'})" for layer, net in sorted(references, key=str))
                issues.append(Issue("reference_change", via.net, via.id, layers[0], names, via.pos, marks, 0,
                                    f"{via.net} via: reference changes between nets: {where}; "
                                    "needs a stitching capacitor close to the via"))
                continue
            (plane_net,) = plane_nets
            span = [order.get(layer, 0) for layer in reference_layers]
            if self._return_via(snapshot, via, plane_net, min(span), max(span), order):
                continue
            issues.append(Issue("no_return_via", via.net, via.id, layers[0], names, via.pos, marks, 0,
                                f"{via.net} via: reference moves to {names} with no {plane_net} via "
                                f"within {_mm(RETURN_VIA_RADIUS_NM)}"))
        return issues

    @staticmethod
    def _return_via(snapshot, signal: model.Via, net: str, top: int, bottom: int, order) -> bool:
        """A via or plated hole of `net` near `signal` that joins layers top..bottom."""
        last = len(order) - 1
        for via in snapshot.vias:
            if via.net != net or math.dist(via.pos, signal.pos) > RETURN_VIA_RADIUS_NM:
                continue
            first, final = sorted((order.get(via.layer_top, 0), order.get(via.layer_bottom, last)))
            if first <= top and bottom <= final:
                return True
        return any(pad.net == net and pad.drill and min(pad.drill) > 0 and
                   math.dist(pad.pos, signal.pos) <= RETURN_VIA_RADIUS_NM for pad in snapshot.pads)
