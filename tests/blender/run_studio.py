"""Headless Blender check of the studio hook (kileido.studio): board size, updates and
settling, holding updates under a render, and lighting claimed by another add-on."""

import itertools
import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Euler, Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))

import kileido  # noqa: E402
from kileido import camera, lighting, live, shading, state, studio  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402
from kileido_bridge.protocol import FrameDecoder, messages_for, snapshot_frames  # noqa: E402

BOARD = "KiLeidoscope: synthetic_rf_geometry.kicad_pcb"


def decoded(frames):
    return FrameDecoder().feed(b"".join(frames))


def near(color, expected):
    return all(abs(a - b) < 1e-6 for a, b in zip(color, expected))


def main():
    assert bpy.app.background
    kileido.register()
    assert sys.modules["kileidoscope_studio"] is studio and studio.API_VERSION == 1
    assert not studio.has_board() and studio.board_bounds() is None and studio.board_size() is None

    fixture = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
    snapshot = snapshot_from_jsonable(json.loads(fixture.read_text(encoding="utf-8")))
    # One footprint declares a model that isn't loaded: it gets a placeholder box.
    first = replace(snapshot.footprints[0], bbox_nm=(8_000_000, 21_000_000, 8_000_000, 4_000_000),
                    model_paths=("${KICAD10_3DMODEL_DIR}/Test.3dshapes/body.step",), model_visible=(True,))
    snapshot = replace(snapshot, footprints=(first, *snapshot.footprints[1:]))
    initial = decoded(snapshot_frames(snapshot, revision=1))
    moved = replace(snapshot.tracks[0], start=(4_000_000, 5_000_000))
    track_change = decoded(messages_for(replace(snapshot, tracks=(moved, *snapshot.tracks[1:])),
                                        frozenset({("F.Cu", "tracks")}), revision=2))
    moved_back = decoded(messages_for(snapshot, frozenset({("F.Cu", "tracks")}), revision=3))

    class FakeClient:
        def __init__(self, *args):
            self.state = "disconnected"
            self.frames = list(initial)

        def connect(self):
            self.state = "connected"

        def poll_io(self):
            frames, self.frames = self.frames, []
            return frames

        def request_resync(self, adopt=False):
            pass

        def close(self):
            self.state = "disconnected"

    real_socket_client = live.SocketClient
    live.SocketClient = FakeClient
    try:
        before = studio.update_count()
        live.connect(47811, "test")
        assert studio.is_live()
        count = studio.wait_for_update(before, timeout=30, quiet_s=0.2)
        assert count > before and studio.is_settled(0.2)
        assert bpy.data.collections[BOARD].all_objects.get("KLS F.Cu tracks") is not None

        # Board size: the fixture board's solid, in metres.
        low, high = studio.board_bounds(parts=False)
        size = studio.board_size(parts=False)
        assert 0.005 < size.x < 0.5 and 0.005 < size.y < 0.5 and 0 < size.z < 0.01, size
        assert (studio.board_center(parts=False) - (low + high) / 2).length < 1e-9
        with_parts = studio.board_size(parts=True)
        assert with_parts.x >= size.x - 1e-9 and with_parts.z >= size.z - 1e-9

        # The parts selected in KiCad: their models or placeholder boxes.
        assert studio.selected_parts() == []
        box = next(obj for obj in bpy.data.collections[BOARD].all_objects
                   if obj.get("kls_footprint_placeholder") == 1 and obj.get("kls_footprint_id") == first.id)
        state.board.highlight_components = {"footprints": {first.id}, "pads": set()}
        assert studio.selected_parts() == [box]
        state.board.highlight_components = {"footprints": set(), "pads": set()}

        # An edit arrives and settles; is_settled stays False while it is fresh.
        client = live.link.client
        top = bpy.data.collections[BOARD].all_objects["KLS F.Cu tracks"]
        before = studio.update_count()
        client.frames.extend(track_change)
        studio.pump(0.05)
        assert studio.update_count() > before and not studio.is_settled(5.0)
        studio.wait_until_settled(timeout=10, quiet_s=0.2)
        assert top.data.vertices[0].co.x < -0.0155

        # Held: received but not applied until the block ends.
        before = studio.update_count()
        with studio.held_updates():
            assert studio.updates_held()
            client.frames.extend(moved_back)
            studio.pump(0.1)
            assert studio.update_count() == before and live.link.pending
            assert not studio.is_settled()
        assert not studio.updates_held()
        studio.wait_for_update(before, timeout=10, quiet_s=0.2)
        assert top.data.vertices[0].co.x > -0.0155 and not live.link.pending

        # A render (F12, Render Animation) holds updates from start to end; a render that
        # ended without render_complete or render_cancel does not hold them for good.
        studio._render_started()
        assert studio.updates_held()
        studio._render_seen_at -= studio.STALE_RENDER_S + 1  # no render job, no handler since
        assert not studio.updates_held()
        studio._render_seen()  # render_pre of a blocking render's next frame
        assert studio.updates_held()
        studio._render_ended()
        assert not studio.updates_held()
        for name in ("render_init", "render_pre", "render_complete", "render_cancel"):
            assert any(handler.__module__ == studio.__name__ for handler in getattr(bpy.app.handlers, name)), name

        # Settled listeners: called once a burst of updates has settled.
        calls = []
        studio.add_settled_listener(lambda: calls.append(studio.update_count()))
        before = studio.update_count()
        client.frames.extend(track_change)
        studio.pump(0.05)
        assert bpy.app.timers.is_registered(studio._settle_check)
        assert studio._settle_check() is not None  # still fresh: checks again later
        time.sleep(studio.SETTLE_S + 0.05)
        assert studio._settle_check() is None and calls == [studio.update_count()] and calls[0] > before

        # Lighting: claimed, KiLeidoscope leaves the world and softboxes alone, also
        # through a full resync; released, it sets them up again.
        scene = bpy.context.scene
        lights = next(child for child in scene.collection.children if child.get("kls_studio_lights") == 1)
        assert scene.world.get("kls_black_background") and not lights.hide_render
        kit_world = bpy.data.worlds.new("Kit world")
        studio.claim_lighting("Turntable add-on")
        assert studio.lighting_owner() == "Turntable add-on" and lighting.claimed()
        assert lights.hide_render and lights.hide_viewport
        scene.world = kit_world
        scene.render.film_transparent = True
        before = studio.update_count()
        client.frames.extend(initial)  # a full snapshot, as after a resync
        studio.wait_for_update(before, timeout=30, quiet_s=0.2)
        gains, real_gain = [], shading.set_lit_gain
        shading.set_lit_gain = gains.append
        try:
            scene.kileido_light_power = 0.5  # the panel's update callback
        finally:
            shading.set_lit_gain = real_gain
        assert gains  # the shaded colours' gain is KiLeidoscope's whoever owns the lights
        assert lighting.ensure_studio_lights() is None
        assert scene.world == kit_world and scene.render.film_transparent and lights.hide_render
        studio.release_lighting()
        assert studio.lighting_owner() == "" and not lighting.claimed()
        assert scene.world.get("kls_black_background") and not lights.hide_render
        assert not scene.render.film_transparent

        # Studio column: the background the camera sees (one colour, a gradient, none).
        tree = scene.world.node_tree
        scene.kileido_background = "GRADIENT"
        scene.kileido_background_color = (0.1, 0.2, 0.3)
        scene.kileido_background_top = (0.4, 0.5, 0.6)
        gradient = tree.nodes[lighting.GRADIENT]
        assert tree.nodes[lighting.CAMERA_BACKGROUND].inputs["Color"].is_linked
        assert near(shading.typed_socket(gradient.inputs, "A").default_value, (0.1, 0.2, 0.3))
        assert near(shading.typed_socket(gradient.inputs, "B").default_value, (0.4, 0.5, 0.6))
        assert not scene.render.film_transparent
        scene.kileido_background = "TRANSPARENT"
        assert scene.render.film_transparent
        scene.kileido_background = "SOLID"
        assert not scene.render.film_transparent
        assert near(shading.typed_socket(gradient.inputs, "B").default_value, (0.1, 0.2, 0.3))  # one colour

        # Frame camera: every corner of the board, parts included, inside the frame from
        # any direction, the frame used, the camera the scene's.
        for rotation in (None, Euler((math.radians(90), 0.0, 0.0)).to_quaternion(),
                         Euler((0.0, 0.0, math.radians(30))).to_quaternion()):
            cam = studio.fit_camera(rotation=rotation)
            assert cam is not None and scene.camera == cam and cam.name == "KLS Camera"
            low, high = studio.board_bounds()
            seen = [world_to_camera_view(scene, cam, Vector(corner))
                    for corner in itertools.product((low.x, high.x), (low.y, high.y), (low.z, high.z))]
            assert all(0.0 <= v.x <= 1.0 and 0.0 <= v.y <= 1.0 and v.z > 0 for v in seen), (rotation, seen)
            spans = (max(v.x for v in seen) - min(v.x for v in seen), max(v.y for v in seen) - min(v.y for v in seen))
            assert max(spans) > 0.8, (rotation, spans)  # the frame is used, not just contained
        assert camera.frame(rotation=Euler((0.0, 0.0, 0.0))) is cam  # anything with to_quaternion()
        cam.data.type = "ORTHO"
        studio.fit_camera()
        seen = [world_to_camera_view(scene, cam, Vector(corner))
                for corner in itertools.product((low.x, high.x), (low.y, high.y), (low.z, high.z))]
        assert all(0.0 <= v.x <= 1.0 and 0.0 <= v.y <= 1.0 and v.z > 0 for v in seen), seen
        # The lens shift is a fraction of the fitted side: an explicit sensor fit with the
        # other aspect, and a portrait frame, frame the board as well.
        cam.data.type = "PERSP"
        resolution = scene.render.resolution_x, scene.render.resolution_y
        for fit, size in (("HORIZONTAL", (500, 900)), ("VERTICAL", (900, 500)), ("AUTO", (500, 900))):
            cam.data.sensor_fit = fit
            scene.render.resolution_x, scene.render.resolution_y = size
            studio.fit_camera(rotation=Euler((math.radians(60), 0.0, math.radians(45))))
            seen = [world_to_camera_view(scene, cam, Vector(corner))
                    for corner in itertools.product((low.x, high.x), (low.y, high.y), (low.z, high.z))]
            assert all(0.0 <= v.x <= 1.0 and 0.0 <= v.y <= 1.0 and v.z > 0 for v in seen), (fit, size, seen)
            spans = (max(v.x for v in seen) - min(v.x for v in seen), max(v.y for v in seen) - min(v.y for v in seen))
            assert max(spans) > 0.8, (fit, size, spans)
        cam.data.sensor_fit = "AUTO"
        scene.render.resolution_x, scene.render.resolution_y = resolution

        # Lights shown to move: a softbox moved by hand stays through refits, until reset.
        top_light = next(obj for obj in lights.objects if obj.get("kls_studio_side") == "top")
        fitted = top_light.location.copy()
        scene.kileido_show_rig = True
        assert not lights.hide_select and lighting.user_placed_lights() == []
        top_light.location.x += 0.05
        lighting.fit_to_boards()
        assert abs(top_light.location.x - fitted.x - 0.05) < 1e-9 and lighting.user_placed_lights() == [top_light]
        lighting.reset_lights()
        assert (top_light.location - fitted).length < 1e-9 and lighting.user_placed_lights() == []
        scene.kileido_show_rig = False
        assert lights.hide_select
    finally:
        live.disconnect()
        live.SocketClient = real_socket_client
        kileido.unregister()
    assert "kileidoscope_studio" not in sys.modules
    assert not any(handler.__module__ == studio.__name__ for handler in bpy.app.handlers.render_init)
    print("KLS_STUDIO_OK")


if __name__ == "__main__":
    main()
