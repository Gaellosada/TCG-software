# Purpose: ATM calendar spread — short the near-term ATM call, long the far-term ATM call.
# Carry strategy: profit if realised vol stays low while front decays faster than back.

import numpy as np
from tcg_backtester.lib import options
from tcg_backtester.lib.engine import (
    ExecutionConfig, run_option_strategy_from_spec,
)

UNDERLYING_NPZ = "data/SPX.npz"
CHAIN_PKL = "data/chain_SPX.pkl"
NEAR_EXPIRY = 20240315
FAR_EXPIRY = 20240419
SPOT_HINT = 4500.0
CAPITAL = 250_000.0


def legs_builder(spot):
    return options.calendar(
        side="long",                  # "long" calendar = long the far, short the near
        option_type="C",
        strike=SPOT_HINT,
        near_expiry=NEAR_EXPIRY,
        far_expiry=FAR_EXPIRY,
        spot_hint=SPOT_HINT, qty_units=1,
    )


def signal_builder(close):
    # Always-on calendar carry signal: enter on the second bar, ride to far expiry.
    sig = np.zeros(close.shape[0], dtype=np.float64)
    sig[1:] = 1.0
    return sig


result = run_option_strategy_from_spec(
    underlying_npz=UNDERLYING_NPZ,
    chain_pkl=CHAIN_PKL,
    legs_builder=legs_builder,
    signal_builder=signal_builder,
    execution=ExecutionConfig(fees_bps=5.0, slippage_bps=5.0),
    capital_base=CAPITAL,
)
print(f"calendar: terminal equity={result.equity_curve[-1]:.0f}, "
      f"trades={len(result.trades)}, "
      f"open_at_end={result.meta.get('open_legs_at_end', [])}")
