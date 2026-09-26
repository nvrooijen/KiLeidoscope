"""KiCad action: start a private bridge and its Blender viewer together."""

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .loop import BridgeRuntime
from .server import BridgeServer

MINIMUM_BLENDER = (5, 1)
_VERSION = re.compile(rb"Blender (\d+)\.(\d+)")


def cache_root() -> Path:
    """The per-user cache folder the add-on also uses (kicad_cli.cache_root)."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "KiLeidoscope"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "KiLeidoscope"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kileidoscope"


def blender_version(executable) -> tuple[int, int] | None:
    """(major, minor) from `blender --version` (~0.1 s), or None when it does not run."""
    try:
        output = subprocess.run([str(executable), "--version"], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search(output)
    return (int(match[1]), int(match[2])) if match else None


def blender_candidates() -> list[str]:
    """Blender executables in the order they are tried: PATH, then per-OS install places,
    newest first. Linux tarballs unpack to folders named `blender-<version>-linux-x64`."""
    found = [shutil.which("blender")]
    if sys.platform == "win32":
        root = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Blender Foundation"
        found += sorted(root.glob("Blender */blender.exe"), key=_version_key, reverse=True)
    elif sys.platform == "darwin":
        found.append("/Applications/Blender.app/Contents/MacOS/Blender")
    else:
        home = Path.home()
        tarballs = [path for folder in (home, home / "Applications", home / ".local" / "opt", Path("/opt"))
                    for path in folder.glob("blender*/blender")]
        found += sorted(tarballs, key=_version_key, reverse=True)
        found += ["/snap/bin/blender", "/usr/bin/blender", "/usr/local/bin/blender"]
    unique = []
    for path in found:
        if path and Path(path).is_file() and str(path) not in unique:
            unique.append(str(path))
    return unique


def _version_key(path: Path) -> tuple[int, ...]:
    """(5, 1, 0) from ".../blender-5.1.0-linux-x64/blender" or ".../Blender 5.1/blender.exe"."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", path.parent.name)
    return tuple(int(part or 0) for part in match.groups()) if match else (0,)


def find_blender() -> str:
    """KILEIDO_BLENDER when set, else the first candidate that is Blender 5.1 or newer."""
    configured = os.environ.get("KILEIDO_BLENDER")
    if configured:
        if not Path(configured).is_file():
            raise RuntimeError(f"KILEIDO_BLENDER does not point to a Blender executable: {configured}")
        return configured
    too_old = []
    for candidate in blender_candidates():
        version = blender_version(candidate)
        if version is not None and version >= MINIMUM_BLENDER:
            return candidate
        if version is not None:
            too_old.append(f"{candidate} ({version[0]}.{version[1]})")
    wanted = ".".join(map(str, MINIMUM_BLENDER))
    if too_old:
        raise RuntimeError(f"Blender {wanted} or newer is needed; found only " + ", ".join(too_old) +
                           ". Install Blender " + wanted + " or set KILEIDO_BLENDER to its executable.")
    raise RuntimeError(f"Blender {wanted} was not found. Install it (on Linux: unpack the official "
                       "tarball in your home folder or /opt, or link its `blender` into ~/.local/bin), "
                       "or set KILEIDO_BLENDER to its executable.")


def _tell_user(message: str) -> None:
    """Best effort: KiCad shows nothing a plugin prints, so failures surface here."""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "KiLeidoscope", 0x10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "--app-name=KiLeidoscope", "KiLeidoscope", message], timeout=10)
    except Exception:
        pass


def _stop_on_signals():
    """KiCad or the session ending sends SIGTERM/SIGHUP: leave through `finally`, so the
    bridge's temporary board copy is removed."""
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda number, frame: sys.exit(0))


def launch(root=None) -> int:
    """Run from kicad_plugin/launch.py: serve the board until the Blender window closes.
    Blender's output goes to `<cache>/blender.log`."""
    try:
        if not os.environ.get("KICAD_API_SOCKET"):
            raise RuntimeError("Start Open in Blender from the KiCad PCB Editor to select the correct board.")
        executable = find_blender()
    except RuntimeError as exc:
        _tell_user(str(exc))
        raise
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    _stop_on_signals()
    server = BridgeServer(port=0)
    runtime = BridgeRuntime(server)
    environment = os.environ.copy()
    environment["KILEIDO_BRIDGE_PORT"] = str(server.port)
    environment["KILEIDO_BRIDGE_TOKEN"] = server.token
    log_path = cache_root() / "blender.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as log:
            viewer = subprocess.Popen(
                [executable, "--python", str(root / "blender_addon" / "start.py")],
                cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            while viewer.poll() is None:
                runtime.step()
                server.pump(0.05)
        return viewer.returncode
    finally:
        runtime.close()
