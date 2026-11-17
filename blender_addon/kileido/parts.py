"""Part libraries: parts made in Blender that take the place of KiCad's 3D models, matched
by footprint. See docs/part-libraries.md.

A part is a collection named after its footprint ("LED_0603_1608Metric"), or after the
footprint's "KiLeidoscope Model" field. A library is a .blend of such collections, or a
folder of .blend files, each named after the footprint whose part it holds. They are tried
in order: My parts (the user's own folder, where Save to my parts writes), the libraries
added in the Parts tab, then the libraries other add-ons added. A footprint no library has
a part for keeps KiCad's model.

A .blend library may hold an index, a text datablock "kls_index" of JSON:

    {"L_0603_1608Metric": [{"part": "L_0603_1608Metric"},
                           {"part": "FerriteBead_0603_1608Metric", "reference_prefixes": ["FB"]}]}

It names the part for each footprint, for a library where one part stands in for several
footprints or two parts share a footprint: an entry with `reference_prefixes` is for the
footprints whose reference starts with one of them ("FB12") and comes before one without.
A footprint the index does not list is matched by collection name, as without an index.

Another add-on reaches this module without knowing how KiLeidoscope was loaded:

    import sys
    parts = sys.modules.get("kileidoscope_parts")  # None: KiLeidoscope isn't running
    if parts is not None and parts.API_VERSION >= 1:
        parts.add_library("My library", "C:/.../my_library.blend", place=place)

`add_library`, `has_library`, `remove_library`, `place_again`, `refresh_all` and API_VERSION are public and
kept stable;
nothing else here is meant for other add-ons. `place(objects, footprint, part)`, optional,
is called after one of that library's parts is placed: `objects` are the placed objects,
`footprint` is {"reference", "library_id", "value", "fields", "pins"} as KiCad has them
(`pins`: pad number -> (pin name, pin type)), `part` is the part's index entry
({"part": name} without an index). What it makes for a placed object (a mesh of its own)
it marks with a custom property kls_dressed, and KiLeidoscope removes that with the part.

A placed part is KiLeidoscope model objects: copies of the part's objects (their meshes
and materials shared), children of the footprint's frame, listed in
board.model_objects_by_fp in place of KiCad's models. So they follow the footprint, hide
with KiCad's model visibility, fade in X-ray, cut open and fold as KiCad's models do. A
footprint without a KiCad model gets its part too. Objects may carry a role, kls_role:
"case" fades when X-ray opens the selected part, "contacts" take solder, "field" shows
only with Field lines; any other stays as it is.
"""

import json
import math
import os
import re
import sys
from pathlib import Path

import bpy
from mathutils import Matrix

from . import components, cut, focus, fold, highlight, models, settings, solder
from .objects import link_owned, lookup, set_visible
from . import state
from .state import board

API_VERSION = 1
MODULE_ALIAS = "kileidoscope_parts"  # the name other add-ons look this module up by
INDEX = "kls_index"  # a library .blend's index: a text datablock of JSON
MODEL_FIELD = "kileidoscope model"  # a footprint field naming its part, compared casefolded
FIELD = "field"  # field lines' role: placed, shown only with Field lines (models.model_is_visible)
PART = "kls_library_part"  # on a placed object: 1
PART_BASE = 1000  # a placed part's objects count from here (kls_model_part): clear of KiCad's model parts
MY_PARTS = "My parts"
MY_KEY = "my"
_FILE_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_added = {}  # name -> {"name", "blend", "place"}: the libraries add-ons gave, in order
_blends = {}  # .blend path -> (modified, collection names, index)
_problems = {}  # library key -> what is wrong with it, for the Parts tab
_stale = []  # meshes and materials of parts dropped in this refresh (_drop_source, _purge)


# --- For other add-ons --------------------------------------------------------------------

def add_library(name, blend, place=None, draw=None):
    """Offer a library: a .blend of parts (or a folder of them), under `name` in the Parts
    tab. `place`, optional: see the module's docstring. `draw(layout, context)`, optional:
    draws the library's own switches in the Parts tab, in a box under its name, while the
    library is on. Adding a name again replaces it."""
    _added[str(name)] = {"name": str(name), "blend": str(blend), "place": place if callable(place) else None,
                         "draw": draw if callable(draw) else None}
    refresh_all()


def place_again(name):
    """Place this library's parts again: something its `place` reads has changed (one of
    its own switches)."""
    for _slot in state.each_loaded():
        for swapped in board.swapped.values():
            if swapped.get("library") == str(name):
                swapped["key"] = None
        refresh()


def has_library(name):
    """True while a library given with `add_library` under this name is offered."""
    return str(name) in _added


def remove_library(name):
    """Take back a library given with `add_library`; its parts give way to the next
    library's, or to KiCad's models."""
    if _added.pop(str(name), None) is not None:
        refresh_all()


def refresh_all():
    """Match every board's footprints again (a library's file changed)."""
    for _slot in state.each_loaded():
        refresh()


def install():
    """At register: other add-ons find this module by MODULE_ALIAS."""
    sys.modules[MODULE_ALIAS] = sys.modules[__name__]


def uninstall():
    if sys.modules.get(MODULE_ALIAS) is sys.modules[__name__]:
        del sys.modules[MODULE_ALIAS]
    _added.clear()


# --- The libraries -------------------------------------------------------------------------

def my_parts_folder():
    """The user's own library: a folder, one .blend per part. Until one is chosen,
    "KiLeidoscope/My parts" in the user's Documents (their home folder without one): never
    beside the settings, which sit with the cache."""
    chosen = settings.read().get("my_parts")
    if chosen:
        return Path(chosen)
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / "KiLeidoscope" / "My parts"


def set_my_parts_folder(folder):
    settings.put(my_parts=str(folder))
    refresh_all()


def _user_libraries():
    found = settings.read().get("part_libraries", [])
    return [row for row in found if isinstance(row, dict) and row.get("path")] if isinstance(found, list) else []


def add_user_library(path):
    """A library the user picked in the Parts tab: a .blend, or a folder of them."""
    path = str(Path(path))
    rows = [row for row in _user_libraries() if not state.same_file(row["path"], path)]
    settings.put(part_libraries=[*rows, {"path": path, "on": True}])
    refresh_all()


def remove_user_library(path):
    settings.put(part_libraries=[row for row in _user_libraries() if not state.same_file(row["path"], path)])
    refresh_all()


def set_on(key, on):
    """Switch one library of `listing` on or off (kept between sessions)."""
    stored = settings.read()
    if key.startswith("user:"):
        path = key[5:]
        settings.put(part_libraries=[{**row, "on": bool(on)} if row["path"] == path else row
                                     for row in _user_libraries()])
    else:
        off = {name for name in stored.get("part_libraries_off", []) if isinstance(name, str)}
        (off.discard if on else off.add)(key)
        settings.put(part_libraries_off=sorted(off))
    refresh_all()


def listing():
    """The libraries in the order they are tried, for the Parts tab and for matching:
    [{"key", "name", "path", "kind": "my" | "user" | "addon", "on", "place", "draw"}]. Reads
    no library file (the panel calls it on every draw)."""
    stored = settings.read()
    off = stored.get("part_libraries_off", [])
    off = set(off) if isinstance(off, list) else set()
    rows = [{"key": MY_KEY, "name": MY_PARTS, "path": str(my_parts_folder()), "kind": "my",
             "on": MY_KEY not in off, "place": None, "draw": None}]
    for row in _user_libraries():
        path = str(row["path"])
        rows.append({"key": f"user:{path}", "name": Path(path).stem or path, "path": path, "kind": "user",
                     "on": bool(row.get("on", True)), "place": None, "draw": None})
    for name, offer in _added.items():
        rows.append({"key": f"addon:{name}", "name": name, "path": offer["blend"], "kind": "addon",
                     "on": f"addon:{name}" not in off, "place": offer["place"], "draw": offer.get("draw")})
    for row in rows:
        row["problem"] = _problems.get(row["key"], "")
    return rows


def file_stem(name):
    """A part's name as a file's: the characters a file name cannot hold become "_"."""
    return _FILE_UNSAFE.sub("_", name).strip(" .") or "_"


def _read_blend(path):
    """(collection names, index) of a library .blend, read once per saved version of it."""
    modified = os.stat(path).st_mtime_ns
    cached = _blends.get(path)
    if cached is not None and cached[0] == modified:
        return cached[1], cached[2]
    index = {}
    with bpy.data.libraries.load(path, link=False) as (source, target):
        names = frozenset(source.collections)
        if INDEX in source.texts:
            target.texts = [INDEX]
    for text in target.texts:
        if text is None:
            continue
        try:
            found = json.loads(text.as_string())
            index = found if isinstance(found, dict) else {}
        except ValueError as error:
            state.log(f"part library {path}: its {INDEX} is not JSON ({error}); matched by name only")
        bpy.data.texts.remove(text)
    _blends[path] = (modified, names, index)
    return names, index


def _entries(raw):
    """An index value as a list of entries: a name or an entry alone stands for a list of one."""
    entries = []
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, str):
            item = {"part": item}
        if isinstance(item, dict) and item.get("part"):
            entries.append(item)
    return entries


def _by_reference(entries, reference):
    """The entry whose `reference_prefixes` the reference starts with ("FB" for "FB12"),
    the longest such prefix winning ("FB" over "F"), or None."""
    best, length = None, 0
    for entry in entries:
        prefixes = entry.get("reference_prefixes", ())
        for prefix in [prefixes] if isinstance(prefixes, str) else prefixes:
            if isinstance(prefix, str) and prefix and reference.startswith(prefix) and len(prefix) > length:
                best, length = entry, len(prefix)
    return best


class _Library:
    """One library as opened for a refresh: what `listing` says of it, and its parts."""

    def __init__(self, row):
        self.row = row
        self.key, self.name, self.place = row["key"], row["name"], row["place"]
        self.files = {}  # a folder: file stem -> .blend
        self.blend, self.names, self.index = None, frozenset(), {}
        path = Path(row["path"])
        _problems.pop(self.key, None)
        try:
            if path.is_dir():
                self.files = {entry.name[:-6]: entry.path for entry in os.scandir(path)
                              if entry.name.lower().endswith(".blend") and entry.is_file()}
            elif path.is_file():
                self.blend = str(path)
                self.names, self.index = _read_blend(self.blend)
            elif row["kind"] != "my":  # the user's own folder is made when a part is first saved
                _problems[self.key] = "Not found"
        except (OSError, RuntimeError) as error:  # unreadable, or no .blend at all
            _problems[self.key] = "Cannot be read"
            state.log(f"part library {row['name']} ({path}): {error}")

    def find(self, name, reference, named):
        """(blend, collection, entry) of the part for a footprint called `name` with this
        reference, or None. `named`: the name came from the footprint's field, and may be
        a part's own name."""
        if self.files:
            file = self.files.get(file_stem(name))
            return (file, "", {"part": name}) if file is not None else None
        if self.blend is None:
            return None
        entries = _entries(self.index.get(name, ()))
        entry = None
        if entries:
            entry = _by_reference(entries, reference or "")
            entry = entry or next((entry for entry in entries if not entry.get("reference_prefixes")), None)
        elif named:  # the field names a part itself: that part, whatever the reference
            entry = next((entry for raw in self.index.values() for entry in _entries(raw) if entry["part"] == name),
                         None)
        if entry is None and not entries and name in self.names:
            entry = {"part": name}
        if entry is None or entry["part"] not in self.names:
            return None
        return self.blend, entry["part"], entry


# --- Matching ------------------------------------------------------------------------------

def footprint_name(library_id):
    """"LED_SMD:LED_0603_1608Metric" -> "LED_0603_1608Metric": parts match by the
    footprint's name, whatever a project calls its library."""
    return library_id.rpartition(":")[2]


def model_field(footprint):
    """(name, degrees) from the footprint's "KiLeidoscope Model" field, or None. A
    footprint under a maker's or a library's own name (an LCSC import "CYUSB3065-BZXC")
    names the KiCad footprint it is ("TFBGA-121_10x10mm_Layout11x11_P0.8mm") or a part
    itself; "SOT-23-6 @90" turns the part 90 degrees counter-clockwise in the footprint,
    for a footprint drawn turned against KiCad's."""
    for name, text in dict(footprint.get("kls_fields", {})).items():
        text = str(text).strip()
        if name.strip().casefold() != MODEL_FIELD or not text:
            continue
        model, _, turn = text.partition("@")
        try:
            degrees = float(turn) if turn.strip() else 0.0
        except ValueError:
            state.log(f"{footprint.get('kls_reference')}: {MODEL_FIELD} {text!r}: no angle after @")
            degrees = 0.0
        return footprint_name(model.strip()), degrees % 360.0
    return None


def match_name(footprint):
    """The name a footprint is matched by: its "KiLeidoscope Model" field's when it has
    one, else its footprint name."""
    named = model_field(footprint)
    return named[0] if named else footprint_name(footprint.get("kls_library_id", ""))


def described(footprint):
    """The footprint as a library's `place` gets it."""
    return {"reference": footprint.get("kls_reference", ""),
            "library_id": footprint.get("kls_library_id", ""),
            "value": footprint.get("kls_value", ""),
            "fields": dict(footprint.get("kls_fields", {})),
            "pins": {number: tuple(pin) for number, pin in dict(footprint.get("kls_pins", {})).items()}}


def _choose(libraries, footprint):
    """(library, blend, collection, entry) for a footprint frame, or None: the first
    library, in order, with a part for it."""
    named = model_field(footprint)
    name = named[0] if named else footprint_name(footprint.get("kls_library_id", ""))
    if not name:
        return None
    for library in libraries:
        found = library.find(name, footprint.get("kls_reference", ""), named is not None)
        if found is not None:
            return (library, *found)
    return None


def _key(library, blend, collection, footprint, stamps):
    """What a placed part is the part of: its file as saved, its name, its turn, and, for
    a library with a `place`, the footprint as `place` is handed it (its reference, and
    kls_identity: library id, value, fields and pins, footprints.py). A footprint whose
    key changes gets its part again. `stamps`: the files' modified times, read once a refresh."""
    if blend not in stamps:
        try:
            stamps[blend] = os.stat(blend).st_mtime_ns
        except OSError:
            stamps[blend] = 0
    named = model_field(footprint)
    key = [blend, collection, stamps[blend], named[1] if named else 0.0]
    if library.place is not None:
        key += [footprint.get("kls_reference", ""), footprint.get("kls_identity", "")]
    return json.dumps(key)


# --- Placing -------------------------------------------------------------------------------

def enabled():
    return bool(getattr(bpy.context.scene, "kileido_library_parts", True))


def refresh(rebind=False, settle=True):
    """Put library parts on the footprints they match and take them off the others.

    `rebind`: KiCad's models were just bound again (models.bind_root rebuilt
    board.model_objects_by_fp), so every footprint with a part is set again. `settle`
    False leaves folding, highlights, X-ray and the cut to the caller."""
    if board.collection is None or board.drop_if_freed("part libraries"):
        return 0
    footprints = {obj["kls_id"]: obj for obj in tuple(board.collection.all_objects)
                  if obj.get("kls_footprint") == 1 and obj.get("kls_active") == 1}
    if not board.swapped:
        _adopt()
    libraries = [_Library(row) for row in listing() if row["on"]] if enabled() else []
    wanted, stamps = {}, {}
    for footprint_id, footprint in footprints.items():
        choice = _choose(libraries, footprint) if libraries else None
        if choice is not None:
            library, blend, collection, entry = choice
            wanted[footprint_id] = (library, blend, collection, entry,
                                    _key(library, blend, collection, footprint, stamps))
    changed = [footprint_id for footprint_id in sorted(set(board.swapped) | set(wanted))
               if board.swapped.get(footprint_id, {}).get("key") != (wanted[footprint_id][4]
                                                                    if footprint_id in wanted else None)]
    if not changed and not (rebind and board.swapped):
        return 0
    if settle:
        fold.unfold_parts()
    sources = _sources({(blend, collection) for footprint_id, (_, blend, collection, _, _) in wanted.items()
                        if footprint_id in changed})
    if rebind:
        for footprint_id in set(board.swapped) - set(changed):
            _reassert(footprint_id, footprints.get(footprint_id))
    for footprint_id in changed:
        _take_off(footprint_id)
        if footprint_id in wanted and footprint_id in footprints:
            library, blend, collection, entry, key = wanted[footprint_id]
            source = sources.get((blend, collection))
            if source is not None:
                _put_on(footprints[footprint_id], source, key, library, entry)
    _drop_unused()
    _purge()
    solder.invalidate()  # reflowed solder flows up the parts' contacts
    if settle:
        fold.refold_parts()
        highlight.refresh()
        focus.refresh()  # the parts' materials fade in X-ray
        cut.add_materials()  # and are cut open with the board
    state.log(f"part libraries: {len(board.swapped)} footprints carry a part, {len(changed)} changed")
    return len(changed)


def _adopt():
    """Parts already in the collection (a saved .blend opened again, an undo) are the
    board's again: found by what they carry, so they are not placed a second time."""
    found = {}
    for obj in board.collection.all_objects:
        if obj.get(PART) == 1 and obj.get("kls_model_fp_id") is not None:
            found.setdefault(obj["kls_model_fp_id"], []).append(obj)
    for footprint_id, objects in found.items():
        objects.sort(key=lambda obj: int(obj.get("kls_model_part", 0)))
        kicad = [obj.name for obj in board.collection.all_objects
                 if obj.get("kls_model_fp_id") == footprint_id and obj.get(PART) != 1]
        board.swapped[footprint_id] = {"key": objects[0].get("kls_part_key", ""), "kicad": kicad,
                                       "names": [obj.name for obj in objects],
                                       "library": objects[0].get("kls_part_library", "")}
        board.model_objects_by_fp[footprint_id] = [obj.name for obj in objects]
        board.model_bound.add(footprint_id)


def _sources(wanted):
    """{(blend, collection name): collection}: the parts appended once into this file (kept
    with a fake user, out of the scene), from each .blend in one read. A part whose file
    was saved again since is read again."""
    found, missing = {}, {}
    for collection in tuple(bpy.data.collections):
        key = (collection.get("kls_part_blend"), collection.get("kls_part_name"))
        if key not in wanted:
            continue
        try:
            fresh = str(os.stat(key[0]).st_mtime_ns) == collection.get("kls_part_modified")
        except OSError:
            fresh = True  # its file is gone: what was read stands
        if fresh:
            found[key] = collection
        else:
            _drop_source(collection)
    for blend, name in wanted - set(found):
        missing.setdefault(blend, []).append(name)
    for blend, names in missing.items():
        try:
            modified = str(os.stat(blend).st_mtime_ns)
            with bpy.data.libraries.load(blend, link=False) as (source, target):
                # "": a folder's part file, whose one collection is the part
                picked = [(name, name if name else next(iter(source.collections), None)) for name in names]
                picked = [(name, held) for name, held in picked if held in source.collections]
                target.collections = [held for _, held in picked]
        except (OSError, RuntimeError) as error:
            state.log(f"part library {blend}: {error}")
            continue
        for (name, _), collection in zip(picked, target.collections):
            if collection is None:
                continue
            collection["kls_part_blend"] = blend
            collection["kls_part_name"] = name
            collection["kls_part_modified"] = modified
            if collection.asset_data is not None:
                collection.asset_clear()  # not an asset of this file: its library has it
            collection.use_fake_user = True  # after asset_clear, which takes the fake user away
            found[(blend, name)] = collection
        for name in set(names) - {name for name, _ in picked}:
            state.log(f"part library {blend}: no part {name or '(any)'}")
    for collection in found.values():
        _steady(collection)
    return found


def _drop_source(collection):
    """A part read from a file saved again since goes. Its meshes and materials go once the
    parts placed from it are off (`_purge`, at the end of the refresh)."""
    objects = list(collection.all_objects)
    _stale.extend(obj.data for obj in objects if obj.data is not None)
    _stale.extend(slot.material for obj in objects for slot in obj.material_slots if slot.material is not None)
    bpy.data.collections.remove(collection)
    for obj in objects:
        if obj.users == 0:
            bpy.data.objects.remove(obj)


def _drop_unused():
    """Parts appended into this file that no placed object, on any board, comes from any
    more (Library parts off, a library taken away, footprints matched elsewhere) go."""
    used = set()
    for obj in bpy.data.objects:
        if obj.get(PART) == 1:
            try:
                key = json.loads(obj.get("kls_part_key", ""))
                used.add((key[0], key[1]))
            except (ValueError, TypeError, IndexError, KeyError):
                continue
    for collection in tuple(bpy.data.collections):
        blend = collection.get("kls_part_blend")
        if blend is not None and (blend, collection.get("kls_part_name")) not in used:
            _drop_source(collection)


def _purge():
    """Remove what dropped parts held, now that nothing uses it. What another board's
    parts still use stays listed, for that board's refresh."""
    owners = {"MESH": bpy.data.meshes, "CURVE": bpy.data.curves, "CURVES": bpy.data.hair_curves,
              "MATERIAL": bpy.data.materials}
    kept = []
    for block in dict.fromkeys(_stale):  # each once; meshes before the materials they use
        try:
            users, owner = block.users, owners.get(block.id_type)
        except ReferenceError:  # removed already
            continue
        if owner is None:
            continue
        if users == 0:
            owner.remove(block)
        else:
            kept.append(block)
    _stale[:] = kept


def _steady(collection):
    """The part's materials get their steady stage now, for good (focus.steady_group):
    X-ray and the cut plane then change values only and never recompile the parts' shaders."""
    for obj in collection.all_objects:
        for slot in obj.material_slots:
            material = slot.material
            if material is None or material.get(focus.STEADY):
                continue
            material[focus.STEADY] = 1
            focus.add_to(material)  # the steady stage: X-ray's fade and the cut in one


def _kicad_models(footprint_id):
    """The names of KiCad's own model objects among a footprint's."""
    found = []
    for name in board.model_objects_by_fp.get(footprint_id, ()):
        obj = lookup(name)
        if obj is not None and obj.get(PART) != 1:
            found.append(name)
    return found


def _put_on(footprint, source, key, library, entry):
    footprint_id = footprint["kls_id"]
    kicad = _kicad_models(footprint_id)
    for name in kicad:
        set_visible(lookup(name), False)
    turn = json.loads(key)[3]
    turned = Matrix.Rotation(math.radians(turn), 4, "Z")  # the field's @: about the footprint's origin
    placed = []
    for index, obj in enumerate(sorted(source.all_objects, key=lambda obj: obj.name)):
        part = PART_BASE + index
        stale = components.find(components.MODEL, footprint_id, part)
        if stale is not None:
            _remove(stale)
        clone = obj.copy()  # its mesh, materials, modifiers and drivers shared or copied as Blender does
        clone.name = components.part_name(footprint.get("kls_reference", ""), entry["part"], index)
        clone.hide_viewport = False  # (a part's field lines may come disabled) set_visible shows and hides it
        local = turned @ _part_matrix(obj)
        clone["kileido_owned"] = 1
        clone["kls_model_fp_id"] = footprint_id
        clone[PART] = 1
        clone["kls_part_library"] = library.name
        clone["kls_part_name"] = entry["part"]
        clone["kls_part_key"] = key
        clone["kls_side"] = footprint.get("kls_side", "")
        clone["kls_model_index"] = 0
        clone["kls_model_count"] = 1
        clone["kls_model_part"] = part
        clone["kls_model_local_matrix"] = [local[row][col] for row in range(4) for col in range(4)]
        link_owned(clone, "components")
        components.attach(clone, footprint, local)
        components.remember(clone)
        set_visible(clone, models.model_is_visible(footprint, clone))
        placed.append(clone)
    if library.place is not None:
        try:
            library.place(placed, described(footprint), dict(entry))
        except Exception as error:  # a broken library must not break the board
            state.log(f"part library {library.name}: placing {footprint.get('kls_reference')} failed: {error}")
    names = [clone.name for clone in placed]
    board.swapped[footprint_id] = {"key": key, "names": names, "kicad": kicad, "library": library.name}
    board.model_objects_by_fp[footprint_id] = list(names)
    board.model_bound.add(footprint_id)
    if board.in_snapshot:
        board.touched.update(names)
    _placeholder(footprint_id)


def _remove(obj):
    """A placed part's object, and its data if a library's `place` made it its own copy."""
    data = obj.data
    bpy.data.objects.remove(obj)
    if data is not None and data.get("kls_dressed") and data.users == 0:
        collection = getattr(bpy.data, {"MESH": "meshes", "CURVES": "hair_curves"}.get(data.id_type, ""), None)
        if collection is not None:
            collection.remove(data)


def _part_matrix(obj):
    """A part's object's place in its part, which is KiCad's footprint space. Appended
    parts are in no scene, so their world matrices may never have been evaluated: their
    own placements (and their parents' within the part) give it."""
    matrix = obj.matrix_basis.copy()
    while obj.parent is not None:
        matrix = obj.parent.matrix_basis @ obj.matrix_parent_inverse @ matrix
        obj = obj.parent
    return matrix


def _reassert(footprint_id, footprint):
    """After KiCad's models were bound again: the part's objects back in the footprint's
    list, KiCad's fresh ones hidden."""
    swapped = board.swapped[footprint_id]
    kicad = _kicad_models(footprint_id)
    for name in kicad:
        set_visible(lookup(name), False)
    swapped["kicad"] = kicad
    board.model_objects_by_fp[footprint_id] = list(swapped["names"])
    board.model_bound.add(footprint_id)
    for name in swapped["names"]:
        obj = lookup(name)
        if obj is not None:
            set_visible(obj, footprint is not None and models.model_is_visible(footprint, obj))


def _take_off(footprint_id):
    """Remove a footprint's part and give it back KiCad's models."""
    swapped = board.swapped.pop(footprint_id, None)
    if swapped is None:
        return
    for name in swapped["names"]:
        obj = lookup(name)
        if obj is not None and obj.get(PART) == 1:
            _remove(obj)
    footprint = components.find(components.FRAME, footprint_id)
    kicad = [name for name in _kicad_models(footprint_id) or swapped["kicad"] if lookup(name) is not None]
    if kicad:
        board.model_objects_by_fp[footprint_id] = kicad
        for name in kicad:
            obj = lookup(name)
            set_visible(obj, models.model_is_visible(footprint, obj))
    else:
        board.model_objects_by_fp.pop(footprint_id, None)
        board.model_bound.discard(footprint_id)
    _placeholder(footprint_id)


def _placeholder(footprint_id):
    """KiCad's bounds envelope shows only for a footprint with a declared, shown model
    and nothing bound (models.placeholder_visible)."""
    box = components.find(components.PLACEHOLDER, footprint_id)
    if box is not None:
        set_visible(box, models.placeholder_visible(components.find(components.FRAME, footprint_id), footprint_id))


def show_fields():
    """Show or hide every placed part's field lines (Field lines), as
    models.model_is_visible has them."""
    for _slot in state.each_loaded():
        for footprint_id, swapped in board.swapped.items():
            footprint = components.find(components.FRAME, footprint_id)
            for name in swapped["names"]:
                obj = lookup(name)
                if obj is not None and obj.get("kls_role") == FIELD:
                    set_visible(obj, footprint is not None and models.model_is_visible(footprint, obj))


# --- The Parts tab -------------------------------------------------------------------------

def status():
    """The panel's line: how many footprints carry a library part."""
    return f"Library parts: {len(board.swapped)} placed" if board.swapped else ""


def selected():
    """The footprint frame selected in KiCad, when it is exactly one; else None."""
    chosen = board.highlight_components.get("footprints", set())
    if board.collection is None or len(chosen) != 1:
        return None
    return components.find(components.FRAME, next(iter(chosen)))


def source_of(footprint):
    """Where a footprint's model comes from: a library's name, "KiCad", or "" for none."""
    footprint_id = footprint.get("kls_id")
    swapped = board.swapped.get(footprint_id)
    if swapped is not None:
        return swapped.get("library") or "a library"
    return "KiCad" if board.model_objects_by_fp.get(footprint_id) else ""


# --- Save to my parts ----------------------------------------------------------------------

_OWN = ("kileido_owned", "kls_model_fp_id", PART, "kls_part_library", "kls_part_name", "kls_part_key", "kls_side",
        "kls_model_index", "kls_model_count", "kls_model_part", "kls_model_local_matrix", "kls_model_asset",
        "kls_model_seated", "kls_layer_hidden", "kls_flat_matrix", "kls_folded_matrix", focus.KEEP,
        focus.SEE_THROUGH, focus.FOCUS)  # what KiLeidoscope keeps on a placed object: not the part's own


def save_to_my_parts(footprint):
    """Write the footprint's model as it is in the scene, with the materials it has now, to
    My parts: "<footprint name>.blend", holding a collection of that name at the
    footprint's origin and orientation, with its own copies of meshes and materials.
    Saving again replaces that file. Returns the file; raises ValueError when there is
    nothing to save, OSError when the file cannot be written."""
    name = match_name(footprint)
    if not name:
        raise ValueError("KiCad gave this footprint no name to save a part under")
    footprint_id = footprint["kls_id"]
    shown = [obj for obj in (lookup(found) for found in board.model_objects_by_fp.get(footprint_id, ()))
             if obj is not None and not obj.hide_render and obj.get("kls_role") != FIELD]
    if not shown:
        raise ValueError("This footprint shows no model to save")
    # The part is saved as the footprint it is named after has it: a footprint whose field
    # turns the part ("SOT-23-6 @90") has its model turned that much against it
    named = model_field(footprint)
    unturned = Matrix.Rotation(math.radians(-named[1] if named else 0.0), 4, "Z")
    folder = my_parts_folder()
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f"{file_stem(name)}.blend"
    collection = bpy.data.collections.new(name)
    made, materials = [collection], {}
    try:
        for obj in sorted(shown, key=lambda obj: int(obj.get("kls_model_part", 0))):
            copy = obj.copy()
            made.append(copy)
            if obj.data is not None:
                copy.data = obj.data.copy()
                copy.data.pop("kls_dressed", None)  # in the file it is the part's own mesh
                made.append(copy.data)
            copy.parent = None
            copy.matrix_parent_inverse = Matrix.Identity(4)
            copy.matrix_basis = unturned @ _local(obj, footprint)
            copy.hide_viewport = copy.hide_render = False
            for key in _OWN:
                copy.pop(key, None)
            for slot in copy.material_slots:
                if slot.material is None:
                    continue
                if slot.material not in materials:
                    materials[slot.material] = _plain(slot.material)
                    made.append(materials[slot.material])
                slot.material = materials[slot.material]
            collection.objects.link(copy)
        bpy.data.libraries.write(str(file), {collection}, fake_user=True)
    finally:
        for block in made:
            owner = {"COLLECTION": bpy.data.collections, "OBJECT": bpy.data.objects, "MESH": bpy.data.meshes,
                     "MATERIAL": bpy.data.materials, "CURVE": bpy.data.curves,
                     "CURVES": bpy.data.hair_curves}.get(block.id_type)
            if owner is not None:
                owner.remove(block)
    state.log(f"part saved to My parts: {file}")
    refresh_all()
    return file


def _local(obj, footprint):
    """A model object's place in its footprint's frame."""
    values = obj.get("kls_model_local_matrix")
    if values is not None and len(values) == 16:
        return Matrix(tuple(values[row * 4:row * 4 + 4] for row in range(4)))
    return obj.matrix_basis.copy() if obj.parent == footprint else footprint.matrix_world.inverted() @ obj.matrix_world


def _plain(material):
    """A copy of a material without KiLeidoscope's own stages (X-ray's fade, the cut
    plane's clip): the part's file holds the material as the user made it."""
    copy = material.copy()
    copy.pop(focus.STEADY, None)
    if copy.node_tree is not None:
        nodes, links = copy.node_tree.nodes, copy.node_tree.links
        for name in (focus.CUT_NODE, focus.NODE, focus.STEADY_NODE):
            stage = nodes.get(name)
            if stage is None:
                continue
            source = stage.inputs[0].links[0].from_socket if stage.inputs[0].is_linked else None
            for link in tuple(stage.outputs[0].links):
                if source is not None:
                    links.new(source, link.to_socket)
            nodes.remove(stage)
    copy.surface_render_method = "DITHERED"
    return copy
