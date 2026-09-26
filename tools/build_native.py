"""Build the add-on's C scanline library for this platform, next to its source.

    python tools/build_native.py            # blender_addon/kileido/native/kls_raster-<os>-<arch>.<ext>
    python tools/build_native.py --cc gcc   # a specific compiler (default: $CC, else cc, gcc, clang)

The library has no Python dependency (it is loaded through ctypes), so one build per
OS and CPU serves every Python and Blender version. Without it the add-on draws
with numpy: same images, slower. tools/build_package.py ships whichever libraries
are in the folder.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "blender_addon" / "kileido" / "native"


def library_name() -> str:
    """The name gerber.native_library_name() loads (tests/test_gerber.py checks they agree).
    Repeated here so the build needs only the standard library, not numpy."""
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    suffix = {"windows": "dll", "macos": "dylib"}.get(system, "so")
    return f"kls_raster-{system}-{platform.machine().lower()}.{suffix}"


def compiler(requested: str | None) -> str:
    for name in (requested, os.environ.get("CC"), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return name
    sys.exit("No C compiler found (tried $CC, cc, gcc, clang); pass --cc")


def build(cc: str) -> Path:
    target = NATIVE / library_name()
    # No fused multiply-add, so the result matches numpy's arithmetic on every CPU.
    command = [cc, "-O2", "-ffp-contract=off", "-o", str(target), str(NATIVE / "kls_raster.c")]
    if sys.platform == "win32":
        command[1:1] = ["-shared", "-static-libgcc", "-s"]
    elif sys.platform == "darwin":
        command[1:1] = ["-dynamiclib", "-fvisibility=hidden"]
    else:
        command[1:1] = ["-shared", "-fPIC", "-fvisibility=hidden", "-s"]
        command.append("-lm")
    if target.exists():
        try:
            target.unlink()
        except PermissionError:  # Windows: a running Blender has it loaded, but renaming is allowed
            aside = target.with_name(target.name + ".in-use")
            aside.unlink(missing_ok=True)
            target.rename(aside)
    subprocess.run(command, check=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cc", help="C compiler to use")
    print(build(compiler(parser.parse_args().cc)))


if __name__ == "__main__":
    main()
