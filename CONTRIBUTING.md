# Contributing to KiLeidoscope

Bug reports, ideas and pull requests are welcome. macOS testing is especially wanted: KiLeidoscope has not been tried there yet.

## Development setup

The add-on keeps its own copy of the frame decoder (`client.py`), checked against the bridge's by `tests/test_addon_protocol.py`.

The add-on draws the mask, silkscreen and drawing overlays with a small C library (`blender_addon/kileido/native/kls_raster.c`), about 8× faster than its numpy fallback and giving the same images. It is loaded through `ctypes`, so one build per OS and CPU serves every Python and Blender version. Build it for your platform (needs `gcc` or `clang`) before running the tests or building the package, which ships whichever libraries are in `native/`:

```
python tools/build_native.py
python -m pip install -e ".[test]"
python -m pytest -q
```

The Blender tests run headless against a synthetic KiCad board (`tests/fixtures/`):

```
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_all.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_live.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_boards.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_collisions.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_outline.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_silk.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_exports.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender/run_balance.py
```

`python tools/build_package.py` writes the KiCad package, `dist/kileidoscope-<version>.zip`.

## Without the plugin

Dump the board open in KiCad and view it without a live link:

```
python -m kileido_bridge dump board.kls
blender --python blender_addon/start.py -- board.kls
```

In a running viewer, F3 **KiLeidoscope: Load dump** opens a `.kls` file. A `.json` output name writes a human-readable dump instead.

`python -m kileido_bridge bridge` serves the open board live on port 47811 and prints its token; start Blender with `KILEIDO_BRIDGE_PORT` and `KILEIDO_BRIDGE_TOKEN` set to those values and `--python blender_addon/start.py` to connect to it.
