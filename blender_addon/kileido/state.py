"""What the add-on knows about the board it currently shows (one board per Blender session).

Every module reads and writes the same `board` instance; nothing else holds board state.
"""


class BoardState:
    def __init__(self):
        # Identity and frame: KiCad nanometres become Blender metres around `origin_nm`.
        self.collection = None  # "KiLeidoscope: <board>", owns every KiLeidoscope object
        self.name_prefix = ""  # of its object names: "" live, "KV<n> " while a view-only board is placed
        self.board_name = ""
        self.board_path = ""
        self.origin_nm = (0, 0)
        self.heights = {}  # copper layer name -> z (m)
        self.layer_thickness = {}  # stackup thickness (m) of copper, mask, ... by layer name
        self.thickness_m = 0.0
        self.layer_names = {}  # canonical layer -> KiCad's display name ("F.SilkS" -> "Top Overlay")
        self.stackup = []  # KiCad's stackup top to bottom: {"name", "type", "thickness_nm", "material", ...}

        # Shared display resources.
        self.groups = {}  # Geometry Nodes groups by role (nodes.ensure_all)
        self.materials = {}  # KiLeidoscope materials by role ("board", "copper:F.Cu", ...)
        self.color_mode = "FAB"
        self.appearance = {}  # colours and finish from the bridge (board_specs.read_appearance)
        self.export = {}  # the bridge's live board copy for kicad-cli (see protocol.board_message)
        self.mask_images = {}  # "F"/"B" -> (saved-board mask plot image, world XY bounds)
        self.relief_images = {}  # "F"/"B" -> (blurred copper plot: the mask's relief, world XY bounds)
        self.silk = {}  # "F"/"B" -> silkscreen printed on the surfaces (cosmetics.refresh_silk)
        self.via_too_big = 0  # vias KiCad tents over a drill larger than the panel's Max tent drill

        # Snapshot bookkeeping: objects not touched by a full snapshot are cleared.
        self.in_snapshot = False
        self.touched = set()
        self.footprint_state = {}  # footprint id -> placement signature of the last apply
        self.framed_board = None  # board the 3D views were last fitted to

        # Component models bound to footprints (models.bind_root).
        self.model_bound = set()
        self.model_objects_by_fp = {}

        # KiCad's selection (protocol.selection_message).
        self.highlight = {"selected": set(), "pair": set()}
        self.highlight_components = {"footprints": set(), "pads": set()}
        self.outline_problem = False  # a malformed outline is drawn red (apply._apply_outline_problem)
        self.xray_for_outline = False  # X-ray mode was ticked for it (and is unticked once fixed)

        # Panel text.
        self.status = "No board loaded"
        self.warnings = []

    def reset(self):
        """Forget everything: the Blender data it referred to is gone (another file loaded)."""
        self.__init__()

    def fail(self, message):
        """Show `message` and never leave a half-loaded board hidden."""
        if self.collection is not None:
            try:
                self.collection.hide_viewport = False
                self.collection.hide_render = False
            except ReferenceError:  # freed under us (file load, undo): a resync rebuilds it
                self.reset()
        self.status = message
        self.in_snapshot = False


board = BoardState()
