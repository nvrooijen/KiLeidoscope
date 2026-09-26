"""KiCad selection highlight: selected tracks and their differential-pair partner."""

from types import SimpleNamespace

from kileido_bridge import model
from kileido_bridge.selection import components, diff_pair_partner, highlight_nets, selected_nets, unconnected


def highlight(snapshot, selected_ids):
    """What the bridge highlights for a KiCad selection: whole nets and their pair partners."""
    return highlight_nets(snapshot, selected_nets(snapshot, selected_ids))


def test_diff_pair_partner_follows_kicads_suffix_rule():
    nets = {"USB_D+", "USB_D-", "ETH_TXP", "ETH_TXN", "GND", "VCAP"}
    assert diff_pair_partner("USB_D+", nets) == "USB_D-"
    assert diff_pair_partner("USB_D-", nets) == "USB_D+"
    assert diff_pair_partner("ETH_TXP", nets) == "ETH_TXN"
    assert diff_pair_partner("ETH_TXN", nets) == "ETH_TXP"
    assert diff_pair_partner("VCAP", nets) is None  # "VCAN" does not exist
    assert diff_pair_partner("GND", nets) is None
    assert diff_pair_partner("", nets) is None


def _snapshot():
    def track(item_id, net):
        return model.Track(item_id, "F.Cu", net, (0, 0), (1_000_000, 0), 200_000)
    arc = model.Arc("n-arc", "F.Cu", "USB_D-", (0, 0), (500_000, 500_000), (1_000_000, 0), 200_000)
    vias = (model.Via("p-via", "USB_D+", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu"),
            model.Via("n-via", "USB_D-", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu"),
            model.Via("g-via", "GND", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu"))
    zones = (model.ZoneFill("gnd-plane", "GND", "In1.Cu", ()), model.ZoneFill("gnd-plane", "GND", "In2.Cu", ()))
    pads = (SimpleNamespace(id="J1-1", net="USB_D+"), SimpleNamespace(id="J1-2", net="USB_D-"),
            SimpleNamespace(id="J1-3", net="GND"), SimpleNamespace(id="J1-4", net=""))
    return SimpleNamespace(tracks=(track("p1", "USB_D+"), track("p2", "USB_D+"), track("n1", "USB_D-"),
                                   track("g1", "GND")), arcs=(arc,), vias=vias, zones=zones, pads=pads)


def test_selecting_one_segment_highlights_its_whole_net_and_the_partner_net():
    selected, pair = highlight(_snapshot(), frozenset({"p1", "not-a-track"}))
    assert selected == ("J1-1", "p-via", "p1", "p2")  # the entire trace, vias and pads included
    assert pair == ("J1-2", "n-arc", "n-via", "n1")
    assert highlight(_snapshot(), frozenset({"g1"})) == (("J1-3", "g-via", "g1", "gnd-plane"), ())  # planes too
    assert highlight(_snapshot(), frozenset()) == ((), ())


def test_selecting_a_pour_highlights_its_net_with_its_pads():
    assert highlight(_snapshot(), frozenset({"gnd-plane"})) == (("J1-3", "g-via", "g1", "gnd-plane"), ())


def test_selecting_a_via_highlights_its_net():
    assert highlight(_snapshot(), frozenset({"n-via"})) == (("J1-2", "n-arc", "n-via", "n1"),
                                                             ("J1-1", "p-via", "p1", "p2"))


def test_selecting_both_nets_of_a_pair_keeps_the_pair_colours():
    """A diff-pair tuning pattern selects both nets: P stays red, N shows as the partner."""
    selected, pair = highlight(_snapshot(), frozenset({"p1", "n1"}))
    assert set(selected) == {"p1", "p2", "p-via", "J1-1"} and set(pair) == {"n1", "n-arc", "n-via", "J1-2"}


def test_selected_items_without_a_net_are_highlighted_alone():
    """No net to follow: only the selected net-less items, never every net-less item."""
    snapshot = _snapshot()
    snapshot.tracks += (model.Track("loose-1", "F.Cu", "", (0, 0), (1, 0), 200_000),
                        model.Track("loose-2", "F.Cu", "", (0, 0), (1, 0), 200_000))
    snapshot.vias += (model.Via("loose-via", "", (0, 0), 600_000, 300_000, "F.Cu", "B.Cu"),)
    assert highlight(snapshot, frozenset({"loose-1", "loose-via"})) == ((), ())  # the net highlight skips them
    assert unconnected(snapshot, frozenset({"loose-1", "loose-via", "p1"})) == ("loose-1", "loose-via")
    assert unconnected(snapshot, frozenset()) == ()


def test_selected_footprints_bring_their_pads():
    snapshot = SimpleNamespace(
        footprints=(SimpleNamespace(id="U1"), SimpleNamespace(id="R1")),
        pads=(SimpleNamespace(id="U1-1", footprint_id="U1"), SimpleNamespace(id="U1-2", footprint_id="U1"),
              SimpleNamespace(id="R1-1", footprint_id="R1")))
    assert components(snapshot, frozenset({"U1", "track"})) == (("U1",), ("U1-1", "U1-2"))
    assert components(snapshot, frozenset()) == ((), ())


def test_selected_tuning_pattern_group_expands_to_its_member_tracks():
    """KiCad 10.0.3 sends a selected tuning pattern as a Group; kipy's `items` wrapper
    is empty, the members are in `proto.items` (measured on the reference board)."""
    from kileido_bridge.kicad_reader import selected_ids

    def kiid(value):
        return SimpleNamespace(value=value)

    group = SimpleNamespace(id=kiid("tuning"), items=[],
                            proto=SimpleNamespace(items=[kiid("p1"), kiid("n1")]))
    track = SimpleNamespace(id=kiid("g1"), proto=SimpleNamespace())
    board = SimpleNamespace(get_selection=lambda: [group, track])
    ids = selected_ids(board)
    assert ids == {"tuning", "p1", "n1", "g1"}
    selected, pair = highlight(_snapshot(), ids)
    assert {"p1", "p2", "g1"} <= set(selected) and {"n1", "n-arc"} <= set(pair)
