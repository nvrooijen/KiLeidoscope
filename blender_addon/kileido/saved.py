"""A saved .blend keeps its boards: reopened, each is back in its slot as a saved copy, and
Open in Blender in its KiCad puts it back live there (join.choose_slot finds it by its file).

Blender keeps the objects, materials, plots and Scene settings, and the hole masks are packed
when the file is saved (holes.pack_for_save). What the add-on knows of a board lives in Python
(state.BoardState): each board collection records it (`remember`), its root Empty carries its
pose (poses.POSE_KEY) and the scene the session's offset, and `restore` builds the slots from
the collections once a file is opened.
"""

import json

import bpy

from . import materials, nodes, poses
from .objects import ROOT, lookup, owned_name
from .state import SLOTS, board, boards, same_file, using

HEADER = "kls_board"  # on a board collection: its board frame, as `remember` keeps it (JSON)
STACKUP = "kls_board_stackup"  # and its stackup: KiCad's layer names and layers (JSON)
SAVED_STATUS = "Saved copy; not connected"
KEPT = ("board_name", "board_path", "origin_nm", "layers", "board_thickness_m", "layer_thickness_m",
        "appearance", "export", "warnings")


def remember(header):
    """A board frame (apply.apply_board): kept on the board collection."""
    board.collection[HEADER] = json.dumps({key: header.get(key) for key in KEPT})
    board.saved_copy = False


def remember_stackup():
    """The stackup frame: KiCad's names and the stackup, as the Layers list draws them."""
    if board.collection is not None:
        board.collection[STACKUP] = json.dumps({"layer_names": board.layer_names, "stackup": board.stackup})


def restore(kept):
    """After a file is opened (the board states reset): each board collection that recorded
    its board is back in its slot as a saved copy. `kept`: slot -> the board path a live link
    showed there before; that slot takes back its own board only (the link sends the rest).
    The slots restored."""
    poses.restore_session(bpy.context.scene)
    found = []
    for collection in bpy.data.collections:
        slot = collection.get("kls_slot")
        if collection.get("kileido_owned") != 1 or slot not in SLOTS or HEADER not in collection:
            continue  # a file saved before boards were recorded: its objects show, as plain objects
        header = json.loads(collection[HEADER])
        if boards[slot].loaded or (slot in kept and not same_file(kept[slot], header.get("board_path"))):
            continue
        with using(slot):
            _restore(collection, header)
        found.append(slot)
    return found


def _restore(collection, header):
    board.collection = collection
    board.board_name = header.get("board_name") or ""
    board.board_path = header.get("board_path") or ""
    board.origin_nm = tuple(header.get("origin_nm") or (0, 0))
    board.heights = {entry["name"]: float(entry["z_m"]) for entry in header.get("layers") or ()}
    board.thickness_m = float(header.get("board_thickness_m") or 0.0)
    board.layer_thickness = dict(header.get("layer_thickness_m") or {})
    board.appearance = dict(header.get("appearance") or {})
    board.export = dict(header.get("export") or {})
    board.warnings = list(header.get("warnings") or ())
    stackup = json.loads(collection.get(STACKUP, "{}"))
    board.layer_names = dict(stackup.get("layer_names") or {})
    board.stackup = list(stackup.get("stackup") or [])
    board.color_mode = getattr(bpy.context.scene, "kileido_color_mode", "FAB")
    board.framed_board = board.board_name  # the view as it was saved
    board.hidden = bool(collection.hide_viewport)
    board.groups = nodes.ensure_all()
    materials.create_all()  # the saved materials, found by name
    for layer in board.heights:
        material = bpy.data.materials.get(owned_name(f"KLS {layer} copper"))
        if material is not None:
            board.materials[f"copper:{layer}"] = material
    for obj in collection.all_objects:  # the saved mask plots: copper shows its finish in their openings
        layer = obj.get("kls_cosmetic_layer", "")
        if layer.endswith(".Mask") and obj.type == "MESH" and obj.data.materials and "kls_plot_rect" in obj:
            texture = next((node for node in obj.data.materials[0].node_tree.nodes if node.type == "TEX_IMAGE"), None)
            if texture is not None and texture.image is not None:
                board.mask_images[layer[0]] = (texture.image, tuple(obj["kls_plot_rect"]))
    root = lookup(ROOT)
    stored = list(root.get(poses.POSE_KEY, ())) if root is not None else []
    if len(stored) == 6:
        board.pose, board.pose_of = (tuple(stored[:3]), tuple(stored[3:])), board.board_path or board.board_name
    board.status = SAVED_STATUS
    board.saved_copy = True
