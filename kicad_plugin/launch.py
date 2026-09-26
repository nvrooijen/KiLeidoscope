"""KiCad action entry point.

Installed from the package (tools/build_package.py), the bridge and the Blender add-on
sit next to this file. A development install (tools/install_kicad_plugin.py) instead
points to one checkout through checkout.json; run in place, the checkout is the parent.
"""

import json
import os
import sys
import sysconfig
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
# PYTHONPATH (from KiCad, or a sourced ROS setup) comes before the plugin's environment,
# so an older system protobuf would shadow the one kicad-python needs. Own packages first.
# Compared normalised: on Windows sys.path may spell ...\Lib\site-packages as ...\lib\...
own = {os.path.normcase(os.path.abspath(sysconfig.get_path(key))) for key in ("purelib", "platlib")}
first = [path for path in sys.path if path and os.path.normcase(os.path.abspath(path)) in own]
for path in first:
    sys.path.remove(path)
sys.path[1:1] = first
from kileido_bridge.launcher import launch  # noqa: E402

raise SystemExit(launch(root))
