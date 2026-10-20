"""kicad-cli lookup and the persistent export cache (worker threads only)."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


CACHE_VERSION = "1"  # bump when an exporter's options or output layout change
_VARIABLE = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")  # KiCad's ${NAME} and $(NAME)
_MODEL_DIR = re.compile(r"KICAD\d+_3DMODEL_DIR")  # where KiCad looks up a relative model path
_EMBEDDED = "kicad-embed://"  # a model stored in the board file itself
# kicad-cli's messages for a model it left out: not found, or found but unreadable
_MODEL_PROBLEM = re.compile(r"File not found: (.+)|No model for filename '(.+)'")


def executable(export=None):
    """The kicad-cli of the running KiCad (sent by the bridge), else a local install."""
    candidates = [(export or {}).get("kicad_cli"), os.environ.get("KILEIDO_KICAD_CLI"),
                  shutil.which("kicad-cli")]
    candidates += [str(path) for path in _installed()]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("kicad-cli was not found; set KILEIDO_KICAD_CLI to its path")


def _version_key(path):
    return tuple(int(part) for part in re.findall(r"\d+", path.parent.parent.name))


def _installed():
    if sys.platform == "win32":
        roots = {Path(os.environ.get(name, "")) for name in ("ProgramFiles", "ProgramW6432")
                 if os.environ.get(name)} or {Path("C:/Program Files")}
        found = [path for root in roots for path in root.glob("KiCad/*/bin/kicad-cli.exe")]
        return sorted(found, key=_version_key, reverse=True)
    if sys.platform == "darwin":
        return [Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")]
    return [Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")]


def identity(cli):
    """kicad-cli as the cache keys see it: path, size and modification time, so an
    upgrade in place (a Linux package keeps /usr/bin/kicad-cli) makes new keys."""
    info = os.stat(cli)
    return f"{cli}|{info.st_size}|{info.st_mtime_ns}"


def project_dir(export, source):
    """The folder ${KIPRJMOD} means: the saved board's (the bridge's live copy sits in
    a temporary folder), else the watched board file's."""
    return (export or {}).get("project_dir") or str(Path(source).parent)


def variant_arguments(export):
    """`--variant NAME` for the assembly variant selected in KiCad (the bridge sends
    it); the default variant needs no flag, and older kicad-cli has none."""
    variant = (export or {}).get("variant", "")
    return ["--variant", variant] if variant else []


def defines(project):
    """kicad-cli resolves ${KIPRJMOD} to the folder of the file it reads, a scratch
    copy (`board_copy`), so point it back at the real project (measured: same models
    found as when exporting the saved board)."""
    return ["--define-var", f"KIPRJMOD={project}"]


def path_variables(paths, settings=""):
    """What each ${NAME} in `paths` stands for, as text for a cache key: a path changed
    in KiCad's Configure Paths must bring a new export, not the old result. kicad-cli
    takes them from its environment (KiCad hands its Configure Paths down to Blender
    that way) and from kicad_common.json in the `settings` folder (the only source for
    a Blender started by hand), so both values count. KIPRJMOD is `defines`'s."""
    names = sorted({match[1] or match[2] for path in paths for match in _VARIABLE.finditer(path)}
                   - {"KIPRJMOD"})
    saved = _saved_variables(settings) if names else {}
    return "\n".join(f"{name}={os.environ.get(name)!r}|{saved.get(name)!r}" for name in names)


def model_files(paths, project, settings=""):
    """The files kicad-cli may read for the board's model `paths`: a model edited in
    place keeps its path, so the cache key and the watcher look at these files too
    (`file_stamps`). As KiCad's 3D resolver tries them: ${NAME}s expanded (KIPRJMOD is
    `project`; the environment wins over kicad_common.json, as in KiCad), that path
    when it is absolute or starts with a variable, else under the project folder and
    under each ${KICADn_3DMODEL_DIR} set (measured, kicad-cli 10: both found; unset, it
    is KiCad's own library, which changes with an install). Embedded models
    (kicad-embed://) are in the board text; a path relative to the footprint's
    library is not followed (that needs the library tables)."""
    saved = {name: value for name, value in _saved_variables(settings).items() if isinstance(value, str)}

    def value(name):
        return project if name == "KIPRJMOD" else os.environ.get(name) or saved.get(name)

    model_dirs = [folder for folder in map(value, sorted({name for name in [*os.environ, *saved]
                                                           if _MODEL_DIR.fullmatch(name)})) if folder]
    files = {}
    for path in paths:
        path = re.sub(r"\\(.)", r"\1", path)  # the board's string escapes
        if path.startswith(_EMBEDDED):
            continue
        expanded = _VARIABLE.sub(lambda match: value(match[1] or match[2]) or match[0], path)
        if path.startswith(("${", "$(")) or Path(expanded).is_absolute():
            files[Path(expanded)] = None
        else:
            files.update(dict.fromkeys(Path(folder) / expanded for folder in [project, *model_dirs]))
    return list(files)


def file_stamps(files):
    """Each file's size and modification time, as text for a cache key; "-" for one
    that is not there (a file turning up makes a new key too)."""
    lines = []
    for path in files:
        try:
            info = os.stat(path)
        except OSError:
            lines.append(f"{path}|-")
        else:
            lines.append(f"{path}|{info.st_size}|{info.st_mtime_ns}")
    return "\n".join(lines)


def model_problems(output):
    """The model files a GLB export left out, one per message, from kicad-cli's output:
    paths it did not find, and files it found but could not read (a VRML file its
    OpenCascade reader rejects even when KiCad's 3D viewer shows it)."""
    return [(match[1] or match[2]).strip() for match in _MODEL_PROBLEM.finditer(output)]


def _saved_variables(settings):
    """Configure Paths as KiCad saved them; {} without a settings folder or file."""
    if not settings:
        return {}
    try:
        common = json.loads((Path(settings) / "kicad_common.json").read_text(encoding="utf-8"))
        variables = common["environment"]["vars"]
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    return variables if isinstance(variables, dict) else {}


def board_copy(source, data, directory):
    """`data`, the board bytes a cache key was made from, as a file for kicad-cli: the
    watched file may be rewritten while an export runs, and the cache entry must hold
    what its key says. The file stem stays (plot file names follow it) and
    `<stem>.kicad_pro` is copied beside it, for the project's text variables."""
    source = Path(source)
    folder = directory / "board"
    folder.mkdir()
    copy = folder / source.name
    copy.write_bytes(data)
    try:
        shutil.copyfile(source.with_suffix(".kicad_pro"), copy.with_suffix(".kicad_pro"))
    except OSError:
        pass  # no project file: KiCad's defaults, as for the board itself
    return copy


def run(arguments, timeout):
    """kicad-cli writes UTF-8 (paths in messages); never fail on a byte the locale lacks."""
    return subprocess.run([str(argument) for argument in arguments], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def cache_root():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "KiLeidoscope"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "KiLeidoscope"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kileidoscope"
    return base / "exports"


def cache_key(*parts):
    digest = hashlib.blake2b(CACHE_VERSION.encode(), digest_size=16)
    for part in parts:
        digest.update(b"\0")
        digest.update(part if isinstance(part, bytes) else str(part).encode("utf-8"))
    return digest.hexdigest()


def cache_entry(kind, key):
    """A finished entry has `done` in it; it is written last."""
    return cache_root() / kind / key


def store(kind, key, files):
    """Copy finished outputs into the cache and return the entry folder, or None when
    the cache cannot be written (read-only, disk full): the outputs are then used
    where they are."""
    entry = cache_entry(kind, key)
    partial = entry.with_name(key + ".partial")
    try:
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True)
        for name, source in files.items():
            shutil.copyfile(source, partial / name)
        (partial / "done").write_text("", encoding="utf-8")
        shutil.rmtree(entry, ignore_errors=True)
        os.replace(partial, entry)
    except OSError:
        shutil.rmtree(partial, ignore_errors=True)
        return None
    prune(kind)
    return entry


def lookup(kind, key):
    entry = cache_entry(kind, key)
    if (entry / "done").is_file():
        try:
            os.utime(entry)  # most recently used survives pruning
        except OSError:
            pass  # a read-only cache is still a cache
        return entry
    return None


def prune(kind, keep=16):
    folder = cache_root() / kind
    try:
        entries = sorted((path for path in folder.iterdir() if path.is_dir()),
                         key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in entries[keep:]:
        shutil.rmtree(path, ignore_errors=True)
