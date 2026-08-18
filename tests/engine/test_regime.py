"""Unit tests for the pure realized-vol regime-signal engine (F2.1).

All synthetic, NO dwh. The expected RV numbers are hand-derived with pure-Python
``math`` / ``statistics`` (an independent oracle — NOT the module under test), so
a wrong formula in :mod:`tcg.engine.regime` is caught rather than co-confirmed.
Covers: hand-verified small-window RV, no-look-ahead alignment, insufficient
history -> None, constant series -> 0, ordering, empty series, the ``window>=2``
guard, non-positive-close guard, and the ``realized_vol_by_date`` key/shape.
"""

from __future__ import annotations

import math
import statistics

import pytest

from tcg.engine.regime import realized_vol_by_date, rolling_realized_vol


def _expected_rv(returns: list[float], annualization: int = 252) -> float:
    """Independent oracle: annualized sample-std (ddof=1) of the log returns."""
    return statistics.stdev(returns) * math.sqrt(annualization)


def test_rolling_rv_hand_verified_small_window() -> None:
    # Distinct returns so the test discriminates a wrong std/annualization.
    # closes -> log returns: r1=ln(105/100), r2=ln(100/105), r3=ln(110/100).
    closes = [100.0, 105.0, 100.0, 110.0]
    r1 = math.log(105 / 100)
    r2 = math.log(100 / 105)
    r3 = math.log(110 / 100)

    out = rolling_realized_vol(closes, window=2)
    assert len(out) == len(closes)
    # window=2 => indices 0,1 None (insufficient history); index 2 & 3 defined.
    assert out[0] is None
    assert out[1] is None
    assert out[2] == pytest.approx(_expected_rv([r1, r2]))
    assert out[3] == pytest.approx(_expected_rv([r2, r3]))


def test_no_look_ahead_index_i_uses_only_closes_up_to_i() -> None:
    # If RV at index 2 leaked a FUTURE close, appending a wildly different close
    # would change out[2]. It must not.
    base = [100.0, 105.0, 100.0]
    extended = base + [500.0]
    assert rolling_realized_vol(base, window=2)[2] == pytest.approx(
        rolling_realized_vol(extended, window=2)[2]
    )


def test_insufficient_history_is_none_not_fabricated() -> None:
    closes = [100.0, 101.0, 102.0]  # 2 returns, window 3 needs 3 -> all None
    out = rolling_realized_vol(closes, window=3)
    assert out == [None, None, None]


def test_constant_series_is_zero_vol_after_warmup() -> None:
    closes = [50.0] * 6
    out = rolling_realized_vol(closes, window=3)
    assert out[:3] == [None, None, None]
    assert out[3] == pytest.approx(0.0)
    assert out[4] == pytest.approx(0.0)
    assert out[5] == pytest.approx(0.0)


def test_ordering_respected_reversed_series_differs() -> None:
    up = [100.0, 101.0, 103.0, 100.0, 108.0, 100.0]
    down = list(reversed(up))
    assert rolling_realized_vol(up, window=2) != rolling_realized_vol(down, window=2)
    # But each is internally consistent with the oracle at a checked index.
    r_up = [math.log(up[i + 1] / up[i]) for i in range(len(up) - 1)]
    assert rolling_realized_vol(up, window=3)[3] == pytest.approx(
        _expected_rv(r_up[0:3])
    )


def test_annualization_scales_by_sqrt() -> None:
    closes = [100.0, 102.0, 99.0, 104.0, 100.0]
    a = rolling_realized_vol(closes, window=2, annualization=252)[2]
    b = rolling_realized_vol(closes, window=2, annualization=63)[2]
    assert a == pytest.approx(b * math.sqrt(252 / 63))


def test_empty_and_short_series() -> None:
    assert rolling_realized_vol([], window=2) == []
    assert rolling_realized_vol([100.0], window=2) == [None]
    assert rolling_realized_vol([100.0, 101.0], window=2) == [None, None]


def test_window_below_two_raises() -> None:
    with pytest.raises(ValueError, match="window must be >= 2"):
        rolling_realized_vol([100.0, 101.0, 102.0], window=1)


def test_non_positive_close_raises_loudly() -> None:
    with pytest.raises(ValueError, match="positive closes"):
        rolling_realized_vol([100.0, 0.0, 100.0, 101.0], window=2)


# --------------------------------------------------------------------------- #
# realized_vol_by_date — the per-date, multi-window signal map.
# --------------------------------------------------------------------------- #
def test_realized_vol_by_date_keys_and_alignment() -> None:
    dates = [20250101, 20250102, 20250103, 20250104, 20250105]
    closes = [100.0, 102.0, 99.0, 104.0, 100.0]
    m = realized_vol_by_date(dates, closes, windows=[2, 3])

    assert set(m) == set(dates)
    # Every date carries both window keys.
    for d in dates:
        assert set(m[d]) == {"h2", "h3"}
    # h2 warms up after 2 points; h3 after 3.
    assert m[20250101]["h2"] is None
    assert m[20250102]["h2"] is None
    assert m[20250103]["h2"] is not None
    assert m[20250103]["h3"] is None
    assert m[20250104]["h3"] is not None
    # Value matches the standalone rolling call (same oracle path).
    rv2 = rolling_realized_vol(closes, 2)
    assert m[dates[2]]["h2"] == pytest.approx(rv2[2])


def test_realized_vol_by_date_empty() -> None:
    assert realized_vol_by_date([], [], windows=[20, 30, 100]) == {}


def test_realized_vol_by_date_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        realized_vol_by_date([20250101, 20250102], [100.0], windows=[2])
