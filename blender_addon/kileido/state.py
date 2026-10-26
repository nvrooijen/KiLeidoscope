"""What the add-on knows about the boards it shows: up to four live boards per Blender
session, each in a slot.

Every module reads and writes `board`, which stands for the board being worked on: a live
frame or a finished export switches to its own board with `using(slot)` first; outside
that, `board` is the picked board (`pick`), the one the panel shows and its tools work on.
Board 1 keeps the names KiLeidoscope always used; the others' objects, materials and
images carry their slot's prefix ("B2 KLS outline"), as Blender names are global.
Nothing else holds board state.
"""

from contextlib import contextmanager

SLOTS = (1, 2, 3, 4)


def slot_prefix(slot):
    """The prefix of a live board's names: none for board 1, "B<slot> " for the others."""
    return "" if slot == 1 else f"B{slot} "


def log(message):
    """One line in Blender's output (blender.log): what changed the board's model bindings."""
    print(f"KiLeidoscope: {message}", flush=True)


class BoardState:
    def __init__(self, slot=1):
        self.slot = slot
        # Identity and frame: KiCad nanometres become Blender metres around `origin_nm`.
        self.collection = None  # "KiLeidoscope: <board>", owns every KiLeidoscope object
        # Of its names: the slot's (slot_prefix), "KV<n> " while a view-only board is placed
        self.name_prefix = slot_prefix(slot)
        self.board_name = ""
        self.board_path = ""
        self.origin_nm = (0, 0)
        # Where it stands (poses.py): T_B, ((x, y, z) metres, XYZ Euler radians) of KiCad's frame,
        # and the board (path, else name) it is for. Kept when its Blender data goes (reset).
        self.pose = None
        self.pose_of = ""
        self.hidden = False  # its eye in the Boards list is shut (slots.set_hidden); kept like the pose
        self.heights = {}  # copper layer name -> z (m)
        self.layer_thickness = {}  # stackup thickness (m) of copper, mask, ... by layer name
        self.thickness_m = 0.0
        self.layer_names = {}  # canonical layer -> KiCad's display name ("F.SilkS" -> "Top Overlay")
        self.stackup = []  # KiCad's stackup top to bottom: {"name", "type", "thickness_nm", "material", ...}
        self.ims = None  # ims.Stack while IMS mode puts a metal base under the board (the heights above are its)
        self.ims_warnings = []  # what would short to the metal base (apply.refresh_ims_warnings)
        self.flex = {}  # flex mode's panel content from the bridge (kileido_bridge.flex_checks.report)
        self.fold_findings = []  # what runs into what at the end of a folding step (fold.check_steps)
        # The DRC and LLM columns (findings.py): the bridge's `findings` payload, and the one finding drawn.
        self.findings = {}  # {"status", "folder", "drc", "sources": {"drc": ..., "file": ...}}
        self.findings_shown = ("", "")  # (source, key) of the finding drawn, ("", "") for none
        self.findings_isolate = False  # the drawn finding wants X-ray (findings_draw.xray_wanted): it fades the
        # rest while the box is ticked, even with nothing lit
        self.xray_for_finding = None  # (source, key) X-ray was last turned on for (findings_draw._follow_xray)
        self.xray_ticked = False  # the box was ticked for it, so it is unticked when the finding goes
        self.finding_severity = "error"  # its colour (materials.FINDING_COLORS)
        self.layer_filter = None  # copper layers its `layers` draws leave shown (layers.set_filter); None: all
        self.prompt_wait = None  # Copy prompt pressed: the run shown then ("" none), findings.MAKING once the
        # bridge is making it; None: not asked
        self.prompt_copied = ""  # the run whose prompt went to the clipboard

        # Shared display resources.
        self.groups = {}  # Geometry Nodes groups by role (nodes.ensure_all)
        self.materials = {}  # KiLeidoscope materials by role ("board", "copper:F.Cu", ...)
        self.color_mode = "FAB"
        self.appearance = {}  # colours and finish from the bridge (board_specs.read_appearance)
        self.export = {}  # the bridge's live board copy for kicad-cli (see protocol.board_message)
        self.mask_images = {}  # "F"/"B" -> (saved-board mask plot image, world XY bounds)
        self.relief_images = {}  # "F"/"B" -> (blurred copper plot: the mask's relief, world XY bounds)
        self.silk = {}  # "F"/"B" -> silkscreen printed on the surfaces (cosmetics.refresh_silk)
        self.via_too_big = 0  # vias KiCad tents over an empty finished hole larger than the panel's Max tent hole
        self.holes = None  # its hole mask (holes._Mask)
        self.solder = None  # its reflowed solder builds (solder._Builds)
        self.cut_cache = None  # its geometry for the section and edges (cut._cache)

        # Snapshot bookkeeping: objects not touched by a full snapshot are cleared.
        self.in_snapshot = False
        self.touched = set()
        self.footprint_state = {}  # footprint id -> placement signature of the last apply
        self.framed_board = None  # board the 3D views were last fitted to

        # Component models bound to footprints (models.bind_root).
        self.model_bound = set()
        self.model_objects_by_fp = {}
        self.component_names = {}  # components.key_of -> object name (components.find)
        self.component_names_of = 0  # the collection (pointer) that index is of
        self.object_index = {}  # object name -> object, as found in the collection (objects.lookup)
        self.object_index_of = 0  # the collection (pointer) that index is of

        # KiCad's selection (protocol.selection_message).
        self.highlight = {"selected": set(), "pair": set(), "finding": set()}  # finding: findings_draw
        self.highlight_components = {"footprints": set(), "pads": set()}
        # Dynamic phase of differential pairs (phase.py): each pair's last frame, by key.
        self.phase = {}
        self.phase_list = {"keys": [], "settings": {}, "warnings": [], "pending": 0}
        self.outline_problem = False  # a malformed outline is drawn red (apply._apply_outline_problem)
        self.xray_for_outline = False  # X-ray mode was ticked for it (and is unticked once fixed)

        # Panel text.
        self.status = "No board loaded"
        self.warnings = []

    def reset(self):
        """Forget everything: the Blender data it referred to is gone (another file loaded)."""
        if self.model_bound:
            log(f"board {self.slot} state reset; {len(self.model_bound)} bound models forgotten")
        kept = self.pose, self.pose_of, self.hidden  # rebuilt, the board stands where it stood
        self.__init__(self.slot)
        self.pose, self.pose_of, self.hidden = kept

    @property
    def loaded(self):
        return self.collection is not None

    def drop_if_freed(self, where, resync=True):
        """True after a collection freed under us (undo, file load) was forgotten: the board
        state starts over and a live bridge sends the board again (`resync`; not needed
        when a full snapshot is what just arrived)."""
        if self.collection is None:
            return False
        try:
            self.collection.name
        except ReferenceError:
            log(f"the board's collection was freed ({where}); state reset"
                f"{', resync requested' if resync else ''} ({len(self.model_bound)} bound models lost)")
            self.reset()
            if resync:
                from . import live
                with using(self.slot):
                    live.request_resync()
            return True
        return False

    def fail(self, message):
        """Show `message` and never leave a half-loaded board hidden."""
        if self.collection is not None:
            try:
                self.collection.hide_viewport = self.hidden
                self.collection.hide_render = self.hidden
            except ReferenceError:  # freed under us (file load, undo): a resync rebuilds it
                self.reset()
        self.status = message
        self.in_snapshot = False


boards = {slot: BoardState(slot) for slot in SLOTS}
_current = [1]
_picked = [1]  # the board the panel shows and its tools work on (slots.pick), outside any `using`
_depth = [0]  # `using` blocks open


def current():
    """The BoardState `board` stands for now."""
    return boards[_current[0]]


def picked():
    """The slot of the picked board: what the panel, its buttons and the tools work on."""
    return _picked[0]


def pick(slot):
    """`board` stands for board `slot` outside any `using` block from now on (slots.pick hands
    the tools over)."""
    _picked[0] = slot
    if not _depth[0]:
        _current[0] = slot


@contextmanager
def using(slot):
    """Work on board `slot` inside the block."""
    previous = _current[0]
    _current[0] = slot
    _depth[0] += 1
    try:
        yield boards[slot]
    finally:
        _depth[0] -= 1
        _current[0] = previous if _depth[0] else _picked[0]


def each_loaded():
    """Slots with a board, each while it is the current board."""
    for slot in SLOTS:
        if boards[slot].loaded:
            with using(slot):
                yield slot


class _Current:
    """`board`: the BoardState of the slot being worked on (see `using`)."""

    __slots__ = ()

    def __getattr__(self, name):
        return getattr(boards[_current[0]], name)

    def __setattr__(self, name, value):
        setattr(boards[_current[0]], name, value)


board = _Current()
