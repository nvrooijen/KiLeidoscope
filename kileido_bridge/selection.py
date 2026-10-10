"""Which nets to highlight in Blender: KiCad's selection and its differential pair.

KiCad's selection is only read here (`Board.get_selection`, ~0.16 ms on the
reference board); `kicad_reader.select_in_kicad` is the one call that sets it.
"""

from __future__ import annotations

from . import model


def diff_pair_partner(net: str, nets: set[str]) -> str | None:
    """KiCad's differential-pair naming rule: a final "+"/"-" or "P"/"N" pairs with
    the other suffix, if a net of that name exists (as KiCad's router checks)."""
    if not net:
        return None
    swap = {"+": "-", "-": "+", "P": "N", "N": "P"}.get(net[-1])
    if swap is None:
        return None
    partner = net[:-1] + swap
    return partner if partner in nets and partner != net else None


def components(snapshot: model.BoardSnapshot, selected_ids: frozenset[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(selected footprint ids, ids of their pads)."""
    footprints = tuple(sorted(f.id for f in snapshot.footprints if f.id in selected_ids))
    chosen = set(footprints)
    pads = tuple(sorted(p.id for p in snapshot.pads if p.footprint_id in chosen))
    return footprints, pads


def selected_nets(snapshot: model.BoardSnapshot, selected_ids: frozenset[str]) -> frozenset[str]:
    """Nets of the selected tracks, arcs, vias and zones (pours)."""
    items = (*snapshot.tracks, *snapshot.arcs, *snapshot.vias, *snapshot.zones)
    return frozenset(item.net for item in items if item.id in selected_ids and item.net)


def selected_copper(snapshot: model.BoardSnapshot, selected_ids: frozenset[str]) -> tuple[str, ...]:
    """Selected tracks, arcs, vias and zones themselves, without their nets: a selection
    made from a finding shows its items, not every net they are on."""
    items = (*snapshot.tracks, *snapshot.arcs, *snapshot.vias, *snapshot.zones)
    return tuple(sorted({item.id for item in items if item.id in selected_ids}))


def unconnected(snapshot: model.BoardSnapshot, selected_ids: frozenset[str]) -> tuple[str, ...]:
    """Selected tracks, arcs, vias and zones without a net: no net to follow, so they
    are highlighted alone (KiCad shows them selected; a net highlight would skip them)."""
    items = (*snapshot.tracks, *snapshot.arcs, *snapshot.vias, *snapshot.zones)
    return tuple(sorted({item.id for item in items if item.id in selected_ids and not item.net}))


def highlight_nets(snapshot: model.BoardSnapshot, nets: frozenset[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(ids on these nets, ids on their differential-pair partner nets): whole nets,
    tracks, arcs, vias, zones (power planes) and footprint pads, so tracks routed
    later join in.

    When both nets of a pair are selected (a diff-pair tuning pattern owns both),
    the "+"/"P" net stays the selection and the "-"/"N" net shows as its partner.
    """
    if not nets:
        return (), ()
    items = (*snapshot.tracks, *snapshot.arcs, *snapshot.vias, *snapshot.zones, *snapshot.pads)
    all_nets = {item.net for item in items}
    primary = {net for net in nets
               if not (net[-1:] in ("-", "N") and diff_pair_partner(net, all_nets) in nets)}
    partners = {partner for net in primary
                if (partner := diff_pair_partner(net, all_nets)) is not None} - primary
    selected = tuple(sorted({item.id for item in items if item.net in primary}))
    pair = tuple(sorted({item.id for item in items if item.net in partners}))
    return selected, pair
