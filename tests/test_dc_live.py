"""DC analysis in the live loop: the worker process, superseded solves, the setup file,
the viewer's edits and the frames they produce."""

import json
import time
from dataclasses import replace

import pytest

import dc_board
from dc_board import MM
from kileido_bridge import dc, model, protocol
from kileido_bridge.dcworker import SolverProcess, solve


def frames_of(payloads, kind=None):
    decoded = [frame for payload in payloads for frame in protocol.FrameDecoder().feed(payload)]
    return [frame for frame in decoded if kind is None or frame[0]["type"] == kind]


def wait_for(solver, until, timeout=60.0):
    messages = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages += solver.poll()
        if until(messages):
            return messages
        time.sleep(0.01)
    raise AssertionError(f"no reply in {timeout} s: {messages}")


def strip_setup(**settings):
    return dc.DcSetup(dc_board.NET, [dc.DcTerminal("S1", "supply", ["J1.1"], 3.3),
                                     dc.DcTerminal("L1", "load", ["J2.1"], 2.0)], **settings)


# --- The worker process ----------------------------------------------------------------

def test_worker_process_solves_and_reports_progress(tmp_path):
    solver = SolverProcess(log_path=tmp_path / "worker.log")
    try:
        built = dc.build_problem(dc_board.strip(), strip_setup())
        solver.submit(1, built, cell_um=250)
        messages = wait_for(solver, lambda found: any(kind == "result" for kind, *_ in found))
        kinds = [kind for kind, *_ in messages]
        assert "ready" in kinds and "progress" in kinds
        (result,) = [payload for kind, job, payload in messages if kind == "result" and job == 1]
        inline = solve(built, cell_um=250)
        assert result["pairs"][0]["r_ohm"] == pytest.approx(inline["pairs"][0]["r_ohm"], rel=1e-12)
        assert result["fields"]["j"].shape == inline["fields"]["j"].shape
        assert solver.job is None  # idle again
        # The solver's own notes arrive as log lines, never inside the replies stream.
        assert any(kind == "log" and "adaptive grid" in payload for kind, _, payload in messages)
    finally:
        solver.close()


def test_superseded_job_is_dropped_and_the_process_replaced(tmp_path):
    solver = SolverProcess(log_path=tmp_path / "worker.log")
    try:
        board = dc_board.strip()
        solver.submit(1, dc.build_problem(board, strip_setup()), cell_um=25)
        first = solver.process.pid
        solver.cancel()  # the copper changed: job 1 must never report
        assert solver.process.pid != first
        solver.submit(2, dc.build_problem(board, strip_setup()), cell_um=500)
        messages = wait_for(solver, lambda found: any(kind == "result" for kind, *_ in found))
        assert {job for kind, job, _ in messages if kind in ("result", "progress", "error")} == {2}
    finally:
        solver.close()


def test_worker_reports_setup_errors(tmp_path):
    solver = SolverProcess(log_path=tmp_path / "worker.log")
    try:
        built = dc.build_problem(dc_board.strip(), strip_setup())
        built.problem.terminals[1].electrodes = built.problem.terminals[0].electrodes  # overlapping contacts
        solver.submit(5, built, cell_um=500)
        messages = wait_for(solver, lambda found: any(kind == "error" for kind, *_ in found))
        (message,) = [payload for kind, job, payload in messages if kind == "error" and job == 5]
        assert "overlap" in message
    finally:
        solver.close()


# --- The setup file --------------------------------------------------------------------

def test_setup_round_trips_in_fill_resistance_format(tmp_path):
    board_path = str(tmp_path / "power.kicad_pcb")
    setup = strip_setup(plating_um=25.0, vias_capped=True)
    setup.terminals.append(dc.DcTerminal("L2", "load", [{"via_mm": [5.5, 0.5]}, "U3"], 0.25, bonded=True))
    setup.terminals.append(dc.DcTerminal("S2", "supply", [], 5.0))
    path = dc.save_setup(setup, board_path)
    assert path.endswith("power.kileidoscope-dc.json")
    data = json.loads((tmp_path / "power.kileidoscope-dc.json").read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["mode"] == "pdn" and data["run"]["net"] == dc_board.NET
    supply = data["terminals"][0]
    assert supply == {"name": "S1", "role": "supply", "parts": ["J1.1"], "active": True, "r_out_ohm": 0.0,
                      "v_oc": 3.3}
    assert data["terminals"][2]["bonded"] and data["terminals"][3]["active"] is False  # no parts yet
    loaded, where = dc.load_setup(board_path)
    assert where == path and loaded.key() == setup.key()


def test_their_config_is_read_when_ours_is_missing(tmp_path):
    text = """// Fill Resistance config, hand written
{
  "version": 1, "mode": "pdn",
  "run": {"net": "+5V", "cell_um": "150"},
  "physics": {"via_plating_um": 20},
  "terminals": [
    {"name": "VIN", "role": "supply", "parts": ["J1.1", "rect:TP"], "r_out_ohm": "10m", "v_oc": 5},
    {"name": "MCU", "role": "load", "parts": ["U2"], "i_draw_a": "350m", "bonded": true}
  ]
}"""
    (tmp_path / "power.fill_res_config.json").write_text(text, encoding="utf-8")
    setup, where = dc.load_setup(str(tmp_path / "power.kicad_pcb"))
    assert where.endswith("power.fill_res_config.json")
    assert setup.net == "+5V" and setup.cell_um == 150 and setup.plating_um == 20
    assert [(t.name, t.role, t.parts, t.value, t.bonded) for t in setup.terminals] == [
        ("VIN", "supply", ["J1.1"], 5.0, False), ("MCU", "load", ["U2"], pytest.approx(0.35), True)]
    ours = json.dumps(dc.config_from_setup(dc.DcSetup("GND")))
    (tmp_path / "power.kileidoscope-dc.json").write_text(ours, encoding="utf-8")
    assert dc.load_setup(str(tmp_path / "power.kicad_pcb"))[0].net == "GND"  # ours wins


# --- The live analysis -----------------------------------------------------------------

class InlineSolver:
    """SolverProcess's interface, solving on poll in this process."""

    def __init__(self):
        self.pending, self.cancelled, self.submitted = [], [], []

    def submit(self, job, built, **options):
        self.pending.append((job, built, options))
        self.submitted.append(job)

    def poll(self):
        pending, self.pending = self.pending, []
        return [("result", job, solve(built, **options)) for job, built, options in pending]

    def cancel(self):
        self.cancelled += [job for job, _, _ in self.pending]
        self.pending = []

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def analysis(tmp_path):
    clock, solver = Clock(), InlineSolver()
    session = dc.DcAnalysis(clock=clock, solver_factory=lambda: solver, settle_s=1.5)
    session.target("power.kicad_pcb", str(tmp_path / "power.kicad_pcb"))
    return session, clock, solver


def test_clicks_build_the_setup_and_save_it(tmp_path):
    session, _, _ = analysis(tmp_path)
    board = dc_board.strip()
    frames = session.handle({"op": "mark", "item": "pad-j1"}, board)  # no terminal yet: a supply
    ((header, arrays),) = frames_of(frames, "dc_setup")
    assert header["net"] == dc_board.NET and header["terminals"][0]["role"] == "supply"
    assert header["terminals"][0]["value"] == 3.3  # from the net name
    assert header["terminals"][0]["parts"] == ["J1.1"] and header["message"] == "J1.1 added to S1"
    assert arrays["marker"].tolist() == [[MM, 5 * MM, 10 * MM]] and arrays["marker_side"].tolist() == [1]
    session.handle({"op": "add", "role": "load"}, board)
    session.handle({"op": "mark", "item": "pad-j2"}, board)
    session.handle({"op": "edit", "index": 1, "value": 2.5, "name": "CPU"}, board)
    saved, _ = dc.load_setup(str(tmp_path / "power.kicad_pcb"))
    assert [(t.name, t.parts, t.value) for t in saved.terminals] == [("S1", ["J1.1"], 3.3), ("CPU", ["J2.1"], 2.5)]
    # A second click on a marked pad takes it off again; one on another net is refused.
    session.handle({"op": "mark", "item": "pad-j2"}, board)
    assert session.setup.terminals[1].parts == []
    gnd = dc_board.smd_pad("pad-gnd", "fp-j2", "2", "F.Cu", 0, 0, MM, MM, net="GND")
    other = replace(board, pads=(*board.pads, gnd))
    ((header, _),) = frames_of(session.handle({"op": "mark", "item": "pad-gnd"}, other), "dc_setup")
    assert header["message"] == "J2.2 is on GND, not +3V3"
    ((header, _),) = frames_of(session.handle({"op": "edit", "index": 0, "value": -1}, board), "dc_setup")
    assert "above 0" in header["message"] and session.setup.terminals[0].value == 3.3


def test_solves_after_edits_settle_and_supersedes_on_change(tmp_path):
    session, clock, solver = analysis(tmp_path)
    board = dc_board.strip()
    session.setup = strip_setup(cell_um=500)
    frames = session.step(board)
    assert frames_of(frames, "dc_status")[0][0]["state"] == "waiting" and not solver.submitted
    clock.now += 1.0
    assert not solver.submitted and not frames_of(session.step(board), "dc_status")
    clock.now += 0.6
    session.step(board)
    assert solver.submitted == [1] and session.status["state"] == "solving"
    # The pour moves before the solve reports: job 1 is cancelled, job 2 waits for the edits to settle.
    moved = replace(board, zones=(replace(board.zones[0], polygons=((dc_board.rect(0, 0, 50 * MM, 9 * MM),),)),))
    session.step(moved)
    assert solver.cancelled == [1] and session.running is None
    clock.now += 1.6
    frames = session.step(moved)
    assert solver.submitted == [1, 2]
    frames += session.step(moved)
    ((header, arrays),) = frames_of(frames, "dc_result")
    assert header["net"] == dc_board.NET and arrays["j"].dtype.str == "<f4"
    assert header["loads"][0]["i_a"] == 2.0 and header["timings_s"]["total"] >= 0
    assert frames_of(frames, "dc_status")[-1][0]["state"] == "done"
    # Nothing changed: no new solve. Another net's copper changing does not count either.
    clock.now += 5
    gnd = model.Track("t-gnd", "B.Cu", "GND", (0, 0), (MM, 0), 200_000)
    session.step(replace(moved, tracks=(gnd,)))
    clock.now += 5
    session.step(replace(moved, tracks=(gnd,)))
    assert solver.submitted == [1, 2]
    # "Solve now" skips the wait.
    session.handle({"op": "solve"}, moved)
    assert solver.submitted == [1, 2, 3]


def test_an_incomplete_setup_clears_the_result(tmp_path):
    session, clock, solver = analysis(tmp_path)
    board = dc_board.strip()
    session.setup = strip_setup(cell_um=500)
    session.step(board)
    clock.now += 2
    session.step(board)
    session.step(board)
    assert frames_of([session.result], "dc_result")[0][0]["net"] == dc_board.NET
    frames = session.handle({"op": "remove", "index": 1}, board)
    ((header, _),) = frames_of(frames, "dc_result")
    assert header["net"] == ""  # cleared
    status = frames_of(frames, "dc_status")[-1][0]
    assert status["state"] == "idle" and status["message"] == "Mark a load: a pad or via that draws current"


def test_net_from_a_click_or_kicads_selection(tmp_path):
    session, _, _ = analysis(tmp_path)
    board = dc_board.via_chain()
    session.handle({"op": "net", "item": "zone-back"}, board)
    assert session.setup.net == dc_board.NET
    session.handle({"op": "net", "from": "selection"}, board, frozenset({"GND", "+5V"}))
    assert session.setup.net == "+5V" and "2 nets selected" in session.message
    ((header, _),) = frames_of(session.handle({"op": "net", "from": "selection"}, board, frozenset()), "dc_setup")
    assert header["message"] == "Nothing with a net is selected in KiCad"


def test_a_setup_error_is_shown_not_retried(tmp_path):
    session, clock, solver = analysis(tmp_path)
    board = dc_board.strip()
    session.setup = strip_setup()
    session.setup.terminals[1].parts = ["J7.1"]
    session.step(board)
    clock.now += 2
    session.step(board)
    assert session.status["state"] == "error" and "J7.1 is not on" in session.status["message"]
    clock.now += 2
    session.step(board)
    assert not solver.submitted


def test_resync_sends_setup_status_and_result(tmp_path):
    session, clock, _ = analysis(tmp_path)
    board = dc_board.strip()
    session.setup = strip_setup(cell_um=500)
    session.step(board)
    clock.now += 2
    session.step(board)
    session.step(board)
    kinds = [header["type"] for header, _ in frames_of(session.resync_frames(board))]
    assert kinds == ["dc_setup", "dc_status", "dc_result"]


# --- Through the bridge ---------------------------------------------------------------

def test_viewer_edits_reach_the_bridge_and_results_come_back(tmp_path):
    """A viewer over the real socket marks a supply and a load; the bridge's worker
    process solves once the edits settle and the result frame arrives."""
    import test_live_link as live_link
    from kileido_bridge.loop import BridgeRuntime
    from kileido_bridge.server import BridgeServer

    board = dc_board.strip()
    reader = live_link.FakeReader(board)
    server = BridgeServer(port=0, token="dc-secret")
    runtime = BridgeRuntime(server, connector=lambda: reader, poll_interval_s=0.0)
    runtime.dc.settle_s = 0.2
    client = live_link.addon_client().SocketClient("127.0.0.1", server.port, "dc-secret")
    try:
        while runtime.snapshot is None:  # the first poll finds the board (and its setup: none)
            runtime.step()
        runtime.dc.target(board.board_name, str(tmp_path / "strip.kicad_pcb"))  # as if it were saved there
        client.connect()
        live_link.exchange(runtime, client, lambda frames: any(h["type"] == "snapshot_end" for h, _ in frames))
        client.request_dc("mark", item="pad-j1")
        client.request_dc("add", role="load")
        client.request_dc("mark", item="pad-j2")
        client.request_dc("settings", cell_um=500)
        frames = live_link.exchange(runtime, client, lambda found: any(
            h["type"] == "dc_result" and h.get("net") for h, _ in found), limit=20_000)
        setups = [h for h, _ in frames if h["type"] == "dc_setup"]
        assert setups[-1]["terminals"][1]["parts"] == ["J2.1"] and setups[-1]["settings"]["cell_um"] == 500
        states = [h["state"] for h, _ in frames if h["type"] == "dc_status"]
        assert "solving" in states and states[-1] == "done"
        header, arrays = next((h, a) for h, a in frames if h["type"] == "dc_result" and h.get("net"))
        assert header["loads"][0]["drop_mean_v"] > 0 and arrays["j"].shape[0] == 1
        assert (tmp_path / "strip.kileidoscope-dc.json").is_file()
    finally:
        client.close()
        runtime.close()


# --- Guessing the supply and the loads ----------------------------------------------------

def test_power_nets_by_name():
    for net in ("+3V3", "BUCK_1V2", "/MIPI SENSOR/VDD_IO_1V8_CAM", "VBUS", "/PRR & GND/AVDD", "1.8V", "VIN_12V"):
        assert dc.is_power(net), net
    for net in ("GND", "/power/AGND", "PWR_GOOD_1V2", "EN_3V3", "Net-(U6-{slash}HOLDor{slash}RESET(IO3))",
                "Net-(U2-SW)", "SPI_MISO", ""):
        assert not dc.is_power(net), net


def auto_board():
    """A buck converter: U1 (switch node SW, input VIN_5V) into L1, whose other pad
    is +3V3; on +3V3 the MCU U2, a ferrite FB1 into +3V3A, output caps C1 and C2, a
    pull-up R1, and the buck's feedback pin on U1."""
    def pads(reference, *nets):
        return [dc_board.smd_pad(f"{reference}-{n}", f"fp-{reference}", str(n + 1), "F.Cu", n * MM, 0,
                                 n * MM + 500_000, 500_000, net=net) for n, net in enumerate(nets)]
    parts = (pads("U1", "VIN_5V", "SW", "GND", "+3V3", "PWR_GOOD") + pads("L1", "SW", "+3V3") +
             pads("U2", *(["+3V3"] * 3 + ["SIG"] * 20 + ["GND"] * 5)) + pads("FB1", "+3V3", "+3V3A") +
             pads("C1", "+3V3", "GND") + pads("C2", "+3V3", "GND") + pads("R1", "+3V3", "RESET"))
    footprints = [dc_board.footprint(f"fp-{reference}", reference, 0, 0)
                  for reference in ("U1", "L1", "U2", "FB1", "C1", "C2", "R1")]
    return dc_board.snapshot(pads=parts, footprints=footprints)


def test_auto_finds_the_buck_inductor_and_the_loads():
    terminals, message = dc.auto_terminals(auto_board(), "+3V3")
    assert [(t.name, t.role, t.parts, t.value, t.bonded) for t in terminals] == [
        ("L1", "supply", ["L1.2"], 3.3, False),
        ("FB1", "load", ["FB1"], dc.AUTO_LOAD_A, False),  # feeds +3V3A
        ("U2", "load", ["U2"], dc.AUTO_LOAD_A, True)]  # three pads, one conductor
    assert "left out U1 (regulator)" in message and "Capacitors draw no DC current" in message


def test_auto_takes_the_regulator_without_an_inductor(tmp_path):
    board = auto_board()
    board = replace(board, pads=tuple(pad for pad in board.pads if not pad.id.startswith("L1")))
    session, _, _ = analysis(tmp_path)
    session.setup.net = "+3V3"
    ((header, _),) = frames_of(session.handle({"op": "auto"}, board), "dc_setup")
    roles = [(t["name"], t["role"]) for t in header["terminals"]]
    assert roles == [("U1", "supply"), ("FB1", "load"), ("U2", "load")]
    assert header["active"] == 0 and header["message"].startswith("Supply U1; 2 loads at 0.1 A each")
    session.setup.net = "SIG"
    ((header, _),) = frames_of(session.handle({"op": "auto"}, board), "dc_setup")
    assert header["message"].startswith("No inductor, regulator or connector on SIG")
