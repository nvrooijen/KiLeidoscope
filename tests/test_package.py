"""The KiCad package (tools/build_package.py) carries everything the action needs."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location("build_package", ROOT / "tools" / "build_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_layout_and_metadata(tmp_path):
    archive = zipfile.ZipFile(_builder().build(tmp_path))
    names = set(archive.namelist())
    assert {"metadata.json", "resources/icon.png", "plugins/plugin.json", "plugins/launch.py",
            "plugins/requirements.txt", "plugins/blender_addon/start.py"} <= names
    for folder in ("kileido_bridge", "blender_addon/kileido"):
        for path in (ROOT / folder).glob("*.py"):
            assert f"plugins/{path.relative_to(ROOT).as_posix()}" in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
    metadata = json.loads(archive.read("metadata.json"))
    plugin = json.loads(archive.read("plugins/plugin.json"))
    assert metadata["identifier"] == plugin["identifier"]
    assert metadata["versions"][0]["runtime"] == "ipc"


def test_versions_agree():
    """pyproject.toml, the package and the Blender add-on's bl_info carry one version."""
    version = _builder().version()
    addon = (ROOT / "blender_addon" / "kileido" / "__init__.py").read_text(encoding="utf-8")
    bl_info = re.search(r'"version": \((\d+), (\d+), (\d+)\)', addon).groups()
    assert ".".join(bl_info) == version


def test_installed_action_finds_its_bundled_code(tmp_path):
    """Unpacked as KiCad installs it, launch.py imports the bridge from beside itself
    (not from this checkout) and passes that folder as the root."""
    zipfile.ZipFile(_builder().build(tmp_path)).extractall(tmp_path / "installed")
    plugins = tmp_path / "installed" / "plugins"
    probe = ("import runpy, sys, kileido_bridge.launcher as launcher\n"
             "launcher.launch = lambda root: print(root, launcher.__file__, sep=chr(10)) or 0\n"
             f"runpy.run_path({str(plugins / 'launch.py')!r})\n")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            cwd=plugins, env={**os.environ, "PYTHONPATH": str(plugins)})
    root, module = result.stdout.splitlines()
    assert Path(root) == plugins and Path(module).is_relative_to(plugins), result.stderr


def test_installed_action_prefers_its_environment_over_pythonpath(tmp_path):
    """KiCad (and a sourced ROS setup) put folders such as /usr/lib/python3/dist-packages on
    PYTHONPATH; their older packages (protobuf) must not shadow the plugin environment's."""
    zipfile.ZipFile(_builder().build(tmp_path)).extractall(tmp_path / "installed")
    plugins = tmp_path / "installed" / "plugins"
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "pytest.py").write_text("raise ImportError('shadowed by PYTHONPATH')\n")
    probe = ("import runpy, sys, types\n"
             "sys.modules['kileido_bridge.launcher'] = types.SimpleNamespace(launch=lambda root: 0)\n"
             "try:\n"
             f"    runpy.run_path({str(plugins / 'launch.py')!r})\n"
             "except SystemExit:\n"
             "    pass\n"
             "import pytest\n"
             "print(pytest.__file__)\n")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            cwd=plugins, env={**os.environ, "PYTHONPATH": str(shadow)})
    paths = subprocess.run([sys.executable, "-c", "import sys, sysconfig; print(sysconfig.get_path('purelib'), "
                            "sysconfig.get_path('platlib'), sys.path)"], capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(shadow)}).stdout
    assert result.returncode == 0, result.stderr + paths
    assert not Path(result.stdout.strip()).is_relative_to(shadow), paths
