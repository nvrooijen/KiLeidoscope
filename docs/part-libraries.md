# Part libraries

A part library holds parts made in Blender that take the place of KiCad's 3D models on the board, matched by footprint name. Where a library has a part for a footprint, KiLeidoscope puts that part on it instead of KiCad's model, and it behaves as KiCad's models do: it follows the footprint when you move it in KiCad, hides with KiCad's model visibility, fades in X-ray mode, opens up in the cut plane, takes reflowed solder on its contacts and folds with a flex board. Footprints with no 3D model in KiCad get their part too.

The reason to make a part in Blender is its materials. KiCad's models reach Blender through `kicad-cli`'s GLB export, which keeps each material's colour and nothing else: a VRML model's specular and shininess are dropped, and every material arrives fully metallic and fully rough. A part made in Blender keeps the materials you gave it and never goes through that export.

## Contents

- [The Parts tab](#the-parts-tab)
- [My parts](#my-parts)
- [How footprints are matched](#how-footprints-are-matched)
- [Making a library](#making-a-library)
- [The index](#the-index)
- [Roles](#roles)
- [A library from another add-on](#a-library-from-another-add-on)
- [How it works](#how-it-works)

## The Parts tab

The **Parts** tab on the sidebar's left edge opens the Parts column.

- **Library parts** switches the parts on or off. Off, every footprint shows KiCad's model.
- **Transparent cases** turns every part's case see-through at once, without X-ray mode or a selection, so the insides show. It acts on the objects a part marks as its case (see [Roles](#roles)).
- **Libraries** lists My parts and the libraries you added, in the order they are tried. The first library with a part for a footprint wins. Untick a library to leave it out. **Add library…** takes a `.blend` file or, with no file picked, the folder shown. The cross takes a library you added off the list; its file stays where it is.
- A library another add-on offers has its own box below, with its tick, its name and the switches the add-on gives it. These are tried after the libraries in the list.
- With one footprint selected in KiCad, the column says where its model comes from (My parts, a library by name, or KiCad), and **Save to my parts** saves it.

## My parts

My parts is your own library: a folder with one `.blend` per part. The folder button beside it in the list chooses the folder; to start with it is `KiLeidoscope/My parts` in your Documents folder.

To give a KiCad model better materials:

1. Select the footprint in KiCad.
2. In Blender, change its materials in the Shader editor: roughness, metallic, anything else.
3. Press **Save to my parts**.

The model as it is in the scene, with the materials it has now, is written to `<footprint name>.blend` in the folder: a collection of that name at the footprint's origin and orientation, with its own copies of the meshes and materials. From then on every footprint of that name, on every board, uses it. Saving again replaces that one file. To share a part, copy its file.

This works for a model of your own too (a VRML or STEP file you set as the footprint's model in KiCad): KiLeidoscope shows it as KiCad exports it, you set its materials, then save it.

## How footprints are matched

A footprint matches by its name in its library: `LED_0603_1608Metric` in `LED_SMD:LED_0603_1608Metric`. The library's nickname doesn't matter, so a project's own copy of KiCad's libraries matches too.

A footprint under a maker's or another library's own name (an LCSC import such as `CYUSB3065-BZXC`) matches nothing by itself. Give it a field named `KiLeidoscope Model` holding the name to match by, `TFBGA-121_10x10mm_Layout11x11_P0.8mm`, and it matches by that instead. Set the field in the footprint library and every board using the footprint gets it. `SOT-23-6 @90` turns the part 90 degrees counter-clockwise in the footprint, for a footprint drawn turned against KiCad's.

## Making a library

A library is either of these:

- **A `.blend` file** with one collection per part, each named after its footprint.
- **A folder** of `.blend` files, each named after its footprint and holding that part's collection. My parts is such a folder.

A part's objects are laid out in KiCad's footprint space: the footprint's origin at the world origin, the board's top face at Z = 0, and Y as Blender shows the board (KiCad's Y flipped). A bottom-side footprint turns the part over as KiCad does. Use any materials Blender has.

A library file saved again is read again the next time the board changes, and the footprints using it get the new parts.

## The index

Matching by collection name is enough while each part belongs to one footprint. A `.blend` library where one part stands in for several footprints, or two parts share a footprint, says so in an index: a text datablock named `kls_index` in the same file, holding JSON.

```json
{
  "LED_0603_1608Metric": [{"part": "My_LED_0603"}],
  "LED_0603_1608Metric_Pad1.05x0.95mm_HandSolder": [{"part": "My_LED_0603"}],
  "L_0603_1608Metric": [{"part": "My_Bead_0603", "reference_prefixes": ["FB"]},
                        {"part": "My_Inductor_0603"}]
}
```

Each key is a footprint name; `part` is the collection's name. An entry with `reference_prefixes` is used only for a footprint whose reference starts with one of them (`FB12`; the longest prefix wins, `FB` over `F`), and comes before an entry without: a ferrite bead shares a chip inductor's footprint and is told apart by its reference, `FB` being KiCad's own for `Device:FerriteBead`. A footprint the index doesn't list is matched by collection name, as without an index. Any other keys in an entry belong to the library and are handed to its `place` (below).

## Roles

Each object of a part can carry a custom property `kls_role`. A part without roles works as KiCad's models do.

| Role | What KiLeidoscope does with it |
|---|---|
| `case` | With the part selected in X-ray mode it fades with the rest of the board, so the insides show. **Transparent cases** fades every case at once |
| `insides`, `cover`, `light` | Stay as they are when X-ray opens the part (`cover`: a clear outside you see in through; Transparent cases fades it with the case) |
| `contacts` | Stay as well, and reflowed solder climbs them |
| `field` | Placed hidden. Collision checks and selection boxes leave them out |

KiLeidoscope sets `kls_focus_keep` to 1 on the objects that stay, and `kls_see_through` to 1 on the cases and covers while Transparent cases is on. Its own material stage reads both from `kls_focus`, the vector (`kls_focus_keep`, `kls_see_through`, 0). Eevee reads at most eight Object attributes per material and, past that, none of them; `kls_focus` is one, so a part's own material may read seven.

## A library from another add-on

A Blender add-on can offer a library, and run code when its parts are placed. It uses one module, found by its alias when needed (KiLeidoscope can be loaded under different module names):

```python
import sys

def offer():
    parts = sys.modules.get("kileidoscope_parts")  # None: KiLeidoscope isn't running
    if parts is not None and parts.API_VERSION >= 1:
        parts.add_library("My library", "/path/to/my_library.blend", place=place)
```

| Call | Does |
|---|---|
| `add_library(name, blend, place=None, draw=None)` | Offers the `.blend` (or folder) under `name`, in its own box in the Parts column, tried after My parts and the libraries the user added. Calling it again with the same name replaces it |
| `has_library(name)` | True while a library added under this name is offered |
| `remove_library(name)` | Takes it back; its parts give way to the next library's, or to KiCad's models |
| `place_again(name)` | Places the library's parts again, calling `place` for each: for when something `place` reads has changed |
| `refresh_all()` | Matches every board's footprints again |
| `API_VERSION` | Counts additions: a caller needing a later call checks for its version |

These are kept stable across KiLeidoscope versions; nothing else in the add-on is meant for other add-ons. An add-on that registers before KiLeidoscope finds `None`: it calls `offer` again later (a timer, checking `has_library` first, or when it is first used).

Every placed object carries `kls_library_part` = 1, `kls_part_library` (the library's name) and `kls_part_name` (the part's), so an add-on finds the objects placed from its own library.

`place(objects, footprint, part)`, if given, is called each time one of the library's parts is placed, and again when the footprint's value, fields, pins or reference change.

- `objects`: the placed objects, copies of the part's.
- `footprint`: the footprint as KiCad has it, `{"reference", "library_id", "value", "fields", "pins"}`. `fields` holds the footprint's own fields by name (an LED's `Color`); `pins` maps each pad number to its schematic pin's `(name, electrical type)`, the type as the board file spells it (`power_in`).
- `part`: the part's index entry (`{"part": name}` without an index), with the library's own keys.

`draw(layout, context)`, if given, draws the library's own switches in its box, under its tick and name, while the library is ticked. It is a panel's draw: it shows properties and operators the add-on registered itself, and changes nothing. A switch that changes what `place` does calls `place_again` from its update.

A part's objects keep their own drivers and animation when placed (a driver on a custom property counting time from the frame, say), so a part can move or change by itself.

Set custom properties on the objects for their materials to read (an Attribute node set to Object), within Eevee's limit above. For more than a few values, change the geometry: give the object its own copy first (`obj.data = obj.data.copy()`) and mark the copy with a custom property `kls_dressed`, and KiLeidoscope removes it when the part is taken off. An error in `place` is logged and the part stays as placed.

## How it works

A placed part is a set of copies of the part's objects, children of the footprint's frame in the board's collection. Their meshes and materials are shared with the part, which is appended into the file once from its library. The copies are listed as the footprint's model objects in place of KiCad's, so moving, hiding, X-ray, the cut plane, solder and folding treat them as they treat KiCad's models. When KiCad's models are exported and bound again (after a model change in KiCad), the parts keep their place.

A part's materials get one extra stage when they are appended, for X-ray's fade and the cut plane's clip together, and keep it. Switching X-ray or the cut plane then changes only values in it, so detailed parts' shaders are not compiled again for a switch.

The bridge sends each footprint's library id, value, fields and its pads' schematic pins (`kileido_bridge/kicad_reader.py`, `_convert_footprint`); the pins come from KiCad's API, so a symbol changed in the schematic shows after the board is updated from it. The add-on matches and places the parts (`blender_addon/kileido/parts.py`). `tests/blender/run_parts.py` checks it on the test board with a small library it builds.
