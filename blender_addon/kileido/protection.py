"""Via protection (IPC-4761) as KiCad sets it, resolved per via. Pure numpy, no bpy.

KiCad stores each via's protection features (`kileido_bridge.model.PROTECTION`): tented,
covered and plugged per side, capped, filled. A via without its own setting follows the
board's (the appearance's `via_rules`). Nothing here is a Blender-side override: the panel
adds only what KiCad does not store (the fill material, the largest hole a tent spans,
the barrels' plating).

- Drilled: a via is a real hole (holes.py: board, copper and mask see-through in its
  bore) on each outer side it reaches, so its lands read as rings there, unless it is
  capped (copper over the drill). A blind via's inner end is closed by its inner land.
- Buried: no outer end, so KiCad's tents, covers, plugs and caps cannot apply. Resin
  from the prepreg fills it when the board is pressed; KiCad's filled flag picks the
  panel's fill material instead.
- Tented or covered on a side: a thin mask disk closes the hole there. No tent spans a
  large empty hole, so where the finished hole (the drill less its plating) is larger than
  `max_tent` a side is closed only over a plug or fill.
- Finished: only a via with nothing on either end gets the board finish in its barrel;
  a tent, plug or fill keeps the plating chemistry out.
- Plugged: solder mask ink from that side, half the barrel (type III-a); plugged from every
  outer end (both sides, or a blind via's one) it fills the barrel.
- Filled: the whole barrel, in the panel's fill material. Capped: filled, and plated over
  at each outer end (type VII).
"""

import numpy as np

FIELDS = ("tent_front", "tent_back", "cover_front", "cover_back", "plug_front", "plug_back", "cap", "fill")
FROM_RULES = 2  # a field's code on the wire: 1 yes, 0 no, 2 the board's rules
KICAD_DEFAULT = (1, 1, 0, 0, 0, 0, 0, 0)  # KiCad's own board defaults: tented both sides
THROUGH, TOP, BOTTOM = 0, 1, 2  # holes.THROUGH, TOP, BOTTOM: the sides a via is a hole on
MAX_TENT_M = 0.30e-3  # the panel's default: a larger empty finished hole is never shown tented


def pack(codes):
    """(n, 8) codes -> one int per via (2 bits per field), to keep on the via mesh."""
    codes = np.asarray(codes, np.int32).reshape(-1, len(FIELDS))
    return (codes << (2 * np.arange(len(FIELDS), dtype=np.int32))).sum(axis=1).astype(np.int32)


def unpack(packed):
    packed = np.asarray(packed, np.int32).reshape(-1, 1)
    return (packed >> (2 * np.arange(len(FIELDS), dtype=np.int32))) & 3


def resolve(packed, rules, hole, outer_top, outer_bottom, max_tent=MAX_TENT_M):
    """Per via, what is drawn: a dict of bool arrays (`side`: ints).

    core_top, core_bottom: a plug or fill in the barrel's upper / lower half. filled: the
    whole barrel is filled, in the panel's fill material. plugged: the core is a plug of
    solder mask ink (not a fill, not a buried via's resin). capped: plated over at its
    outer ends. drilled: a hole on its outer sides (rings, see-through where nothing closes
    it); side: holes.THROUGH, TOP or BOTTOM, the sides it is a hole on. tent_top,
    tent_bottom: a mask tent over the hole. open: nothing closes it, see-through; the same
    vias are `finished`, the board finish reaching the barrel. too_big: KiCad tents or
    covers it over an empty finished `hole` (diameter inside the plating) larger than
    `max_tent`, so it is drawn open there.
    """
    codes = unpack(packed)
    rules = np.asarray(rules if rules is not None and len(rules) == len(FIELDS) else KICAD_DEFAULT, bool)
    on = np.where(codes == FROM_RULES, rules[None, :], codes == 1)
    top, bottom = np.asarray(outer_top, bool), np.asarray(outer_bottom, bool)
    capped = on[:, 6] & (top | bottom)  # a cap needs an outer end, and a filled barrel under it
    filled = on[:, 7] | capped
    plug_top, plug_bottom = on[:, 4] & top, on[:, 5] & bottom
    every_end = (plug_top | ~top) & (plug_bottom | ~bottom) & (plug_top | plug_bottom)
    buried = ~top & ~bottom  # laminated over: pressed full of prepreg resin
    core_top = filled | every_end | plug_top | buried
    core_bottom = filled | every_end | plug_bottom | buried
    small = np.asarray(hole, np.float64) <= max_tent + 1e-9
    asked_top, asked_bottom = (on[:, 0] | on[:, 2]) & top, (on[:, 1] | on[:, 3]) & bottom
    closed_top = asked_top & (small | core_top)
    closed_bottom = asked_bottom & (small | core_bottom)
    drilled = (top | bottom) & ~capped
    open_ = drilled & ~(closed_top | closed_bottom | core_top | core_bottom)
    return {"core_top": core_top, "core_bottom": core_bottom, "filled": filled, "capped": capped,
            "plugged": (core_top | core_bottom) & ~filled & ~buried, "drilled": drilled,
            "tent_top": drilled & closed_top, "tent_bottom": drilled & closed_bottom,
            "open": open_, "finished": open_,
            "too_big": (asked_top & ~closed_top) | (asked_bottom & ~closed_bottom),
            "side": np.where(top & bottom, THROUGH, np.where(top, TOP, BOTTOM))}
