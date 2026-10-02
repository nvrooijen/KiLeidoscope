"""KiCad's selection in Blender: highlighted nets in red-orange, their differential-pair
partner in blue, selected components boxed.

Highlights are copies of the live copper meshes (so they follow edits), slightly
wider and higher than the copper they cover. Zones switch material instead: a copy
would double the heaviest geometry on the board.
"""

import numpy as np
from mathutils import Matrix, Vector

from . import focus, materials, transform
from .objects import (camera_rays_only, hide, owned_object, read_attribute, read_coordinates, read_edges,
                      set_modifier, set_node_input, set_visible, single_point, write_attribute)
from .placement import PLACEHOLDER_HEIGHT_M, copper_placement, copper_thickness, outward
from .state import board

LIFT_M = 1e-6  # above the copper's outer surface
GROW_M = 4e-6  # 2 um wider per side, so the copper edge never shows through
BOX_MARGIN_M = 0.1e-3  # around the component body


def apply_selection(header):
    """KiCad selection changed (protocol.selection_message)."""
    board.highlight = {"selected": set(header.get("selected", ())), "pair": set(header.get("pair", ()))}
    board.highlight_components = {"footprints": set(header.get("footprints", ())),
                                  "pads": set(header.get("pads", ()))}
    refresh()
    focus.refresh()


def refresh(layer=None, kind=None):
    """Rebuild the highlight copies from the current copper.

    `kind` ("tracks", "vias" or "zones", optionally with `layer`) limits the refresh
    to what one live frame changed. Skipped inside a snapshot: `snapshot_end`
    refreshes everything once.
    """
    if board.collection is None or board.in_snapshot:
        return
    wanted = board.highlight
    if kind in (None, "tracks"):
        _refresh_tracks(wanted, layer)
    if kind in (None, "vias"):
        _refresh_vias(wanted)
    if kind in (None, "zones"):
        _refresh_zones(wanted, layer)
    if kind in (None, "pads"):
        _refresh_pads(wanted, board.highlight_components["pads"], layer)
    if kind is None:
        _refresh_component_boxes(board.highlight_components["footprints"])


def _hide(obj):
    """An unused highlight copy: no geometry, hidden. Cheap when it already is."""
    if len(obj.data.vertices):
        obj.data.clear_geometry()
    if not obj.hide_get():
        hide(obj, True)
    if not obj.hide_render:
        obj.hide_render = True
    board.touched.add(obj.name)


def _show(obj):
    """Overlapping highlight segments share heights, like copper: camera rays only."""
    camera_rays_only(obj)
    set_visible(obj, True)
    board.touched.add(obj.name)


def _item_mask(ids, chosen, item):
    """Per vertex: does its item (an index into `ids`) belong to `chosen`?"""
    if not chosen or not ids or not len(item):
        return np.zeros(len(item), bool)
    return np.fromiter((item_id in chosen for item_id in ids), bool, len(ids))[item]


def _copper_sources(kind):
    return [obj for obj in board.collection.all_objects if (obj.get("kls_copper") or ("", ""))[1] == kind]


def _refresh_tracks(wanted, only_layer=None):
    for source in _copper_sources("tracks"):
        layer = source["kls_copper"][0]
        if only_layer is not None and layer != only_layer:
            continue
        if not any(wanted.values()):  # nothing highlighted: skip reading the copper mesh
            for kind in wanted:
                obj = board.collection.all_objects.get(f"KLS {layer} highlight {kind}")
                if obj is not None:
                    _hide(obj)
            continue
        ids = list(source.get("kls_ids", ()))
        mesh = source.data
        item = read_attribute(mesh, "item", np.int32)
        coordinates = widths = None
        for kind, chosen in wanted.items():
            name = f"KLS {layer} highlight {kind}"
            keep = _item_mask(ids, chosen, item)
            if not keep.any():
                if (obj := board.collection.all_objects.get(name)) is not None:
                    _hide(obj)
                continue
            chosen_vertices = np.flatnonzero(keep)  # a segment's two vertices share its item
            if coordinates is None:
                coordinates = read_coordinates(mesh)
                widths = read_attribute(mesh, "width", np.float32)
            obj = owned_object(name)
            target = obj.data
            target.clear_geometry()
            target.vertices.add(len(chosen_vertices))
            target.vertices.foreach_set("co", coordinates[chosen_vertices].ravel())
            target.edges.add(len(chosen_vertices) // 2)
            target.edges.foreach_set("vertices", np.arange(len(chosen_vertices), dtype=np.int32))
            write_attribute(target, "width", "FLOAT", widths[chosen_vertices] + GROW_M)
            write_attribute(target, "item", "INT", item[chosen_vertices])
            target.update()
            z, thickness = copper_placement(layer, "tracks")
            up = outward(layer)
            if thickness:
                obj.location.z, lift = z, thickness + up * LIFT_M
            else:
                obj.location.z, lift = z + up * LIFT_M, 0.0
            set_modifier(obj, board.groups["tracks"], f"highlight_{kind}", {"Thickness": lift, "Up": up})
            _show(obj)


def _refresh_zones(wanted, only_layer=None):
    for obj in _copper_sources("zones"):
        layer = obj["kls_copper"][0]
        if only_layer not in (None, layer):
            continue
        zone_id = (list(obj.get("kls_ids", ())) or [None])[0]
        kind = next((kind for kind, chosen in wanted.items() if zone_id in chosen), "")
        if obj.get("kls_zone_highlight", "") == kind:
            continue
        obj["kls_zone_highlight"] = kind
        set_node_input(obj, "Material",
                       board.materials[f"highlight_{kind}"] if kind else materials.layer_material(layer))


def _refresh_pads(wanted, component_pads, only_layer=None):
    """Pads on a highlighted net (red-orange; blue on the diff-pair partner net) and
    pads of selected components (red-orange): a sheet just above each pad's outer
    copper surface (no side walls, so nothing is coplanar with the copper's)."""
    chosen_by_kind = {"selected": wanted["selected"] | component_pads, "pair": wanted["pair"]}
    for source in _copper_sources("pads"):
        layer = source["kls_copper"][0]
        if only_layer is not None and layer != only_layer:
            continue
        for kind, chosen in chosen_by_kind.items():
            _refresh_pad_sheet(source, layer, kind, chosen)


def _refresh_pad_sheet(source, layer, kind, chosen):
    name = f"KLS {layer} highlight pads" + ("" if kind == "selected" else f" {kind}")
    existing = board.collection.all_objects.get(name)
    if not chosen:  # nothing selected: skip reading the pad mesh
        if existing is not None:
            _hide(existing)
        return
    mesh = source.data
    item = read_attribute(mesh, "item", np.int32)
    keep = _item_mask(list(source.get("kls_ids", ())), chosen, item)
    if not keep.any():
        if existing is not None:
            _hide(existing)
        return
    edges = read_edges(mesh)
    edges = edges[keep[edges[:, 0]] & keep[edges[:, 1]]]  # rings of the chosen pads only
    chosen_vertices = np.flatnonzero(keep)
    index = np.full(len(item), -1, np.int32)
    index[chosen_vertices] = np.arange(len(chosen_vertices), dtype=np.int32)
    obj = owned_object(name)
    target = obj.data
    target.clear_geometry()
    target.vertices.add(len(chosen_vertices))
    target.vertices.foreach_set("co", read_coordinates(mesh)[chosen_vertices].ravel())
    target.edges.add(len(edges))
    target.edges.foreach_set("vertices", index[edges].ravel())
    write_attribute(target, "item", "INT", item[chosen_vertices])
    write_attribute(target, "hole", "INT", read_attribute(mesh, "hole", np.int32)[chosen_vertices])
    target.update()
    up = outward(layer)
    obj.location.z = transform.copper_z(layer, "pads", board.heights) + up * LIFT_M
    set_modifier(obj, board.groups["fill"], f"highlight_{kind}", {"Thickness": 0.0, "Up": up})
    _show(obj)


def _refresh_vias(wanted):
    """Highlighted vias: lands slightly larger and higher, barrel slightly narrower
    than the real via, so the colour sits on top of it and inside the hole."""
    source = board.collection.all_objects.get("KLS vias")
    ids = list(source.get("kls_ids", ())) if source is not None else []
    mesh = source.data if source is not None else None
    count = len(mesh.vertices) if mesh is not None else 0
    for kind, chosen in wanted.items():
        name = f"KLS vias highlight {kind}"
        keep = (np.fromiter((item_id in chosen for item_id in ids), bool, len(ids))
                if chosen and len(ids) == count else np.zeros(count, bool))
        if not keep.any():
            if (obj := board.collection.all_objects.get(name)) is not None:
                _hide(obj)
            continue
        chosen_vertices = np.flatnonzero(keep)
        obj = owned_object(name)
        target = obj.data
        target.clear_geometry()
        target.vertices.add(len(chosen_vertices))
        target.vertices.foreach_set("co", read_coordinates(mesh)[chosen_vertices].ravel())
        for attribute, change in (("diameter", GROW_M), ("drill", -GROW_M),
                                  ("z_top", LIFT_M), ("z_bottom", -LIFT_M), ("outer_top", 0.0), ("outer_bottom", 0.0),
                                  ("core_top", 0.0), ("core_bottom", 0.0), ("fill_copper", 0.0), ("cap", 0.0),
                                  ("drilled", 0.0), ("tent_top", 0.0), ("tent_bottom", 0.0), ("bare_barrel", 0.0)):
            values = read_attribute(mesh, attribute, np.float32)[chosen_vertices]
            write_attribute(target, attribute, "FLOAT", values + change)
        target.update()
        obj.location.z = 0
        barrel = board.materials[f"highlight_{kind}_barrel"]
        set_modifier(obj, board.groups["vias"], board.materials[f"highlight_{kind}"],
                     {"Top Thickness": copper_thickness("F.Cu"),
                      "Bottom Thickness": copper_thickness("B.Cu"),
                      **materials.via_inputs(barrel)})
        _show(obj)


def _component_extent(footprint_id, footprint):
    """(local min, local max) of a component in its footprint frame: the bound 3D
    model parts, else the placeholder envelope."""
    to_local = footprint.matrix_world.inverted()
    corners = []
    for name in board.model_objects_by_fp.get(footprint_id, ()):
        part = board.collection.all_objects.get(name)
        if part is not None and not part.hide_get():
            corners += [to_local @ (part.matrix_world @ Vector(corner)) for corner in part.bound_box]
    if corners:
        points = np.array([tuple(corner) for corner in corners])
        return points.min(axis=0), points.max(axis=0)
    box = board.collection.all_objects.get(f"KLS footprint placeholder {footprint_id}")
    if box is None or "kls_placeholder_width_m" not in box:
        return None
    center = np.array(tuple(to_local @ box.matrix_world.translation))
    half = np.array((box["kls_placeholder_width_m"] / 2, box["kls_placeholder_height_m"] / 2,
                     PLACEHOLDER_HEIGHT_M))
    low, high = center - half, center + half
    low[2], high[2] = 0.0, PLACEHOLDER_HEIGHT_M  # footprint frame: +z points away from the board
    return low, high


def _refresh_component_boxes(chosen):
    """Selected components: a translucent red-orange box around each one."""
    active = set()
    for footprint_id in chosen:
        footprint = board.collection.all_objects.get(f"KLS footprint {footprint_id}")
        extent = _component_extent(footprint_id, footprint) if footprint is not None else None
        if extent is None:
            continue
        low, high = extent[0] - BOX_MARGIN_M, extent[1] + BOX_MARGIN_M
        obj = owned_object(f"KLS footprint highlight {footprint_id}")
        obj["kls_highlight_box"] = 1
        single_point(obj.data)
        obj.matrix_world = footprint.matrix_world @ Matrix.Translation(Vector(tuple((low + high) / 2)))
        set_modifier(obj, board.groups["highlight_box"], "highlight_box",
                     {"Size": tuple(float(v) for v in np.abs(high - low))})
        _show(obj)
        active.add(obj.name)
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_highlight_box") == 1 and obj.name not in active:
            set_visible(obj, False)
