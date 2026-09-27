"""Command-line entry points for the bridge process."""

import argparse
import json
import sys
import time
from pathlib import Path

from .board_specs import read_appearance
from .kicad_reader import (connect_board, connect_reader, explain_connection_error, kicad_socket, read_snapshot,
                           saved_board_path)
from .launcher import cache_root, launch
from .loop import BridgeRuntime
from .model import to_jsonable
from .protocol import return_path_message, snapshot_frames
from .return_path import ReturnPathCheck, checked_nets
from .server import DEFAULT_PORT, BridgeServer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kileido_bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    dump = commands.add_parser("dump", help="Read the open KiCad board into OUTPUT.kls (Blender) or .json")
    dump.add_argument("output", type=Path)
    dump.add_argument("--timeout-ms", type=int, default=3000)
    commands.add_parser("open", help="Open the board open in KiCad in Blender, from this checkout: what "
                                     "KiCad's Open in Blender does, without installing the plugin")
    check = commands.add_parser("check", help="Print the return-path check of the open KiCad board")
    check.add_argument("--net", action="append", default=[],
                       help="also check this net and its differential-pair partner (repeatable)")
    check.add_argument("--timeout-ms", type=int, default=3000)
    solve = commands.add_parser("dc", help="Solve the DC analysis set up for the open KiCad board once "
                                           "(the setup saved beside the board) and print the result")
    solve.add_argument("--dump", type=Path, help="also write the solver's input as a Fill Resistance "
                                                 "geometry dump (python -m fill_resistance.standalone re-solves it)")
    solve.add_argument("--timeout-ms", type=int, default=3000)
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
        nets = checked_nets(snapshot)
        args.output.write_bytes(b"".join(snapshot_frames(
            snapshot, board_path=board_path, appearance=read_appearance(board_path))) +
            return_path_message(nets, ReturnPathCheck().check(snapshot, nets), revision=1))
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


def _run_check(args: argparse.Namespace) -> int:
    """The bridge's return-path check once, as text: differential pairs and `--net`s."""
    try:
        snapshot = read_snapshot(connect_board(timeout_ms=args.timeout_ms))
    except Exception as exc:
        print(f"KiCad read failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    nets = checked_nets(snapshot, set(args.net))
    started = time.perf_counter()
    issues = ReturnPathCheck().check(snapshot, nets)
    print(f"{snapshot.board_name}: {len(issues)} issues on {len(nets)} nets "
          f"({(time.perf_counter() - started) * 1000:.0f} ms): {', '.join(sorted(nets)) or 'none'}")
    for issue in issues:
        print(f"{issue.kind:<17} ({issue.at[0] / 1e6:8.3f}, {issue.at[1] / 1e6:8.3f}) mm  {issue.message}")
    return 0


def _run_dc(args: argparse.Namespace) -> int:
    """The DC analysis once, as text, from the setup file beside the open board."""
    from .dc import SetupError, build_problem, load_setup
    from .dcsolve.errors import UserFacingError
    from .dcsolve.geometry import save_problem
    from .dcworker import solve
    try:
        board = connect_board(timeout_ms=args.timeout_ms)
        snapshot, board_path = read_snapshot(board), saved_board_path(board)
    except Exception as exc:
        print(f"KiCad read failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    try:
        setup, where = load_setup(board_path)
        if setup.ready():
            print(f"No DC setup to solve ({where or 'none saved beside the board'}): {setup.ready()}",
                  file=sys.stderr)
            return 1
        built = build_problem(snapshot, setup, board_path)
        if args.dump:
            save_problem(built.problem, args.dump)
        result = solve(built, cell_um=setup.cell_um)
    except (SetupError, UserFacingError) as exc:
        print(f"DC analysis: {exc}", file=sys.stderr)
        return 1
    print(f"{snapshot.board_name}, {setup.net}: {result['cells']:,} cells of {result['cell_nm'] / 1000:.0f} um, "
          f"{result['unknowns']:,} unknowns; " + ", ".join(f"{key} {value:.2f} s"
                                                           for key, value in result["timings_s"].items()))
    for supply in result["supplies"]:
        print(f"{supply['name']:<12} supply {supply['v_oc']:.4g} V, delivers {supply['i_a']:.4g} A")
    for load in result["loads"]:
        print(f"{load['name']:<12} load {load['i_a']:.4g} A at {load['v_mean']:.5g} V (min {load['v_min']:.5g} V), "
              f"drop {load['drop_mean_v'] * 1e3:.3f} mV (max {load['drop_max_v'] * 1e3:.3f} mV)")
    for pair in result["pairs"]:
        text = "no copper path" if pair["r_ohm"] is None else f"{pair['r_ohm'] * 1e3:.4f} mOhm"
        print(f"R {pair['supply']} -> {pair['load']}: {text}")
    print(f"copper loss {result['p_copper_w'] * 1e3:.4f} mW (vias {result['p_vias_w'] * 1e3:.4f} mW); "
          f"|J| {result['j_max']:.3g} A/mm2 (99.9th percentile), peak {result['j_peak']:.3g} A/mm2")
    return 0


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
    if args.command == "check":
        return _run_check(args)
    if args.command == "dc":
        return _run_dc(args)
    if args.command == "open":
        return _run_open()
    return _run_bridge(args, parser)  # the only other subcommand; argparse rejects unknown ones
