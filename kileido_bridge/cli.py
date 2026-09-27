"""Command-line entry points for the bridge process."""

import argparse
import json
import sys
from pathlib import Path

from .board_specs import read_appearance
from .kicad_reader import (connect_board, connect_reader, explain_connection_error, kicad_socket, read_snapshot,
                           saved_board_path)
from .launcher import cache_root, launch
from .loop import BridgeRuntime
from .model import to_jsonable
from .protocol import snapshot_frames
from .server import DEFAULT_PORT, BridgeServer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kileido_bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    dump = commands.add_parser("dump", help="Read the open KiCad board into OUTPUT.kls (Blender) or .json")
    dump.add_argument("output", type=Path)
    dump.add_argument("--timeout-ms", type=int, default=3000)
    commands.add_parser("open", help="Open the board open in KiCad in Blender, from this checkout: what "
                                     "KiCad's Open in Blender does, without installing the plugin")
    bridge = commands.add_parser("bridge", help="Serve the open KiCad board live to Blender")
    bridge.add_argument("--port", type=int, default=DEFAULT_PORT)
    bridge.add_argument("--connections", type=int, default=4)
    bridge.add_argument("--poll-ms", type=int, default=200)
    return parser


def _run_dump(args: argparse.Namespace) -> int:
    try:
        board = connect_board(timeout_ms=args.timeout_ms)
        snapshot = read_snapshot(board)
    except Exception as exc:
        print(f"KiCad read failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.output.suffix == ".kls":  # binary frames: what Blender's "Load dump" reads
        board_path = saved_board_path(board)
        args.output.write_bytes(b"".join(snapshot_frames(
            snapshot, board_path=board_path, appearance=read_appearance(board_path))))
    else:  # JSON: for people and debugging
        args.output.write_text(json.dumps(to_jsonable(snapshot), indent=2), encoding="utf-8")
    print(json.dumps({"board_name": snapshot.board_name,
                      "counts": {"tracks": len(snapshot.tracks), "arcs": len(snapshot.arcs),
                                 "vias": len(snapshot.vias), "pads": len(snapshot.pads),
                                 "footprints": len(snapshot.footprints), "zone_fills": len(snapshot.zones)},
                      "read_timings_ms": snapshot.read_timings_ms,
                      "warnings": snapshot.warnings,
                      "output": str(args.output)}))
    return 0


def _run_open() -> int:
    """KiCad's Open in Blender, run from a terminal: the bridge and the add-on come from
    this checkout, so an edit needs only a new Blender window, not a new package."""
    try:
        connect_board()
    except Exception as exc:
        print(f"KiCad not reachable: {explain_connection_error(exc)}", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[1]
    print(f"Blender follows the open board until its window closes. Code from {root}; "
          f"Blender's output in {cache_root() / 'blender.log'}", flush=True)
    return launch(root, socket=kicad_socket())


def _run_bridge(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.poll_ms < 20 or args.connections < 1:
        parser.error("--poll-ms must be at least 20 and --connections at least 1")
    server = BridgeServer(port=args.port)
    runtime = BridgeRuntime(server,
                            connector=lambda: connect_reader(connections=args.connections),
                            poll_interval_s=args.poll_ms / 1000)
    print(json.dumps({"host": "127.0.0.1", "port": server.port, "token": server.token}), flush=True)
    try:
        runtime.run_forever()
    except KeyboardInterrupt:
        pass  # Ctrl+C is the normal way to stop the bridge
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "dump":
        return _run_dump(args)
    if args.command == "open":
        return _run_open()
    return _run_bridge(args, parser)  # the only other subcommand; argparse rejects unknown ones
