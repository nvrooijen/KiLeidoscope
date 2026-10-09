# Studio

The Studio column is about the picture, not the board: the light on it, what
reflects in it, what the camera sees behind it, and a camera that frames it.
Nothing here changes the board or reaches KiCad; the settings are saved with
the Blender file.

Open it with the **Studio** tab on the left edge of the KiLeidoscope sidebar,
above IMS and Flex.

## Contents

- [Light and reflections](#light-and-reflections)
- [Background](#background)
- [Frame camera](#frame-camera)
- [Camera and lights by hand](#camera-and-lights-by-hand)
- [Other add-ons](#other-add-ons)

## Light and reflections

**Light** is the power of the two softboxes, one above the board and one below
it. They are fitted to the boards shown, growing with a larger board or an
assembly, and dimmed on their own for light solder masks, which would
otherwise wash out. The colour swatch beside it is the fill: what shadows and
diffuse bounces see around the board.

**Reflections** picks what glossy surfaces (the solder mask, metal finishes,
parts) reflect: one of the studio environments bundled with Blender, seen by
reflections only, or None for the fill colour. Neither the softboxes nor the
background change with it.

## Background

What the camera sees behind the board; lights and reflections do not see it.

- **Color:** one colour. Black by default.
- **Gradient:** from the bottom colour at the bottom of the frame to the top
  colour at its top.
- **None:** no background at all. Renders come out transparent; save them as
  PNG to keep that, for a slide or a web page. In the 3D view the background
  still shows.

## Frame camera

**Frame camera** puts the KiLeidoscope camera ("KLS Camera") where it sees
every board shown, the parts on it included, from the direction the 3D view
looks from, makes it the scene camera and looks through it. F12 then renders
what the view showed. Turn the view to another angle and press it again for a
new framing.

The camera is placed only when the button is pressed. Between presses it is
yours: move it, change its lens, animate it. Its lens, sensor and perspective
or orthographic type are kept from one framing to the next.

## Camera and lights by hand

**Show camera and lights** draws the softboxes and the camera in the 3D view as
the objects they are, so you can select and move them (G, R). Off, they still
light and render; Blender only stops drawing them.

A softbox you moved stays where you put it, through edits in KiCad and other
boards being added, until **Reset lights** (the button beside the switch) fits
both softboxes to the boards again.

## Other add-ons

Add-ons that build on KiLeidoscope (turntables, camera rigs, lighting setups)
use its studio hook, `kileido.studio`: the board's size, when edits from KiCad
have settled, the same camera framing as Frame camera, and a way to take the
lighting over. While one has, the Studio column says so in place of the light
settings. See [docs/studio-hook.md](studio-hook.md).
