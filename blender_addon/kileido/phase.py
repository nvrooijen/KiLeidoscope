"""Dynamic phase of differential pairs, as the bridge measures it (kileido_bridge/phase.py).

Both traces of each pair are tinted, P red and N blue (as the selection highlight
shows a pair), the stronger the further the two are out of phase there: where
they are in phase the copper shows almost as it is. Like the selection
highlight, the tint is a copy of the trace laid just over its copper, open over
drilled holes (holes.add_to). The colour scale is the panel's and costs no new data.

Pulses: a flash in each trace's own colour travels along P and N, launched
together at the start terminal, with an energy field around it: a glowing tube
standing out of the board, lit only where the pulse is. It reaches each point of
a trace at that point's delay (the same delays as the panel's numbers), slowed
down to picoseconds per second, so the lagging side visibly falls behind the one
ahead. Only the pulse clocks move, on a timer (held while a view renders with
Cycles, so that view converges).

Markers stand on both terminals (green start, violet end) with their pads as
labels, and at the start of every stretch the bridge found out of phase (amber);
they stand on the board faces. A pair inside the board shows in Solid view
(drawn in front); in Material Preview, X-ray mode or the Board eye shows it. A
click on a trace copy or a marker selects its copper or pads in KiCad (pick.py).

Frames: `phase_list` (the pairs that exist and the settings they were measured
with) and `phase_pair` (one pair). The panel's settings go back as `phase_settings`.
"""

import hashlib
import json
import math
import time

import bpy
import numpy as np

from . import holes, live, transform
from .objects import link_owned, owned_object, set_visible, write_attribute
from .placement import copper_thickness
from .state import board

PREFIX = "KLS phase "
LIFT_M = 2e-6  # the trace copy over (under, on the bottom) its copper's outer face
GROW_M = 4e-6  # 2 um wider per side, so the copper edge never shows through
MARKER_HEIGHT_M = 1.0e-3
MARKER_RADIUS_M = 0.3e-3
LABEL_SIZE_M = 0.7e-3
# Pure primaries at low emission stay pure in AgX; brighter bleaches them to pink and white.
COLORS = {"p": (1.0, 0.0, 0.0), "n": (0.0, 0.1, 1.0), "start": (0.15, 0.85, 0.3), "end": (0.7, 0.3, 1.0),
          "excursion": (1.0, 0.65, 0.0)}
TINT_GLOW = 1.0  # emission of the tint: brighter washes it pastel in AgX (as materials.HIGHLIGHT_GLOW found)
FLASH_GLOW = 1.0  # extra emission of the trace at a pulse's peak, times the panel's pulse brightness
IN_PHASE_ALPHA = 0.12  # a faint tint: which traces are measured, with their copper still seen
TRACE = "KLS Phase trace"  # + " P" / " N"
GLOW = "KLS Phase glow"  # + " P" / " N": the pulse's energy field around the trace
GLOW_RADIUS_M = 0.15e-3  # of the tube around each trace, its axis raised off the copper by half of it
GLOW_STRENGTH = 1.0  # the glow's emission: a denser, not a brighter, field keeps it pure red or blue
GLOW_DENSITY = 2.5  # how opaque the field's middle is at the pulse's peak (clamped at 1)
GLOW_SIDES = 10
COLUMN_GROW_M = 30e-6  # a via's light column: just outside its drill, over the plating
TAIL_NODE = "KLS phase pulse tail"
TAIL_SHARE = 0.6  # the comet tail's brightness next to the pulse's head
OLD_BLOOM = "KLS Bloom"  # a compositor group earlier versions set on the scene; removed when seen
SCALE_NODE, WIDTH_NODE, PULSE_NODE, BRIGHT_NODE = ("KLS phase scale", "KLS phase pulse width", "KLS phase pulses",
                                                   "KLS phase brightness")
COLOUR_NODE = "KLS phase colours"  # 1: the tint shows; 0 (Colours off): only the pulses light the trace
PULSES = 2  # per trace, evenly spaced over the loop
CLOCK_NODES = tuple(f"KLS phase clock {index}" for index in range(PULSES))
NODES_VERSION = 4  # materials built by an older add-on (a saved .blend) are rebuilt
LEAD, DELAY = "kls_lead", "kls_t"  # per point: ps ahead of the partner; ps from the start terminal
TICK_S = 1 / 30
DEFAULTS = {"tolerance_ps": 1.0, "min_length_mm": 5.0, "follow_series": True}
_syncing = False  # the pair list is being rebuilt: its index changes are not clicks
_clock_start = None  # time.monotonic() when the pulses were launched


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
    board.phase[key] = {**header, **{name: np.array(values) for name, values in arrays.items()}}
    if board.collection is None:
        return  # no board yet: drawn with the next pair frame or setting change
    draw(key)
    sync_list()
    set_pulses()


def _clear(obj):
    if obj.type == "MESH" and len(obj.data.vertices):
        obj.data.clear_geometry()
    set_visible(obj, False)


def redraw_all():
    """Every pair again (a setting that changes geometry, a new board collection)."""
    for key in board.phase:
        draw(key)


# --- Materials ----------------------------------------------------------------------------

def _shown():
    return bool(getattr(bpy.context.scene, "kileido_phase_show", True))


def _node(tree, kind, name=None, **inputs):
    node = tree.nodes.new(kind)
    if name:
        node.name = name
    for socket, value in inputs.items():
        node.inputs[socket].default_value = value
    return node


def _math(tree, operation, first, second=None, clamp=False):
    """A Math node on sockets or numbers; returns its output."""
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    node.use_clamp = clamp
    for index, value in enumerate((first, second)):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = value
        else:
            tree.links.new(value, node.inputs[index])
    return node.outputs[0]


def _value(tree, name, value):
    node = tree.nodes.new("ShaderNodeValue")
    node.name = name
    node.outputs[0].default_value = value
    return node.outputs[0]


def _pulse(tree, delay):
    """How lit a point is now: for each pulse, a Gaussian head at its clock and a comet
    tail fading behind it (points it passed a moment ago); 0 while pulses are off."""
    width, fade = _value(tree, WIDTH_NODE, _pulse_width()), _value(tree, TAIL_NODE, _pulse_tail())
    lit = None
    for name in CLOCK_NODES:
        gap = _math(tree, "SUBTRACT", delay, _value(tree, name, -1e3))  # > 0: not reached yet
        offset = _math(tree, "DIVIDE", gap, width)
        head = _math(tree, "EXPONENT", _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", offset, offset), -1.0))
        fading = _math(tree, "EXPONENT", _math(tree, "DIVIDE", _math(tree, "MINIMUM", gap, 0.0), fade))
        tail = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", fading, _math(tree, "LESS_THAN", gap, 0.0)),
                     TAIL_SHARE)
        one = _math(tree, "MAXIMUM", head, tail)
        lit = one if lit is None else _math(tree, "MAXIMUM", lit, one)
    return _math(tree, "MULTIPLY", lit, _value(tree, PULSE_NODE, 0.0))


def _fresh(name):
    """The material `name`, emptied for building when it is new or an older add-on built
    it; None when it is current."""
    material = bpy.data.materials.get(name)
    if material is not None and material.get("kls_nodes") == NODES_VERSION:
        return None
    material = material or bpy.data.materials.new(name)
    material.use_nodes = True
    material.node_tree.nodes.clear()
    material["kls_nodes"] = NODES_VERSION
    return material


def _trace_material(side):
    """P red or N blue, more opaque the further the pair is out of phase there (|lead|
    over the colour scale), plus a flash in the same colour where the pulse is now:
    exp(-((t - clock) / width)^2). Open over drilled holes, like the copper."""
    name = f"{TRACE} {side.upper()}"
    material = _fresh(name)
    if material is not None:
        material.diffuse_color = (*COLORS[side], 1.0)
        tree = material.node_tree
        lead = _node(tree, "ShaderNodeAttribute")
        lead.attribute_type, lead.attribute_name = "GEOMETRY", LEAD
        delay = _node(tree, "ShaderNodeAttribute")
        delay.attribute_type, delay.attribute_name = "GEOMETRY", DELAY
        share = _math(tree, "DIVIDE", lead.outputs["Fac"], _value(tree, SCALE_NODE, _scale()))
        tint = _math(tree, "ADD", _math(tree, "MULTIPLY", _math(tree, "ABSOLUTE", share, clamp=True),
                                        1.0 - IN_PHASE_ALPHA), IN_PHASE_ALPHA)
        tint = _math(tree, "MULTIPLY", tint, _value(tree, COLOUR_NODE, 1.0 if _shown() else 0.0))
        glow = _pulse(tree, delay.outputs["Fac"])
        color = _node(tree, "ShaderNodeMix")
        color.data_type = "RGBA"
        tree.links.new(glow, color.inputs["Factor"])
        color.inputs["A"].default_value = (*COLORS[side], 1.0)
        color.inputs["B"].default_value = (*COLORS[side], 1.0)
        emission = _node(tree, "ShaderNodeEmission")
        tree.links.new(color.outputs["Result"], emission.inputs["Color"])
        flash = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", glow, FLASH_GLOW),
                      _value(tree, BRIGHT_NODE, _brightness()))
        tree.links.new(_math(tree, "ADD", flash, TINT_GLOW), emission.inputs["Strength"])
        surface = _node(tree, "ShaderNodeMixShader")
        tree.links.new(_math(tree, "MAXIMUM", tint, glow), surface.inputs["Fac"])
        tree.links.new(_node(tree, "ShaderNodeBsdfTransparent").outputs[0], surface.inputs[1])
        tree.links.new(emission.outputs[0], surface.inputs[2])
        tree.links.new(surface.outputs[0], _node(tree, "ShaderNodeOutputMaterial").inputs["Surface"])
    material = bpy.data.materials[name]
    holes.add_to(material)  # follows the hole mask (a new board, moved vias): cheap once built
    material.surface_render_method = "BLENDED"  # a see-through tint over the copper, not dithered noise
    return material


def _glow_material(side):
    """The energy field around a trace: only where the pulse is, densest down the
    tube's middle as seen from the camera (Layer Weight facing). It covers what is
    behind it rather than adding to it, so it stays pure red or blue."""
    name = f"{GLOW} {side.upper()}"
    material = _fresh(name)
    if material is None:
        return bpy.data.materials[name]
    material.diffuse_color = (*COLORS[side], 0.3)
    material.surface_render_method = "BLENDED"
    material.use_backface_culling = True  # the near wall only: the far one would double the light
    tree = material.node_tree
    delay = _node(tree, "ShaderNodeAttribute")
    delay.attribute_type, delay.attribute_name = "GEOMETRY", DELAY
    pulse = _pulse(tree, delay.outputs["Fac"])
    facing = _node(tree, "ShaderNodeLayerWeight", Blend=0.5)
    core = _math(tree, "POWER", _math(tree, "SUBTRACT", 1.0, facing.outputs["Facing"]), 1.5)
    emission = _node(tree, "ShaderNodeEmission", Color=(*COLORS[side], 1.0))
    tree.links.new(_math(tree, "MULTIPLY", _value(tree, BRIGHT_NODE, _brightness()), GLOW_STRENGTH),
                   emission.inputs["Strength"])
    light = _node(tree, "ShaderNodeMixShader")  # covers what is behind: pure red or blue, not added to it
    tree.links.new(_math(tree, "MULTIPLY", _math(tree, "MULTIPLY", pulse, core), GLOW_DENSITY, clamp=True),
                   light.inputs["Fac"])
    tree.links.new(_node(tree, "ShaderNodeBsdfTransparent").outputs[0], light.inputs[1])
    tree.links.new(emission.outputs[0], light.inputs[2])
    tree.links.new(light.outputs[0], _node(tree, "ShaderNodeOutputMaterial").inputs["Surface"])
    return material


def _trace_materials():
    """Every material the pulse clock and colour scale drive (trace tints and glows)."""
    return [material for prefix in (TRACE, GLOW) for side in "PN"
            if (material := bpy.data.materials.get(f"{prefix} {side}")) is not None]


def _material(role):
    """A flat marker colour ("start", "end", "excursion")."""
    name = f"KLS Phase {role}"
    material = bpy.data.materials.get(name)
    if material is not None:
        return material
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.diffuse_color = (*COLORS[role], 1.0)
    tree = material.node_tree
    tree.nodes.clear()
    emission = _node(tree, "ShaderNodeEmission", Color=(*COLORS[role], 1.0))
    tree.links.new(emission.outputs[0], _node(tree, "ShaderNodeOutputMaterial").inputs["Surface"])
    return material


def _scale():
    return max(0.01, float(getattr(bpy.context.scene, "kileido_phase_scale_ps", 3.0)))


def _pulse_width():
    return max(0.1, float(getattr(bpy.context.scene, "kileido_phase_pulse_width_ps", 1.5)))


def _pulse_tail():
    return max(0.01, float(getattr(bpy.context.scene, "kileido_phase_pulse_tail_ps", 5.0)))


def _brightness():
    return max(0.0, float(getattr(bpy.context.scene, "kileido_phase_pulse_glow", 5.0)))


def _set(name, value):
    for material in _trace_materials():
        node = material.node_tree.nodes.get(name)  # the glow has no colour scale
        if node is not None and node.outputs[0].default_value != value:
            node.outputs[0].default_value = value


def set_scale():
    """The colour scale or a pulse setting changed: one value in each trace material."""
    _set(SCALE_NODE, _scale())
    _set(WIDTH_NODE, _pulse_width())
    _set(TAIL_NODE, _pulse_tail())
    _set(BRIGHT_NODE, _brightness())


# --- Pulses -------------------------------------------------------------------------------

def _pulsing(scene):
    """Pulses run whether or not the colours show (Colours only fades the tint)."""
    return getattr(scene, "kileido_phase_pulses", False) and bool(board.phase)


def _trace_shown(scene):
    """A trace copy carries both the tint and the pulses' flash: shown for either."""
    return _shown() or _pulsing(scene)


def _set_clock(scene, seconds):
    """The pulses `seconds` into a loop: from before the start terminals until the
    slowest pair's far end, at the panel's picoseconds per second."""
    width = _pulse_width()
    longest = max(max(float(data.get("delay_p_ps", 0.0)), float(data.get("delay_n_ps", 0.0)))
                  for data in board.phase.values())
    period = longest + 6 * width + _pulse_tail() * 4  # in before the start, out with its tail past the end
    rate = max(0.1, float(getattr(scene, "kileido_phase_pulse_rate", 30.0)))
    _set(PULSE_NODE, 1.0)
    for index, name in enumerate(CLOCK_NODES):  # evenly spaced over the loop
        _set(name, (seconds * rate + index * period / PULSES) % period - 3 * width)


def _cycles_viewport(scene):
    """A 3D view path tracing with Cycles: a moving clock would restart it every tick."""
    from .objects import view3d_spaces
    return scene.render.engine == "CYCLES" and any(space.shading.type == "RENDERED" for space in view3d_spaces())


def _pulse_tick():
    """The pulse clock (wall time). It holds still while a view renders with Cycles, so
    that view converges, and stops itself when the pulses go off."""
    scene = bpy.context.scene
    if not _pulsing(scene):
        _show_pulses(scene)
        return None
    if not _cycles_viewport(scene):
        _set_clock(scene, time.monotonic() - (_clock_start or time.monotonic()))
    return TICK_S


def _show_pulses(scene):
    """The glows shown only while pulsing (and the trace copies that carry the flash)."""
    for obj in _objects():
        if obj.get("kls_phase_glow"):
            set_visible(obj, _pulsing(scene) and len(obj.data.vertices) > 0)
        elif obj.get("kls_phase_trace"):
            set_visible(obj, _trace_shown(scene) and len(obj.data.vertices) > 0 and obj["kls_phase_key"] in board.phase)
    if not _pulsing(scene):
        _set(PULSE_NODE, 0.0)


def set_pulses():
    """Pulses on or off: start the clock, or put them out."""
    global _clock_start
    scene = bpy.context.scene
    _drop_old_bloom(scene)
    _show_pulses(scene)
    if not _pulsing(scene):
        stop_pulses()
    elif not bpy.app.timers.is_registered(_pulse_tick):
        _clock_start = time.monotonic()
        bpy.app.timers.register(_pulse_tick, first_interval=0.0)


def stop_pulses():
    if bpy.app.timers.is_registered(_pulse_tick):
        bpy.app.timers.unregister(_pulse_tick)


def _drop_old_bloom(scene):
    """Earlier versions could composite the scene through a bloom group: take it out."""
    group = bpy.data.node_groups.get(OLD_BLOOM)
    if group is None:
        return
    if getattr(scene, "compositing_node_group", None) == group:
        scene.compositing_node_group = None
    bpy.data.node_groups.remove(group)


# --- Drawing ------------------------------------------------------------------------------

def _mesh(obj, vertices, faces):
    """A few thousand faces at most: from_pydata is quick enough."""
    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(np.asarray(vertices, np.float64).tolist(), [], [list(map(int, face)) for face in faces])
    mesh.update()


def _owned(name, key, material, ids):
    """`ids` None: not clickable, a click passes through it (the glow)."""
    obj = owned_object(name)
    obj["kls_phase_key"] = key
    if ids is None:
        obj.pop("kls_phase_ids", None)
    else:
        obj["kls_phase_ids"] = list(ids)
    obj.show_in_front = True  # Solid view: an inner-layer pair shows through the board
    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)
    board.touched.add(obj.name)
    return obj


def _surface_z(z_nm):
    """Blender z just outside a copper layer's outer face (outer layers have thickness)."""
    z = np.asarray(z_nm, np.float64) * 1e-9
    top = z >= board.thickness_m - 1e-9
    bottom = z <= 1e-9
    return (z + np.where(top, copper_thickness("F.Cu") + LIFT_M, 0.0)
            - np.where(bottom, copper_thickness("B.Cu") + LIFT_M, 0.0) + np.where(top | bottom, 0.0, LIFT_M))


def _strip(xyz_nm, width_nm):
    """(vertices, quads, source index of each vertex) of a flat strip over a trace:
    one run per layer (a via's barrel, where only z changes, ends a run), mitred
    at the corners so neighbouring quads never overlap (they are see-through)."""
    xy = transform.xy_m(xyz_nm[:, :2], board.origin_nm).astype(np.float64)
    z = _surface_z(xyz_nm[:, 2])
    half = np.asarray(width_nm, np.float64) * 1e-9 / 2 + GROW_M / 2
    moved = np.hypot(*np.diff(xy, axis=0).T) > 1e-9  # a step along the board, not a barrel or a repeat
    same_z = np.abs(np.diff(z)) < 1e-9
    vertices, faces, source = [], [], []
    start = 0
    for index in range(len(xy)):
        last = index == len(xy) - 1
        if not last and moved[index] and same_z[index]:
            continue  # the run goes on
        run = [start] + [k + 1 for k in range(start, index) if moved[k]]  # drop repeated points
        start = index + 1
        if len(run) < 2:
            continue
        points = xy[run]
        direction = np.diff(points, axis=0)
        direction /= np.hypot(*direction.T)[:, None]
        normal = np.column_stack((-direction[:, 1], direction[:, 0]))
        at = np.vstack((normal[:1], normal[:-1] + normal[1:], normal[-1:]))
        at /= np.maximum(np.hypot(*at.T), 1e-12)[:, None]
        before = np.vstack((normal[:1], normal))  # each vertex's incoming segment normal
        mitre = 1 / np.maximum((at * before).sum(axis=1), 0.5)  # at most 2x at a very sharp turn
        offset = at * (half[run] * mitre)[:, None]
        first = len(vertices)
        for sign in (1, -1):
            vertices.extend(np.column_stack((points + sign * offset, z[run])))
        count = len(run)
        faces += [(first + k, first + k + 1, first + count + k + 1, first + count + k) for k in range(count - 1)]
        source += run * 2
    return np.asarray(vertices).reshape(-1, 3), faces, np.asarray(source, np.int64)


def _lands(vias, sides=24):
    """(vertices, fans, lead, delay) of a disc over each via land the route uses, on
    both layers it connects: tinted like the trace, the pulse crossing it as it
    enters and leaves (the drill stays open through the hole mask)."""
    vertices, faces, lead, delay = [], [], [], []
    angles = np.linspace(0.0, 2 * math.pi, sides, endpoint=False)
    for via in vias:
        centre = transform.xy_m(np.array([[via["x"], via["y"]]]), board.origin_nm)[0]
        radius = float(via.get("diameter", 0)) * 1e-9 / 2 + GROW_M / 2
        for layer, reached in zip(via.get("layers", ()), via.get("t", ())):
            if layer not in board.heights or radius <= 0:
                continue
            z = float(_surface_z([board.heights[layer] * 1e9])[0])
            first = len(vertices)
            vertices.append((*centre, z))
            vertices += [(centre[0] + radius * math.cos(a), centre[1] + radius * math.sin(a), z) for a in angles]
            faces += [(first, first + 1 + k, first + 1 + (k + 1) % sides) for k in range(sides)]
            lead += [float(via.get("lead", 0.0))] * (sides + 1)
            delay += [float(reached)] * (sides + 1)
    return np.asarray(vertices, np.float64).reshape(-1, 3), faces, np.asarray(lead), np.asarray(delay)


def _tube(xyz_nm, radius=GLOW_RADIUS_M, sides=GLOW_SIDES):
    """(vertices, quads, source index of each vertex) of a tube along a trace, one per
    layer (a via's barrel gets its own light column, `_columns`); its axis rises off
    outer copper by half its radius, so it stands out of the board. Rings follow the
    path by parallel transport (no twisting)."""
    xy = transform.xy_m(xyz_nm[:, :2], board.origin_nm).astype(np.float64)
    z = _surface_z(xyz_nm[:, 2])
    raw = np.asarray(xyz_nm[:, 2], np.float64) * 1e-9
    z += np.where(raw >= board.thickness_m - 1e-9, radius / 2, np.where(raw <= 1e-9, -radius / 2, 0.0))
    points = np.column_stack((xy, z))
    keep = np.concatenate(([True], np.linalg.norm(np.diff(points, axis=0), axis=1) > 1e-9))
    index = np.flatnonzero(keep)
    points = points[keep]
    along_board = np.hypot(*np.diff(points[:, :2], axis=0).T) > 1e-9
    vertices, faces, source = [], [], []
    start = 0
    for last in range(len(points)):
        if last < len(points) - 1 and along_board[last]:
            continue
        if last > start:
            run_vertices, run_faces = _tube_run(points[start:last + 1], radius, sides)
            faces += [tuple(len(vertices) + corner for corner in face) for face in run_faces]
            vertices += list(run_vertices)
            source += list(np.repeat(index[start:last + 1], sides))
        start = last + 1
    return np.asarray(vertices, np.float64).reshape(-1, 3), faces, np.asarray(source, np.int64)


def _tube_run(points, radius, sides):
    step = np.diff(points, axis=0)
    step /= np.linalg.norm(step, axis=1)[:, None]
    tangent = np.vstack((step[:1], step[:-1] + step[1:], step[-1:]))
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1), 1e-12)[:, None]
    normal = np.cross(tangent[0], (0.0, 0.0, 1.0) if abs(tangent[0][2]) < 0.9 else (1.0, 0.0, 0.0))
    angles = np.linspace(0.0, 2 * math.pi, sides, endpoint=False)
    rings = []
    for point, along in zip(points, tangent):
        normal = normal - np.dot(normal, along) * along
        normal /= np.linalg.norm(normal)
        binormal = np.cross(along, normal)
        rings.append(point + radius * (np.outer(np.cos(angles), normal) + np.outer(np.sin(angles), binormal)))
    faces = [(k * sides + a, k * sides + (a + 1) % sides, (k + 1) * sides + (a + 1) % sides, (k + 1) * sides + a)
             for k in range(len(points) - 1) for a in range(sides)]
    return np.vstack(rings), faces


def _columns(vias, sides=GLOW_SIDES + 4):
    """(vertices, quads, delay) of a light column in each via barrel the route passes,
    between the two layers it connects, just outside the drill: the pulse lights it
    on its way down (or up), as X-ray mode shows through the faded board."""
    vertices, faces, delay = [], [], []
    angles = np.linspace(0.0, 2 * math.pi, sides, endpoint=False)
    for via in vias:
        layers, reached = via.get("layers", ()), via.get("t", ())
        if len(layers) != 2 or any(layer not in board.heights for layer in layers):
            continue
        centre = transform.xy_m(np.array([[via["x"], via["y"]]]), board.origin_nm)[0]
        drill = float(via.get("drill") or 0) or float(via.get("diameter", 0)) / 2
        radius = drill * 1e-9 / 2 + COLUMN_GROW_M
        first = len(vertices)
        for layer, at in zip(layers, reached):
            z = board.heights[layer]
            vertices += [(centre[0] + radius * math.cos(a), centre[1] + radius * math.sin(a), z) for a in angles]
            delay += [float(at)] * sides
        faces += [(first + a, first + (a + 1) % sides, first + sides + (a + 1) % sides, first + sides + a)
                  for a in range(sides)]
    return np.asarray(vertices, np.float64).reshape(-1, 3), faces, np.asarray(delay)


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


def _board_face(layer):
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
    """Build (or rebuild) one pair's trace colours and markers from its last frame."""
    data = board.phase.get(key)
    if data is None or board.collection is None:
        return
    slot = _slot(key)
    shown = _shown()
    pulsing = _pulsing(bpy.context.scene)
    active = set()
    for side in ("p", "n"):
        xyz = data.get(f"{side}_xyz")
        if xyz is None or len(xyz) < 2:
            continue
        trace = _owned(f"{PREFIX}trace {side} {slot}", key, _trace_material(side), data.get("ids", ()))
        vertices, faces, source = _strip(xyz, data[f"{side}_width"])
        lead, delay = data[f"{side}_lead"][source], data[f"{side}_t"][source]
        land_vertices, land_faces, land_lead, land_delay = _lands(
            [via for via in data.get("vias", ()) if via.get("side") == side])
        faces = list(faces) + [tuple(len(vertices) + index for index in face) for face in land_faces]
        _mesh(trace, np.vstack((vertices, land_vertices)), faces)
        write_attribute(trace.data, LEAD, "FLOAT", np.concatenate((lead, land_lead)).astype(np.float32))
        write_attribute(trace.data, DELAY, "FLOAT", np.concatenate((delay, land_delay)).astype(np.float32))
        trace.data.update()
        trace["kls_phase_trace"] = 1
        set_visible(trace, _trace_shown(bpy.context.scene))
        active.add(trace.name)
        glow = _owned(f"{PREFIX}glow {side} {slot}", key, _glow_material(side), None)
        glow["kls_phase_glow"] = 1
        glow.show_in_front = False  # light in the scene, not a flat overlay
        glow.visible_shadow = False
        vertices, faces, source = _tube(xyz)
        column_vertices, column_faces, column_delay = _columns(
            [via for via in data.get("vias", ()) if via.get("side") == side])
        faces = list(faces) + [tuple(len(vertices) + corner for corner in face) for face in column_faces]
        _mesh(glow, np.vstack((vertices, column_vertices)), faces)
        glow.data.shade_smooth()  # a round field, not facets
        write_attribute(glow.data, DELAY, "FLOAT",
                        np.concatenate((data[f"{side}_t"][source], column_delay)).astype(np.float32))
        glow.data.update()
        set_visible(glow, pulsing)
        active.add(glow.name)
    labels = getattr(bpy.context.scene, "kileido_phase_labels", True)
    path = data.get("p_xyz", np.zeros((1, 3)))
    line = transform.xy_m(path[:, :2], board.origin_nm).astype(np.float64)
    for role, ahead in (("start", line[:12]), ("end", line[::-1][:12])):
        end = data[role]
        xy = transform.xy_m(np.array([[end["x"], end["y"]]]), board.origin_nm)[0]
        surface, up = _board_face(end.get("layer", "F.Cu"))
        tip = (float(xy[0]), float(xy[1]), surface + up * LIFT_M)
        marker = _owned(f"{PREFIX}{role} {slot}", key, _material(role), end.get("ids", ()))
        _mesh(marker, *_cone(tip, up))
        set_visible(marker, shown)
        active.add(marker.name)
        outward = ahead[0] - ahead[-1]  # away from the pair, so the label clears marker and traces
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
        surface, up = _board_face("B.Cu" if excursion["z"] * 1e-9 < board.thickness_m / 2 else "F.Cu")
        marker = _owned(f"{PREFIX}excursion {slot} {index}", key, _material("excursion"), [excursion["id"]])
        marker["kls_phase_excursion"] = index
        _mesh(marker, *_cone((float(xy[0]), float(xy[1]), surface + up * LIFT_M), up, MARKER_HEIGHT_M,
                             1.3 * MARKER_RADIUS_M))
        set_visible(marker, shown)
        active.add(marker.name)
    for obj in _objects(key):
        if obj.name not in active:
            _clear(obj)


def refresh_visibility():
    """Colours, labels or pulses switched: markers and labels follow Colours; the trace
    copies stay while pulsing, with their tint faded out (COLOUR_NODE)."""
    shown, labels = _shown(), getattr(bpy.context.scene, "kileido_phase_labels", True)
    _set(COLOUR_NODE, 1.0 if shown else 0.0)
    for obj in _objects():
        drawn = obj.type != "MESH" or len(obj.data.vertices) > 0
        set_visible(obj, drawn and shown and (labels or obj.type != "FONT") and
                    obj["kls_phase_key"] in board.phase)
    set_pulses()  # the glows show only while pulsing


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

