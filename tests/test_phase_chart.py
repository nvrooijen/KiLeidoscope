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
    assert rows[0] == ["pair", "start", "end", "along_mm", "dt_ps", "dt_read_ps", "dt_lo_ps", "dt_hi_ps"]
    assert rows[1] == ["TX_P/N → TX_C_P/N", "U1.A1/A2, via C1", "J1.1/2", "0.0000", "0.0000", "0.0000", "0.0000", "0.0000"]
    # No read values or limits given: Δt itself.
    assert rows[3][3:] == ["1.0000", "-0.8000", "-0.8000", "-0.8000", "-0.8000"] and len(rows) == 4


def test_the_model_limits_are_shaded_along_the_line_named_once_and_carried_in_the_numbers():
    """A coupled turn makes +5 ps, known to be up to 9 % more: the band's shape runs
    from Δt to Δt + 0.45 ps, the y scale takes the band in, the legend names it once with
    the bridge's note, and the SVG's metadata and the CSV carry both edges."""
    chart = _chart()
    note = "known model error at tight features (one full-wave study, 2026-10-16)"
    dt = [0.0, 2.5, 5.0, 5.0, 5.0]
    turned = _pair("HDMI_D0_P/N", dt, dt_lo_ps=dt, dt_hi_ps=[0.0, 2.725, 5.45, 5.45, 5.45], band_note=note)
    plain = _pair("USB_D±", [0.0] * 5)
    text = chart.svg([turned, plain], 1.0, 5.0)
    drawn = re.sub(r"<metadata>.*?</metadata>", "", text, flags=re.S)
    assert drawn.count("<polygon") == 1 and drawn.count(note) == 1  # the band once, its name once in the legend
    legend = re.sub(r"</text>\s*<text[^>]*>", " ", drawn)  # the key's lines, wrapped to the chart's width, joined
    assert "10–20 % less" in legend and "not corrected" in legend and "±3 %" not in legend
    assert f">Shaded: {note}.</text>" in drawn  # the name whole, on a line of its own
    assert max(len(line) for line in re.findall(r"<text[^>]*>([^<]*)</text>", drawn[drawn.index("Shaded:"):])) < 90
    assert chart.y_limit([turned], 1.0) == 10.0  # 5.45 ps × 1.05 is past 5: the next step
    data = json.loads(re.search(r"<metadata><!\[CDATA\[(.*?)\]\]></metadata>", text, re.S).group(1))
    assert data["band_note"] == note
    assert data["pairs"][0]["dt_lo_ps"] == dt and data["pairs"][0]["dt_hi_ps"][2] == 5.45
    assert data["pairs"][1]["dt_lo_ps"] == data["pairs"][1]["dt_hi_ps"] == [0.0] * 5  # none sent: Δt itself
    rows = list(csv_module.reader(io.StringIO(chart.csv([turned]))))
    assert rows[3][4:] == ["5.0000", "5.0000", "5.0000", "5.4500"]
    assert "<polygon" not in chart.svg([plain], 1.0, 5.0) and note not in chart.svg([plain], 1.0, 5.0)


def test_an_empty_chart_still_draws():
    chart = _chart()
    text = chart.svg([], 1.0, 5.0)
    assert "<svg" in text and "No measured pair" in text
    assert chart.csv([]) == "pair,start,end,along_mm,dt_ps,dt_read_ps,dt_lo_ps,dt_hi_ps\n"
