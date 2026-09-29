"""Differential-pair channels and each side's routed path, terminal to terminal.

A pair is two nets named by KiCad's rule (`selection.diff_pair_partner`). With
series parts followed, pairs joined by a 2-pad part on each side (AC-coupling
caps: TX_P -> C1 -> TX_C_P and TX_N -> C2 -> TX_C_N) form one channel.

Each side's copper is a graph: track and arc ends are nodes, joined where they
touch (an end inside another track's copper, as KiCad connects them), and vias
and pads are nodes their touching ends connect to. Terminals are the pads at the
ends of the routed path, pads with one connection; with more than two (a stub to
an ESD diode, multi-drop), the two farthest apart along the copper are the route's
ends and the others are branches. The route is the shortest path between them.

Lengths follow the copper from where the route leaves the start pad to where it
enters the end pad. A via or plated hole adds its barrel between the two layers
it connects (from `protocol.layer_heights_nm`); a pad or series part in the middle
counts as the straight line between where the tracks meet it.
"""

import heapq
import math
from dataclasses import dataclass, replace

import numpy as np

from . import model
from .geometry import sample_arc
from .selection import diff_pair_partner

CONTACT_NM = 1_000  # ends this close always meet, also between zero-width items
PAD_FALLBACK_NM = 50_000  # a pad without copper shapes (old dumps): ends this close to its centre


@dataclass(frozen=True)
class Terminal:
    label: str  # "U1.A5", or "track end" for an unfinished route
    item_id: str  # the pad (KiCad selects its footprint), else the last track
    pos: model.Point
    layer: str


@dataclass(frozen=True)
class Piece:
    """One stretch of a route, in walking order."""
    kind: str  # "track" (a track or arc), "hop" (straight across a pad or part), "barrel"
    item_id: str
    layer: str
    to_layer: str  # a barrel's far layer; else the same as `layer`
    points: tuple[model.Point, ...]
    length_nm: float  # along the copper; a barrel's height between its two layers
    width: int = 0
    span: tuple[str, str] = ("", "")  # a barrel's via or hole layers (top, bottom)
    net: str = ""


@dataclass(frozen=True)
class Branch:
    terminal: Terminal
    at_nm: float  # along the route, where the branch leaves it
    length_nm: float


@dataclass(frozen=True)
class Route:
    start: Terminal
    end: Terminal
    pieces: tuple[Piece, ...]
    branches: tuple[Branch, ...] = ()
    complete: bool = True  # every pad of the nets is on the route's copper

    @property
    def length_nm(self) -> float:
        return sum(piece.length_nm for piece in self.pieces)

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(piece.item_id for piece in self.pieces if piece.item_id))

    def reversed(self) -> "Route":
        """The same route walked from its other end."""
        total = self.length_nm
        pieces = tuple(replace(piece, layer=piece.to_layer, to_layer=piece.layer, points=piece.points[::-1])
                       for piece in reversed(self.pieces))
        branches = tuple(replace(branch, at_nm=total - branch.at_nm) for branch in self.branches)
        return Route(self.end, self.start, pieces, branches, self.complete)


@dataclass(frozen=True)
class Link:
    """A series part joining two nets of one side: a 2-pad part's pads."""
    footprint_id: str
    reference: str
    pads: tuple[str, str]  # pad ids, in channel order


@dataclass(frozen=True)
class Channel:
    key: str  # stable id: the P nets, in channel order
    name: str  # "USB_D±", "TX_P/N → TX_C_P/N"
    p_nets: tuple[str, ...]
    n_nets: tuple[str, ...]
    p_links: tuple[Link, ...] = ()
    n_links: tuple[Link, ...] = ()

    @property
    def nets(self) -> tuple[str, ...]:
        return self.p_nets + self.n_nets


def pair_name(p_net: str) -> str:
    return p_net[:-1] + ("±" if p_net.endswith("+") else "P/N")


# --- Pairs and channels -------------------------------------------------------------------

def _pairs(snapshot: model.BoardSnapshot) -> dict[str, str]:
    """P net -> N net, for every pair on the board."""
    items = (*snapshot.tracks, *snapshot.arcs, *snapshot.vias, *snapshot.pads)
    nets = {item.net for item in items if item.net}
    return {net: partner for net in nets if net[-1] in "+P"
            and (partner := diff_pair_partner(net, nets)) is not None}


def _series_parts(snapshot: model.BoardSnapshot) -> list[tuple[str, str, Link]]:
    """(net, net, link) for every footprint with exactly two pads on two different nets."""
    references = {footprint.id: footprint.reference for footprint in snapshot.footprints}
    pads_of: dict[str, list[model.Pad]] = {}
    for pad in snapshot.pads:
        if pad.net:
            pads_of.setdefault(pad.footprint_id, []).append(pad)
    parts = []
    for footprint_id, pads in pads_of.items():
        if len(pads) == 2 and pads[0].net != pads[1].net and footprint_id:
            parts.append((pads[0].net, pads[1].net,
                          Link(footprint_id, references.get(footprint_id, ""), (pads[0].id, pads[1].id))))
    return parts


def find_channels(snapshot: model.BoardSnapshot, follow_series: bool = True) -> list[Channel]:
    """Every pair, as its own channel or chained through series parts on both sides."""
    pairs = _pairs(snapshot)
    n_to_p = {n: p for p, n in pairs.items()}
    joins: dict[frozenset, dict[str, Link]] = {}  # {P net, P net} -> side -> part
    if follow_series:
        for a, b, link in _series_parts(snapshot):
            if a in pairs and b in pairs:
                joins.setdefault(frozenset((a, b)), {})["p"] = link
            elif a in n_to_p and b in n_to_p:
                joins.setdefault(frozenset((n_to_p[a], n_to_p[b])), {})["n"] = link
    neighbours: dict[str, list[str]] = {p: [] for p in pairs}
    for key, sides in joins.items():
        if "p" in sides and "n" in sides:
            a, b = tuple(key)
            neighbours[a].append(b)
            neighbours[b].append(a)
    crowded = {p for p, others in neighbours.items() if len(others) > 2}  # no single path through it
    chained = {p: [other for other in others if other not in crowded]
               for p, others in neighbours.items() if p not in crowded}
    channels, seen = [], set()
    for p in sorted(pairs):
        if p in seen:
            continue
        chain = _chain(p, chained)
        seen.update(chain)
        p_links, n_links = [], []
        for first, second in zip(chain, chain[1:]):
            sides = joins[frozenset((first, second))]
            p_links.append(_ordered(sides["p"], first, snapshot))
            n_links.append(_ordered(sides["n"], pairs[first], snapshot))
        channels.append(Channel("|".join(chain), " → ".join(pair_name(net) for net in chain), tuple(chain),
                                tuple(pairs[net] for net in chain), tuple(p_links), tuple(n_links)))
    return channels


def _chain(start: str, neighbours: dict[str, list[str]]) -> list[str]:
    """The pairs chained with `start`, end to end (a pair with more than two joins,
    or a ring, stays on its own)."""
    if start not in neighbours or not neighbours[start]:
        return [start]
    component, stack = {start}, [start]
    while stack:
        for other in neighbours.get(stack.pop(), ()):
            if other in neighbours and other not in component:
                component.add(other)
                stack.append(other)
    ends = [p for p in component if len(neighbours[p]) == 1]
    if len(ends) != 2:
        return [start]  # a ring or a branch: no single channel
    chain = [min(ends)]
    while len(chain) < len(component):
        chain.append(next(p for p in neighbours[chain[-1]] if p not in chain))
    return chain


def _ordered(link: Link, from_net: str, snapshot: model.BoardSnapshot) -> Link:
    """The link with its pad on `from_net` first."""
    net = next(pad.net for pad in snapshot.pads if pad.id == link.pads[0])
    return link if net == from_net else replace(link, pads=link.pads[::-1])


# --- One side's copper as a graph ---------------------------------------------------------

@dataclass
class _Edge:
    kind: str  # "track", "port" (an end, via or pad touching another), "link" (a series part)
    item_id: str
    layer: str
    a: int
    b: int
    points: tuple = ()  # a track's polyline from node a to node b
    length: float = 0.0
    width: int = 0
    net: str = ""


class _Graph:
    def __init__(self):
        self.kind: list[str] = []  # "end", "via", "pad"
        self.pos: list[model.Point] = []
        self.layer: list[str] = []  # an end's layer
        self.item: list[object] = []  # the via or pad record
        self.edges: list[_Edge] = []

    def node(self, kind, pos, layer="", item=None) -> int:
        self.kind.append(kind)
        self.pos.append(pos)
        self.layer.append(layer)
        self.item.append(item)
        return len(self.kind) - 1


def _inside(point, polygons) -> bool:
    """Even-odd point in polygon over outer rings and holes."""
    x, y = point
    inside = False
    for polygon in polygons:
        for ring in polygon:
            count = len(ring)
            for index in range(count):
                (x1, y1), (x2, y2) = ring[index], ring[index - 1]
                if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
    return inside


def _on_pad(pad: model.Pad, layer: str, point) -> bool:
    if layer not in pad.layers:
        return False
    polygons = pad.polygons.get(layer)
    if polygons:
        return _inside(point, polygons)
    return math.dist(point, pad.pos) <= PAD_FALLBACK_NM


def _arc_polyline(arc: model.Arc) -> tuple[tuple, float]:
    """The sampled arc and its true length (chords scaled up to it)."""
    points = sample_arc(arc.start, arc.mid, arc.end)
    chords = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
    (ax, ay), (mx, my), (bx, by) = arc.start, arc.mid, arc.end
    determinant = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if not determinant:
        return tuple(points), chords
    ux = ((ax * ax + ay * ay) * (my - by) + (mx * mx + my * my) * (by - ay) + (bx * bx + by * by) * (ay - my))
    uy = ((ax * ax + ay * ay) * (bx - mx) + (mx * mx + my * my) * (ax - bx) + (bx * bx + by * by) * (mx - ax))
    centre = (ux / determinant, uy / determinant)
    radius = math.dist(centre, arc.start)
    angles = [math.atan2(p[1] - centre[1], p[0] - centre[0]) for p in (arc.start, arc.mid, arc.end)]
    sweep = (angles[1] - angles[0]) % (2 * math.pi) + (angles[2] - angles[1]) % (2 * math.pi)
    if sweep > 2 * math.pi:  # clockwise: measure the other way round
        sweep = 4 * math.pi - sweep
    return tuple(points), radius * sweep


def _polyline_length(points) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


class _Parent:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, node: int) -> int:
        root = node
        while self.parent.get(root, root) != root:
            root = self.parent[root]
        while self.parent.get(node, node) != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, a: int, b: int):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def build_graph(items: "NetItems", heights: dict[str, int], links=()) -> _Graph:
    """The copper of one side (one or more nets) as a graph."""
    graph = _Graph()
    ends: dict[tuple, int] = {}

    def end(layer, point):
        key = (layer, point)
        if key not in ends:
            ends[key] = graph.node("end", point, layer)
        return ends[key]

    polylines = []  # (item id, layer, points, length, width)
    nets = {}
    for track in items.tracks:
        polylines.append((track.id, track.layer, (track.start, track.end), math.dist(track.start, track.end),
                          track.width))
        nets[track.id] = track.net
    for arc in items.arcs:
        polylines.append((arc.id, arc.layer, *_arc_polyline(arc), arc.width))
        nets[arc.id] = arc.net
    # Where an end touches another track between its ends, that track is split there.
    splits: dict[int, list[tuple[int, float, model.Point]]] = {}
    merge = _Parent()
    for layer in {entry[1] for entry in polylines}:
        chords = [(index, chord, a, b, width) for index, (_, on, points, _, width) in enumerate(polylines)
                  if on == layer for chord, (a, b) in enumerate(zip(points, points[1:]))]
        starts = np.array([c[2] for c in chords], np.float64).reshape(-1, 2)
        span = np.array([c[3] for c in chords], np.float64).reshape(-1, 2) - starts
        reach = np.maximum(np.array([c[4] for c in chords], np.float64) / 2, CONTACT_NM)
        squared = np.maximum((span * span).sum(axis=1), 1e-9)
        for index, (_, on, points, _, _) in enumerate(polylines):
            if on != layer:
                continue
            for tip in (points[0], points[-1]):
                relative = np.asarray(tip, np.float64) - starts
                t = np.clip((relative * span).sum(axis=1) / squared, 0.0, 1.0)
                gap = np.hypot(*(relative - t[:, None] * span).T)
                for hit in np.flatnonzero(gap <= reach):
                    other, chord, a, b, width = chords[hit]
                    if other == index:
                        continue
                    other_points = polylines[other][2]
                    near = max(width / 2, CONTACT_NM)
                    if math.dist(tip, other_points[0]) <= near:
                        merge.union(end(layer, tip), end(layer, other_points[0]))
                    elif math.dist(tip, other_points[-1]) <= near:
                        merge.union(end(layer, tip), end(layer, other_points[-1]))
                    else:
                        splits.setdefault(other, []).append((chord, float(t[hit]), tip))
    for index, (item_id, layer, points, length, width) in enumerate(polylines):
        # Cut at the splits; each part keeps its share of the true (arc) length.
        scale = length / max(_polyline_length(points), 1e-9)
        cuts = sorted(splits.get(index, ()))
        parts, current, done = [], [points[0]], 0
        for chord, t, tip in cuts:
            while done < chord:
                done += 1
                current.append(points[done])
            a, b = points[chord], points[chord + 1]
            cut = (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t))
            current.append(cut)
            parts.append((tuple(current), tip))
            current = [cut]
        current.extend(points[done + 1:])
        parts.append((tuple(current), None))
        previous = end(layer, points[0])
        for part_points, tip in parts:
            last = end(layer, part_points[-1])
            if tip is not None:
                merge.union(last, end(layer, tip))
            graph.edges.append(_Edge("track", item_id, layer, previous, last, part_points,
                                     _polyline_length(part_points) * scale, width, nets[item_id]))
            previous = last

    end_nodes = [(node, layer, point) for (layer, point), node in ends.items()]
    via_nodes = [(graph.node("via", via.pos, item=via), via) for via in items.vias]
    pad_nodes = [(graph.node("pad", pad.pos, item=pad), pad) for pad in items.pads]

    def spans(via, layer):
        low, high = sorted((heights.get(via.layer_top, 0), heights.get(via.layer_bottom, 0)))
        return layer in heights and low <= heights[layer] <= high

    def port(a, b, layer):
        graph.edges.append(_Edge("port", "", layer, a, b))

    for node, layer, point in end_nodes:
        for via_node, via in via_nodes:
            if spans(via, layer) and math.dist(point, via.pos) <= max(via.diameter / 2, CONTACT_NM):
                port(node, via_node, layer)
        for pad_node, pad in pad_nodes:
            if _on_pad(pad, layer, point):
                port(node, pad_node, layer)
    for via_node, via in via_nodes:
        for pad_node, pad in pad_nodes:  # via in pad
            layer = next((layer for layer in pad.layers if spans(via, layer) and _on_pad(pad, layer, via.pos)), None)
            if layer is not None:
                port(via_node, pad_node, layer)
        for other_node, other in via_nodes:  # stacked vias
            if other_node > via_node and math.dist(via.pos, other.pos) <= max(via.diameter, other.diameter) / 2:
                shared = [layer for layer in heights if spans(via, layer) and spans(other, layer)]
                if shared:
                    port(via_node, other_node, shared[0])
    pad_index = {pad.id: node for node, pad in pad_nodes}
    for link in links:
        a, b = (pad_index.get(pad_id) for pad_id in link.pads)
        if a is not None and b is not None:
            layer = next(iter(graph.item[a].layers), "")
            graph.edges.append(_Edge("link", link.footprint_id, layer, a, b))
    for edge in graph.edges:
        edge.a, edge.b = merge.find(edge.a), merge.find(edge.b)
    graph.edges = [edge for edge in graph.edges if edge.a != edge.b]
    return graph


@dataclass(frozen=True)
class NetItems:
    tracks: tuple[model.Track, ...] = ()
    arcs: tuple[model.Arc, ...] = ()
    vias: tuple[model.Via, ...] = ()
    pads: tuple[model.Pad, ...] = ()


def items_by_net(snapshot: model.BoardSnapshot) -> dict[str, NetItems]:
    buckets: dict[str, dict[str, list]] = {}
    for kind, items in (("tracks", snapshot.tracks), ("arcs", snapshot.arcs), ("vias", snapshot.vias),
                        ("pads", snapshot.pads)):
        for item in items:
            if item.net:
                buckets.setdefault(item.net, {}).setdefault(kind, []).append(item)
    return {net: NetItems(**{kind: tuple(values) for kind, values in kinds.items()})
            for net, kinds in buckets.items()}


def combined(by_net: dict[str, NetItems], nets) -> NetItems:
    parts = [by_net.get(net, NetItems()) for net in nets]
    return NetItems(*(tuple(item for part in parts for item in getattr(part, kind))
                      for kind in ("tracks", "arcs", "vias", "pads")))


# --- The route: terminals, the path between them, branches --------------------------------

def _adjacency(graph: _Graph) -> dict[int, list[tuple[int, _Edge]]]:
    adjacent: dict[int, list[tuple[int, _Edge]]] = {}
    for edge in graph.edges:
        adjacent.setdefault(edge.a, []).append((edge.b, edge))
        adjacent.setdefault(edge.b, []).append((edge.a, edge))
    return adjacent


def _distances(adjacent, source: int) -> tuple[dict[int, float], dict[int, tuple[int, _Edge]]]:
    """Dijkstra over copper length from `source`: distances and the step into each node."""
    best, previous = {source: 0.0}, {}
    queue = [(0.0, source)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance > best.get(node, math.inf):
            continue
        for other, edge in adjacent.get(node, ()):
            candidate = distance + edge.length
            if candidate < best.get(other, math.inf):
                best[other] = candidate
                previous[other] = (node, edge)
                heapq.heappush(queue, (candidate, other))
    return best, previous


def _terminal(graph: _Graph, node: int, references: dict[str, str], adjacent) -> Terminal:
    if graph.kind[node] == "pad":
        pad = graph.item[node]
        reference = references.get(pad.footprint_id, "?")
        layer = next((edge.layer for _, edge in adjacent.get(node, ())), pad.layers[0] if pad.layers else "")
        return Terminal(f"{reference}.{pad.number}", pad.id, pad.pos, layer)
    if graph.kind[node] == "via":
        via = graph.item[node]
        return Terminal("via", via.id, via.pos, via.layer_top)
    track = next((edge for _, edge in adjacent.get(node, ()) if edge.kind == "track"), None)
    return Terminal("track end", track.item_id if track else "", graph.pos[node], graph.layer[node])


def route(items: NetItems, heights: dict[str, int], references: dict[str, str], links=()) -> Route | None:
    """The routed path between the two terminals farthest apart; None without copper."""
    graph = build_graph(items, heights, links)
    adjacent = _adjacency(graph)
    if not any(edge.kind == "track" for edge in graph.edges):
        return None
    degree = {node: len({other for other, _ in neighbours}) for node, neighbours in adjacent.items()}
    leaves = [node for node, count in degree.items() if count == 1]
    pads = [node for node in leaves if graph.kind[node] == "pad"]
    candidates = pads if len(pads) >= 2 else leaves
    if len(candidates) < 2:  # a ring without ends: walk from any node round to itself is not a route
        return None
    best = None
    tables = {node: _distances(adjacent, node) for node in candidates}
    for index, first in enumerate(candidates):
        distances = tables[first][0]
        for second in candidates[index + 1:]:
            if second in distances and (best is None or distances[second] > best[0]):
                best = (distances[second], first, second)
    if best is None:
        return None
    _, first, second = best
    previous = tables[first][1]
    steps, node = [], second
    while node != first:
        before, edge = previous[node]
        steps.append((before, edge, node))
        node = before
    steps.reverse()
    pieces = _pieces(graph, steps, heights)
    on_route = {first} | {node for _, _, node in steps}
    along = {first: 0.0}
    total = 0.0
    for before, edge, node in steps:
        total += edge.length
        along[node] = total
    branches = []
    for leaf in candidates:
        if leaf in on_route:
            continue
        distances, back = tables[leaf]
        if not any(node in distances for node in on_route):
            continue  # on copper not connected to the route
        joint = min((node for node in on_route if node in distances), key=lambda node: distances[node])
        branches.append(Branch(_terminal(graph, leaf, references, adjacent), along[joint], distances[joint]))
    reached = _distances(adjacent, first)[0]
    complete = all(node in reached for node in range(len(graph.kind)) if graph.kind[node] == "pad")
    return Route(_terminal(graph, first, references, adjacent), _terminal(graph, second, references, adjacent),
                 pieces, tuple(sorted(branches, key=lambda branch: branch.at_nm)), complete)


def _pieces(graph: _Graph, steps, heights: dict[str, int]) -> tuple[Piece, ...]:
    """The path's tracks in walking order, with a barrel wherever it changes layer
    and a straight hop across pads and series parts."""
    pieces: list[Piece] = []
    point, layer = None, None
    for before, edge, node in steps:
        if layer is not None and edge.layer and edge.layer != layer:
            hub = graph.item[before]  # the via or plated pad the layer change happens in
            at = graph.pos[before]
            span = ((hub.layer_top, hub.layer_bottom) if isinstance(hub, model.Via)
                    else (hub.layers[0], hub.layers[-1]) if isinstance(hub, model.Pad) else (layer, edge.layer))
            pieces.append(Piece("barrel", getattr(hub, "id", ""), layer, edge.layer, (at, at),
                                float(abs(heights.get(layer, 0) - heights.get(edge.layer, 0))), 0, span,
                                getattr(hub, "net", "")))
            point = point if point is not None else at
        if edge.layer:
            layer = edge.layer
        if edge.kind == "track":
            points = edge.points if edge.a == before else edge.points[::-1]
            if point is not None and point != points[0]:
                pieces.append(Piece("hop", "", layer, layer, (point, points[0]), math.dist(point, points[0]),
                                    net=edge.net))
            pieces.append(Piece("track", edge.item_id, layer, layer, tuple(points), edge.length, edge.width,
                                net=edge.net))
            point = points[-1]
        elif point is None and graph.kind[node] != "pad":
            point = graph.pos[node]  # the route starts through a via in its pad
    return tuple(pieces)
