"""4-leg iron condor on SPX, 30-DTE, ATM +/-5% / +/-10%, 2020-2024.

Demonstrates the options seam: the ``run``-shape escape hatch builds four
:class:`OptionLegSpec` instances via :func:`lib.options.build_legs`,
attaches them to a :class:`BacktestSpec` with ``sizing.fraction=0`` so the
underlying carries no exposure, and uses :func:`daily_pulse` as the
fire-on-every-bar entry trigger. The compile pipeline embeds this file
verbatim into the notebook, so the audit trail is the source you're
reading.

Look-ahead discipline (manual, since the ``no_lookahead`` probe is skipped
for run-shape strategies): ``daily_pulse`` is causal by construction
(deterministic alternating +/-1 by index), and the engine still applies
its own ``signal[t-1]`` shift on top.
"""

META = {
    "slug": "complex-iron-condor",
    "description": "30-DTE SPX iron condor, ATM +/-5% / +/-10%, daily rebalance.",
    "dates": {"start": "2020-01-01", "end": "2024-12-31"},
    "universe": ["SPX"],
    "benchmark": "SPX",
    "asset_class": "INDEX",
    "sizing": {"method": "fixed_fraction", "fraction": 0.0},
    "execution": {"fees_bps": 0.0, "slippage_bps": 5.0, "fill_timing": "next_open"},
    "tags": ["options", "iron-condor", "multi-leg"],
}

import numpy as np
import plotly.graph_objects as go

from lib import BacktestResult, BacktestSpec, ExecutionConfig, SizingConfig
from lib import data as lib_data
from lib.data_load import OptionChainSnapshot, load_option_chain_sync
from lib.engine import DteSelector
from lib.options import build_legs
from lib.plotting import PlotJob


# ---- helpers ---------------------------------------------------------------


def _meta_dates_yyyymmdd(meta: dict) -> tuple[int, int]:
    """Parse META['dates'] ISO strings into YYYYMMDD ints."""
    from datetime import datetime
    s = datetime.strptime(meta["dates"]["start"], "%Y-%m-%d").date()
    e = datetime.strptime(meta["dates"]["end"], "%Y-%m-%d").date()
    return (s.year * 10000 + s.month * 100 + s.day,
            e.year * 10000 + e.month * 100 + e.day)


# ---- run-shape entry point -------------------------------------------------


def run(ctx) -> BacktestResult:
    """Load SPX bars, build a 4-leg iron condor, drive the engine."""
    start, end = _meta_dates_yyyymmdd(ctx.meta)
    bars = ctx.load_bars("SPX", asset_class="INDEX", start=start, end=end)

    # Build the 4-leg structure via lib.options.build_legs. Strikes are
    # specified as offsets from spot (resolved per-bar by the engine's
    # StrikeOffsetPctSelector against each chain snapshot's spot).
    legs = build_legs(
        [
            {"side": "short", "option_type": "C",
             "strike": ("offset_pct",  0.05), "leg_id": "short_call"},
            {"side": "long",  "option_type": "C",
             "strike": ("offset_pct",  0.10), "leg_id": "long_call"},
            {"side": "short", "option_type": "P",
             "strike": ("offset_pct", -0.05), "leg_id": "short_put"},
            {"side": "long",  "option_type": "P",
             "strike": ("offset_pct", -0.10), "leg_id": "long_put"},
        ],
        expiry_selector=DteSelector(target_dte=30, tolerance_days=5),
        spot_hint=float(bars.close[-1]) if len(bars.close) else 4500.0,
    )

    # Per-bar option chain provider. Required for any strategy with
    # `option_legs`: the engine calls this on every signal-firing bar to
    # resolve concrete strikes against the live chain. Without it, every
    # leg logs `no_chain` unfilled and equity is constant.
    #
    # Three narrowing knobs the engine can NOT do for us, applied here:
    # 1. Collection-name root is `"SP_500"` (the OPT_<ROOT> collection
    #    suffix), NOT `"SPX"` (the underlying-symbol ticker). See
    #    `lib.data.KNOWN_OPTION_ROOTS` for the live-verified list.
    # 2. `expiration` filter — narrow to a single ~30-DTE expiration
    #    cycle. Without it the chain returns every live expiration's
    #    strikes (~1000+ contracts).
    # 3. `strike_filter` against per-bar spot — the iron condor's widest
    #    leg is at offset_pct ±10% from spot, so anything outside ±15%
    #    is dead weight at retrieval time.
    #
    # Performance note (verified live 2026-05-05 against tcg-instrument):
    # even with both narrowings the per-bar load is ~10s — the underlying
    # collection lacks a compound index over
    # (eodDatasStart, eodDatasEnd, expiration, strike), so Mongo still
    # evaluates the predicate over many docs. A full 5-year backtest is
    # therefore ~3 hours wall-clock; iterate on shorter META.dates ranges
    # (e.g. one quarter ≈ 10 minutes). A server-side index addition is
    # the right structural fix for the canonical multi-year case but is
    # outside this template's scope.
    db = lib_data.raw_db()
    _all_expiries = lib_data.list_option_expiries("SP_500")
    # Per-bar spot lookup (closure over `bars`) — close-of-day at the
    # corresponding YYYYMMDD; the engine fires on next_open so close[t]
    # is the latest information already available at chain-pick time.
    _spot_by_date: dict[int, float] = {
        int(d): float(c) for d, c in zip(bars.dates.tolist(), bars.close.tolist())
    }

    def _pick_target_expiration(date: int) -> int | None:
        """30-DTE ± 5d expiration, closest to target by DTE; None if no match."""
        from datetime import date as _date
        y, m, d = date // 10000, (date // 100) % 100, date % 100
        asof = _date(y, m, d)
        best_dte = None
        best_exp = None
        for exp in _all_expiries:
            ey, em, ed = exp // 10000, (exp // 100) % 100, exp % 100
            try:
                dte = (_date(ey, em, ed) - asof).days
            except ValueError:
                continue
            if 25 <= dte <= 35 and (best_dte is None or abs(dte - 30) < abs(best_dte - 30)):
                best_dte = dte
                best_exp = exp
        return best_exp

    def _chain_provider(date: int) -> OptionChainSnapshot | None:
        target = _pick_target_expiration(int(date))
        if target is None:
            return None
        spot = _spot_by_date.get(int(date))
        # Strike window: ±15% of spot covers the iron-condor's widest leg
        # (±10%) with headroom for spot drift inside the cycle. If spot
        # isn't available (gap day), fall through with no strike filter
        # rather than dropping the bar entirely.
        strike_filter = (spot * 0.85, spot * 1.15) if spot and spot > 0 else None
        try:
            return load_option_chain_sync(
                db,
                "SP_500",
                asof_date=int(date),
                expiration=int(target),
                strike_filter=strike_filter,
                progress=False,
            )
        except (LookupError, ValueError):
            # Missing-collection or bad-shape: surface as no_chain rather
            # than aborting the backtest; the engine logs unfilled legs.
            return None

    # Daily-rebalance entry: alternating +/-1 fires on every bar; the leg
    # side comes from OptionLegSpec.side, so PnL is invariant to the sign
    # of the trigger. Sizing fraction=0 keeps underlying exposure off the
    # book; only the option legs trade.
    n = len(bars.dates)
    signal = ctx.indicators.daily_pulse(n)
    spec = BacktestSpec(
        bars=bars,
        signal=signal,
        sizing=SizingConfig(method="fixed_fraction", fraction=0.0),
        execution=ExecutionConfig(
            fees_bps=float(ctx.meta["execution"].get("fees_bps", 0.0)),
            slippage_bps=float(ctx.meta["execution"].get("slippage_bps", 5.0)),
            fill_timing=ctx.meta["execution"].get("fill_timing", "next_open"),
        ),
        capital_base=float(ctx.meta.get("capital_base", 100_000.0)),
        option_legs=legs,
        option_chain_provider=_chain_provider,
        label=str(ctx.meta.get("slug", "iron-condor")),
    )
    return ctx.run_backtest(spec)


# ---- EXTRA_PLOTS -----------------------------------------------------------


def _plot_payoff_diagram(result, **_kwargs) -> go.Figure:
    """Static payoff diagram at expiration for the 4-leg iron condor.

    Reads the leg specs off ``result.meta['spec']['option_legs']`` (when
    available) to render a piecewise-linear P&L vs underlying-at-expiry
    curve. Falls back to a labelled-but-empty figure if leg metadata is
    missing — the wiring is the demonstration; numeric accuracy here is
    not the load-bearing claim of this example.
    """
    spec_meta = (result.meta or {}).get("spec") or {}
    legs_meta = spec_meta.get("option_legs") or []
    spot_hint = 4500.0
    spots = np.linspace(spot_hint * 0.85, spot_hint * 1.15, 120)
    payoff = np.zeros_like(spots)
    # Approximate piecewise payoff: short call kink at +5%, long call at +10%,
    # short put kink at -5%, long put at -10%. Premiums omitted (relative shape
    # is what matters for an iron-condor diagnostic).
    short_call_k = spot_hint * 1.05
    long_call_k = spot_hint * 1.10
    short_put_k = spot_hint * 0.95
    long_put_k = spot_hint * 0.90
    payoff -= np.maximum(spots - short_call_k, 0.0)
    payoff += np.maximum(spots - long_call_k, 0.0)
    payoff -= np.maximum(short_put_k - spots, 0.0)
    payoff += np.maximum(long_put_k - spots, 0.0)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=spots, y=payoff, mode="lines", name="payoff at expiry"))
    fig.add_vline(x=spot_hint, line_dash="dot", line_color="grey",
                  annotation_text="spot hint")
    fig.update_layout(
        title=f"Iron condor payoff at expiry ({len(legs_meta)} legs, ATM +/-5/10%)",
        xaxis_title="Underlying at expiration",
        yaxis_title="P&L per 1 contract (premiums omitted)",
        template="plotly_dark",
    )
    return fig


EXTRA_PLOTS = [
    PlotJob(id="iron_condor_payoff", builder=_plot_payoff_diagram, kwargs={}),
]
