"""Start KiLeidoscope in Blender from this checkout without installing a copy.

    blender --python blender_addon/start.py [-- board.kls]

The script registers the local add-on and can load a dump. Saved-board models
and drawing layers load automatically when that dump contains a board path.
"""

import os
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kileido  # noqa: E402
from kileido import devices, dump, live, lighting  # noqa: E402

if (bpy.context.window is not None and not bpy.data.filepath and
        {obj.name for obj in bpy.context.scene.objects} == {"Cube", "Camera", "Light"}):
    # Keep the factory scene intact; open an empty viewer scene so its cube
    # cannot obscure the board.
    bpy.context.window.scene = bpy.data.scenes.new("KiLeidoscope Viewer")
kileido.register()
lighting.ensure_black_background()
bpy.context.scene.kileido_color_mode = "REALISTIC"


def _start_in_cycles(scene):
    """Cycles as render engine, Material Preview in the viewport (the panel toggles
    a Cycles viewport), sidebar shown, rendering on the GPU when there is one
    (`devices.use_gpu`)."""
    scene.render.engine = "CYCLES"
    # A board reads cleanly after 16 denoised samples; final renders keep their own count.
    scene.cycles.preview_samples = 16
    scene.cycles.use_preview_denoising = True
    scene.cycles.preview_denoiser = "AUTO"  # OptiX on an RTX GPU, else OpenImageDenoise
    if devices.use_gpu():
        scene.cycles.device = "GPU"
    for area in bpy.context.screen.areas if bpy.context.screen else ():
        if area.type != "VIEW_3D":
            continue
        space = area.spaces.active
        space.region_3d.view_distance = 0.15
        space.shading.type = "MATERIAL"  # Material Preview; the panel toggles Cycles
        space.show_region_ui = True


def _open_kileido_tab():
    """Show the KiLeidoscope sidebar tab. The sidebar only accepts a tab once it has
    been drawn, so this retries briefly."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "UI":
                        try:
                            region.active_panel_category = "KiLeidoscope"
                        except (AttributeError, TypeError):
                            pass
    _open_kileido_tab.tries -= 1
    return 0.5 if _open_kileido_tab.tries > 0 else None


_open_kileido_tab.tries = 4
_start_in_cycles(bpy.context.scene)
if not bpy.app.background:
    bpy.app.timers.register(_open_kileido_tab, first_interval=0.3)
port = os.environ.pop("KILEIDO_BRIDGE_PORT", "")
token = os.environ.pop("KILEIDO_BRIDGE_TOKEN", "")
if port and token:
    live.connect(int(port), token)
if "--" in sys.argv:
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) != 1 or not Path(args[0]).is_file():
        raise ValueError("Pass one existing .kls dump after --")
    dump.load_async(str(Path(args[0]).resolve()))
