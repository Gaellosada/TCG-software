# Inconsistency Probes Catalog — Intake Phase

## Policy

Probe firing mechanics, ask-limit (max 3 per round), and dismissal behavior: see `pipeline/01-intake.md` § Probe firing rule and § Probe execution.

---

## Probes

### 1. window_exceeds_history
- **Category:** time / data
- **Detection heuristic:**
  ```
  longest_window = max(spec.indicator_windows or [0])
  bars_available = len(data.dates_in_range)
  fire if longest_window * 1.25 > bars_available
  ```
  (1.25x buffer: an N-bar MA needs N bars warm-up plus enough out-of-warmup signal.)
- **Probe message:** "Your strategy uses a {N}-day window but only {M} days of data fit your range. Shrink the window, extend the range, or accept a tiny test sample?"
- **If user dismisses:** Keep window, truncate effective backtest start to first bar where window is filled, log truncation in `ASSUMPTIONS.json`.

### 2. fees_dominate_edge
- **Category:** fees / sizing
- **Detection heuristic:** the spec stores the per-trade edge target as a
  fraction (`signals.legs[0].target_return_per_trade`, e.g. `0.005` = 50 bps),
  while fees and slippage are in bps. Convert the target to bps before
  comparing.
  ```
  legs = (spec.signals or {}).get("legs") or []
  tgt  = legs[0].get("target_return_per_trade") if legs else None
  target_per_trade_bps = (tgt * 10_000) if tgt is not None else None

  rt_cost_bps = 2 * (spec.execution.fees_bps + spec.execution.slippage_bps)  # round-trip
  fire if target_per_trade_bps is not None \
       and rt_cost_bps >= 0.5 * target_per_trade_bps
  ```
- **Probe message:** "Round-trip costs are about {C} bps but your target per-trade edge is only {E} bps. Costs will eat at least half the edge — confirm fees, raise the target, or trade less often?"
- **If user dismisses:** Run with declared fees, flag in report header as "cost-dominated regime".

### 3. rebalance_signal_frequency_mismatch
- **Category:** execution
- **Detection heuristic:**
  ```
  # Concrete: measure how often the realized signal series actually changes
  # value in the backtest window, then cross-check against the configured
  # rebalance frequency. No "estimate_signal_period_days" hand-wave.
  changes = np.where(np.diff(spec.signal_array) != 0)[0]
  if len(changes) < 2:
      period_days = float('inf')      # no churn -> no mismatch
  else:
      period_days = float(np.mean(np.diff(changes)))   # mean bars between flips
  rebalance_freq = spec.rebalance_freq                  # "daily"|"weekly"|"monthly"|... (lives on BacktestSpec, not ExecutionConfig)
  fire if rebalance_freq == "daily"   and period_days >= 5   # wasteful: rebal faster than signal
  fire if rebalance_freq == "monthly" and period_days <= 2   # stale: signal flips faster than rebal
  ```
  The two branches catch the two failure modes separately so the message can
  explain *why* (wasteful vs stale) rather than just "mismatch".
- **Probe message:** "Signal updates roughly every {S} days but you rebalance every {R} days — that's a {ratio}x mismatch. Match the rebalance to the signal cadence?"
- **If user dismisses:** Keep configured cadences, log "stale_or_wasteful_rebalance" tag.

### 4. direction_spec_vs_signal
- **Category:** signals
- **Detection heuristic:**
  ```
  if spec.direction == "long_only":
      fire if signal_can_be_negative(spec.signal_expr)  # parse weight ∈ [-100,+100]
  if spec.direction == "short_only":
      fire if signal_can_be_positive(spec.signal_expr)
  ```
- **Probe message:** "You said {direction} but the signal can produce {opposite-side} positions. Clip negatives to zero, flip to long/short, or change the direction setting?"
- **If user dismisses:** Apply hard clip on the disallowed side, log clipped-bar count.

### 5. date_range_invalid
- **Category:** time
- **Detection heuristic:**
  ```
  start, end = spec.date_range.start, spec.date_range.end
  fire if (start > end) or (start == end) \
       or weekday(start) in {5,6} or weekday(end) in {5,6} \
       or end > today_yyyymmdd() \
       or not is_valid_yyyymmdd(start) or not is_valid_yyyymmdd(end)
  ```
- **Probe message:** "Date range {start} to {end} looks off — {reason: reversed / weekend / future / malformed}. What range did you intend?"
- **If user dismisses:** Snap to nearest valid trading days inside available data, log adjustment.

### 6. universe_signal_underlying_mismatch
- **Category:** universe
- **Detection heuristic:**
  ```
  traded   = set(spec.universe.symbols)               # what we trade
  referenced = extract_symbols(spec.signal_expr)      # what the signal reads
  fire if not referenced.issubset(traded) and not spec.cross_asset_explicit
  ```
- **Probe message:** "Your signal references {ref_symbols} but you only trade {traded_symbols}. Add the referenced symbols to the universe, or confirm this is a cross-asset signal?"
- **If user dismisses:** Treat as cross-asset, fetch referenced symbols read-only, log "cross_asset_inferred".

### 7. capital_vs_trade_size
- **Category:** sizing
- **Detection heuristic:**
  ```
  notional_per_trade = spec.sizing.notional_target       # absolute USD
  fire if notional_per_trade > spec.capital_base * 1.05  # >100% of capital + 5% buffer
       or notional_per_trade < spec.capital_base * 0.001 # <0.1% — likely typo
  ```
- **Probe message:** "Trade size {N} vs capital {C} — {ratio}% of capital per trade. Is that intentional or a units mistake?"
- **If user dismisses:** Cap notional at capital_base, log clamp.

### 8. lookahead_no_shift
- **Category:** methodology
- **Detection heuristic:**
  ```
  fire if spec.lookahead_shift == 0 and any(
      ind.uses_close_of_bar_t for ind in spec.indicators
  ) and spec.fill_timing == "same_bar_close"
  ```
- **Probe message:** "Your signal reads bar {t}'s close and fills at the same bar's close — that uses information you wouldn't have live. Shift fills to {t+1} open?"
- **If user dismisses:** Force `lookahead_shift = 1` (default policy is t→t+1), log override.

### 9. survivorship_implicit
- **Category:** universe / data
- **Detection heuristic:**
  ```
  fire if spec.universe.definition_type in {"current_top_N", "current_index_members"} \
       and spec.date_range.start < today_yyyymmdd() - 365
  ```
- **Probe message:** "Your universe is defined as today's top {N} — backtesting backwards on it builds in survivorship bias. Use a point-in-time membership snapshot, or accept the bias?"
- **If user dismisses:** Proceed with as-of-today universe, tag report with "survivorship_present: true".

### 10. slippage_vs_liquidity
- **Category:** execution / fees
- **Detection heuristic:**
  ```
  for sym in spec.universe.symbols:
      adv = data[sym].avg_dollar_volume_60d
      trade_notional = spec.sizing.notional_target
      participation = trade_notional / adv
      fire if participation > 0.05 and spec.slippage_bps < 10
  ```
- **Probe message:** "On {symbol}, each trade is {p}% of average daily volume but slippage is set to {S} bps. That's optimistic for an illiquid name — raise slippage?"
- **If user dismisses:** Keep slippage, flag affected symbols in report's caveats section.

### 11. missing_risk_controls_high_leverage
- **Category:** sizing / methodology
- **Detection heuristic:**
  ```
  leverage = spec.sizing.gross_exposure / spec.capital_base
  fire if leverage > 1.5 and spec.stop_loss is None and spec.max_drawdown_kill is None
  ```
- **Probe message:** "Strategy runs at {L}x gross with no stop-loss or drawdown kill. One bad day could wipe the book — add a risk control or confirm bare-bones?"
- **If user dismisses:** No stops applied, add "no_risk_controls" warning to report header.

### 12. walkforward_oos_missing
- **Category:** methodology
- **Detection heuristic:**
  ```
  has_param_search = bool(spec.parameter_grid)
  has_oos_split    = spec.oos_start is not None
  fire if has_param_search and not has_oos_split
  ```
- **Probe message:** "You're searching over {K} parameter combinations but have no out-of-sample split. The reported metrics will overfit. Reserve the last 25% of dates for OOS?"
- **If user dismisses:** Run in-sample only, brand report "in_sample_only", do not emit Sharpe in headline.

### 13. risk_free_rate_unset
- **Category:** methodology
- **Detection heuristic:**
  ```
  # Fire if the requested date range OVERLAPS the post-ZIRP window (>= 2022-01-01)
  # AND the user has not set a non-default risk_free_rate.
  # `risk_free_rate` lives on `ExecutionConfig` (default 0.0). The probe reads
  # the YAML directly because the dataclass default would otherwise look the
  # same as a user-set 0.0; we fire whenever the field is absent from the
  # source YAML or explicitly left blank.
  start = spec.date_range.start
  end   = spec.date_range.end
  yaml_rf = spec_yaml.get("execution", {}).get("risk_free_rate", None)
  fire if (end >= 20220101) and (start <= 20251231) and (yaml_rf is None)
  ```
- **Probe message:** "Sharpe assumes a risk-free rate; you left it blank for a period when rates were nonzero. Use a constant {x}% or pull a series, or accept r=0?"
- **If user dismisses:** Use `r=0`, log assumption with high-visibility flag in metrics block.

### 14. calendar_mismatch
- **Category:** data
- **Detection heuristic:**
  ```
  asset_calendar    = spec.calendar                 # e.g. "NYSE"
  required_calendar = INFER_CALENDAR[spec.universe.asset_class]
  # FUT_VIX uses CFE; FX uses 24/5; crypto 24/7; OPT_SP_500 follows CBOE
  fire if asset_calendar != required_calendar
  ```
- **Probe message:** "You set the calendar to {asset_calendar} but you're trading {asset_class}, which trades on {required_calendar}. Switch calendars?"
- **If user dismisses:** Use required_calendar, override spec, log forced switch.

### 15. dte_filter_empty
- **Category:** universe / data (options-specific)
- **Detection heuristic:**
  ```
  matched = data.options_chain.filter(
      dte_min=spec.dte_min, dte_max=spec.dte_max,
      moneyness=spec.moneyness, root=spec.root
  )
  fire if len(matched) == 0
  ```
- **Probe message:** "No option contracts matched DTE {min}–{max} on {root}. Widen the DTE window, change the underlying, or pick another expiry rule?"
- **If user dismisses:** Halt — this is a data-empty error, not a soft default. Surface diagnostic and exit intake.

### 16. delta_target_no_greeks
- **Category:** data (options-specific)
- **Detection heuristic:**
  ```
  fire if spec.option_selection.method == "delta_target" and (
      data.options_chain.has_iv is False
      or data.options_chain.has_greeks is False
  )
  ```
- **Probe message:** "You want to target delta {d}, but the option data has no IV/Greek field. Compute Greeks from BS with our {r,q} defaults, or pick by strike instead?"
- **If user dismisses:** Compute Greeks via py_vollib using spec defaults (r=0, q=0), log assumption.

### 17. short_dated_options_no_roll
- **Category:** execution (options-specific)
- **Detection heuristic:**
  ```
  fire if spec.universe.asset_class == "options" \
       and spec.dte_min < 10 \
       and spec.roll_logic is None \
       and (spec.date_range.end - spec.date_range.start) > 30  # YYYYMMDD diff approx
  ```
- **Probe message:** "Trading sub-10-DTE options over {N} days with no roll rule means positions just expire. Roll at {x} DTE, or hold to expiry every time?"
- **If user dismisses:** Hold to expiry, settle at intrinsic value, log behavior.

### 18. exercise_style_mismatch
- **Category:** methodology (options-specific)
- **Detection heuristic:**
  ```
  fire if spec.universe.asset_class == "options" \
       and spec.pricing_model == "european" \
       and underlying_is_american_style(spec.root)  # SPY/most equities
  ```
- **Probe message:** "You priced these as European but {root} options are American. Use American (binomial) pricing, or accept European as a small-error approximation?"
- **If user dismisses:** Stick with European pricing, log expected-error magnitude (small for OTM, larger for deep ITM).

### 19. zero_signal_after_filters
- **Category:** signals
- **Detection heuristic:**
  ```
  est_signal_count = simulate_signal_on_history(spec, dry_run=True).num_entries
  fire if est_signal_count == 0
       or est_signal_count < 5
  ```
- **Probe message:** "Your filters fire {K} times in the whole window — too few to learn from. Loosen a threshold, extend the range, or confirm the rare-event focus?"
- **If user dismisses:** Run anyway, mark report "low_sample_warning, n={K}".

### 20. benchmark_undefined
- **Category:** methodology
- **Detection heuristic:**
  ```
  fire if spec.benchmark is None and spec.universe.asset_class in {"equity","index","etf","options"}
  ```
- **Probe message:** "No benchmark set. Compare against buy-and-hold {default_benchmark}, or run benchmark-free?"
- **If user dismisses:** Use `SPX` for equity/index/options, `cash (r=0)` for futures/FX, log inference.

### 21. capital_currency_vs_instrument_currency
- **Category:** data / sizing
- **Detection heuristic:**
  ```
  cap_ccy = spec.capital_base.currency or "USD"
  inst_ccys = {data[s].quote_ccy for s in spec.universe.symbols}
  fire if len(inst_ccys) > 1 or (cap_ccy not in inst_ccys and len(inst_ccys) > 0)
  ```
- **Probe message:** "Capital is in {cap_ccy} but instruments quote in {inst_ccys}. Convert PnL to {cap_ccy} at daily FX, or treat as same-currency?"
- **If user dismisses:** Treat all as same currency (no FX conversion), log "fx_unconverted" warning.

### 22. instrument_lookup_failed
- **Category:** universe
- **Detection heuristic:**
  ```
  raw = extract_instrument_term(user_prompt)            # e.g. "Apple"
  resolved = lib.aliases.resolve_ticker(raw)            # case-fold + alias map
  fire if raw is not None and resolved is None
  ```
- **Probe message:** "Couldn't resolve `{user_term}` to a ticker. Did you mean `{closest_match}`?"
- **If user dismisses:** HALT P1 with a `PROBLEMS.md` entry "instrument lookup failed: `<user_term>`". No fall-through to a guessed ticker.

---

## Probe firing order (cascade)

When multiple probes match, ask only the first one in this priority. The downstream probes
re-evaluate after the user answers (or dismisses), so the cascade naturally drains.

1. **Universe** — probes 22, 6, 9, 14, 21
2. **Time / date integrity** — probes 5, 1
3. **Data integrity** — probes 15, 16
4. **Signals** — probes 4, 19
5. **Methodology** — probes 8, 12, 13, 18
6. **Execution** — probes 3, 10, 17, 20
7. **Sizing / fees** — probes 7, 11, 2

Within each group, ask in catalog order. After the user answers, re-run all probes;
only newly-or-still-firing probes can prompt further questions in the same intake round
(maximum 3 questions total per round to avoid interrogation fatigue — beyond 3, take
defaults for the rest and log them as `confidence: "low"`).
