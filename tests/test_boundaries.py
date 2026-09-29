"""CI guard for process separation and read-only KiCad calls."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "kileido_bridge"
ADDON = ROOT / "blender_addon"
FORBIDDEN_IMPORTS = {"bpy", "pcbnew", "shapely", "bmesh", "kipy"}
FORBIDDEN_CALLS = {
    "create_items", "update_items", "remove_items", "remove_items_by_id",
    "push_commit", "begin_commit", "save", "save_as", "revert",
    "refill_zones", "interactive_move", "add_to_selection",
    "remove_from_selection", "clear_selection", "run_action",
}


def imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


def test_only_reader_imports_kipy_and_no_banned_bridge_imports():
    for path in BRIDGE.rglob("*.py"):
        present = set(imports(path))
        assert not present & (FORBIDDEN_IMPORTS - {"kipy"}), path
        if path.name != "kicad_reader.py":
            assert "kipy" not in present, path


def test_addon_has_no_bridge_or_kipy_imports():
    for path in ADDON.rglob("*.py") if ADDON.exists() else ():
        assert not set(imports(path)) & {"kipy", "kileido_bridge", "pcbnew"}, path


SELECTION_CALLS = {"clear_selection", "add_to_selection"}  # select_in_kicad: a click in Blender


def _calls(tree):
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


def test_no_board_mutation_calls_in_reader():
    tree = ast.parse((BRIDGE / "kicad_reader.py").read_text(encoding="utf-8"))
    exception = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "select_in_kicad"]
    assert len(exception) == 1
    assert _calls(exception[0]) & FORBIDDEN_CALLS <= SELECTION_CALLS  # only the selection calls
    exception[0].body = []  # everything outside it stays read-only
    used = _calls(tree)
    assert not used & FORBIDDEN_CALLS
    assert not any(name.startswith("set_") for name in used)


def test_no_board_mutation_calls_elsewhere_in_bridge():
    for path in BRIDGE.rglob("*.py"):
        if path.name != "kicad_reader.py":
            used = _calls(ast.parse(path.read_text(encoding="utf-8")))
            assert not used & FORBIDDEN_CALLS, path


RAW_COMMANDS = {"GetKiCadBinaryPath", "GetVersion", "PathResponse", "GetVersionResponse"}
VIEW_COMMANDS = {"RunAction", "RunActionResponse"}  # select_in_kicad: pan KiCad to the selection
VIEW_ACTION = "common.Control.centerSelection"


def _commands(tree):
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "commands"}


def _select_in_kicad(tree):
    return next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "select_in_kicad")


def test_raw_ipc_commands_are_read_only():
    """`client.send` passes any command through kipy: only these queries may use it,
    and select_in_kicad may also run a view action."""
    tree = ast.parse((BRIDGE / "kicad_reader.py").read_text(encoding="utf-8"))
    select = _select_in_kicad(tree)
    assert _commands(select) <= RAW_COMMANDS | VIEW_COMMANDS
    select.body = []
    assert _commands(tree) <= RAW_COMMANDS


def test_the_only_kicad_action_pans_the_view():
    """RunAction runs any KiCad tool, editing ones too: the action is always
    CENTER_ACTION, which only pans the PCB editor."""
    tree = ast.parse((BRIDGE / "kicad_reader.py").read_text(encoding="utf-8"))
    assigned = [node.value for node in ast.walk(_select_in_kicad(tree)) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Attribute) and target.attr == "action" for target in node.targets)]
    assert assigned and all(isinstance(value, ast.Name) and value.id == "CENTER_ACTION" for value in assigned)
    constants = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets if isinstance(target, ast.Name)}
    assert isinstance(constants["CENTER_ACTION"], ast.Constant)
    assert constants["CENTER_ACTION"].value == VIEW_ACTION
