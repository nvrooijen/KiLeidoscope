"""Scene properties each board keeps for itself (multi-board): its layer eyes, IMS mode, flex
mode and fold, and its dynamic-phase settings. Board 1 keeps the names KiLeidoscope always
had ("kileido_ims"); boards 2-4 have copies of their own ("kileido_b2_ims"), so a saved file
keeps every board's and a keyed fold stays with its board. The panel shows and changes the
picked board's; code reads and writes the current board's (`get`, `put`); a change runs its
update with its own board in use. Everything else (colours, opacities, thickness settings)
is one setting for every board.
"""

from .state import SLOTS, board, using

PER_BOARD = {
    "kileido_ims", "kileido_ims_metal", "kileido_ims_finish", "kileido_ims_epoxy_um",
    "kileido_flex", "kileido_flex_use", "kileido_fold", "kileido_fold_frames", "kileido_fold_fps",
    "kileido_phase_enabled", "kileido_phase_show", "kileido_phase_labels", "kileido_phase_pulses",
    "kileido_phase_pulse_clock", "kileido_phase_pulse_count", "kileido_phase_pulse_rate",
    "kileido_phase_pulse_width_ps", "kileido_phase_pulse_tail_ps", "kileido_phase_pulse_glow",
    "kileido_phase_scale_ps", "kileido_phase_tolerance_ps", "kileido_phase_min_mm", "kileido_phase_follow_series",
    "kileido_phase_flipped",
}
_layer_names = set()  # the Layers list's eyes (layers.property_name, cosmetics.property_name): `expand` adds them


def of(prop, slot):
    """Board `slot`'s Scene property for `prop` (its board-1 name)."""
    return prop if slot == 1 else prop.replace("kileido_", f"kileido_b{slot}_", 1)


def name(prop):
    """The current board's Scene property for `prop` (its board-1 name), for a panel's `prop`."""
    return of(prop, board.slot) if prop in PER_BOARD or prop in _layer_names else prop


def get(scene, prop, default=None):
    return getattr(scene, name(prop), default)


def put(scene, prop, value):
    setattr(scene, name(prop), value)


def _in_slot(update, slot):
    def run(scene, context):
        with using(slot):
            return update(scene, context)
    return run


def expand(properties, layer_names):
    """`properties` (Scene property definitions by name), each board's own ones once per board:
    their updates run with that board in use. `layer_names`: the Layers list's eyes."""
    _layer_names.update(layer_names)
    out = {}
    for prop, definition in properties.items():
        if prop not in PER_BOARD and prop not in _layer_names:
            out[prop] = definition
            continue
        for slot in SLOTS:
            keywords = dict(definition.keywords)
            if keywords.get("update") is not None:
                keywords["update"] = _in_slot(keywords["update"], slot)
            out[of(prop, slot)] = definition.function(**keywords)
    return out
