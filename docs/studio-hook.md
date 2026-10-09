# Studio hook

Other Blender add-ons can build on KiLeidoscope: turntables, camera rigs,
lighting setups, render scripts. They use one module, `kileido.studio`, and
nothing else in the add-on. That module is kept stable across KiLeidoscope
versions. A change that would break a caller raises `API_VERSION`.

## Finding it

KiLeidoscope can be loaded under different module names, so look it up by its
alias when you need it, not at register time:

```python
import sys

def kileidoscope():
    studio = sys.modules.get("kileidoscope_studio")
    return studio if studio is not None and studio.API_VERSION >= 1 else None
```

`None` means KiLeidoscope isn't running in this Blender.

## The board

| Call | Returns |
|---|---|
| `has_board()` | a board is shown: live, a loaded dump or a view-only board |
| `is_live()` | the live link to KiCad is on |
| `board_bounds(parts=True)` | world `(low, high)` corners in metres, or `None` |
| `board_size(parts=True)` | the `(x, y, z)` extent in metres, or `None` |
| `board_center(parts=True)` | the middle of the bounds, or `None` |
| `selected_parts()` | the models (or placeholder boxes) of the footprints selected in KiCad |

With `parts`, the bounds include every component model that renders. Without
it, only the board solids.

## Updates from KiCad

Edits in KiCad arrive as several frames. Wait for them to settle before
fitting a camera or rendering.

| Call | Does |
|---|---|
| `update_count()` | board updates applied so far |
| `seconds_since_update()` | since the latest, or `None` |
| `is_settled(quiet_s=0.6)` | nothing queued, nothing new for `quiet_s` |
| `add_settled_listener(fn)` | calls `fn()` after each burst of updates settles |
| `remove_settled_listener(fn)` | |

Blender's timers don't run while a script blocks it, so a script that renders
frame by frame drives the link itself:

| Call | Does |
|---|---|
| `pump(seconds=0.05)` | runs the live link for `seconds` |
| `wait_for_update(since, timeout=60)` | pumps until an update newer than `since` has settled |
| `wait_until_settled(timeout=60)` | pumps until `is_settled()` |

Both waits return the new update count and raise `TimeoutError`.

```python
before = studio.update_count()
change_something_in_kicad()
studio.wait_for_update(before)
bpy.ops.render.render(write_still=True)
studio.pump()  # keep the link drained between frames
```

## Rendering while the link is live

Renders hold board updates from start to end: KiLeidoscope keeps receiving from
KiCad but changes nothing in the scene until the render is done. This covers
F12, Render Animation and `bpy.ops.render.render`. To hold updates around
other work:

```python
with studio.held_updates():
    ...  # the board stays as it is
```

`is_settled()` is `False` while held frames wait, so don't wait for settling
inside the block.

## Camera

| Call | Does |
|---|---|
| `fit_camera(camera=None, rotation=None, parts=True, margin=1.08)` | puts the camera where every board fills its frame, and makes it the scene camera |

Without `camera` it is KiLeidoscope's own, "KLS Camera", made when missing.
`rotation` is the direction to look from, a Quaternion (or anything with
`to_quaternion()`); without it, the direction the 3D view looks from. The
camera's lens, sensor and type (perspective or orthographic) are kept; its lens
shift centres the board. The board takes `1/margin` of the frame's limiting
side. Returns the camera, or `None` before any board. It is the same framing
the Studio column's Frame camera does.

## Lighting

KiLeidoscope sets up two softboxes and a world of its own (the Studio column's
background, seen by camera rays only), and refits them after every board update. An add-on with its own lights takes the scene
over:

| Call | Does |
|---|---|
| `claim_lighting(owner, scene=None)` | hides KiLeidoscope's softboxes; it stops changing the world, background and film transparency |
| `release_lighting(scene=None)` | KiLeidoscope sets its lighting up again |
| `lighting_owner(scene=None)` | who claimed it, or `""` |

The claim is saved with the scene. While it lasts the Studio column names
`owner` above the light, reflection and background settings, which are
disabled; Frame camera and the camera-and-lights overlay stay usable.
