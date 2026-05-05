# Purpose: bull call vertical — long ATM call + short OTM call, entered on bullish
# momentum (20-day SMA cross above 50-day SMA), held to expiration.

import numpy as np
from tcg_backtester.lib import options, signals
from tcg_backtester.lib.engine import (
    ExecutionConfig, run_option_strategy_from_spec,
)

UNDERLYING_NPZ = "data/SPX.npz"
CHAIN_PKL = "data/chain_SPX.pkl"
EXPIRY = 20240315
SPOT_HINT = 4500.0
CAPITAL = 250_000.0
NEAR_STRIKE = SPOT_HINT          # ATM long
FAR_STRIKE = SPOT_HINT * 1.05    # 5% OTM short


def legs_builder(spot):
    return options.vertical(
        side="long", option_type="C",
        near_strike=NEAR_STRIKE, far_strike=FAR_STRIKE,
        expiry=EXPIRY, spot_hint=SPOT_HINT, qty_units=1,
    )


def signal_builder(close):
    sma20 = signals.sma(close, 20)
    sma50 = signals.sma(close, 50)
    raw = (sma20 > sma50).astype(np.float64)
    # NaN warm-up positions emit False (0.0) which is the safe default.
    return np.where(np.isnan(sma20) | np.isnan(sma50), 0.0, raw)


result = run_option_strategy_from_spec(
    underlying_npz=UNDERLYING_NPZ,
    chain_pkl=CHAIN_PKL,
    legs_builder=legs_builder,
    signal_builder=signal_builder,
    execution=ExecutionConfig(fees_bps=5.0, slippage_bps=5.0),
    capital_base=CAPITAL,
)
print(f"vertical: terminal equity={result.equity_curve[-1]:.0f}, "
      f"trades={len(result.trades)}, unfilled={len(result.meta.get('unfilled_legs', []))}")
