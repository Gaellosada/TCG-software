# Purpose: short iron condor (sell call spread + sell put spread) with weekly entries
# triggered by low realised vol; held to expiration.

from tcg_backtester.lib import options, signals
from tcg_backtester.lib.engine import (
    ExecutionConfig, run_option_strategy_from_spec,
)

UNDERLYING_NPZ = "data/SPX.npz"
CHAIN_PKL = "data/chain_SPX.pkl"
EXPIRY = 20240315          # YYYYMMDD anchor; calendar in chain must include this.
SPOT_HINT = 4500.0         # rough current spot used to build strike-pct selectors
VOL_THRESHOLD = 0.15       # entry only when 20d realised vol < threshold
CAPITAL = 250_000.0


def legs_builder(spot):
    return options.iron_condor(
        short_call_strike=SPOT_HINT * 1.05,
        long_call_strike=SPOT_HINT * 1.10,
        short_put_strike=SPOT_HINT * 0.95,
        long_put_strike=SPOT_HINT * 0.90,
        expiry=EXPIRY, spot_hint=SPOT_HINT, qty_units=1,
    )


def signal_builder(close):
    vol = signals.rolling_vol(close, 20)
    import numpy as np
    return np.where(np.isnan(vol), 0.0, (vol < VOL_THRESHOLD).astype(float))


result = run_option_strategy_from_spec(
    underlying_npz=UNDERLYING_NPZ,
    chain_pkl=CHAIN_PKL,
    legs_builder=legs_builder,
    signal_builder=signal_builder,
    execution=ExecutionConfig(fees_bps=5.0, slippage_bps=5.0),
    capital_base=CAPITAL,
)
print(f"iron_condor: terminal equity={result.equity_curve[-1]:.0f}, "
      f"trades={len(result.trades)}, unfilled={len(result.meta.get('unfilled_legs', []))}")
