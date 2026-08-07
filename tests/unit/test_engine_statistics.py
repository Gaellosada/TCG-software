"""Tests for tcg.engine.statistics — equity-curve StatisticsSuite."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tcg.engine.statistics import compute_statistics
from tcg.types.statistics import StatisticsSuite


# ── Helpers ────────────────────────────────────────────────────────────


def _yyyymmdd_range(start_year: int, start_month: int, n: int) -> np.ndarray:
    """Generate N consecutive calendar dates as YYYYMMDD ints starting at
    ``start_year-start_month-01``. Wraps months naively (max 28 days per month
    so leap-year handling is irrelevant)."""
    out: list[int] = []
    year, month, day = start_year, start_month, 1
    for _ in range(n):
        out.append(year * 10000 + month * 100 + day)
        day += 1
        if day > 28:
            day = 1
            month += 1
            if month > 12:
                month = 1
                year += 1
    return np.array(out, dtype=np.int64)


# ── Validation ─────────────────────────────────────────────────────────


class TestValidation:
    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="length"):
            compute_statistics(
                np.array([20240101, 20240102], dtype=np.int64),
                np.array([100.0], dtype=np.float64),
            )

    def test_fewer_than_two_raises(self):
        with pytest.raises(ValueError, match="2 observations"):
            compute_statistics(
                np.array([20240101], dtype=np.int64),
                np.array([100.0], dtype=np.float64),
            )

    def test_non_positive_equity_raises(self):
        with pytest.raises(ValueError, match="positive"):
            compute_statistics(
                np.array([20240101, 20240102], dtype=np.int64),
                np.array([100.0, 0.0], dtype=np.float64),
            )

    def test_positive_infinity_equity_raises(self):
        with pytest.raises(ValueError, match="finite"):
            compute_statistics(
                np.array([20240101, 20240102], dtype=np.int64),
                np.array([100.0, np.inf], dtype=np.float64),
            )

    def test_nan_equity_raises(self):
        with pytest.raises(ValueError, match="finite"):
            compute_statistics(
                np.array([20240101, 20240102], dtype=np.int64),
                np.array([100.0, np.nan], dtype=np.float64),
            )


# ── Return stats ───────────────────────────────────────────────────────


class TestReturnStats:
    def test_total_return_and_cagr_one_trading_year(self):
        # 253 points => 252 daily returns => exactly 1 trading year by
        # the project convention.
        n = 253
        dates = _yyyymmdd_range(2020, 1, n)
        equity = np.linspace(100.0, 200.0, n)
        suite = compute_statistics(dates, equity, risk_free_rate=0.0)

        assert math.isclose(suite.return_.total_return, 1.0, abs_tol=1e-12)
        # CAGR ≈ (200/100) ** (252/252) - 1 = 1.0
        assert math.isclose(suite.return_.cagr, 1.0, abs_tol=1e-12)

    def test_annualized_volatility_matches_manual(self):
        rng = np.random.default_rng(42)
        n = 1000
        dates = _yyyymmdd_range(2020, 1, n)
        # Build equity from random returns
        rets = rng.normal(0.0005, 0.01, size=n - 1)
        equity = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + rets)))
        suite = compute_statistics(dates, equity, risk_free_rate=0.0)

        manual_daily = np.diff(equity) / equity[:-1]
        expected_vol = float(np.std(manual_daily, ddof=1) * np.sqrt(252.0))
        assert math.isclose(
            suite.return_.annualized_volatility, expected_vol, rel_tol=1e-10
        )

    def test_best_worst_day(self):
        dates = _yyyymmdd_range(2020, 1, 6)
        # Daily returns: +5%, -3%, +10%, -8%, +1%
        rets = [0.05, -0.03, 0.10, -0.08, 0.01]
        equity = [100.0]
        for r in rets:
            equity.append(equity[-1] * (1 + r))
        suite = compute_statistics(
            np.array(dates, dtype=np.int64),
            np.array(equity, dtype=np.float64),
        )
        assert math.isclose(suite.return_.best_day, 0.10, abs_tol=1e-12)
        assert math.isclose(suite.return_.worst_day, -0.08, abs_tol=1e-12)

    def test_excess_return_equals_cagr_minus_rf(self):
        # 253 daily samples → exactly 1 trading year. CAGR is exact, so
        # excess_return must equal CAGR - Rf to machine precision.
        n = 253
        dates = _yyyymmdd_range(2020, 1, n)
        equity = np.linspace(100.0, 200.0, n)
        rf = 0.04
        suite = compute_statistics(dates, equity, risk_free_rate=rf)
        expected = suite.return_.cagr - rf
        assert math.isclose(suite.return_.excess_return, expected, abs_tol=1e-12)

    def test_best_worst_month(self):
        # Three months: Jan up 10%, Feb down 5%, Mar up 2%
        # Days per month (synthetic): 20 each
        dates_jan = [20240100 + d for d in range(1, 21)]
        dates_feb = [20240200 + d for d in range(1, 21)]
        dates_mar = [20240300 + d for d in range(1, 21)]
        dates = np.array(dates_jan + dates_feb + dates_mar, dtype=np.int64)

        # Equity: linearly interpolate to hit the targets
        eq_jan = np.linspace(100.0, 110.0, 20)
        eq_feb = np.linspace(110.0, 104.5, 20)  # -5% from 110
        eq_mar = np.linspace(104.5, 106.59, 20)  # +2% from 104.5
        equity = np.concatenate([eq_jan, eq_feb, eq_mar])

        suite = compute_statistics(dates, equity)

        # Jan: (110/100)-1 = 0.10 — anchor is equity[0] for the first bucket
        # Feb: (104.5/110)-1 ≈ -0.05 — anchor is end-of-Jan = 110
        # Mar: (106.59/104.5)-1 ≈ 0.02
        assert math.isclose(suite.return_.best_month, 0.10, abs_tol=1e-6)
        assert math.isclose(suite.return_.worst_month, -0.05, abs_tol=1e-6)


# ── Risk-adjusted ──────────────────────────────────────────────────────


class TestRiskAdjusted:
    def test_sharpe_rf_zero(self):
        rng = np.random.default_rng(7)
        n = 1000
        dates = _yyyymmdd_range(2020, 1, n)
        rets = rng.normal(0.0008, 0.012, size=n - 1)
        equity = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + rets)))
        suite = compute_statistics(dates, equity, risk_free_rate=0.0)

        manual_daily = np.diff(equity) / equity[:-1]
        expected = float(
            np.mean(manual_daily) / np.std(manual_daily, ddof=1) * np.sqrt(252.0)
        )
        assert math.isclose(suite.risk_adjusted.sharpe_ratio, expected, rel_tol=1e-9)

    def test_log_return_type_uses_log_return_basis(self):
        """HIGH#3 regression: a log-built equity curve must have its
        risk-adjusted stats (Sharpe / vol) computed on the LOG return
        basis, not the simple-return formula."""
        rng = np.random.default_rng(13)
        n = 500
        dates = _yyyymmdd_range(2020, 1, n)
        log_rets = rng.normal(0.0008, 0.012, size=n - 1)
        equity = 100.0 * np.exp(np.cumsum(np.concatenate([[0.0], log_rets])))

        suite = compute_statistics(dates, equity, risk_free_rate=0.0, return_type="log")

        true_log = np.log(equity[1:] / equity[:-1])
        expected_sharpe = float(
            np.mean(true_log) / np.std(true_log, ddof=1) * np.sqrt(252.0)
        )
        assert math.isclose(
            suite.risk_adjusted.sharpe_ratio, expected_sharpe, rel_tol=1e-9
        )

        expected_vol = float(np.std(true_log, ddof=1) * np.sqrt(252.0))
        assert math.isclose(
            suite.return_.annualized_volatility, expected_vol, rel_tol=1e-9
        )

        # Differs from the simple-return basis.
        simple = np.diff(equity) / equity[:-1]
        wrong_sharpe = float(np.mean(simple) / np.std(simple, ddof=1) * np.sqrt(252.0))
        assert not math.isclose(
            suite.risk_adjusted.sharpe_ratio, wrong_sharpe, rel_tol=1e-9
        )

    def test_default_return_type_is_normal(self):
        """Omitting ``return_type`` preserves the prior simple-return
        behaviour exactly."""
        rng = np.random.default_rng(99)
        n = 300
        dates = _yyyymmdd_range(2020, 1, n)
        rets = rng.normal(0.0005, 0.01, size=n - 1)
        equity = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + rets)))
        s_default = compute_statistics(dates, equity, risk_free_rate=0.0)
        s_normal = compute_statistics(
            dates, equity, risk_free_rate=0.0, return_type="normal"
        )
        assert (
            s_default.risk_adjusted.sharpe_ratio == s_normal.risk_adjusted.sharpe_ratio
        )
        assert (
            s_default.return_.annualized_volatility
            == s_normal.return_.annualized_volatility
        )

    def test_sharpe_rf_nonzero(self):
        rng = np.random.default_rng(11)
        n = 800
        dates = _yyyymmdd_range(2020, 1, n)
        rets = rng.normal(0.001, 0.01, size=n - 1)
        equity = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + rets)))

        rf = 0.04
        suite = compute_statistics(dates, equity, risk_free_rate=rf)

        manual_daily = np.diff(equity) / equity[:-1]
        daily_rf = (1 + rf) ** (1 / 252) - 1
        excess = manual_daily - daily_rf
        expected = float(np.mean(excess) / np.std(excess, ddof=1) * np.sqrt(252.0))
        assert math.isclose(suite.risk_adjusted.sharpe_ratio, expected, rel_tol=1e-9)

    def test_sortino_positive_when_more_upside(self):
        # Trend up overall but with occasional down days — Sortino must be
        # well-defined (non-zero downside) and positive (mean excess > 0).
        rng = np.random.default_rng(3)
        n = 500
        dates = _yyyymmdd_range(2020, 1, n)
        rets = rng.normal(0.001, 0.01, size=n - 1)
        equity = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + rets)))
        suite = compute_statistics(dates, equity, risk_free_rate=0.0)
        assert suite.risk_adjusted.sortino_ratio > 0

    def test_sortino_zero_when_no_downside(self):
        # Strictly monotonic up — no negative excess returns.
        n = 100
        dates = _yyyymmdd_range(2020, 1, n)
        equity = 100.0 * (1.01) ** np.arange(n)
        suite = compute_statistics(dates, equity, risk_free_rate=0.0)
        assert suite.risk_adjusted.sortino_ratio == 0.0

    def test_calmar_uses_excess_over_rf(self):
        n = 253
        dates = _yyyymmdd_range(2020, 1, n)
        # Trend up then a small drop to create a drawdown.
        equity = np.concatenate(
            [np.linspace(100.0, 150.0, 200), np.linspace(150.0, 130.0, 53)]
        )
        rf = 0.04
        suite = compute_statistics(dates, equity, risk_free_rate=rf)

        expected = (suite.return_.cagr - rf) / abs(suite.drawdown.max_drawdown)
        assert math.isclose(suite.risk_adjusted.calmar_ratio, expected, rel_tol=1e-12)


# ── Tail ───────────────────────────────────────────────────────────────


class TestTail:
    def test_var_and_cvar(self):
        rng = np.random.default_rng(0)
        n = 500
        dates = _yyyymmdd_range(2020, 1, n)
        rets = rng.normal(0.0, 0.01, size=n - 1)
        equity = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + rets)))
        suite = compute_statistics(dates, equity, risk_free_rate=0.0)

        manual_daily = np.diff(equity) / equity[:-1]
        expected_var_95 = float(np.quantile(manual_daily, 0.05))
        expected_var_99 = float(np.quantile(manual_daily, 0.01))
        expected_cvar = float(np.mean(manual_daily[manual_daily <= expected_var_95]))

        assert math.isclose(suite.tail.var_95, expected_var_95, rel_tol=1e-12)
        assert math.isclose(suite.tail.var_99, expected_var_99, rel_tol=1e-12)
        assert math.isclose(suite.tail.cvar_5, expected_cvar, rel_tol=1e-12)
        assert suite.tail.var_95 < 0  # quantile of typical returns
        assert suite.tail.var_99 <= suite.tail.var_95

    def test_skew_kurtosis_none_when_insufficient_obs(self):
        # 25 equity points → 24 returns < 30 threshold
        n = 25
        dates = _yyyymmdd_range(2020, 1, n)
        equity = 100.0 * (1.001) ** np.arange(n)
        suite = compute_statistics(dates, equity)
        assert suite.tail.skewness is None
        assert suite.tail.kurtosis is None

    def test_skew_kurtosis_populated_when_enough_obs(self):
        n = 200
        dates = _yyyymmdd_range(2020, 1, n)
        rng = np.random.default_rng(1)
        rets = rng.normal(0.0, 0.01, size=n - 1)
        equity = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + rets)))
        suite = compute_statistics(dates, equity)
        assert suite.tail.skewness is not None
        assert suite.tail.kurtosis is not None
        # Skew of ~symmetric normal-ish returns is small.
        assert abs(suite.tail.skewness) < 1.0


# ── Drawdown ───────────────────────────────────────────────────────────


class TestDrawdown:
    def test_max_drawdown_obvious_case(self):
        # 100 -> 200 -> 100 → max DD = -50%
        dates = _yyyymmdd_range(2020, 1, 3)
        equity = np.array([100.0, 200.0, 100.0])
        suite = compute_statistics(dates, equity)
        assert math.isclose(suite.drawdown.max_drawdown, -0.5, abs_tol=1e-12)

    def test_current_drawdown_at_new_high_is_zero(self):
        dates = _yyyymmdd_range(2020, 1, 3)
        equity = np.array([100.0, 90.0, 110.0])
        suite = compute_statistics(dates, equity)
        assert suite.drawdown.current_drawdown == 0.0

    def test_current_drawdown_below_peak(self):
        dates = _yyyymmdd_range(2020, 1, 3)
        equity = np.array([100.0, 200.0, 150.0])
        suite = compute_statistics(dates, equity)
        # current_dd = 150 / 200 - 1 = -0.25
        assert math.isclose(suite.drawdown.current_drawdown, -0.25, abs_tol=1e-12)

    def test_avg_drawdown_only_underwater(self):
        dates = _yyyymmdd_range(2020, 1, 5)
        equity = np.array([100.0, 90.0, 80.0, 100.0, 95.0])
        # Drawdowns:
        #   idx0: cummax 100 -> 0
        #   idx1: cummax 100 -> -0.10
        #   idx2: cummax 100 -> -0.20
        #   idx3: cummax 100 -> 0
        #   idx4: cummax 100 -> -0.05
        # Underwater values: [-0.10, -0.20, -0.05] → mean = -0.1166...
        suite = compute_statistics(dates, equity)
        assert math.isclose(suite.drawdown.avg_drawdown, -0.35 / 3, rel_tol=1e-12)

    def test_longest_drawdown_duration(self):
        # 100, 99, 98, 97, 96, 97, 96, 100, 95, 100
        # Underwater runs: idx1..6 (length 6 — never recovers above 100 until idx7),
        # then idx8 (length 1).
        # Peak is at idx0=100. idx7=100 matches peak → not underwater.
        # So longest = 6.
        dates = _yyyymmdd_range(2020, 1, 10)
        equity = np.array(
            [100.0, 99.0, 98.0, 97.0, 96.0, 97.0, 96.0, 100.0, 95.0, 100.0]
        )
        suite = compute_statistics(dates, equity)
        assert suite.drawdown.longest_drawdown_days == 6

    def test_time_underwater_days(self):
        dates = _yyyymmdd_range(2020, 1, 5)
        equity = np.array([100.0, 90.0, 100.0, 110.0, 105.0])
        # Underwater: idx1 (-10%), idx4 (-4.5%). Two bars.
        suite = compute_statistics(dates, equity)
        assert suite.drawdown.time_underwater_days == 2


# ── Suite shape ────────────────────────────────────────────────────────


def test_suite_returns_correct_type_and_metadata():
    n = 60
    dates = _yyyymmdd_range(2020, 1, n)
    equity = 100.0 * (1.001) ** np.arange(n)
    suite = compute_statistics(dates, equity, risk_free_rate=0.04)

    assert isinstance(suite, StatisticsSuite)
    assert suite.risk_free_rate_used == 0.04
    assert suite.num_observations == n - 1


# ── Legacy InformationRatio reconciliation ─────────────────────────────
#
# Pins the DEFINITIONAL relationship between TCG's displayed Sharpe and the
# legacy Java headline reward/risk (StatisticsUtils.getInformationRatio):
#
#     legacy IR = 260 * mean(dailyPerf) / (sqrt(260) * std(ddof=1))
#               = sqrt(260) * mean / std(ddof=1)      (NO risk-free term)
#
# At rf=0, compute_statistics's Sharpe is sqrt(252) * mean / std(ddof=1) on
# the SAME per-bar returns. Therefore, on any identical return series, the
# ONLY difference is the annualization constant:
#
#     TCG_sharpe / legacy_IR  ==  sqrt(252 / 260)  ==  0.984495…
#
# There is NO residual formula-level gap at rf=0 beyond this constant. These
# tests are regression guards: if someone changes ddof, the annualization
# factor, or injects a risk-free term at rf=0, the identity breaks loudly.

_LEGACY_DAYS = 260
_TCG_DAYS = 252
_EXPECTED_TCG_OVER_LEGACY = math.sqrt(_TCG_DAYS / _LEGACY_DAYS)  # 0.984495…


def _legacy_information_ratio(daily_returns: np.ndarray) -> float:
    """Legacy IR reimplemented in Python, rf=0, per StatisticsUtils.java."""
    mean = float(np.mean(daily_returns))
    std = float(np.std(daily_returns, ddof=1))
    ann_vol = math.sqrt(_LEGACY_DAYS) * std
    if ann_vol == 0.0:
        return 0.0
    return _LEGACY_DAYS * mean / ann_vol


def _equity_from_returns(daily_returns: np.ndarray, start: float = 100.0) -> np.ndarray:
    equity = np.empty(len(daily_returns) + 1, dtype=np.float64)
    equity[0] = start
    equity[1:] = start * np.cumprod(1.0 + daily_returns)
    return equity


class TestLegacyInformationRatioReconciliation:
    def test_legacy_ir_reproduced_from_known_series(self):
        # Hand-computable two-value series: mean = 0.005 exactly.
        dr = np.array([0.02, -0.01] * 130, dtype=np.float64)  # 260 returns
        mean = float(np.mean(dr))
        assert mean == pytest.approx(0.005, abs=1e-12)
        std = float(np.std(dr, ddof=1))
        expected_ir = math.sqrt(_LEGACY_DAYS) * mean / std
        assert _legacy_information_ratio(dr) == pytest.approx(expected_ir, rel=1e-12)

    def test_tcg_sharpe_at_rf0_equals_0p9845_times_legacy(self):
        # The crux: at rf=0 the residual vs legacy is EXACTLY sqrt(252/260),
        # no more. Checked on a realistic pseudo-random return series.
        rng = np.random.default_rng(7)
        dr = rng.normal(loc=0.0006, scale=0.011, size=252 * 5)
        dates = _yyyymmdd_range(2015, 1, len(dr) + 1)
        suite = compute_statistics(
            dates, _equity_from_returns(dr), risk_free_rate=0.0
        )
        legacy = _legacy_information_ratio(dr)
        ratio = suite.risk_adjusted.sharpe_ratio / legacy
        assert ratio == pytest.approx(_EXPECTED_TCG_OVER_LEGACY, rel=1e-9)

    def test_tcg_sharpe_at_rf0_is_textbook_sqrt252(self):
        # compute_statistics at rf=0 must equal sqrt(252)*mean/std(ddof=1).
        rng = np.random.default_rng(11)
        dr = rng.normal(loc=0.0004, scale=0.009, size=400)
        dates = _yyyymmdd_range(2016, 1, len(dr) + 1)
        suite = compute_statistics(
            dates, _equity_from_returns(dr), risk_free_rate=0.0
        )
        mean = float(np.mean(dr))
        std = float(np.std(dr, ddof=1))
        textbook = math.sqrt(_TCG_DAYS) * mean / std
        assert suite.risk_adjusted.sharpe_ratio == pytest.approx(textbook, rel=1e-9)

    def test_ratio_holds_independent_of_date_cadence(self):
        # Sharpe reads ONLY the return series and a FIXED annualization constant
        # (252) — never the date axis nor the gaps between bars. So the SAME
        # equity presented on a genuinely irregular / sparse date axis must yield
        # an IDENTICAL Sharpe. (A gap-derived annualization would instead drift
        # with cadence — this test would catch that regression.)
        rng = np.random.default_rng(3)
        dr = rng.normal(loc=0.0005, scale=0.010, size=300)
        equity = _equity_from_returns(dr)
        m = len(equity)
        dense = _yyyymmdd_range(2018, 1, m)  # consecutive calendar days
        # Genuinely sparse axis: SAME number of points, but spread with irregular
        # 1..4-day gaps (an uneven, weekly-like cadence) picked out of a big pool —
        # NOT an identity clone of ``dense``.
        pool = _yyyymmdd_range(2018, 1, 5 * m)
        steps = rng.integers(1, 5, size=m - 1)  # irregular gaps in [1, 4]
        idx = np.concatenate(([0], np.cumsum(steps)))
        sparse = pool[idx]
        # Guard the guard: the sparse axis is really different and strictly
        # increasing (so this can never silently degrade to the identity no-op).
        assert not np.array_equal(sparse, dense)
        assert np.all(np.diff(sparse) > 0)
        s_dense = compute_statistics(dense, equity, risk_free_rate=0.0)
        s_sparse = compute_statistics(sparse, equity, risk_free_rate=0.0)
        assert s_dense.risk_adjusted.sharpe_ratio == pytest.approx(
            s_sparse.risk_adjusted.sharpe_ratio, rel=1e-12
        )
