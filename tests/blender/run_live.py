"""Headless Blender check of the live link: frames applied on its timer, stale geometry replaced."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
sys.path.insert(0, str(ROOT))

from kileido import live, models, state  # noqa: E402
from kileido_bridge.model import snapshot_from_jsonable  # noqa: E402
from kileido_bridge.protocol import FrameDecoder, messages_for, snapshot_frames  # noqa: E402


def decoded(frames):
    return FrameDecoder().feed(b"".join(frames))


def main():
    assert bpy.app.background
    fixture = ROOT / "tests" / "fixtures" / "synthetic_rf_geometry.kicad_dump.json"
    snapshot = snapshot_from_jsonable(json.loads(fixture.read_text(encoding="utf-8")))
    # A declared (not yet loaded) model gets a placeholder box; a footprint with no
    # model at all, such as a fiducial, gets none (as in KiCad's 3D viewer).
    first = replace(snapshot.footprints[0], bbox_nm=(8_000_000, 21_000_000,
                                                    8_000_000, 4_000_000),
                    model_paths=("${KICAD10_3DMODEL_DIR}/Test.3dshapes/body.step",),
                    model_visible=(True,))
    bare = replace(snapshot.footprints[1], bbox_nm=(20_000_000, 21_000_000,
                                                    1_000_000, 1_000_000),
                   model_paths=(), model_visible=())
    snapshot = replace(snapshot, footprints=(first, bare, *snapshot.footprints[2:]))
    initial = decoded(snapshot_frames(snapshot, revision=1))
    moved = replace(snapshot.tracks[0], start=(4_000_000, 5_000_000))
    changed = replace(snapshot, tracks=(moved, *snapshot.tracks[1:]))
    track_change = decoded(messages_for(changed, frozenset({("F.Cu", "tracks")}), revision=2))
    shifted = replace(first, pos=(first.pos[0] + 1_000_000, first.pos[1]),
                      bbox_nm=(9_000_000, 21_000_000, 8_000_000, 4_000_000))
    moved_footprint = replace(snapshot, footprints=(shifted, *snapshot.footprints[1:]))
    footprint_change = decoded(messages_for(moved_footprint,
                                            frozenset({("", "footprints")}), revision=3))

    class FakeClient:
        def __init__(self, *args):
            self.state = "disconnected"
            self.frames = list(initial)
            self.resyncs = 0

        def connect(self):
            self.state = "connected"

        def poll_io(self):
            frames, self.frames = self.frames, []
            return frames

        def request_resync(self):
            self.resyncs += 1

        def close(self):
            self.state = "disconnected"

    real_socket_client = live.SocketClient
    live.SocketClient = FakeClient
    try:
        live.connect(47811, "test")
        for _ in range(100):
            live.tick()
            if (not state.board.in_snapshot and
                    bpy.data.collections.get("KiLeidoscope: synthetic_rf_geometry.kicad_pcb") is not None and
                    bpy.data.collections["KiLeidoscope: synthetic_rf_geometry.kicad_pcb"].all_objects.get("KLS F.Cu tracks")):
                break
        else:
            raise AssertionError("initial snapshot was not applied")
        collection = bpy.data.collections["KiLeidoscope: synthetic_rf_geometry.kicad_pcb"]
        placeholder = collection.all_objects[f"KLS footprint placeholder {first.id}"]
        footprint_empty = collection.all_objects[f"KLS footprint {first.id}"]
        assert placeholder.parent is None and footprint_empty.parent is None
        assert footprint_empty.hide_get() and footprint_empty["kls_active"] == 1
        assert footprint_empty.empty_display_size < 1.01e-4
        assert placeholder["kls_footprint_id"] == first.id
        assert len(placeholder.data.vertices) == 1
        assert not placeholder.hide_get()
        assert collection.all_objects[f"KLS footprint placeholder {bare.id}"].hide_get()
        top = collection.all_objects["KLS F.Cu tracks"]
        original_pointer = top.as_pointer()
        original_data_pointer = top.data.as_pointer()
        client = live.link.client
        client.frames.extend(track_change)
        for _ in range(100):
            live.tick()
            if top.data.vertices[0].co.x < -0.0155:
                break
        else:
            raise AssertionError("incremental track frame was not applied")
        assert top.as_pointer() == original_pointer
        assert top.data.as_pointer() == original_data_pointer
        assert live.link.last_update_at is not None and live.link.last_apply_ms is not None
        placeholder_pointer = placeholder.as_pointer()
        placeholder_data_pointer = placeholder.data.as_pointer()
        before_x = footprint_empty.location.x
        client.frames.extend(footprint_change)
        for _ in range(100):
            live.tick()
            if footprint_empty.location.x > before_x + 0.0009:
                break
        else:
            raise AssertionError("live footprint move was not applied")
        assert placeholder.as_pointer() == placeholder_pointer
        assert placeholder.data.as_pointer() == placeholder_data_pointer
        assert placeholder.parent is None
        source_mesh = bpy.data.meshes.new("KLS test model mesh")
        source_mesh.vertices.add(1)
        source_root = bpy.data.objects.new("KLS test GLB root", None)
        source_ref = bpy.data.objects.new(first.reference, None)
        source_part = bpy.data.objects.new("KLS test model part", source_mesh)
        for obj in (source_root, source_ref, source_part):
            bpy.context.scene.collection.objects.link(obj)
        source_ref.parent = source_root
        source_part.parent = source_ref
        source_ref.location = footprint_empty.location
        source_part.location.x = 0.0003
        bound = models.bind_root(source_root, "synthetic-test-hash")
        assert bound["matched"] == 1 and bound["parts"] == 1
        clone = collection.all_objects[f"KLS model {first.id} 0"]
        assert clone.parent is None and clone.data == source_mesh
        assert len(clone["kls_model_local_matrix"]) == 16
        assert placeholder.hide_get()
        bpy.context.view_layer.update()
        clone_before = clone.matrix_world.translation.copy()
        clone_pointer, clone_data_pointer = clone.as_pointer(), clone.data.as_pointer()
        second_shift = replace(shifted, pos=(shifted.pos[0] + 1_000_000, shifted.pos[1]),
                               bbox_nm=(10_000_000, 21_000_000, 8_000_000, 4_000_000))
        second_frame = decoded(messages_for(replace(snapshot, footprints=(second_shift,
                                                    *snapshot.footprints[1:])),
                                            frozenset({("", "footprints")}), revision=4))
        client.frames.extend(second_frame)
        live.tick()
        bpy.context.view_layer.update()
        assert abs((clone.matrix_world.translation.x - clone_before.x) - 0.001) < 1e-7
        assert clone.as_pointer() == clone_pointer and clone.data.as_pointer() == clone_data_pointer
        changed_path = replace(second_shift, model_paths=("new-model.glb",))
        path_frame = decoded(messages_for(replace(snapshot, footprints=(changed_path,
                                                  *snapshot.footprints[1:])),
                                          frozenset({("", "footprints")}), revision=5))
        client.frames.extend(path_frame)
        live.tick()
        assert clone.hide_get() and not placeholder.hide_get()
        client.state = "disconnected"
        live.tick()
        assert not collection.hide_viewport
        assert len(top.data.vertices) > 0
        assert "Stale" in live.status_text()
    finally:
        live.disconnect()
        live.SocketClient = real_socket_client
    print("KLS_PHASE3_TIMER_OK")


if __name__ == "__main__":
    main()
