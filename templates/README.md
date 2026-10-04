# KiCad templates for flex mode

Two KiCad 10 project templates to start a flex or rigid-flex board from. They come with everything flex mode needs already set up: a polyimide stack-up, the user layers named **Flex**, **Bend** and **Stiffener**, design rules set for flex, and a few marks that fold the board. A cheat sheet with every mark sits beside the board on *User.Comments*. See the [flex mode guide](../docs/flex.md) for what each mark does.

| Template | What it is |
| --- | --- |
| [`kileidoscope-flex`](kileidoscope-flex) | Pure flex: 2 copper layers on a 50 µm polyimide core, amber coverlay. A body with mounting holes on a steel stiffener, and a tail that bends (R20) and twists 45° at once, ending in ZIF contacts on an epoxy stiffener. |
| [`kileidoscope-rigid-flex`](kileidoscope-rigid-flex) | Rigid-flex: 4 copper layers, FR4 outside and a 50 µm polyimide core inside, so In1.Cu and In2.Cu run through the flex. Two rigid ends joined by a flex zone with black coverlay; one bend (−180° R5) folds one end under the other. |

Both are templates to start from, not boards ready to fabricate.

## Installing

Copy a template's folder into KiCad's template folder for your own templates:

| System | Folder |
| --- | --- |
| Windows | `Documents\KiCad\10.0\template` |
| macOS | `~/Documents/KiCad/10.0/template` |
| Linux | `~/.local/share/kicad/10.0/template` |

It is KiCad's `KICAD_USER_TEMPLATE_DIR` (**Preferences → Configure Paths**). Then, in KiCad's project manager, choose **File → New Project from Template**. The templates are listed under **User Templates**.

You can also open a template's `.kicad_pro` straight from here to look around. Save your own work as a new project, so the template stays as it is.

## Hatched fills

KiCad 10 can fill a shape with a hatch or cross-hatch. KiCad 10.0.0 to 10.0.5 leave such fills out of plots made with `kicad-cli` (Gerbers included); plotting from the PCB Editor is not affected, and KiCad 10.0.6 fixes it ([KiCad issue #25177](https://gitlab.com/kicad/code/kicad/-/issues/25177)). The flex template's hatched ground on its tail is drawn as copper lines, so it plots either way. If you use hatched fills yourself, check your Gerbers before you order.
