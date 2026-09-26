"""KiCad action entry point.

Installed from the package (tools/build_package.py), the bridge and the Blender add-on
sit next to this file. A development install (tools/install_kicad_plugin.py) instead
points to one checkout through checkout.json; run in place, the checkout is the parent.
"""

import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
configuration = here / "checkout.json"
if configuration.exists():
    root = Path(json.loads(configuration.read_text(encoding="utf-8"))["root"])
elif (here / "kileido_bridge").is_dir():
    root = here
else:
    root = here.parent
sys.path.insert(0, str(root))
from kileido_bridge.launcher import launch  # noqa: E402

raise SystemExit(launch(root))
