"""The drift: what a probe on each trace reads once skew has turned into common mode.

Δt (phase.py) is the pair's geometric skew: how much longer the signal takes on P's copper
than on N's up to each point. A probe on one trace does not read that once the pair has
carried the skew for a while. Skew made at a bend puts part of the signal into the pair's
common mode, which on microstrip travels slower than the differential mode (delays.mode_lag),
so it slides behind the edge; what the probe sees on P is the differential edge plus that
sliding pulse, on N the same edge inverted plus the same pulse, and their timings move.

This is the two-mode solution of a coupled line (Tripathi 1975; the guide's equations):
between two readings the odd part is delayed by the mean of the two conductors' delays,
the even part by that plus the mode lag, and where the geometry makes one side longer the
two traces are shifted apart by the skew increment. Every step is a delay or a half-sum,
so it is done exactly in the frequency domain on a Gaussian-edged pulse of the given rise
time; the two traces are then timed as a probe would time them, by the 50 % point of
each pulse's running integral. For one skew δ followed by a lag Δτ that is, to first
order, Δt_read = δ · exp(−Δτ² / 2σ²) with σ the edge's Gaussian width: the skew reads in
full where it is made and fades as its common mode falls behind, until the two edges are
aligned again and the skew lives only in a common-mode pulse running behind them.
"""

import numpy as np

SIGMA_PER_RISE = 1 / 2.563  # a Gaussian edge's σ per 10–90 % rise time
SAMPLES_PER_SIGMA = 24
WINDOW_SIGMAS = 4.0  # the running integral taken this far either side of the pulse's peak


def apparent_dt(delays_ps: np.ndarray, lag_ps: np.ndarray, rise_ps: float = 25.0) -> np.ndarray:
    """(k,) ps: Δt as a probe on each trace would read it at every reading, from each
    side's delay at the reading (k, 2) and the even mode's lag there (k,), for an edge of
    `rise_ps` (10–90 %). Equal to the geometric Δt where no common mode has built up."""
    delays = np.asarray(delays_ps, dtype=np.float64).reshape(-1, 2)
    lag = np.asarray(lag_ps, dtype=np.float64).reshape(-1)
    count = len(delays)
    if count == 0:
        return np.zeros(0)
    sigma = max(float(rise_ps), 1.0) * SIGMA_PER_RISE
    dt = sigma / SAMPLES_PER_SIGMA
    span = float(delays.max() - delays.min()) + float(lag.max() - lag.min()) + 40 * sigma
    n = int(2 ** np.ceil(np.log2(max(span / dt, 64))))
    t = (np.arange(n) - n // 4) * dt
    pulse = np.exp(-t ** 2 / (2 * sigma ** 2))
    f = np.fft.rfftfreq(n, dt)
    launch = np.fft.rfft(pulse)
    p_spec, n_spec = launch.copy(), -launch

    def delayed(spectrum, ps):
        return spectrum * np.exp(-2j * np.pi * f * ps)

    out = np.empty(count)
    for k in range(count):
        if k:
            dtp, dtn = delays[k] - delays[k - 1]
            mean = (dtp + dtn) / 2
            even, odd = (p_spec + n_spec) / 2, (p_spec - n_spec) / 2
            even, odd = delayed(even, mean + (lag[k] - lag[k - 1])), delayed(odd, mean)
            p_spec, n_spec = even + odd, even - odd
            p_spec, n_spec = delayed(p_spec, (dtp - dtn) / 2), delayed(n_spec, -(dtp - dtn) / 2)
        out[k] = _crossing(t, np.fft.irfft(p_spec, n), sigma) - _crossing(t, np.fft.irfft(n_spec, n), sigma)
    return out


def _crossing(t: np.ndarray, v: np.ndarray, sigma: float) -> float:
    """The 50 % point of the pulse's running integral within WINDOW_SIGMAS of its peak, the
    pulse taken positive (a probe's timing, as openems_pair.crossing)."""
    v = v * np.sign(v[np.argmax(np.abs(v))])
    peak = int(np.argmax(v))
    reach = int(WINDOW_SIGMAS * sigma / (t[1] - t[0]))
    lo, hi = max(0, peak - reach), min(len(v), peak + reach + 1)
    tw, vw = t[lo:hi], v[lo:hi]
    running = np.concatenate([[0.0], np.cumsum((vw[1:] + vw[:-1]) / 2 * np.diff(tw))])
    target = running[-1] / 2
    index = int(np.searchsorted(running, target))
    if index <= 0 or index >= len(running):
        return float(tw[min(max(index, 0), len(tw) - 1)])
    a, b = running[index - 1], running[index]
    frac = (target - a) / (b - a) if b != a else 0.0
    return float(tw[index - 1] + frac * (tw[index] - tw[index - 1]))
