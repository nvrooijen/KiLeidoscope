"""KiCad action entry point.

Installed from the package (tools/build_package.py), the bridge and the Blender add-on
sit next to this file. A development install (tools/install_kicad_plugin.py) instead
points to one checkout through checkout.json; run in place, the checkout is the parent.
"""

import json
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
for own in dict.fromkeys((sysconfig.get_path("purelib"), sysconfig.get_path("platlib"))):
    if own in sys.path:
        sys.path.remove(own)
        sys.path.insert(1, own)
from kileido_bridge.launcher import launch  # noqa: E402

raise SystemExit(launch(root))
