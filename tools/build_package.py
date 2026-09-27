"""Build the KiCad package: one zip with the KiCad action, the bridge and the Blender
add-on, installed through KiCad's Plugin and Content Manager (Install from File...).

    python tools/build_package.py            # writes dist/kileidoscope-<version>.zip

The zip follows KiCad's PCM layout: metadata.json, resources/icon.png, and the plugin
itself in plugins/ (plugin.json at its top, so KiCad finds the action there). The
version comes from pyproject.toml, the name and identifier from kicad_plugin/plugin.json.
"""
import argparse
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOMEPAGE = "https://github.com/nvrooijen/KiLeidoscope"
TIMESTAMP = (2026, 1, 1, 0, 0, 0)  # fixed: the same sources give the same zip


def version() -> str:
    """pyproject.toml's version (read with a pattern: tomllib needs Python 3.11)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.MULTILINE)[1]


def plugin_files() -> dict[str, Path]:
    """Archive path under plugins/ -> source file."""
    files = {name: ROOT / "kicad_plugin" / name
             for name in ("plugin.json", "launch.py", "requirements.txt", "icon_24.png", "icon_48.png")}
    files["LICENSE"] = ROOT / "LICENSE"
    files["blender_addon/start.py"] = ROOT / "blender_addon" / "start.py"
    for folder, patterns in (("kileido_bridge", ("*.py", "dcsolve/*.py")),
                             ("blender_addon/kileido", ("*.py", "icons/*.png", "native/*.c",
                                                        "native/*.dll", "native/*.so", "native/*.dylib"))):
        for pattern in patterns:
            for path in sorted((ROOT / folder).glob(pattern)):
                files[path.relative_to(ROOT).as_posix()] = path
    return files


def metadata(plugin: dict) -> dict:
    """PCM metadata (schema v2). download_* fields belong only in a repository's copy."""
    return {
        "$schema": "https://go.kicad.org/pcm/schemas/v2",
        "name": plugin["name"],
        "description": plugin["description"],
        "description_full": (
            "Opens the board in the PCB Editor in Blender 5.1 and keeps it current as you edit, "
            "unsaved changes included. Read-only: the only thing it changes in KiCad is the "
            "selection, when you click an item in Blender.\n\n"
            "Needs Blender 5.1 or newer (found on PATH or in its default install places, or set "
            "KILEIDO_BLENDER) and KiCad's API enabled: Preferences > Plugins > Enable KiCad API."),
        "identifier": plugin["identifier"],
        "type": "plugin",
        "author": {"name": "nvrooijen", "contact": {"web": HOMEPAGE}},
        "license": "GPL-3.0-or-later",
        "resources": {"Homepage": HOMEPAGE},
        "tags": ["blender", "viewer", "live-view"],
        "versions": [{
            "version": version(),
            "status": "development",
            "kicad_version": "10.0",
            "runtime": "ipc",
            "platforms": ["windows", "linux", "macos"],
        }],
    }


def build(output_dir: Path) -> Path:
    plugin = json.loads((ROOT / "kicad_plugin" / "plugin.json").read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"kileidoscope-{version()}.zip"
    entries = {"metadata.json": json.dumps(metadata(plugin), indent=2).encode("utf-8"),
               "resources/icon.png": (ROOT / "kicad_plugin" / "icon_64.png").read_bytes()}
    entries.update({f"plugins/{name}": path.read_bytes() for name, path in plugin_files().items()})
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    print(build(parser.parse_args().output_dir))
