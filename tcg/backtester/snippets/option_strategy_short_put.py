# Purpose: build a short-put position series from a chain (delta-target, DTE window, exit on DTE threshold).
#
# The chain pickle here is produced by `fetch_options_chain.py`, which now
# emits a FOCUSED chain (server-side strike band + expiration_cycle from
# `chain_args_from_spec(spec, spot_hint=...)`). For the canonical 4-year
# 10-delta short-put on SPX, the focused chain loads in <2 min vs ~21 min
# without strike-band push-down. Always pass `spot_hint` upstream — never
# `load_chain` with strike_min/strike_max=None on multi-year SPX.

from tcg_backtester.lib import options, data_load

CHAIN_PKL = "data/chain_SPX_20240102_20241231.pkl"
UNDERLYING_NPZ = "data/SPX.npz"
DTE_MIN = 30
DTE_MAX = 45
DELTA_TARGET = -0.20
EXIT_DTE = 7
CONTRACTS_PER_TRADE = 1

chain = options.load_chain_pkl(CHAIN_PKL)
spot = data_load.load_npz(UNDERLYING_NPZ)

position = options.short_put_series(
    chain=chain,
    spot=spot,
    dte_min=DTE_MIN,
    dte_max=DTE_MAX,
    delta_target=DELTA_TARGET,
    exit_dte=EXIT_DTE,
    contracts_per_trade=CONTRACTS_PER_TRADE,
)
options.save_position_pkl(position, "data/position_short_put.pkl")
print(f"short_put: {position.n_trades} trades, avg_dte_at_entry={position.avg_dte:.1f}")

# Edit points:
#   1. CHAIN_PKL           — chain cache produced by fetch_options_chain (pickle)
#   2. UNDERLYING_NPZ      — spot/index bar npz
#   3. DTE_MIN/MAX, DELTA_TARGET, EXIT_DTE
#   4. CONTRACTS_PER_TRADE — sizing in number of contracts
#
# Daily-rebalance variant (engine-style, not options.short_put_series):
#   When you want the engine's full OptionLegSpec machinery driving entries
#   every bar (`exit_rule: days_to_hold n=1`), pair the leg with
#   `lib.signals.daily_pulse(n_bars)` as the entry signal. A constant
#   `signal=np.ones(N)` will only fire ONCE — the engine triggers entry on
#   0->nonzero transitions or sign changes.
#
#       from tcg_backtester.lib.signals import daily_pulse
#       signal = daily_pulse(n_bars=len(bars.dates))
#       spec = BacktestSpec(..., signal=signal,
#                           sizing=SizingConfig(method="fixed_fraction", fraction=0.0),
#                           option_legs=[OptionLegSpec(...,
#                               exit_rule=DaysToHold(n=1))])
