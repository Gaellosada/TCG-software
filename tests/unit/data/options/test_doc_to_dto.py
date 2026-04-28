"""Pure-function unit tests for ``tcg.data.options._doc_to_dto``.

No Mongo. Synthetic dicts only — see ``conftest.py``.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data.options._doc_to_dto import (
    _compute_mid,
    _normalize_type,
    _parse_yyyymmdd,
    bar_and_greek_to_row,
    doc_to_contract,
    index_greeks_by_date,
)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


class TestParseYYYYMMDD:
    def test_int(self):
        assert _parse_yyyymmdd(20240315) == date(2024, 3, 15)

    def test_string_int(self):
        assert _parse_yyyymmdd("20240315") == date(2024, 3, 15)

    def test_iso_string(self):
        assert _parse_yyyymmdd("2024-03-15T00:00:00Z") == date(2024, 3, 15)

    def test_none(self):
        assert _parse_yyyymmdd(None) is None

    def test_garbage(self):
        assert _parse_yyyymmdd("not-a-date") is None

    def test_float(self):
        assert _parse_yyyymmdd(20240315.0) == date(2024, 3, 15)

    def test_out_of_range(self):
        assert _parse_yyyymmdd(99990101) is None


# ---------------------------------------------------------------------------
# Type normalization
# ---------------------------------------------------------------------------


class TestNormalizeType:
    def test_lowercase_c(self):
        assert _normalize_type("c") == "C"

    def test_lowercase_p(self):
        assert _normalize_type("p") == "P"

    def test_uppercase_passthrough(self):
        assert _normalize_type("C") == "C"
        assert _normalize_type("P") == "P"

    def test_whitespace(self):
        assert _normalize_type(" C ") == "C"

    def test_invalid(self):
        assert _normalize_type("X") is None
        assert _normalize_type("") is None
        assert _normalize_type(None) is None
        assert _normalize_type(42) is None


# ---------------------------------------------------------------------------
# Mid computation rule (guardrail #4)
# ---------------------------------------------------------------------------


class TestComputeMid:
    def test_both_positive(self):
        assert _compute_mid(2.0, 2.1) == pytest.approx(2.05)

    def test_bid_missing(self):
        assert _compute_mid(None, 2.1) is None

    def test_ask_missing(self):
        assert _compute_mid(2.0, None) is None

    def test_both_missing(self):
        assert _compute_mid(None, None) is None

    def test_zero_bid(self):
        assert _compute_mid(0.0, 2.1) is None

    def test_zero_ask(self):
        assert _compute_mid(2.0, 0.0) is None

    def test_both_zero(self):
        assert _compute_mid(0.0, 0.0) is None

    def test_negative(self):
        # bid<0 is non-physical for an option quote — treat as missing.
        assert _compute_mid(-0.1, 2.1) is None


# ---------------------------------------------------------------------------
# doc_to_contract
# ---------------------------------------------------------------------------


class TestDocToContract:
    def test_full_sp500(self, sp500_doc):
        contract = doc_to_contract(sp500_doc, "OPT_SP_500", "IVOLATILITY")
        assert contract is not None
        assert contract.collection == "OPT_SP_500"
        assert contract.expiration == date(2024, 3, 15)
        assert contract.strike == 5000.0
        assert contract.type == "C"
        assert contract.expiration_cycle == "M"
        assert contract.root_underlying == "IND_SP_500"
        assert contract.underlying_ref == "FUT_SP_500_EMINI_20240315"
        assert contract.underlying_symbol == "ES"
        assert contract.contract_size == 50.0
        assert contract.currency == "USD"
        assert contract.provider == "IVOLATILITY"
        # Strike factor flag from config:
        assert contract.strike_factor_verified is True

    def test_vix_lowercase_type_normalized(self, vix_doc):
        contract = doc_to_contract(vix_doc, "OPT_VIX", "CBOE")
        assert contract is not None
        assert contract.type == "P"
        assert contract.expiration_cycle == "W"
        assert contract.strike_factor_verified is True

    def test_t_note_unverified(self, t_note_doc):
        contract = doc_to_contract(t_note_doc, "OPT_T_NOTE_10_Y", "IVOLATILITY")
        assert contract is not None
        assert contract.strike_factor_verified is False

    def test_missing_expiration_returns_none(self, sp500_doc):
        sp500_doc["expiration"] = None
        assert doc_to_contract(sp500_doc, "OPT_SP_500", "IVOLATILITY") is None

    def test_missing_strike_returns_none(self, sp500_doc):
        sp500_doc["strike"] = None
        assert doc_to_contract(sp500_doc, "OPT_SP_500", "IVOLATILITY") is None

    def test_invalid_type_returns_none(self, sp500_doc):
        sp500_doc["type"] = "X"
        assert doc_to_contract(sp500_doc, "OPT_SP_500", "IVOLATILITY") is None

    def test_btc_no_underlying_field(self, btc_doc):
        contract = doc_to_contract(btc_doc, "OPT_BTC", "INTERNAL")
        assert contract is not None
        assert contract.underlying_ref is None
        assert contract.underlying_symbol is None
        assert contract.root_underlying == "BTC"


# ---------------------------------------------------------------------------
# bar_and_greek_to_row
# ---------------------------------------------------------------------------


class TestBarAndGreekToRow:
    def test_full_row(self, make_bar, make_greek_full):
        bar = make_bar(20240301, bid=2.0, ask=2.1)
        greek = make_greek_full(20240301)
        row = bar_and_greek_to_row(bar, greek)
        assert row is not None
        assert row.date == date(2024, 3, 1)
        assert row.bid == 2.0
        assert row.ask == 2.1
        assert row.mid == pytest.approx(2.05)
        assert row.iv_stored == 0.20
        assert row.delta_stored == 0.50
        assert row.gamma_stored == 0.01
        assert row.theta_stored == -0.05
        assert row.vega_stored == 0.10
        assert row.underlying_price_stored == 100.0

    def test_no_greek_means_all_stored_none(self, make_bar):
        bar = make_bar(20240301, bid=2.0, ask=2.1)
        row = bar_and_greek_to_row(bar, None)
        assert row is not None
        assert row.iv_stored is None
        assert row.delta_stored is None
        assert row.gamma_stored is None
        assert row.theta_stored is None
        assert row.vega_stored is None
        assert row.underlying_price_stored is None
        # mid still computed from quotes:
        assert row.mid == pytest.approx(2.05)

    def test_iv_minus_one_sentinel_treated_as_missing(self, make_bar):
        """Regression (2026-04-28): IVolatility uses ``-1.0`` as a "no IV"
        sentinel for deep-OTM rows where its solver did not converge.

        Without this guard, ~45% of OPT_SP_500 chain rows render as a
        spurious ``-1.0000`` IV in the UI. IV must be strictly positive;
        anything <= 0 surfaces as ``None`` so Module 6 can wrap it as
        ``source="missing"`` (or compute it on demand).
        """
        bar = make_bar(20240315, bid=0.0, ask=0.05)
        greek = {
            "date": 20240315,
            "impliedVolatility": -1.0,  # IVolatility sentinel
            "delta": 0.0,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "underlyingPrice": 5100.0,
        }
        row = bar_and_greek_to_row(bar, greek)
        assert row is not None
        assert row.iv_stored is None, "IV=-1.0 sentinel must surface as None"

    def test_iv_zero_treated_as_missing(self, make_bar):
        """IV = 0 is also unphysical. Treat as missing."""
        bar = make_bar(20240315, bid=1.0, ask=1.5)
        greek = {"date": 20240315, "impliedVolatility": 0.0}
        row = bar_and_greek_to_row(bar, greek)
        assert row is not None
        assert row.iv_stored is None

    def test_iv_positive_passes_through(self, make_bar):
        """Sanity: a real positive IV is preserved."""
        bar = make_bar(20240315, bid=1.0, ask=1.5)
        greek = {"date": 20240315, "impliedVolatility": 0.18}
        row = bar_and_greek_to_row(bar, greek)
        assert row is not None
        assert row.iv_stored == 0.18

    def test_missing_greek_field_stays_none_not_zero(self, make_bar, make_greek_sparse):
        """Sparse IVOLATILITY entry: delta missing → None, not 0."""
        bar = make_bar(20240301, bid=2.0, ask=2.1)
        greek = make_greek_sparse(20240301)
        row = bar_and_greek_to_row(bar, greek)
        assert row is not None
        assert row.delta_stored is None
        assert row.iv_stored == 0.20
        # gamma / vega were stored as 0.0 — that is the doc's value;
        # we surface what was stored, even if 0.0.
        assert row.gamma_stored == 0.0
        assert row.vega_stored == 0.0

    def test_bad_bar_date_returns_none(self, make_bar):
        bar = make_bar(20240301)
        bar["date"] = "garbage"
        row = bar_and_greek_to_row(bar, None)
        assert row is None

    def test_bid_missing_mid_none(self, make_bar):
        bar = make_bar(20240301, bid=None, ask=2.1)
        row = bar_and_greek_to_row(bar, None)
        assert row is not None
        assert row.bid is None
        assert row.ask == 2.1
        assert row.mid is None

    def test_zero_quotes_mid_none(self, make_bar):
        bar = make_bar(20240301, bid=0.0, ask=0.0)
        row = bar_and_greek_to_row(bar, None)
        assert row is not None
        assert row.mid is None

    def test_atm_moneyness_dte_not_surfaced(self, make_bar, make_greek_full):
        """Stored fields ``atTheMoney`` / ``moneyness`` / ``daysToExpiry``
        appear in the doc but must not appear on the DTO (guardrail #3).
        """
        bar = make_bar(20240301, bid=2.0, ask=2.1)
        greek = make_greek_full(20240301)
        # Sanity: the source greek dict contains the ignored fields.
        assert "atTheMoney" in greek
        assert "moneyness" in greek
        assert "daysToExpiry" in greek

        row = bar_and_greek_to_row(bar, greek)
        assert row is not None
        # OptionDailyRow has no such attribute — confirms via getattr default.
        assert getattr(row, "at_the_money", "missing") == "missing"
        assert getattr(row, "moneyness", "missing") == "missing"
        assert getattr(row, "days_to_expiry", "missing") == "missing"


# ---------------------------------------------------------------------------
# index_greeks_by_date
# ---------------------------------------------------------------------------


class TestIndexGreeksByDate:
    def test_empty_or_none(self):
        assert index_greeks_by_date(None) == {}
        assert index_greeks_by_date([]) == {}

    def test_normal(self, make_greek_full):
        idx = index_greeks_by_date([
            make_greek_full(20240301),
            make_greek_full(20240302),
        ])
        assert date(2024, 3, 1) in idx
        assert date(2024, 3, 2) in idx
        assert len(idx) == 2

    def test_skips_undated(self, make_greek_full):
        bad = {"impliedVolatility": 0.20}
        idx = index_greeks_by_date([bad, make_greek_full(20240301)])
        assert len(idx) == 1
        assert date(2024, 3, 1) in idx
