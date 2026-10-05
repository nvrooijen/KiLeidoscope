"""Automatic 3D-model export; live updates only move linked meshes.

kicad-cli exports the bridge's live board copy (else the saved board). Footprint
moves never need an export: IPC moves the bound meshes. A new export runs only
when a footprint's model definitions change (`model_signature`), and exports are
cached by that signature and the path variables the models use, so reopening a
board imports the cached GLB. An export with model files missing is not cached:
the next open looks for them again (a path set in KiCad, a file added).
"""

import bisect
import csv
import hashlib
import json
import math
import re
from pathlib import Path

import bpy
import numpy as np
from mathutils import Euler, Matrix

from . import components, cut, focus, fold, highlight, kicad_cli
from .objects import link_owned, set_visible
from . import state
from .state import board
from .watcher import BoardWatcher

_BLENDER_SUFFIX = re.compile(r"\.\d{3}$")
_REFERENCE = re.compile(r'\(property\s+"Reference"\s+"((?:[^"\\]|\\.)*)"')
_MODEL = re.compile(r'\(model\s+"')
_MODEL_PATH = re.compile(r'\(model\s+"((?:[^"\\]|\\.)*)"')
_FOOTPRINT = re.compile(r'\(footprint\s+"')
_UUID = re.compile(r'\(uuid\s+"([^"]+)"')
MATCH_TOLERANCE_M = 0.0002  # a GLB root this close to a footprint origin belongs to it
GLB_ASSET = "kls_glb_asset"  # on a GLB import's collection: its content's id (`load_glb`)


def _block(text, start):
    """The balanced (...) expression starting at `start` (quotes respected)."""
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return text[start:]


def model_signature(board_text):
    """What the GLB depends on beyond footprint placement: each footprint's identity
    (UUID, the first `uuid` after `(footprint`; reference) and its `(model ...)`
    blocks (file, offset, scale, rotation, hidden, opacity). Bound meshes follow
    footprint ids, so a footprint deleted and re-added needs a new export."""
    references = [(match.start(), match[1]) for match in _REFERENCE.finditer(board_text)]
    reference_starts = [start for start, _ in references]
    footprints = []
    for match in _FOOTPRINT.finditer(board_text):
        uuid = _UUID.search(board_text, match.start())
        footprints.append((match.start(), uuid[1] if uuid else ""))
    footprint_starts = [start for start, _ in footprints]
    digest = hashlib.blake2b(digest_size=16)
    for match in _MODEL.finditer(board_text):
        owner = bisect.bisect(reference_starts, match.start()) - 1
        footprint = bisect.bisect(footprint_starts, match.start()) - 1
        digest.update((footprints[footprint][1] if footprint >= 0 else "").encode("utf-8") + b"\0")
        digest.update((references[owner][1] if owner >= 0 else "").encode("utf-8") + b"\0")
        digest.update(_block(board_text, match.start()).encode("utf-8") + b"\0")
    return digest.hexdigest()


def _read_positions(path):
    with path.open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def _on_board_change(job, memory):
    """Worker: export only when model definitions (or the path variables they use)
    changed; moves alone are applied live. The key is kept once an export succeeded,
    so a failed one is retried on the next file change. One with model files missing
    is kept too: re-exporting on every edit would not find them either."""
    data = job.source.read_bytes()
    text = data.decode("utf-8")
    cli = kicad_cli.executable(job.export)
    project = kicad_cli.project_dir(job.export, job.source)
    variables = kicad_cli.path_variables(_MODEL_PATH.findall(text), job.export.get("kicad_settings", ""))
    key = kicad_cli.cache_key(model_signature(text), kicad_cli.identity(cli), project, variables)
    if key == memory.get("key"):
        return
    if _export(job, cli, key, data, project):
        memory["key"] = key


def _export(job, cli, key, data, project):
    """Worker: KiCad CLI resolves the board's STEP/VRML paths into one GLB, plus the
    saved footprint positions it was exported at, from `data` (the board bytes `key`
    was made from). Results are cached by `key`. True once a result was handed over.

    A model kicad-cli cannot read (measured: a malformed VRML file) is left out of a
    GLB that is still written, but kicad-cli then exits 2: the GLB is used whenever it
    exists, and kicad-cli's messages go to Blender's output (blender.log)."""
    with job.scratch_directory("kileido_models_") as directory:
        entry = kicad_cli.lookup("models", key)
        if entry is not None:  # only exports without problems are cached
            job.emit(directory, glb=str(entry / "models.glb"), positions=_read_positions(entry / "positions.csv"),
                     problems=[], asset=key)
            return True
        board_file = kicad_cli.board_copy(job.source, data, directory)
        output = directory / "models.glb"
        result = kicad_cli.run(
            [cli, "pcb", "export", "glb", "--no-board-body", *kicad_cli.defines(project),
             "--output", output, board_file], 120)
        messages = (result.stdout + result.stderr).strip()
        problems = kicad_cli.model_problems(messages)
        if result.returncode != 0 or problems:
            print(f"KiLeidoscope model export, kicad-cli exit code {result.returncode}:\n{messages}")
        if not output.is_file():
            raise RuntimeError((messages or f"kicad-cli exit code {result.returncode}")[-400:])
        missing = len(problems)
        positions_file = directory / "positions.csv"
        positions_result = kicad_cli.run(
            [cli, "pcb", "export", "pos", "--format", "csv", "--units", "mm",
             "--side", "both", "--output", positions_file, board_file], 30)  # takes no --define-var
        if positions_result.returncode != 0 or not positions_file.is_file():
            raise RuntimeError("KiCad could not export footprint positions: " +
                               (positions_result.stderr or positions_result.stdout).strip()[-300:])
        positions = _read_positions(positions_file)
        if job.stop.is_set():
            return False
        (directory / "meta.json").write_text(json.dumps({"missing_count": missing}), encoding="utf-8")
        entry = directory  # missing files may turn up (the key cannot see them): not cached
        if not missing:
            entry = kicad_cli.store("models", key, {name: directory / name for name in
                                                    ("models.glb", "positions.csv", "meta.json")}) or directory
        job.emit(directory, glb=str(entry / "models.glb"), positions=positions, problems=problems, asset=key)
        return True
    return False  # the scratch folder reported the failure


def _on_result(result):
    """Main thread: one import may briefly pause Blender."""
    loaded = load_glb(result.data["glb"], result.data["positions"], result.data["asset"])
    return (f"Models loaded: {loaded['matched']} footprints, {loaded['parts']} parts" +
            problems_text(result.data["problems"]))


def problems_text(problems):
    """The status suffix naming the model files an export left out (first three names;
    kicad-cli's full messages are in blender.log)."""
    if not problems:
        return ""
    names = list(dict.fromkeys(re.split(r"[\\/]", path)[-1] for path in problems))
    return (f"; {len(problems)} not found or unreadable: " + ", ".join(names[:3]) +
            (", …" if len(names) > 3 else ""))


_watcher = BoardWatcher(
    "model export", _on_board_change, _on_result,
    starting=lambda export: "Finding 3D models from the KiCad board…",
    unavailable="3D models unavailable: save the board in KiCad, then press Resync",
    failed="Model export failed", import_failed="Model import failed", first_interval=0.1)


def export_status():
    return _watcher.status


def follow_board(board_path, export=None):
    """Start automatic model loading when a board snapshot becomes visible."""
    _watcher.follow(board_path, export)


def stop_following():
    _watcher.stop()


def drain():
    """Handle one finished export now (headless tools; the GUI timer does this)."""
    return _watcher.drain()


def _reference(name):
    return _BLENDER_SUFFIX.sub("", name)


def _source_root(objects):
    roots = [obj for obj in objects if obj.parent is None]
    if not roots:
        raise ValueError("GLB contains no root object")
    return max(roots, key=lambda obj: len(obj.children))


def _source_meshes(reference_obj):
    """The meshes of one GLB reference node. kicad-cli puts a single-part model's mesh
    on the reference node itself (R1 [mesh], no children); an assembly's parts are
    its children (D1 -> Body, PinK, ...)."""
    pending = list(reference_obj.children)
    meshes = [reference_obj] if reference_obj.type == "MESH" else []
    while pending:
        obj = pending.pop()
        pending.extend(obj.children)
        if obj.type == "MESH":
            meshes.append(obj)
    return sorted(meshes, key=lambda obj: obj.name)


def _mesh_signature(mesh):
    """Hash imported mesh data; only byte-identical geometry and shading may share it."""
    digest = hashlib.sha256()
    for collection, field, dtype, width in (
            (mesh.vertices, "co", np.float32, 3),
            (mesh.loops, "vertex_index", np.int32, 1),
            (mesh.polygons, "loop_start", np.int32, 1),
            (mesh.polygons, "loop_total", np.int32, 1),
            (mesh.polygons, "material_index", np.int32, 1)):
        values = np.empty(len(collection) * width, dtype=dtype)
        collection.foreach_get(field, values)
        digest.update(values.tobytes())
    for layer in mesh.uv_layers:
        digest.update(layer.name.encode("utf-8"))
        values = np.empty(len(layer.data) * 2, dtype=np.float32)
        layer.data.foreach_get("uv", values)
        digest.update(values.tobytes())
    for layer in mesh.color_attributes:
        digest.update((layer.name + layer.domain + layer.data_type).encode("utf-8"))
        values = np.empty(len(layer.data) * 4, dtype=np.float32)
        layer.data.foreach_get("color", values)
        digest.update(values.tobytes())
    for material in mesh.materials:
        digest.update((material.name if material else "<none>").encode("utf-8"))
    return digest.hexdigest()


def _matches(root, saved_positions=None):
    """Match reference nodes to live footprints, using position for duplicate refs."""
    targets = {}
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_footprint") == 1 and obj.get("kls_active") == 1:
            targets.setdefault(obj.get("kls_reference"), []).append(obj)
    sources = {}
    for obj in root.children:
        if _source_meshes(obj):
            sources.setdefault(_reference(obj.name), []).append(obj)
    matches = []
    origin_x, origin_y = board.origin_nm

    def distance(source, target):
        sx, sy = source.matrix_world.translation[:2]
        tx, ty = _saved_footprint_matrix(target, saved_positions or []).translation[:2]
        return ((sx - (tx + origin_x * 1e-9)) ** 2 +
                (sy - (ty - origin_y * 1e-9)) ** 2) ** 0.5

    for ref, candidates in sources.items():
        footprints = targets.get(ref, [])
        if len(candidates) == len(footprints) == 1:
            matches.append((candidates[0], footprints[0]))
            continue
        if len(footprints) == 1 and len(candidates) > 1:
            target = footprints[0]
            # KiCad names the model roots of one footprint U5, U5.001, ... Each sits
            # where its model offset puts it, and all of them can lie far from the
            # footprint origin (CM5 MINIMA's module footprints: two connectors 21 and
            # 40 mm away). The reference places them, as it does a single root.
            if len(target.get("kls_model_paths", ())) >= len(candidates):
                matches.extend((source, target) for source in candidates)
                continue
        pairs = []
        for source in candidates:
            for target in footprints:
                separation = distance(source, target)
                if separation <= MATCH_TOLERANCE_M:
                    pairs.append((separation, source, target))
        used_sources, used_targets = set(), set()
        for _, source, target in sorted(pairs, key=lambda row: row[0]):
            if source not in used_sources and target not in used_targets:
                matches.append((source, target))
                used_sources.add(source)
                used_targets.add(target)
    return matches


def _model_index(source, target, matches):
    """Position of this GLB root among the roots bound to the same footprint (name order)."""
    siblings = sorted((s for s, t in matches if t == target), key=lambda obj: obj.name)
    return siblings.index(source), len(siblings)


def dnp_excluded(footprint):
    """True for a footprint KiCad marks "Do not populate" while the panel hides them."""
    return footprint.get("kls_dnp") == 1 and not getattr(bpy.context.scene, "kileido_show_dnp", True)


def placeholder_visible(footprint, footprint_id):
    """A footprint's missing-model box shows while it has a model file KiCad would show,
    none of its models are bound, and it is not an excluded DNP part."""
    if footprint is None or footprint.get("kls_active") != 1 or dnp_excluded(footprint):
        return False
    flags = list(footprint.get("kls_model_visible", ()))
    return (bool(footprint.get("kls_model_paths")) and (not flags or any(flags)) and
            footprint_id not in board.model_bound)


def model_is_visible(footprint, model_obj):
    """Live KiCad 3D-model visibility for one imported model part.

    `kls_model_visible` holds KiCad's live per-model "Show" flags (IPC), one per model
    slot of the footprint. KiCad's GLB export omits models hidden at save time
    (measured), so the GLB roots map onto the slots that were visible then.
    """
    if footprint is None or footprint.get("kls_active") != 1 or dnp_excluded(footprint):
        return False
    flags = [bool(flag) for flag in footprint.get("kls_model_visible", ())]
    if not flags:
        return True
    index = int(model_obj.get("kls_model_index", 0))
    count = int(model_obj.get("kls_model_count", 1))
    if count == len(flags):  # every model slot was exported: direct mapping
        return flags[index] if index < len(flags) else any(flags)
    shown = [slot for slot, flag in enumerate(flags) if flag]
    if count == len(shown):  # the hidden slots were skipped by the export
        return True
    return any(flags)  # visibility changed since the last save; refreshed on next save


def _saved_footprint_matrix(target, saved_positions):
    """Build KiCad's saved footprint frame in Blender's board-centred axes."""
    entries = [row for row in saved_positions
               if row["Ref"] == target.get("kls_reference") and
               row["Side"].lower() == target.get("kls_side")]
    if not entries:
        return target.matrix_world.copy()
    origin_x, origin_y = board.origin_nm
    tx, ty = target.matrix_world.translation[:2]
    row = min(entries, key=lambda item:
              (float(item["PosX"]) * 0.001 - origin_x * 1e-9 - tx) ** 2 +
              (float(item["PosY"]) * 0.001 + origin_y * 1e-9 - ty) ** 2)
    x = float(row["PosX"]) * 0.001 - origin_x * 1e-9
    # KiCad's position CSV already negates PCB Y; its value matches GLB Y.
    y = float(row["PosY"]) * 0.001 + origin_y * 1e-9
    z = 0.0 if row["Side"].lower() == "bottom" else board.thickness_m
    rotation = Euler((0.0, 0.0, math.radians(float(row["Rot"]))))
    scale = (1.0, -1.0, -1.0) if row["Side"].lower() == "bottom" else (1.0, 1.0, 1.0)
    return (Matrix.Translation((x, y, z)) @ rotation.to_matrix().to_4x4() @
            Matrix.Diagonal((*scale, 1.0)))


def bind_root(root, asset_hash, saved_positions=None):
    """Link imported mesh data to existing footprint transforms, without file I/O."""
    board.drop_if_freed("model bind")
    if board.collection is None:
        raise ValueError("Load or connect a board before loading models")
    fold.unfold_parts()  # models match and sit on the footprints' flat places; folded again below
    bpy.context.view_layer.update()
    matches = _matches(root, saved_positions)
    active = set()
    mesh_cache = {}
    board.model_objects_by_fp.clear()
    origin_x, origin_y = board.origin_nm
    rebase = Matrix.Translation((-origin_x * 1e-9, origin_y * 1e-9, 0.0))
    for source, target in matches:
        footprint_id = target["kls_id"]
        model_index, model_count = _model_index(source, target, matches)
        names = board.model_objects_by_fp.setdefault(footprint_id, [])
        for mesh_source in _source_meshes(source):
            part = len(names)
            key = (tuple(target.get("kls_model_paths", ())), asset_hash,
                   _mesh_signature(mesh_source.data))
            geometry = mesh_cache.setdefault(key, mesh_source.data)
            if mesh_source.data != geometry:
                redundant = mesh_source.data
                mesh_source.data = geometry
                if redundant.users == 0:
                    bpy.data.meshes.remove(redundant)
            name = components.model_name(target.get("kls_reference", ""), target.get("kls_model_paths", ()), part)
            clone = components.find(components.MODEL, footprint_id, part)
            if clone is None:
                clone = bpy.data.objects.new(name, geometry)
                link_owned(clone, "components")
            clone.data = geometry  # linked geometry, including UVs and materials
            prior = clone.get("kls_model_local_matrix")
            if clone.get("kls_model_asset") == asset_hash and prior is not None and len(prior) == 16:
                local = Matrix(tuple(prior[row * 4:row * 4 + 4] for row in range(4)))
            else:
                saved_frame = _saved_footprint_matrix(target, saved_positions or [])
                local = saved_frame.inverted() @ rebase @ mesh_source.matrix_world
                clone["kls_model_local_matrix"] = [local[row][col]
                                                   for row in range(4) for col in range(4)]
            components.attach(clone, target, local)
            clone["kileido_owned"] = 1
            clone["kls_model_fp_id"] = footprint_id
            clone["kls_side"] = target.get("kls_side", "")
            clone["kls_model_asset"] = asset_hash
            clone["kls_model_index"] = model_index
            clone["kls_model_count"] = model_count
            clone["kls_model_part"] = part
            components.rename(clone, name)
            set_visible(clone, model_is_visible(target, clone))
            names.append(clone.name)
            active.add(clone.name)
        if not names:
            board.model_objects_by_fp.pop(footprint_id, None)
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_model_fp_id") is not None and obj.name not in active:
            set_visible(obj, False)
    board.model_bound = set(board.model_objects_by_fp)
    highlight.refresh()  # component boxes size themselves to the loaded models
    focus.refresh()  # model materials fade in focus mode too
    cut.add_materials()  # and are cut open with the board
    for obj in tuple(board.collection.all_objects):
        if obj.get("kls_footprint_placeholder") == 1:
            footprint_id = obj.get("kls_footprint_id")
            footprint = components.find(components.FRAME, footprint_id)
            set_visible(obj, placeholder_visible(footprint, footprint_id))
    unbound = [footprint_id for footprint_id in list(board.model_bound) if not board.model_objects_by_fp.get(footprint_id)]
    fold.refold_parts()  # a folded board folds its new models with it
    state.log(f"models bound: {len(board.model_bound)} footprints, {len(active)} parts, "
              f"{len(matches)} GLB roots matched, {len(unbound)} without parts")
    return {"matched": len(board.model_bound),
            "parts": len(active),
            "distinct_meshes": len({board.collection.all_objects[name].data.as_pointer()
                                    for name in active}),
            "unmatched": sum(bool(_source_meshes(obj)) for obj in root.children) - len(matches)}


def _remove_superseded(current):
    """Drop earlier GLB imports (model definitions changed since): their objects, and
    the meshes, materials and images nothing else uses. Bound model parts now link
    `current`'s meshes; a part still on an old mesh (its footprint unmatched) keeps it."""
    for collection in [collection for collection in bpy.data.collections
                       if collection != current and collection.get("kileido_owned") == 1
                       and collection.get(GLB_ASSET) is not None]:
        meshes = {obj.data for obj in collection.objects if obj.type == "MESH"}
        materials = {slot.material for obj in collection.objects for slot in obj.material_slots if slot.material}
        materials |= {material for mesh in meshes for material in mesh.materials if material is not None}
        images = {node.image for material in materials if material.node_tree is not None
                  for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image is not None}
        for obj in tuple(collection.objects):
            if tuple(obj.users_collection) == (collection,):
                bpy.data.objects.remove(obj)
            else:
                collection.objects.unlink(obj)
        bpy.data.collections.remove(collection)
        for blocks, used in ((bpy.data.meshes, meshes), (bpy.data.materials, materials), (bpy.data.images, images)):
            for block in used:
                if block.users == 0:
                    blocks.remove(block)


def load_glb(filepath, saved_positions=None, asset=None):
    """Import a worker-generated KiCad GLB; Blender's importer may pause once.
    `asset` names the GLB's content (the export's cache key); without one the file
    is hashed."""
    path = Path(filepath).resolve()
    if path.suffix.lower() != ".glb":
        raise ValueError("KiCad model export is not a .glb file")
    if board.collection is None:
        raise ValueError("Load or connect a board before loading models")
    asset_hash = asset or hashlib.sha256(path.read_bytes()).hexdigest()
    name = f"KiLeidoscope models {asset_hash[:16]}"
    collection = bpy.data.collections.get(name)
    if collection is None:
        existing = set(bpy.data.objects)
        selected = tuple(bpy.context.selected_objects)
        active = bpy.context.view_layer.objects.active
        bpy.ops.import_scene.gltf(filepath=str(path))
        imported = set(bpy.data.objects) - existing
        if not imported:
            raise ValueError("GLB import contained no objects")
        collection = bpy.data.collections.new(name)
        collection["kileido_owned"] = 1
        collection[GLB_ASSET] = asset_hash
        bpy.context.scene.collection.children.link(collection)
        for obj in imported:
            collection.objects.link(obj)
            for previous in tuple(obj.users_collection):
                if previous != collection:
                    previous.objects.unlink(obj)
            obj.select_set(False)
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
    elif collection.get(GLB_ASSET) != asset_hash:
        raise ValueError(f"Collection {name} is not a matching KiLeidoscope model cache")
    root = _source_root(set(collection.objects))
    result = bind_root(root, asset_hash, saved_positions)
    for obj in collection.objects:
        set_visible(obj, False)
    collection.hide_viewport = True
    collection.hide_render = True
    _remove_superseded(collection)
    result["cache_hash"] = asset_hash[:16]
    return result
