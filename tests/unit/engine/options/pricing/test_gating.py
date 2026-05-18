"""Unit tests for ``tcg.engine.options.pricing._gating``.

After Phase 2 of the VIX greeks rollout the gating module exposes two
public helpers used by ``DefaultOptionsPricer``:

- ``is_blocked_root(collection)`` — structurally-blocked roots. OPT_VIX
  was removed in Phase 2 (it now reaches the compute path with a
  resolved FUT_VIX forward); OPT_ETH stays blocked.
- ``missing_underlying_error(collection)`` — per-root override for the
  "underlying price not available" branch. OPT_VIX returns
  ``missing_forward_vix_curve`` (the forward curve is the missing
  input, not the spot underlying); every other root returns the generic
  ``missing_underlying_price``.
"""

from __future__ import annotations

from tcg.engine.options.pricing._gating import (
    is_blocked_root,
    missing_underlying_error,
)


class TestIsBlockedRoot:
    def test_opt_vix_no_longer_unconditionally_blocked(self) -> None:
        """Phase 2 lifted the structural block on OPT_VIX. The Greeks now
        compute when a forward is resolved; the missing-forward case is
        routed through ``missing_underlying_error`` instead.
        """
        blocked, code, missing = is_blocked_root("OPT_VIX")
        assert blocked is False
        assert code is None
        assert missing == ()

    def test_opt_eth_still_blocked(self) -> None:
        blocked, code, missing = is_blocked_root("OPT_ETH")
        assert blocked is True
        assert code == "missing_deribit_feed"
        assert missing == ("underlying_price",)

    def test_opt_sp_500_not_blocked(self) -> None:
        blocked, code, missing = is_blocked_root("OPT_SP_500")
        assert blocked is False
        assert code is None
        assert missing == ()


class TestMissingUnderlyingError:
    def test_opt_vix_returns_forward_vix_curve(self) -> None:
        """The Phase 2 path: OPT_VIX with no resolved forward surfaces
        the more-specific ``missing_forward_vix_curve`` rather than the
        generic ``missing_underlying_price``.
        """
        code, missing = missing_underlying_error("OPT_VIX")
        assert code == "missing_forward_vix_curve"
        assert missing == ("forward_vix_curve",)

    def test_default_returns_missing_underlying_price(self) -> None:
        code, missing = missing_underlying_error("OPT_SP_500")
        assert code == "missing_underlying_price"
        assert missing == ("underlying_price",)

    def test_opt_gold_default(self) -> None:
        code, missing = missing_underlying_error("OPT_GOLD")
        assert code == "missing_underlying_price"
        assert missing == ("underlying_price",)
