# complex_iron_condor — 4-leg SPX iron condor, 30-DTE

Short call at ATM+5%, long call at ATM+10%, short put at ATM-5%, long put
at ATM-10%, all at the nearest 30-DTE expiry. The strategy uses the
`run`-shape escape hatch because options + multi-leg construction is
naturally driven from a custom `BacktestSpec`. Legs are built via
`lib.options.build_legs` (no hand-constructed `OptionLegSpec` dicts).
`EXTRA_PLOTS` adds a static payoff diagram next to the baseline plots.

Expected smoke output:

```
Loaded 1,260 SPX bars 2020-01-02 to 2024-12-31.
build_legs: 4 OptionLegSpec entries (short_call, long_call, short_put, long_put).
Notebook: results/notebook.ipynb (BASELINE_PLOTS + iron_condor_payoff.json).
```
