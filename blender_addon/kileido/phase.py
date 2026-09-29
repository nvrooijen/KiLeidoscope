"""Dynamic phase of differential pairs, as the bridge measures it (kileido_bridge/phase.py).

Each pair gets a ribbon along its centreline coloured by Δt = t_P - t_N, the delay
difference gathered from the start terminal up to each point: red where P is ahead
(its edge arrives first, Δt < 0), blue where N is ahead, neutral grey in phase; the
colour scale is the panel's and costs no new data. Markers stand on both terminals
(green start, violet end) with their pads as labels, and on the start of every
stretch the bridge found out of phase (amber); the markers stand on the board faces.
Ribbons sit at their copper's height and draw in front in Solid view; in Material
Preview, X-ray mode or the Board eye shows a pair inside the board. A click on a
ribbon or marker selects its copper or pads in KiCad (pick.py).

Frames: `phase_list` (the pairs that exist and the settings they were measured
with) and `phase_pair` (one pair). The panel's settings go back as `phase_settings`.
"""

import hashlib
import json
import math

import bpy
import numpy as np

from . import live, transform
from .objects import link_owned, owned_object, set_visible, write_attribute
from .state import board

PREFIX = "KLS phase "
LIFT_M = 10e-6  # the ribbon above (below, on the bottom) its copper
MARKER_HEIGHT_M = 1.0e-3
MARKER_RADIUS_M = 0.3e-3
LABEL_SIZE_M = 0.7e-3
COLORS = {"p_ahead": (0.95, 0.12, 0.08), "in_phase": (0.78, 0.78, 0.78), "n_ahead": (0.1, 0.3, 1.0),
          "start": (0.15, 0.85, 0.3), "end": (0.7, 0.3, 1.0), "excursion": (1.0, 0.65, 0.0)}
SCALE_NODE = "KLS phase scale"
ATTRIBUTE = "kls_dt"
DEFAULTS = {"tolerance_ps": 1.0, "min_length_mm": 5.0, "follow_series": True}
_syncing = False  # the pair list is being rebuilt: its index changes are not clicks


def _slot(key):
    """A short stable name part for a pair: keys can be longer than Blender's 63-character names."""
    return hashlib.blake2b(key.encode("utf-8"), digest_size=4).hexdigest()


def _objects(key=None):
    if board.collection is None:
        return []
    return [obj for obj in board.collection.all_objects
            if obj.get("kls_phase_key") is not None and (key is None or obj["kls_phase_key"] == key)]


# --- Frames -------------------------------------------------------------------------------

def apply(header, arrays):
    if header["type"] == "phase_list":
        _apply_list(header)
    else:
        _apply_pair(header, arrays)


def _apply_list(header):
    board.phase_list = {"keys": list(header.get("keys", ())), "settings": dict(header.get("settings", {})),
                        "warnings": list(header.get("warnings", ())), "pending": int(header.get("pending", 0))}
    keys = set(board.phase_list["keys"])
    for key in [key for key in board.phase if key not in keys]:
        del board.phase[key]
    for obj in _objects():
        if obj["kls_phase_key"] not in keys:
            _clear(obj)
    sync_list()
    if live.linked() and not _same_settings(board.phase_list["settings"]):
        send_settings()  # a reconnected bridge starts from its defaults


def _apply_pair(header, arrays):
    key = header["key"]
    board.phase[key] = {**header, "centre": np.array(arrays["centre"]), "dt": np.array(arrays["dt"])}
    if board.collection is None:
        return  # no board yet: drawn with the next pair frame or setting change
    draw(key)
    sync_list()


def _clear(obj):
    if obj.type == "MESH" and len(obj.data.vertices):
        obj.data.clear_geometry()
    set_visible(obj, False)


def redraw_all():
    """Every pair again (a setting that changes geometry, a new board collection)."""
    for key in board.phase:
        draw(key)


# --- Drawing ------------------------------------------------------------------------------

def _shown():
    return bool(getattr(bpy.context.scene, "kileido_phase_show", True))


def _material(role):
    name = f"KLS Phase {role}"
    material = bpy.data.materials.get(name)
    if material is not None:
        return material
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    emission = tree.nodes.new("ShaderNodeEmission")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    if role != "ribbon":
        material.diffuse_color = (*COLORS[role], 1.0)
        emission.inputs["Color"].default_value = (*COLORS[role], 1.0)
        return material
    # Δt / scale, clamped to -1..1, on a red - grey - blue ramp.
    attribute = tree.nodes.new("ShaderNodeAttribute")
    attribute.attribute_type = "GEOMETRY"
    attribute.attribute_name = ATTRIBUTE
    scale = tree.nodes.new("ShaderNodeValue")
    scale.name = SCALE_NODE
    scale.outputs[0].default_value = _scale()
    divide = tree.nodes.new("ShaderNodeMath")
    divide.operation = "DIVIDE"
    tree.links.new(attribute.outputs["Fac"], divide.inputs[0])
    tree.links.new(scale.outputs[0], divide.inputs[1])
    ramp_input = tree.nodes.new("ShaderNodeMapRange")
    ramp_input.inputs["From Min"].default_value = -1.0
    ramp_input.inputs["From Max"].default_value = 1.0
    ramp_input.clamp = True
    tree.links.new(divide.outputs[0], ramp_input.inputs["Value"])
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    elements = ramp.color_ramp.elements
    elements[0].color = (*COLORS["p_ahead"], 1.0)
    elements[1].position = 1.0
    elements[1].color = (*COLORS["n_ahead"], 1.0)
    middle = elements.new(0.5)
    middle.color = (*COLORS["in_phase"], 1.0)
    tree.links.new(ramp_input.outputs["Result"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], emission.inputs["Color"])
    material.diffuse_color = (*COLORS["in_phase"], 1.0)
    return material


def _scale():
    return max(0.01, float(getattr(bpy.context.scene, "kileido_phase_scale_ps", 5.0)))


def set_scale():
    """The colour scale changed: one value in the ribbon material."""
    material = bpy.data.materials.get("KLS Phase ribbon")
    if material is not None:
        material.node_tree.nodes[SCALE_NODE].outputs[0].default_value = _scale()


def _mesh(obj, vertices, faces):
    """A few hundred faces at most: from_pydata is quick enough."""
    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(np.asarray(vertices, np.float64).tolist(), [], [list(map(int, face)) for face in faces])
    mesh.update()


def _owned(name, key, material, ids):
    obj = owned_object(name)
    obj["kls_phase_key"] = key
    obj["kls_phase_ids"] = list(ids)
    obj.show_in_front = True  # Solid view: an inner-layer pair shows through the board
    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)
    board.touched.add(obj.name)
    return obj


def _board_z(z_nm):
    """Blender z of a point at a copper height, lifted off the copper outward."""
    z = np.asarray(z_nm, np.float64) * 1e-9
    return z + np.where(z > board.thickness_m / 2, LIFT_M, -LIFT_M)


def _ribbon(centre_nm, width_m):
    """(vertices, quads) of a flat strip along the centreline; vertical where the pair
    goes through vias (its cross direction stays the last horizontal one)."""
    xy = transform.xy_m(centre_nm[:, :2], board.origin_nm).astype(np.float64)
    z = _board_z(centre_nm[:, 2])
    count = len(xy)
    tangent = np.zeros((count, 2))
    if count > 1:
        forward = np.diff(xy, axis=0)
        tangent[:-1] += forward
        tangent[1:] += forward
    length = np.hypot(*tangent.T)
    valid = length > 1e-9
    if not valid.any():
        tangent[:] = (1.0, 0.0)
    else:
        last = np.maximum.accumulate(np.where(valid, np.arange(count), -1))
        last[last < 0] = np.flatnonzero(valid)[0]  # before the first move: the first direction
        tangent = tangent[last] / length[last][:, None]
    across = np.column_stack((-tangent[:, 1], tangent[:, 0])) * width_m / 2
    left = np.column_stack((xy + across, z))
    right = np.column_stack((xy - across, z))
    vertices = np.empty((count * 2, 3))
    vertices[0::2], vertices[1::2] = left, right
    faces = [np.array((2 * k, 2 * k + 1, 2 * k + 3, 2 * k + 2)) for k in range(count - 1)]
    return vertices, faces


def _cone(tip, up, height=MARKER_HEIGHT_M, radius=MARKER_RADIUS_M, sides=12):
    """A cone standing on its tip at `tip`, opening along +z (`up` = 1) or -z."""
    angles = np.linspace(0.0, 2 * math.pi, sides, endpoint=False)
    rim = np.column_stack((tip[0] + radius * np.cos(angles), tip[1] + radius * np.sin(angles),
                           np.full(sides, tip[2] + up * height)))
    vertices = np.vstack(([tip], rim, [(tip[0], tip[1], tip[2] + up * height)]))
    faces = [np.array((0, 1 + (k + 1) % sides, 1 + k)) for k in range(sides)]
    faces += [np.array((sides + 1, 1 + k, 1 + (k + 1) % sides)) for k in range(sides)]
    if up < 0:
        faces = [face[::-1] for face in faces]
    return vertices, faces


def _surface_z(layer):
    """The outer face on the terminal's side: markers stand outside the board."""
    bottom = layer.startswith("B.")
    return (0.0 if bottom else board.thickness_m), (-1.0 if bottom else 1.0)


def _label(name, key, role, text, location, up, ids, align):
    obj = board.collection.all_objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, bpy.data.curves.new(name, "FONT"))
        obj["kileido_owned"] = 1
        link_owned(obj)
    obj.data.body = text
    obj.data.size = LABEL_SIZE_M
    obj.data.align_x = align
    obj.data.align_y = "CENTER"
    obj.location = location
    obj.rotation_euler = (math.pi if up < 0 else 0.0, 0.0, 0.0)  # bottom labels read from below
    obj["kls_phase_key"] = key
    obj["kls_phase_ids"] = list(ids)
    obj.show_in_front = True
    material = _material(role)
    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)
    board.touched.add(obj.name)
    return obj


def draw(key):
    """Build (or rebuild) one pair's ribbon and markers from its last frame."""
    data = board.phase.get(key)
    if data is None or board.collection is None:
        return
    slot = _slot(key)
    shown = _shown()
    ids = data.get("ids", ())
    active = set()
    centre = data["centre"]
    if len(centre) >= 2:
        ribbon = _owned(f"{PREFIX}ribbon {slot}", key, _material("ribbon"), ids)
        vertices, faces = _ribbon(centre, max(float(data.get("width_nm", 0.0)) * 1e-9, 0.1e-3))
        _mesh(ribbon, vertices, faces)
        write_attribute(ribbon.data, ATTRIBUTE, "FLOAT", np.repeat(data["dt"].astype(np.float32), 2))
        ribbon.data.update()
        set_visible(ribbon, shown)
        active.add(ribbon.name)
    labels = getattr(bpy.context.scene, "kileido_phase_labels", True)
    line = transform.xy_m(centre[:, :2], board.origin_nm).astype(np.float64) if len(centre) else np.zeros((1, 2))
    for role, ahead in (("start", line[:12]), ("end", line[::-1][:12])):
        end = data[role]
        xy = transform.xy_m(np.array([[end["x"], end["y"]]]), board.origin_nm)[0]
        surface, up = _surface_z(end.get("layer", "F.Cu"))
        tip = (float(xy[0]), float(xy[1]), surface + up * LIFT_M)
        marker = _owned(f"{PREFIX}{role} {slot}", key, _material(role), end.get("ids", ()))
        _mesh(marker, *_cone(tip, up))
        set_visible(marker, shown)
        active.add(marker.name)
        outward = ahead[0] - ahead[-1]  # away from the pair, so the label clears marker and ribbon
        length = float(np.hypot(*outward))
        outward = outward / length if length > 1e-9 else np.array((-1.0 if role == "start" else 1.0, 0.0))
        where = np.asarray(tip[:2]) + outward * (MARKER_RADIUS_M + LABEL_SIZE_M / 2)
        label = _label(f"{PREFIX}label {role} {slot}", key, role, end.get("label", ""),
                       (float(where[0]), float(where[1]), tip[2]), up, end.get("ids", ()),
                       "LEFT" if outward[0] >= 0 else "RIGHT")
        set_visible(label, shown and labels)
        active.add(label.name)
    for index, excursion in enumerate(data.get("excursions", ())):
        xy = transform.xy_m(np.array([[excursion["x"], excursion["y"]]]), board.origin_nm)[0]
        # On the nearer board face, like the terminals: seen even where the pair runs inside.
        surface, up = _surface_z("B.Cu" if excursion["z"] * 1e-9 < board.thickness_m / 2 else "F.Cu")
        z = surface + up * LIFT_M
        marker = _owned(f"{PREFIX}excursion {slot} {index}", key, _material("excursion"), [excursion["id"]])
        marker["kls_phase_excursion"] = index
        _mesh(marker, *_cone((float(xy[0]), float(xy[1]), z), up, MARKER_HEIGHT_M, 1.3 * MARKER_RADIUS_M))
        set_visible(marker, shown)
        active.add(marker.name)
    for obj in _objects(key):
        if obj.name not in active:
            _clear(obj)


def refresh_visibility():
    shown, labels = _shown(), getattr(bpy.context.scene, "kileido_phase_labels", True)
    for obj in _objects():
        drawn = obj.type != "MESH" or len(obj.data.vertices) > 0
        set_visible(obj, drawn and shown and (labels or obj.type != "FONT") and
                    obj["kls_phase_key"] in board.phase)


# --- Settings and the pair list -----------------------------------------------------------

def settings():
    scene = bpy.context.scene
    return {"tolerance_ps": float(scene.kileido_phase_tolerance_ps),
            "min_length_mm": float(scene.kileido_phase_min_mm),
            "follow_series": bool(scene.kileido_phase_follow_series),
            "flipped": sorted(flipped())}


def _same_settings(reported):
    wanted = settings()
    try:
        return (abs(float(reported.get("tolerance_ps", -1)) - wanted["tolerance_ps"]) < 1e-6 and
                abs(float(reported.get("min_length_mm", -1)) - wanted["min_length_mm"]) < 1e-6 and
                bool(reported.get("follow_series")) == wanted["follow_series"] and
                sorted(reported.get("flipped", ())) == wanted["flipped"])
    except (TypeError, ValueError):
        return False


def send_settings(_scene=None, _context=None):
    live.request_phase_settings(settings())


def flipped():
    try:
        return set(json.loads(bpy.context.scene.kileido_phase_flipped or "[]"))
    except ValueError:
        return set()


def flip(key):
    """Walk this pair from its other end (the bridge measures again)."""
    keys = flipped() ^ {key}
    bpy.context.scene.kileido_phase_flipped = json.dumps(sorted(keys))
    send_settings()


def sync_list():
    """The panel's pair list (a Scene collection, for Blender's UIList) from the frames."""
    global _syncing
    scene = bpy.context.scene
    rows = getattr(scene, "kileido_phase_pairs", None)
    if rows is None:
        return
    keys = [key for key in board.phase_list.get("keys", ()) if key in board.phase]
    _syncing = True
    try:
        if [row.key for row in rows] != keys:
            active = rows[scene.kileido_phase_index].key if 0 <= scene.kileido_phase_index < len(rows) else ""
            rows.clear()
            for key in keys:
                rows.add().key = key
            scene.kileido_phase_index = keys.index(active) if active in keys else -1
        for row in rows:
            data = board.phase[row.key]
            row.name = data.get("name", row.key)
            row.route = f"{data['start']['label']} → {data['end']['label']}"
            row.skew_ps = float(data.get("skew_ps", 0.0))
            row.max_ps = float(data.get("max_ps", 0.0))
            row.delay_ps = (float(data.get("delay_p_ps", 0.0)) + float(data.get("delay_n_ps", 0.0))) / 2
            row.excursions = len(data.get("excursions", ()))
    finally:
        _syncing = False


def select_row(scene):
    """A click on a row of the pair list: select the pair's copper in KiCad."""
    if _syncing or not (0 <= scene.kileido_phase_index < len(scene.kileido_phase_pairs)):
        return
    data = board.phase.get(scene.kileido_phase_pairs[scene.kileido_phase_index].key)
    if data is not None and data.get("ids"):
        live.request_select(list(data["ids"]))


def active_pair():
    scene = bpy.context.scene
    rows = getattr(scene, "kileido_phase_pairs", ())
    index = getattr(scene, "kileido_phase_index", -1)
    return board.phase.get(rows[index].key) if 0 <= index < len(rows) else None


def sources_warning():
    """Which delay source the pairs use, when it is only a guess."""
    used = {source for data in board.phase.values() for source in data.get("sources", ())}
    return next((source for source in sorted(used) if source.startswith("FR-4")), "")

