"""HMM regime-switch on SPY: 2-state Gaussian HMM on log-returns.

Fits a 2-state Gaussian HMM via :mod:`hmmlearn` to SPY's daily log
returns over the in-sample window, identifies the high-return state
(positive mean), then goes long when the most-recent posterior says
we're in that state and flat otherwise. Demonstrates a strategy that
imports a package outside the project's pyproject.toml (``hmmlearn``)
— pre-flight `pip install -r requirements.txt` is what makes it work.

The fit uses only data up to and including bar ``i`` to compute the
state at bar ``i`` (causal walk-forward); the engine still applies its
own one-bar shift on top. Refit cadence is monthly to keep the cost
reasonable while preserving regime-tracking.
"""

META = {
    "slug": "novel-dep-hmm-regime",
    "description": "2-state Gaussian HMM regime-switch on SPY daily log returns.",
    "dates": {"start": "2015-01-01", "end": "2024-12-31"},
    "universe": ["SPY"],
    "benchmark": "SPY",
    "asset_class": "ETF",
    "sizing": {"method": "fixed_fraction", "fraction": 1.0},
    "execution": {"fees_bps": 5.0, "slippage_bps": 5.0, "fill_timing": "next_open"},
    "tags": ["hmm", "regime"],
    "seed": 42,
}

import numpy as np
from numpy.typing import NDArray

# hmmlearn declared in this workspace's requirements.txt; not in pyproject.toml.
from hmmlearn.hmm import GaussianHMM


def _log_returns(close: NDArray[np.float64]) -> NDArray[np.float64]:
    """Causal log returns; r[0] = 0 by convention."""
    out = np.zeros_like(close, dtype=np.float64)
    if close.shape[0] >= 2:
        prev = close[:-1]
        nxt = close[1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            out[1:] = np.where(prev > 0, np.log(nxt / prev), 0.0)
    return np.where(np.isfinite(out), out, 0.0)


def compute_signal(bars, ctx) -> NDArray[np.float64]:
    rets = _log_returns(bars.close)
    n = rets.shape[0]
    sig = np.zeros(n, dtype=np.float64)
    seed = int(ctx.meta.get("seed", 42))
    refit_every = 21         # ~ one trading month
    warmup = 252             # one trading year before first prediction
    last_state_high: int | None = None
    for i in range(warmup, n):
        if (i - warmup) % refit_every == 0 or last_state_high is None:
            window = rets[: i + 1].reshape(-1, 1)
            model = GaussianHMM(n_components=2, covariance_type="diag",
                                n_iter=50, random_state=seed)
            try:
                model.fit(window)
            except (ValueError, np.linalg.LinAlgError):
                # Bad numerical conditioning at this slice — keep prior label.
                continue
            means = model.means_.reshape(-1)
            last_state_high = int(np.argmax(means))
        # Predict state at bar i with the most recently fit model.
        states = model.predict(rets[: i + 1].reshape(-1, 1))
        sig[i] = 1.0 if int(states[-1]) == last_state_high else 0.0
    return sig
