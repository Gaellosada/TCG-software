# simple_sma_cross — SMA 50/200 crossover on SPY

Long SPY when the 50-day SMA crosses above the 200-day SMA; flat otherwise.
This is the canonical *simple* example: META at the top, five-line
`compute_signal` body, no escape hatch, no extra plots. It exercises the
full pipeline (data load -> backtest -> probes -> notebook compile) on a
real Yahoo/MongoDB-backed instrument.

Expected smoke output:

```
Loaded 1,260 SPY bars 2020-01-02 to 2024-12-31, 0 gaps.
BacktestResult: equity 100,000 -> ~165,000, n_trades ~12, probes PASS.
Notebook: results/notebook.ipynb (strategy.py embedded verbatim).
```
