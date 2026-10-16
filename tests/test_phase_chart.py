"""The Δt chart export (blender_addon/kileido/phase_chart.py): drawn headless, no Blender."""

import csv as csv_module
import importlib.util
import io
import json
import re
from pathlib import Path


def _chart():
    path = Path(__file__).resolve().parents[1] / "blender_addon" / "kileido" / "phase_chart.py"
    spec = importlib.util.spec_from_file_location("kileido_phase_chart_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pair(name, dt, **extra):
    along = [0.5 * index for index in range(len(dt))]
    return {"name": name, "start": "U1.1/2", "end": "J1.1/2", "along_mm": along, "dt_ps": dt,
            "excursions": extra.pop("excursions", []), "skew_ps": extra.pop("skew_ps", dt[-1]),
            "max_ps": max(abs(value) for value in dt), "delay_p_ps": 300.0, "delay_n_ps": 300.0 - dt[-1],
            "length_p_mm": along[-1], "length_n_mm": along[-1], "sources": ["tuning profile 'Fast'"], **extra}


def test_the_chart_draws_every_pair_on_one_scale_with_its_excursions_and_carries_its_numbers():
    chart = _chart()
    flat = _pair("USB_D±", [0.0] * 21)
    bumped = _pair("HDMI_D0_P/N", [0.0, 0.5] + [2.8] * 16 + [1.5, 0.3, 0.0],
                   excursions=[{"at_mm": 0.5, "length_mm": 8.5, "peak_ps": 2.8}], skew_ps=0.0, flipped=True)
    text = chart.svg([flat, bumped], 1.0, 5.0, "demo.kicad_pcb")
    assert text.startswith("<svg") and text.rstrip().endswith("</svg>")
    assert text.count("<polyline") == 4  # per pair: the Δt line and, dashed, Δt as read on the traces
    assert text.count('fill="#f5a623"') == 1  # the excursion, shaded once
    assert "+2.8 ps" in text and "skew +0.0 ps" in text and "(flipped)" in text
    assert "HDMI_D0_P/N" in text and "demo.kicad_pcb" in text
    assert chart.y_limit([flat, bumped], 1.0) == 5.0  # 2.8 ps, rounded up to the next 1-2-5 step
    assert chart.y_limit([flat], 1.0) == 2.0  # never narrower than twice the tolerance, or ±2 ps
    data = json.loads(re.search(r"<metadata><!\[CDATA\[(.*?)\]\]></metadata>", text, re.S).group(1))
    assert data["format"] == "kls-dynamic-phase-chart 1" and [p["name"] for p in data["pairs"]] == ["USB_D±", "HDMI_D0_P/N"]
    assert data["pairs"][1]["dt_ps"][2] == 2.8 and data["pairs"][1]["excursions"][0]["length_mm"] == 8.5
    assert data["tolerance_ps"] == 1.0 and data["min_length_mm"] == 5.0


def test_the_csv_has_one_row_per_sample_and_quotes_names_with_commas():
    chart = _chart()
    pair = _pair("TX_P/N → TX_C_P/N", [0.0, -0.4, -0.8])
    pair["start"] = "U1.A1/A2, via C1"
    rows = list(csv_module.reader(io.StringIO(chart.csv([pair]))))
    assert rows[0] == ["pair", "start", "end", "along_mm", "dt_ps", "dt_read_ps"]
    assert rows[1] == ["TX_P/N → TX_C_P/N", "U1.A1/A2, via C1", "J1.1/2", "0.0000", "0.0000", "0.0000"]
    assert rows[3][3:] == ["1.0000", "-0.8000", "-0.8000"] and len(rows) == 4  # no read values given: Δt itself


def test_an_empty_chart_still_draws():
    chart = _chart()
    text = chart.svg([], 1.0, 5.0)
    assert "<svg" in text and "No measured pair" in text and chart.csv([]) == "pair,start,end,along_mm,dt_ps,dt_read_ps\n"
