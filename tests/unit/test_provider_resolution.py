"""Unit tests for provider resolution logic.

Tests cover the full resolution chain:
1. Explicit provider found -> return it
2. Explicit provider not found -> raise DataNotFoundError
3. Config default exact match (INDEX -> YAHOO)
4. Config default prefix match (FUT_VIX -> IVOLATILITY)
5. Config default specific over prefix (FUT_BTC -> DERIBIT, not IVOLATILITY)
6. Config default not available -> fall through to first available
7. No config -> first available
8. Empty available -> raise DataNotFoundError
9. Single provider, no request -> return it
"""

from __future__ import annotations

import pytest

from tcg.data._providers import resolve_provider, PROVIDER_DEFAULTS
from tcg.types.errors import DataNotFoundError


class TestProviderResolution:

    def test_explicit_provider_found(self):
        """Explicit request matching an available provider returns it."""
        result = resolve_provider(
            "INDEX", ["YAHOO", "BLOOMBERG"], requested="BLOOMBERG"
        )
        assert result == "BLOOMBERG"

    def test_explicit_provider_not_found_raises(self):
        """Explicit request for unavailable provider raises DataNotFoundError."""
        with pytest.raises(DataNotFoundError, match="not available"):
            resolve_provider("INDEX", ["YAHOO"], requested="NONEXISTENT")

    def test_config_default_exact_match(self):
        """INDEX defaults to YAHOO per PROVIDER_DEFAULTS."""
        result = resolve_provider("INDEX", ["BLOOMBERG", "YAHOO"])
        assert result == "YAHOO"

    def test_config_default_prefix_match(self):
        """FUT_VIX matches FUT_ prefix -> IVOLATILITY."""
        result = resolve_provider("FUT_VIX", ["IVOLATILITY", "BLOOMBERG"])
        assert result == "IVOLATILITY"

    def test_config_default_specific_over_prefix(self):
        """FUT_BTC has exact match (DERIBIT), not prefix fallback (IVOLATILITY)."""
        result = resolve_provider(
            "FUT_BTC", ["DERIBIT", "IVOLATILITY", "BLOOMBERG"]
        )
        assert result == "DERIBIT"

    def test_config_default_not_available_falls_through(self):
        """Config says YAHOO for INDEX but only BLOOMBERG available -> first available."""
        result = resolve_provider("INDEX", ["BLOOMBERG"])
        assert result == "BLOOMBERG"

    def test_no_config_returns_first_available(self):
        """Unknown collection with no config default -> first available."""
        result = resolve_provider("UNKNOWN_COL", ["ALPHA", "BETA"])
        assert result == "ALPHA"

    def test_empty_available_raises(self):
        """Empty available_providers list raises DataNotFoundError."""
        with pytest.raises(DataNotFoundError, match="No providers available"):
            resolve_provider("INDEX", [])

    def test_single_provider_no_request(self):
        """Single provider available, no explicit request -> returns it."""
        result = resolve_provider("INDEX", ["BLOOMBERG"])
        assert result == "BLOOMBERG"

    def test_etf_default_yahoo(self):
        """ETF defaults to YAHOO."""
        result = resolve_provider("ETF", ["YAHOO", "BLOOMBERG"])
        assert result == "YAHOO"

    def test_opt_prefix_match(self):
        """OPT_VIX matches OPT_ prefix -> IVOLATILITY."""
        result = resolve_provider("OPT_VIX", ["IVOLATILITY", "YAHOO"])
        assert result == "IVOLATILITY"

    def test_opt_btc_exact_over_prefix(self):
        """OPT_BTC has exact match (DERIBIT), not prefix fallback."""
        result = resolve_provider("OPT_BTC", ["DERIBIT", "IVOLATILITY"])
        assert result == "DERIBIT"

    def test_fund_default_bloomberg(self):
        """FUND defaults to BLOOMBERG."""
        result = resolve_provider("FUND", ["YAHOO", "BLOOMBERG"])
        assert result == "BLOOMBERG"

    def test_forex_default_yahoo(self):
        """FOREX defaults to YAHOO."""
        result = resolve_provider("FOREX", ["YAHOO", "BLOOMBERG"])
        assert result == "YAHOO"

    def test_explicit_request_overrides_default(self):
        """Even when config default exists, explicit request takes precedence."""
        # INDEX defaults to YAHOO, but explicit BLOOMBERG request wins
        result = resolve_provider(
            "INDEX", ["YAHOO", "BLOOMBERG"], requested="BLOOMBERG"
        )
        assert result == "BLOOMBERG"

    def test_provider_defaults_dict_is_not_empty(self):
        """Sanity: PROVIDER_DEFAULTS has entries."""
        assert len(PROVIDER_DEFAULTS) > 0
        assert "INDEX" in PROVIDER_DEFAULTS
