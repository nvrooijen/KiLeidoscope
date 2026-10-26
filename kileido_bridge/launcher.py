"""KiCad action: start a private bridge and its Blender viewer together, or join a
KiLeidoscope Blender that is open already (multi-board: blender_addon/kileido/join.py)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .loop import BridgeRuntime
from .server import BridgeServer

MINIMUM_BLENDER = (5, 1)
_VERSION = re.compile(rb"Blender (\d+)\.(\d+)")
JOIN_PROTOCOL = 1  # blender_addon/kileido/join.py reads and writes the same files
IDENTITY_WAIT_S = 5.0  # to read which board this KiCad has open, for the join request
REPLY_WAIT_S = 10.0  # until the open Blender takes the request (it may be busy a few seconds)
ANSWER_WAIT_S = 120.0  # once taken: the user may be asked "Add here / Open in its own window"
LOG_KEEP_S = 7 * 24 * 3600  # numbered Blender logs (blender-<pid>.log) older than this are removed


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


def _viewer_file(socket: str, suffix: str) -> Path:
    return cache_root() / f"viewer-{hashlib.sha256(socket.encode()).hexdigest()[:16]}{suffix}"


def viewer_pid(socket: str) -> int | None:
    """The Blender process of the open viewer of the KiCad at `socket`, when known."""
    try:
        return int(_viewer_file(socket, ".pid").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def already_open_message(socket: str) -> str:
    pid = viewer_pid(socket)
    where = f" (Blender, process {pid})" if pid else ""
    message = (f"KiLeidoscope is already open for this KiCad{where}. Close that Blender window to open "
               "a new one. If you cannot find the window, end that process.")
    if sys.platform == "win32":
        message += (" If this is another KiCad window, close and reopen that KiCad: it shares the first "
                    "KiCad's connection, and a KiCad started now gets its own.")
    return message


def keep_kicad_connections_apart() -> None:
    """Windows: every running KiCad answers on one API pipe (KiCad issue #23994,
    https://gitlab.com/kicad/code/kicad/-/work_items/23994), so a second KiCad's plugin
    reaches the first KiCad. With an empty file of the pipe's name in the temp folder,
    each KiCad started afterwards uses a pipe of its own (api-<pid>.sock), as on Linux.
    Windows' temp cleanup may remove the file: it is made again on every run. The fix
    is the one Kikakuka documents (https://github.com/buganini/Kikakuka)."""
    if sys.platform != "win32":
        return
    marker = Path(tempfile.gettempdir()) / "kicad" / "api.sock"
    try:
        marker.parent.mkdir(exist_ok=True)
        if not marker.exists():
            marker.touch()
    except OSError:
        pass


# --- Joining an open KiLeidoscope Blender ----------------------------------------------------

def viewer_folder() -> Path:
    """Where open KiLeidoscope Blenders list themselves and join requests wait (per user)."""
    folder = cache_root() / "viewers"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def open_viewers(folder: Path) -> list[int]:
    """Process ids of open KiLeidoscope Blenders that take join requests, newest first;
    the files of closed ones are removed."""
    viewers = []
    for path in sorted(folder.glob("viewer-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            if data.get("protocol") != JOIN_PROTOCOL:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if process_alive(pid):
            viewers.append(pid)
        else:
            path.unlink(missing_ok=True)
    return viewers


def _write(path: Path, data: dict) -> None:
    """Whole file or nothing: the other side never reads half a file."""
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(temporary, path)


def ask_to_join(folder: Path, pid: int, offer: dict, wait, clock=time.monotonic) -> dict | None:
    """Ask the open Blender `pid` to show this bridge's board: its answer ({"accepted",
    "slot", "reason"}), or None when it gave none. Until the Blender takes the request it
    can be withdrawn; once taken, the user may be asked first. `wait(seconds)` keeps the
    bridge serving meanwhile."""
    nonce = secrets.token_hex(8)
    request = folder / f"join-{pid}-{nonce}.json"
    reply = folder / f"reply-{nonce}.json"
    _write(request, {**offer, "protocol": JOIN_PROTOCOL})
    deadline = clock() + REPLY_WAIT_S
    taken = False
    while True:
        if reply.exists():
            try:
                return json.loads(reply.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            finally:
                reply.unlink(missing_ok=True)
        if not taken and not request.exists():
            taken, deadline = True, clock() + ANSWER_WAIT_S
        if clock() >= deadline:
            if taken:
                return None
            try:
                request.unlink()  # not taken yet: withdrawn
                return None
            except FileNotFoundError:
                taken, deadline = True, clock() + ANSWER_WAIT_S  # taken just now: its answer follows
        wait(0.05)


def board_identity(runtime, wait, clock=time.monotonic) -> dict:
    """Which board this KiCad has open (empty names when it cannot be read in time)."""
    deadline = clock() + IDENTITY_WAIT_S
    while getattr(runtime, "snapshot", None) is None and clock() < deadline:
        wait(0.05)
    snapshot = getattr(runtime, "snapshot", None)
    if snapshot is None:
        return {"board_name": "", "board_path": ""}
    return {"board_name": snapshot.board_name, "board_path": getattr(runtime, "board_path", "")}


class Abandoned:
    """True once the viewer has been away from this bridge for ABANDON_S.

    A viewer reconnects every 2 s while it wants the board (live.RETRY_S). It stays away
    when the board's slot took a newer bridge (Open in Blender clicked again, e.g. after
    KiCad crashed and came back) or was closed: then this bridge only loads KiCad, so it
    stops. Before the first connection the viewer gets START_S (Blender starting, a user
    asked whether to add the board)."""

    START_S = 120.0
    ABANDON_S = 30.0

    def __init__(self, server, clock=None):
        self.server, self.clock = server, clock or time.monotonic
        self.started = self.clock()
        self.seen = None  # last time a viewer was connected

    def __call__(self) -> bool:
        now = self.clock()
        if getattr(self.server, "authenticated", True):
            self.seen = now
            return False
        if self.seen is None:
            return now - self.started > self.START_S
        return now - self.seen > self.ABANDON_S


def _join(viewers: list[int], runtime, server, wait) -> int | None:
    """The process id of an open Blender that took this board, or None (no answer, the
    user chose its own window, or that Blender shows four boards already)."""
    folder = viewer_folder()
    offer = {"port": server.port, "token": server.token, **board_identity(runtime, wait)}
    for pid in viewers:
        answer = ask_to_join(folder, pid, offer, wait)
        if answer and answer.get("accepted"):
            return pid
        if answer and answer.get("reason") == "full":
            _tell_user("The open KiLeidoscope window shows four boards already: this board opens "
                       "in a window of its own.")
            return None
        if answer:  # "Open in its own window"
            return None
    return None


def log_path(shared: bool) -> Path:
    """Blender's output: blender.log, or blender-<pid>.log while another KiLeidoscope
    Blender is open (two writers garble one file). Numbered logs older than a week go."""
    root = cache_root()
    if not shared:
        return root / "blender.log"
    now = time.time()
    for old in root.glob("blender-*.log"):
        try:
            if now - old.stat().st_mtime > LOG_KEEP_S:
                old.unlink()
        except OSError:
            pass
    return root / f"blender-{os.getpid()}.log"


def viewer_lock(socket: str):
    """The open lock file that makes this the one viewer of the KiCad at `socket`, or None
    when another viewer holds it. The OS releases it when this process ends, even on a crash."""
    path = _viewer_file(socket, ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _stop_on_signals():
    """KiCad or the session ending sends SIGTERM/SIGHUP: leave through `finally`, so the
    bridge's temporary board copy is removed."""
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda number, frame: sys.exit(0))


def launch(root=None) -> int:
    """Run from kicad_plugin/launch.py: serve the board until its Blender is done with it.
    With a KiLeidoscope Blender open already, that Blender is asked to show the board
    (it may ask the user); else, or when it declines, a Blender of its own starts. Its
    output goes to `<cache>/blender.log` (`log_path`)."""
    try:
        socket = os.environ.get("KICAD_API_SOCKET")
        if not socket:
            raise RuntimeError("Start Open in Blender from the KiCad PCB Editor to select the correct board.")
        keep_kicad_connections_apart()
        lock = viewer_lock(socket)
        if lock is None:  # a repeated click must not start another Blender
            _tell_user(already_open_message(socket))
            return 0
    except RuntimeError as exc:
        _tell_user(str(exc))
        raise
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    _stop_on_signals()
    server = BridgeServer(port=0)
    runtime = BridgeRuntime(server)
    pid_path = _viewer_file(socket, ".pid")
    abandoned = Abandoned(server)

    def serve(seconds):
        runtime.step()
        runtime.wait(seconds)

    try:
        try:
            viewers = open_viewers(viewer_folder())
        except OSError:
            viewers = []
        if viewers:
            runtime.start()  # reads the board's name for the request
            joined = _join(viewers, runtime, server, serve)
            if joined is not None:
                pid_path.write_text(str(joined), encoding="ascii")  # named if a later click finds it open
                while process_alive(joined) and not abandoned():
                    serve(0.05)
                return 0
        try:
            executable = find_blender()
        except RuntimeError as exc:
            _tell_user(str(exc))
            raise
        environment = os.environ.copy()
        environment["KILEIDO_BRIDGE_PORT"] = str(server.port)
        environment["KILEIDO_BRIDGE_TOKEN"] = server.token
        output = log_path(shared=bool(viewers))
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "wb") as log:
            viewer = subprocess.Popen(
                [executable, "--python", str(root / "blender_addon" / "start.py")],
                cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            pid_path.write_text(str(viewer.pid), encoding="ascii")  # named if a later click finds it open
            if not viewers:
                runtime.start()
            while viewer.poll() is None:
                if abandoned():
                    return 0  # its board's slot took a newer bridge; that Blender runs on
                serve(0.05)
        return viewer.returncode
    finally:
        runtime.close()
        pid_path.unlink(missing_ok=True)
        lock.close()
