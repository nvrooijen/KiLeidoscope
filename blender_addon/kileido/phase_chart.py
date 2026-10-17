"""The Δt chart of dynamic phase: one small plot per differential pair, as SVG, and its numbers as CSV.

x: mm along the pair from the start end; y: Δt = t_P − t_N in ps (Δt < 0: P's edge is
ahead there, Δt > 0: N's), and dashed beside it Δt as a probe on each trace would read it:
the same where skew is made, fading as its common mode falls behind the edge (the bridge's
modes.apparent_dt, for the Edge rise time setting). Shaded along the Δt line, where the pair
has them, the known model error at tight features: a coupled bend makes up to 9 % more skew
than its length difference and a tight meander of a lone side 10–20 % less (the bridge's
phase._band, from one full-wave study of 2026-10-16; Δt itself is not corrected). A band at ± the marker tolerance, every excursion (|Δt| above it
for longer than the marker length) shaded, and over each plot the pair's ends, its
end-to-end skew and its largest |Δt|. All plots share one y scale, so the pairs compare at a
glance. The SVG carries its numbers too, in a <metadata> block (JSON), and `csv` writes
them as rows (pair, mm, ps) for anything that reads tables.

Pure Python: no Blender, no numpy, so a test draws it headless. The pairs come from
phase.measured_pairs(): {"name", "start", "end", "along_mm", "dt_ps", "excursions"
[{"at_mm", "length_mm", "peak_ps"}], "skew_ps", "max_ps", "delay_p_ps", "delay_n_ps",
"length_p_mm", "length_n_mm", "sources", "flipped"}, and when the bridge sent them
"dt_lo_ps", "dt_hi_ps" (the model limits, below and above dt_ps) and "band_note" (what
they are, for the legend and the metadata).
"""

import html
import json
import math
import textwrap

WIDTH, HEIGHT = 560, 210  # one plot
LEFT, RIGHT, TOP, BOTTOM = 52, 14, 34, 34  # margins inside a plot: axis numbers, nothing, title row, x axis
HEADER = 44  # the chart's title
FOOTER = 54  # the key; the model limits' lines, when there are any, add to it
CHAR_PX = 6.4  # about the width of a character of the key's text, to wrap it to the chart
FONT = "Helvetica, Arial, sans-serif"
LINE, BAND, EXCURSION, GRID, AXIS, TEXT, DIM = "#2a78d6", "#dcefdc", "#f5a623", "#e4e4e4", "#999999", "#222222", "#666666"
READ = "#0b6b45"  # Δt as read on the traces
LIMITS = "#2a78d6"  # the model limits, shaded along the Δt line
MIN_SCALE_PS = 2.0  # the y axis spans at least ± this


def _limits(pair) -> tuple[list, list] | None:
    """The model limits as drawn: (below, above), one value per sample, or None."""
    low, high = pair.get("dt_lo_ps"), pair.get("dt_hi_ps")
    if not low or not high or not (len(low) == len(high) == len(pair["dt_ps"])):
        return None
    return [float(v) for v in low], [float(v) for v in high]


def columns_for(count: int) -> int:
    return 1 if count <= 2 else 2 if count <= 6 else 3


def y_limit(pairs, tolerance_ps: float) -> float:
    """± the y range: a round number above the largest |Δt| (its model limits included)
    and twice the tolerance."""
    largest = max([abs(float(value)) for pair in pairs for value in pair["dt_ps"]] + [0.0])
    for pair in pairs:
        limits = _limits(pair)
        if limits:
            largest = max([largest] + [abs(value) for edge in limits for value in edge])
    return _round_up(max(largest * 1.05, 2 * tolerance_ps, MIN_SCALE_PS))


def _round_up(value: float) -> float:
    """The next 1, 2 or 5 times a power of ten at or above `value`."""
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if step * power >= value - 1e-9:
            return step * power
    return 10 * power


def _ticks(limit: float) -> list:
    step = _round_up(limit / 2.5)
    ticks, value = [], 0.0
    while value <= limit + 1e-9:
        ticks += [value] if value == 0 else [-value, value]
        value += step
    return sorted(ticks)


def _x_ticks(length: float) -> list:
    step = _round_up(max(length, 1e-9) / 6)
    return [value * step for value in range(int(length / step) + 1)]


def _number(value: float) -> str:
    text = f"{value:g}"
    return text


def svg(pairs, tolerance_ps: float, min_length_mm: float, title: str = "") -> str:
    """The chart. `title`: the board's name, or ""."""
    pairs = [pair for pair in pairs if len(pair["along_mm"]) >= 2]
    columns = columns_for(len(pairs))
    rows = max(1, (len(pairs) + columns - 1) // columns)
    width = WIDTH * columns
    note = _band_note(pairs)  # the band's name on a line of its own, then what it means (the bridge's
    limits = [f"Shaded: {note}."] + textwrap.wrap(  # phase.BEND_EXCESS and TIGHT_MEANDER, in words)
        "A coupled bend makes up to 9 % more skew than its length difference, a tight meander of a side running "
        "alone (a corner radius or leg spacing under 3 track widths) 10–20 % less: the shading applies those to "
        "the Δt each feature made. Δt itself is not corrected.",
        max(40, int((width - 28) / CHAR_PX))) if note else []
    height = HEADER + HEIGHT * rows + FOOTER + 16 * len(limits)
    limit = y_limit(pairs, tolerance_ps)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'font-family="{FONT}" font-size="12" fill="{TEXT}">',
           "<title>Dynamic phase: Δt along each differential pair</title>",
           '<metadata><![CDATA[' + json.dumps(_data(pairs, tolerance_ps, min_length_mm, title), ensure_ascii=False)
           + ']]></metadata>',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<text x="14" y="24" font-size="16" font-weight="bold">Dynamic phase: Δt = t_P − t_N along each pair'
           f'{", " + html.escape(title) if title else ""}</text>',
           f'<text x="14" y="38" fill="{DIM}">Walked from the start end; Δt &lt; 0: P ahead, Δt &gt; 0: N ahead. '
           f'All plots share one scale (±{_number(limit)} ps).</text>']
    if not pairs:
        out.append(f'<text x="14" y="{HEADER + 40}" fill="{DIM}">No measured pair.</text>')
    for index, pair in enumerate(pairs):
        out += _plot(pair, (index % columns) * WIDTH, HEADER + (index // columns) * HEIGHT, limit, tolerance_ps)
    y = HEADER + HEIGHT * rows + 22
    out.append(f'<text x="14" y="{y}" fill="{DIM}">Green band: ±{_number(tolerance_ps)} ps, the marker tolerance. '
               f'Orange: an excursion, |Δt| above it for longer than {_number(min_length_mm)} mm; the number is its '
               f'peak Δt.</text>')
    out.append(f'<text x="14" y="{y + 16}" fill="{DIM}">Dashed: Δt as a probe on each trace would read it, fading where '
               f'its common mode has fallen behind the edge. Skew: the end-to-end delay difference. The numbers are in '
               f'this file\'s &lt;metadata&gt; and in the .csv beside it.</text>')
    for index, line in enumerate(limits):
        out.append(f'<text x="14" y="{y + 32 + 16 * index}" fill="{DIM}">{html.escape(line)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _band_note(pairs) -> str:
    """What the shading along the Δt line means, from the first pair that has it."""
    return next((str(pair["band_note"]) for pair in pairs if pair.get("band_note") and _limits(pair)), "")


def _plot(pair, ox: float, oy: float, limit: float, tolerance_ps: float) -> list:
    along, dt = [float(v) for v in pair["along_mm"]], [float(v) for v in pair["dt_ps"]]
    length = max(along[-1], 1e-9)
    x0, x1, y0, y1 = ox + LEFT, ox + WIDTH - RIGHT, oy + TOP, oy + HEIGHT - BOTTOM

    def x(value):
        return x0 + value / length * (x1 - x0)

    def y(value):
        return y1 - (max(-limit, min(limit, value)) + limit) / (2 * limit) * (y1 - y0)

    head = f"{pair['name']}  {pair['start']} → {pair['end']}" + ("  (flipped)" if pair.get("flipped") else "")
    out = [f'<text x="{x0}" y="{oy + 16}" font-weight="bold">{html.escape(head)}</text>',
           f'<text x="{x1}" y="{oy + 16}" text-anchor="end" fill="{DIM}">skew {pair["skew_ps"]:+.1f} ps · '
           f'max |Δt| {pair["max_ps"]:.1f} ps</text>',
           f'<rect x="{x0}" y="{y(tolerance_ps):.1f}" width="{x1 - x0}" height="{y(-tolerance_ps) - y(tolerance_ps):.1f}" '
           f'fill="{BAND}"/>']
    for excursion in pair.get("excursions", ()):
        start, stop = x(float(excursion["at_mm"])), x(min(length, float(excursion["at_mm"]) + float(excursion["length_mm"])))
        out.append(f'<rect x="{start:.1f}" y="{y0}" width="{max(stop - start, 1.0):.1f}" height="{y1 - y0}" '
                   f'fill="{EXCURSION}" fill-opacity="0.28"/>')
        peak = float(excursion["peak_ps"])
        out.append(f'<text x="{(start + stop) / 2:.1f}" y="{y(peak) + (14 if peak < 0 else -5):.1f}" '
                   f'text-anchor="middle" fill="#b36b00" font-size="11">{peak:+.1f} ps</text>')
    for tick in _ticks(limit):
        out.append(f'<line x1="{x0}" x2="{x1}" y1="{y(tick):.1f}" y2="{y(tick):.1f}" '
                   f'stroke="{AXIS if tick == 0 else GRID}"/>')
        out.append(f'<text x="{x0 - 5}" y="{y(tick) + 4:.1f}" text-anchor="end" fill="{DIM}">{_number(tick)}</text>')
    for tick in _x_ticks(length):
        out.append(f'<line x1="{x(tick):.1f}" x2="{x(tick):.1f}" y1="{y1}" y2="{y1 + 4}" stroke="{AXIS}"/>')
        out.append(f'<text x="{x(tick):.1f}" y="{y1 + 16}" text-anchor="middle" fill="{DIM}">{_number(tick)}</text>')
    out.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="none" stroke="{AXIS}"/>')
    limits = _limits(pair)
    if limits:  # the model limits: a shape between the band's two edges, under the lines
        low, high = limits
        edges = [f"{x(a):.1f},{y(d):.1f}" for a, d in zip(along, low)] + \
                [f"{x(a):.1f},{y(d):.1f}" for a, d in zip(along[::-1], high[::-1])]
        out.append(f'<polygon points="{" ".join(edges)}" fill="{LIMITS}" fill-opacity="0.18" stroke="none"/>')
    read = [float(v) for v in pair.get("dt_read_ps", dt)]
    if len(read) == len(along):
        dashed = " ".join(f"{x(a):.1f},{y(d):.1f}" for a, d in zip(along, read))
        out.append(f'<polyline points="{dashed}" fill="none" stroke="{READ}" stroke-width="1.2" stroke-dasharray="5 3" '
                   f'stroke-linejoin="round"/>')
    points = " ".join(f"{x(a):.1f},{y(d):.1f}" for a, d in zip(along, dt))
    out.append(f'<polyline points="{points}" fill="none" stroke="{LINE}" stroke-width="1.6" stroke-linejoin="round"/>')
    out.append(f'<text x="{(x0 + x1) / 2}" y="{oy + HEIGHT - 6}" text-anchor="middle" fill="{DIM}">mm along the pair '
               f'(0–{length:.1f}); Δt in ps</text>')
    out.append(f'<text x="{x0 - 44}" y="{(y0 + y1) / 2}" text-anchor="middle" fill="{DIM}" '
               f'transform="rotate(-90 {x0 - 44} {(y0 + y1) / 2})">Δt (ps)</text>')
    return out


def _data(pairs, tolerance_ps: float, min_length_mm: float, title: str) -> dict:
    """The numbers behind the chart, as the <metadata> block carries them."""
    return {"format": "kls-dynamic-phase-chart 1", "board": title,
            "tolerance_ps": tolerance_ps, "min_length_mm": min_length_mm,
            "band_note": _band_note(pairs),  # what dt_lo_ps and dt_hi_ps are
            "pairs": [{"name": pair["name"], "start": pair["start"], "end": pair["end"],
                       "flipped": bool(pair.get("flipped")),
                       "delay_p_ps": _f(pair.get("delay_p_ps")), "delay_n_ps": _f(pair.get("delay_n_ps")),
                       "length_p_mm": _f(pair.get("length_p_mm")), "length_n_mm": _f(pair.get("length_n_mm")),
                       "skew_ps": _f(pair["skew_ps"]), "max_abs_ps": _f(pair["max_ps"]),
                       "sources": list(pair.get("sources", ())),
                       "excursions": [{"at_mm": _f(e["at_mm"]), "length_mm": _f(e["length_mm"]),
                                       "peak_ps": _f(e["peak_ps"])} for e in pair.get("excursions", ())],
                       "along_mm": [round(float(v), 4) for v in pair["along_mm"]],
                       "dt_ps": [round(float(v), 4) for v in pair["dt_ps"]],
                       "dt_read_ps": [round(float(v), 4) for v in pair.get("dt_read_ps", pair["dt_ps"])],
                       "dt_lo_ps": [round(v, 4) for v in (_limits(pair) or (pair["dt_ps"], []))[0]],
                       "dt_hi_ps": [round(v, 4) for v in (_limits(pair) or ([], pair["dt_ps"]))[1]]}
                      for pair in pairs]}


def _f(value, digits: int = 4):
    return None if value is None else round(float(value), digits)


def csv(pairs) -> str:
    """One row per sample: pair, mm along the pair, Δt in ps (as made, as read, and the
    model limits below and above it: Δt itself without them); a header line first."""
    lines = ["pair,start,end,along_mm,dt_ps,dt_read_ps,dt_lo_ps,dt_hi_ps"]
    for pair in pairs:
        name = _field(pair["name"])
        start, end = _field(pair["start"]), _field(pair["end"])
        read = pair.get("dt_read_ps", pair["dt_ps"])
        low, high = _limits(pair) or (pair["dt_ps"], pair["dt_ps"])
        for along, dt, seen, lo, hi in zip(pair["along_mm"], pair["dt_ps"], read, low, high):
            lines.append(f"{name},{start},{end},{float(along):.4f},{float(dt):.4f},{float(seen):.4f},"
                         f"{float(lo):.4f},{float(hi):.4f}")
    return "\n".join(lines) + "\n"


def _field(text: str) -> str:
    text = str(text)
    return '"' + text.replace('"', '""') + '"' if any(c in text for c in ',"\n') else text
