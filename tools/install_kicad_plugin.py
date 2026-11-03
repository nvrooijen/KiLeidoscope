"""Install the small KiCad action; implementation stays in this checkout."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIERS = ("com.github.nvrooijen.kileidoscope", "org.kileido.core")  # the second: before 1.0


def install(destination):
    destination = Path(destination)
    manifest = destination / "plugin.json"
    if destination.exists() and (not manifest.is_file() or
            json.loads(manifest.read_text())["identifier"] not in IDENTIFIERS):
        raise RuntimeError("Destination exists and is not our KiLeidoscope plugin")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.json", "launch.py", "requirements.txt", "icon_24.png", "icon_48.png"):
        shutil.copyfile(ROOT / "kicad_plugin" / name, destination / name)
    (destination / "checkout.json").write_text(json.dumps({"root": str(ROOT)}), encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(install(args.destination))
