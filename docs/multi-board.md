# (New!) Multi-board assemblies (beta)

A product is rarely one board. KiLeidoscope shows up to four boards from four KiCad PCB editors in one Blender window, each following its own KiCad live, and lets you stand them where they go in the product: stacked on headers, folded over each other, side by side in a housing. It marks where they collide, remembers which connectors are plugged into which, and keeps where each board stands in a small arrangement file in an open standard (OpenUSD) that other tools read too.

KiCad itself has no notion of an assembly: each PCB editor holds one board. KiLeidoscope works with that rather than against it. The way that works best is to keep **one board live at a time**: the board you are editing follows its KiCad, the others stay in Blender as saved copies where you put them, and swapping which board is live is one click in Blender and one in KiCad. The **Multi-board** column holds the controls.

<p align="center">
<img src="../assets/collisions.png" width="520" alt="Two boards in a multi-board assembly. Red boxes mark where boards and components collide, and a malformed board outline is highlighted red.">
</p>
<p align="center"><sub>Two boards from two KiCads in one window. Red boxes mark where parts and boards run into each other.</sub></p>

## Contents

- [The workflow: one live board, swapped](#the-workflow-one-live-board-swapped)
- [A second board joins](#a-second-board-joins)
- [The Boards list](#the-boards-list)
- [The Multi-board column](#the-multi-board-column)
- [Placing a board](#placing-a-board)
- [Collisions and Mated parts](#collisions-and-mated-parts)
- [Saving and reopening](#saving-and-reopening)
- [The arrangement file and its standard](#the-arrangement-file-and-its-standard)
- [How it works](#how-it-works)
- [Where each value comes from](#where-each-value-comes-from)
- [Known limitations](#known-limitations)
- [Disclaimer](#disclaimer)

## The workflow: one live board, swapped

1. **Open in Blender** from the PCB editor, as always. The first board comes in centred, as it always did.
2. Open the second board in a **second PCB editor window** (open its project in a new KiCad window) and click **Open in Blender** there. Blender asks whether to **Add here** or open the board in its own window. Add it: it comes in at its own KiCad coordinates, a centimetre above the first board, and the **Multi-board** tab on the sidebar's edge lights up.
3. **Place** the boards: the select arrow in front of a board's row, then **G** and **R** in the 3D view, or the Offset and Rotation numbers, **Flip** and **Turn 90°** in the Multi-board column. **Collision check** shows where they run into each other; mark a connector and its mate **Mated** so they stop counting.
4. **Save** the arrangement (`.usda`) from the Multi-board column, and the `.blend` if you want the scene back as it is.
5. **Swap.** Close the first board's PCB editor: its board stays in Blender exactly where it stands, its LED goes red and an **Open in KiCad** button appears on its row. Edit the second board in KiCad, live in Blender. When you want the first board live again, click **Open in KiCad** on its row: its PCB editor opens with its file, and **Open in Blender** there puts the board back live in its slot, in place.

Everything below is detail; the five steps are the feature.

## A second board joins

**Open in Blender** in a second PCB editor looks for a KiLeidoscope Blender that is open already and asks it to show the board. What happens then:

| Situation | What Blender does |
| --- | --- |
| The board is shown already (its KiCad came back after a crash, or Open in Blender was clicked again) | Its slot takes the new connection, without asking. |
| An opened arrangement was waiting for this board | It is placed where the arrangement says, without asking. |
| Otherwise, the first time | A question, as a popup and in the Boards section: *Main wants to join this window*, with **Add here** and **Open in its own window**. Tick **Remember my choice** and it is not asked again. |
| A remembered choice | **Add here** adds the board; **Own window** starts a Blender of its own for it. Change the choice under **Other KiCads** in the Multi-board column: Ask, Add here or Own window. |
| Four boards shown already | The board opens in a window of its own, and KiCad says so. |

The question waits two minutes; unanswered, the board opens in its own window. Up to four live boards share a window.

**Windows.** KiCad on Windows lets every running PCB editor answer on one connection, so a second KiCad window started *before* the first Open in Blender reaches the first KiCad's board. Close and reopen that KiCad: KiLeidoscope leaves a marker that gives every KiCad started afterwards a connection of its own (KiCad issue [23994](https://gitlab.com/kicad/code/kicad/-/work_items/23994)). On Linux each KiCad has its own socket already.

## The Boards list

With one board, the KiLeidoscope panel shows its name, its source (**live**, the assembly variant selected in KiCad, **saved** or **dump**) and, when it is offline, **Open in KiCad**. With two boards or more, the **Boards** section lists them:

| Row | What it is |
| --- | --- |
| **All boards** | The Layers list below switches every board's layers at once. |
| A live board | Its select arrow, its eye (hide or show the whole board), the LED of its KiCad link, its name, its source, **Open in KiCad** when it is offline, and an **X** that removes it (its link to KiCad closes; Open in Blender there brings it back). |
| A view-only board | A board imported from an **Export…** package, as before: not in any KiCad. |
| A waiting board | A board an opened arrangement names that is not shown yet: **Open in KiCad** starts its PCB editor, the **X** stops waiting for it. |

Clicking a row **picks** that board: the Layers list, the cut plane, the fold, a finding's drawing, the Diff pairs list and the Status section all work on the picked board. Clicking a board in the 3D view picks it as well, and a click on a track or part selects it in that board's own KiCad.

## The Multi-board column

The **Multi-board** tab on the sidebar's left edge opens a column beside the panel. It is greyed with one board and lit with two or more. At its top, the picked board as its row shows it, with its size, thickness and copper layer count; the controls below work on that board.

| Control | What it does |
| --- | --- |
| **Offset (mm)** | How far the picked board's centre has moved from where its KiCad coordinates put it. |
| **Rotation** | The picked board's turn about its centre: X first, then Y, then Z. |
| **Flip** / **Turn 90°** / **Reset** | Turn the board over (180° about X) or a quarter turn about Z, about its centre; or put it back where its KiCad coordinates put it, unturned. |
| **Collision check** | On by default: red boxes where boards and parts overlap, and the list of what collides. |
| **Mated** (link icon on a listed pair) | These two parts are plugged into each other: no collision between them, nor between each and the other's board. The marks are listed with an **X** to take them off. |
| **Arrangement: Save… / Open…** | Where every board stands, as a `.usda` file (also under **File > Export** and **File > Import**). |
| **Other KiCads** | What Open in Blender in another KiCad does: Ask, Add here, Own window. |

## Placing a board

Each live board hangs from a root Empty, `KLS root` (board 2's `B2 KLS root`), at the top of its collection. The select arrow in front of the board's row selects that root; **G** moves the whole board and **R** turns it, while everything on it keeps following KiCad. Scale is locked, as a board does not shrink. The Position numbers follow a move, and a move follows the numbers. Undo puts a board back where it stood.

A board alone stands where KiLeidoscope always put it, centred on its outline. A board that joins others keeps its own KiCad X and Y and comes in a centimetre above the highest thing shown, so two boards from one project land over each other, ready to be stacked.

Each board keeps its own **IMS**, **Flex** and **Dynamic phase** settings and its own layer eyes. Colours, opacities, the via and thickness settings are one setting for every board.

## Collisions and Mated parts

The check runs a moment after the boards stop changing, whenever two boards or more are shown. Each part is a box (its 3D model's bounds in the footprint's frame, or the placeholder envelope) and each board a solid of its outline; parts are tested against the other boards' parts and solids, and boards against boards. A pair listed as *J3 (Main) ↔ J1 (Daughter)* can be marked **Mated**; a part against a board solid cannot, as a connector reaching into its mate's board is excused by the mark on the pair. Parts touching within 20 µm are no collision.

The marks live in the scene (a saved `.blend` keeps them) and in the arrangement file, named by board and reference, so they survive a board being closed and reopened.

## Saving and reopening

A saved `.blend` keeps every board. Reopened, each is back in its slot as a **saved** copy: the objects, materials, plots and settings as they were, not connected. **Open in Blender** in a board's KiCad takes its slot over in place; nothing moves.

An **arrangement** keeps only where the boards stand. Open one and the boards shown that it names are placed at once; the others become waiting rows, each placed as its KiCad sends it. Save one and every board shown is written, plus the boards an opened arrangement still waits for, so saving over a file never loses a board.

## The arrangement file and its standard

The arrangement is an **OpenUSD** text file (`.usda`, "usda 1.0"). USD, the Universal Scene Description from Pixar, now kept by the Alliance for OpenUSD, is the open standard for describing scenes that Blender reads and writes natively, as do Houdini, Maya, Unreal, NVIDIA Omniverse and usdview. It is plain text: you can read it, edit it by hand and keep it in version control beside the KiCad projects. KiLeidoscope writes a small, conventional stage:

- **Units and axes.** `metersPerUnit = 0.001` (millimetres, as KiCad) and `upAxis = "Z"`.
- **One Xform prim per board** under the default prim `/Boards`. Its transform places the board's **KiCad frame**: X to the right, Y up (KiCad's Y is negated), Z up with 0 at the board's bottom face, so a board with no transform sits with its KiCad origin at the scene origin. `xformOp:translate` is in millimetres and `xformOp:rotateXYZ` in degrees, X turned first, then Y, then Z; `xformOpOrder` lists translate before rotate, so the board is turned and then moved, as in any USD tool.
- **No geometry.** The boards are KiCad files; the prim points at the board's `.kicad_pcb` as an asset path, relative to the arrangement where it can be. KiCad stays the source of every board.
- **KiLeidoscope's own values** sit in a `kileidoscope:` namespace, as USD custom data should: `asset kileidoscope:board`, `string kileidoscope:name`, `bool kileidoscope:hidden`, and `int version` in the layer's `customLayerData`.
- **Mated pairs** are a `Scope` per pair under `/Boards/Mates`, each with a relationship to the two boards' prims and the two parts' references.

```
#usda 1.0
(
    customLayerData = {
        dictionary kileidoscope = {
            int version = 1
        }
    }
    defaultPrim = "Boards"
    doc = "KiLeidoscope arrangement: where each board stands. The boards are KiCad files."
    metersPerUnit = 0.001
    upAxis = "Z"
)

def Xform "Boards"
{
    def Xform "Main"
    {
        asset kileidoscope:board = @Main/Main.kicad_pcb@
        string kileidoscope:name = "Main.kicad_pcb"
        bool kileidoscope:hidden = 0
        double3 xformOp:translate = (0, 0, 0)
        double3 xformOp:rotateXYZ = (0, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]
    }

    def Xform "Daughter"
    {
        asset kileidoscope:board = @Daughter/Daughter.kicad_pcb@
        string kileidoscope:name = "Daughter.kicad_pcb"
        bool kileidoscope:hidden = 0
        double3 xformOp:translate = (20, -5, 11.6)
        double3 xformOp:rotateXYZ = (180, 0, 30)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]
    }

    def Scope "Mates"
    {
        def Scope "Mate_1"
        {
            rel kileidoscope:first = </Boards/Daughter>
            string kileidoscope:firstReference = "J1"
            rel kileidoscope:second = </Boards/Main>
            string kileidoscope:secondReference = "J3"
        }
    }
}
```

Any USD tool opens this file and shows the prims and their transforms; reference a USD of each board under its prim (a STEP export of the board, converted, placed so that its KiCad origin is the prim's origin) and the assembly stands as it does in KiLeidoscope. KiLeidoscope reads arrangements with Blender's own USD module, and with a reader of its own for the files it writes where that module is missing.

## How it works

<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/multi-board-dark.svg">
  <img src="../assets/multi-board-light.svg" width="680" alt="Each KiCad's Open in Blender runs a launcher, which joins the KiLeidoscope Blender that is open already through request and reply files, or starts a Blender of its own, and serves the board through a bridge of its own. The Blender add-on holds up to four boards in slots, each under its own root Empty with a pose and its own names, lists them in the Boards list and places them from the Multi-board column, checks collisions between them in world space, keeps where they stand in an arrangement file in OpenUSD and brings them back from a saved .blend. Open in KiCad starts a board's PCB editor again, and Open in Blender there takes its slot back.">
</picture>
</p>

**One bridge per KiCad.** Every Open in Blender starts a launcher and a bridge of its own, on its own port with its own token, as before. New is what the launcher does first: it looks in KiLeidoscope's per-user cache for Blenders that take requests (each lists itself in `viewers/viewer-<pid>.json`), leaves a request file with its port, token and board, and waits for the reply file. Until Blender takes the request the launcher can withdraw it; once taken, the user may be asked, so the launcher waits up to two minutes. Declined, or with no Blender open, it starts a Blender of its own, as before. A bridge whose board's slot has taken a newer bridge, or whose Blender has closed, stops by itself half a minute later.

**Slots.** The add-on runs one live link per slot, up to four; each KiCad's frames reach that slot's board and nothing else. Board 1 keeps the names KiLeidoscope always used; boards 2 to 4 carry their slot in front of every object, material, image and node group name (`B2 KLS outline`) and live in collections of their own (`KiLeidoscope 2: Daughter.kicad_pcb`), as Blender's names are global. Each board's settings that cannot be shared, IMS, flex, fold, dynamic phase and the layer eyes, are Scene properties of its own.

**Roots and poses.** Everything a board owns hangs from its root Empty, and the add-on places it in the root's space: KiCad nanometres around the board's origin, Z 0 at the bottom face. So a board moves as one, and nothing it draws needs to know where it stands; only what looks at the scene as a whole works in world space: picking, the camera and lights, collisions, the cut plane, the views. A board's pose is kept in KiCad's frame, not around its centre, which moves with an outline edit; the panel's numbers are worked out about the centre each time. A move by hand updates the pose, a KiCad frame places the root from it, and an undo step carries it back with the root.

**The picked board.** Tools that draw on one board at a time, the fold, a finding's drawing and markers, the cut plane's section, the Diff pairs list, work on the picked board; picking another hands them over, and the board left lies flat again.

**Saved copies.** Each board collection records its board frame and stack-up; the root carries its pose; the scene the session's offset. Reopening a `.blend` rebuilds the slots from the collections, without their KiCads, and the hole masks were packed into the file when it was saved.

## Where each value comes from

| Shown | Source |
| --- | --- |
| Where a board stands | Its pose: the Position numbers, a move with G or R, an opened arrangement, or the default (centred alone; at KiCad's X and Y a centimetre above the others). |
| Which KiCad a board belongs to | The bridge of the PCB editor whose Open in Blender added it. |
| Which KiCad Open in KiCad starts | The one the board came from, else the last KiCad seen, else an installed one. |
| Parts and board solids in the collision check | 3D model bounds (or placeholder envelopes) and the board outline, as drawn. |
| Mated pairs | Your marks; kept in the scene and the arrangement. |
| Add here or own window | Your answer, remembered in KiLeidoscope's settings beside its cache. |
| Copper, parts, layers of every board | Its KiCad, live; or the saved copy. |

## Known limitations

**Beta.** Multi-board is new, and the joining of boards has been exercised live on Windows; Linux and macOS use the same files and are untried live. Please report what you find.

- Four live boards per window. A fifth opens in a window of its own.
- One board per PCB editor window: a second board needs a second KiCad window. Opening another board in the same editor replaces the board in that slot, as it always did.
- Windows: a KiCad window started before the first Open in Blender shares the first KiCad's connection; close and reopen it.
- A dump file (`.kls`) always loads as board 1.
- View-only boards (**Import…**) are not in any KiCad: clicks do not select on them, and a package exported from a turned board is shown unturned.
- The collision check is a box check: parts are boxes, boards are their outlines. It says where to look, not whether a 0.1 mm clearance holds.
- The cut plane, fold, findings and Diff pairs work on the picked board only.
- The arrangement places KiCad frames. A board moved in KiCad (its outline redrawn somewhere else) stands where its coordinates now put it; re-save the arrangement after such an edit.

## Disclaimer

Multi-board mode is an illustration built from your design data and the assumptions above. It is not a mechanical CAD assembly, nor a clearance or tolerance check.

- **It is not a fit check.** Whether two boards fit a housing, whether a connector seats, and whether their heights clear each other, is for your mechanical CAD and your fab to confirm.
- **Do not take measurements from it.** Part heights come from 3D model bounds or placeholder envelopes, not from datasheets.

The [disclaimer](../README.md#disclaimer) and [license](../LICENSE) of KiLeidoscope apply in full: the software comes without any warranty, and you use it at your own risk.
