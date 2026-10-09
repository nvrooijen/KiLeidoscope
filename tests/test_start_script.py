"""blender_addon/start.py runs only inside the Blender KiCad launches, so no headless test
imports it. Every `module.name` it uses from the add-on must exist there: a rename in the
add-on that misses this script breaks Open in Blender with an empty scene."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "blender_addon"


def _top_level_names(path):
    """Functions, classes and assigned names defined at the top of a module."""
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("."):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_start_script_uses_names_the_addon_defines():
    tree = ast.parse((ROOT / "start.py").read_text(encoding="utf-8"))
    modules = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "kileido":
            for alias in node.names:
                modules[alias.asname or alias.name] = alias.name
    assert modules, "start.py imports add-on modules with `from kileido import ...`"
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in modules:
            module = modules[node.value.id]
            if node.attr not in _top_level_names(ROOT / "kileido" / f"{module}.py"):
                missing.append(f"{node.value.id}.{node.attr}")
    assert not missing, missing
