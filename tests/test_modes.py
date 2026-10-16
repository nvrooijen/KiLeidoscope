"""The drift (kileido_bridge/modes.py): what a probe on each trace reads once skew has become common mode."""

import math

import numpy as np
import pytest

from kileido_bridge import modes


def test_skew_reads_in_full_where_it_is_made_and_fades_as_its_common_mode_falls_behind():
    s = np.linspace(0.0, 80.0, 161)  # mm along a coupled microstrip pair
    skew = np.where(s > 0, 8.0, 0.0)  # 8 ps made at the start
    delays = np.column_stack((5.7 * s + skew, 5.7 * s))
    lag = 0.33 * s  # the even mode falls behind by this much
    read = modes.apparent_dt(delays, lag, rise_ps=25.0)
    sigma = 25.0 * modes.SIGMA_PER_RISE
    at = {mm: read[int(np.argmin(np.abs(s - mm)))] for mm in (1, 10, 30, 60)}
    assert at[1] == pytest.approx(8.0, abs=0.1)  # right after the source: the geometric skew
    assert 7.0 < at[10] < 8.0 and 4.0 < at[30] < 6.5 and abs(at[60]) < 1.5  # fading with the lag
    first_order = 8.0 * math.exp(-lag[int(np.argmin(np.abs(s - 30)))] ** 2 / (2 * sigma ** 2))
    assert at[30] == pytest.approx(first_order, abs=1.2)  # the closed form, to first order in the skew
    assert np.all(np.diff(read[(s > 2) & (s < 50)]) <= 0.05)  # fading, never growing


def test_without_mode_lag_the_reading_is_the_geometric_skew_everywhere():
    s = np.linspace(0.0, 60.0, 121)
    delays = np.column_stack((6.8 * s + np.where(s > 0, 5.0, 0.0), 6.8 * s))
    read = modes.apparent_dt(delays, np.zeros_like(s), rise_ps=25.0)  # stripline: the two modes run together
    assert read[0] == pytest.approx(0.0, abs=1e-3) and np.allclose(read[1:], 5.0, atol=0.02)


def test_a_faster_edge_fades_sooner():
    s = np.linspace(0.0, 40.0, 81)
    delays = np.column_stack((5.7 * s + np.where(s > 0, 6.0, 0.0), 5.7 * s))
    lag = 0.33 * s
    slow, fast = (modes.apparent_dt(delays, lag, rise_ps=rise)[-1] for rise in (50.0, 12.0))
    assert fast < slow < 6.0  # 13 ps of lag: nothing to a 50 ps edge, most of it to a 12 ps edge
    assert len(modes.apparent_dt(np.zeros((0, 2)), np.zeros(0))) == 0
