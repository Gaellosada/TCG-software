# novel_dependency_hmm_regime — 2-state HMM regime-switch on SPY

A 2-state Gaussian HMM (`hmmlearn`) is fit on SPY daily log returns;
the strategy goes long when the most-recent posterior says we're in
the high-return state and flat otherwise. Refit monthly. This example
demonstrates a strategy that pulls in a Python package outside the
project's `pyproject.toml` — `hmmlearn` is declared in this workspace's
`requirements.txt`, and pre-flight `pip install -r requirements.txt`
makes it importable.

Expected smoke output:

```
pip install -r requirements.txt: hmmlearn-0.3.x installed.
Loaded 2,520 SPY bars 2015-01-02 to 2024-12-31.
HMM fit (refit every 21 bars), state-label PASS, equity computed.
```
