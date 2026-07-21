"""Parity + overflow guards for the shared delta-rank reference (audit_d3 INV-1/2/3).

``symbol_delta_rank`` (in ``tcg.data._sql.options``) is the SINGLE Python encoding
of the SQL delta pushdown's symbol rank.  These tests BIND it to
``match_by_delta`` — the load-bearing invariant that the pushdown's byte-identity
rests on (real SQL rank ≡ ``match_by_delta``'s pick), which previously had NO
in-tree guard: the SQL string tests compute nothing and the engine fakes carried
their own third copy of the rank.

The primary-metric parity test MUST go RED if someone changes
``match_by_delta``'s PRIMARY distance metric (e.g. drops the ``abs`` or switches
to signed distance) without updating the rank; a tie-break-only change stays
green (the chain is built with a UNIQUE closest symbol so no boundary tie exists).
"""

from __future__ import annotations

from datetime import date

from tcg.data._sql.options import _pushdown_overflow_groups, symbol_delta_rank
from tcg.engine.options.selection._match import match_by_delta
from tcg.types.options import OptionContractDoc, OptionDailyRow

_EXP = date(2024, 3, 15)
_D = date(2024, 2, 15)


def _c(contract_id: str, strike: float) -> OptionContractDoc:
    return OptionContractDoc(
        collection="OPT_SP_500",
        contract_id=contract_id,
        root_underlying="IND_SP_500",
        underlying_ref=None,
        underlying_symbol="SPX",
        expiration=_EXP,
        expiration_cycle="M",
        strike=float(strike),
        type="P",
        contract_size=100.0,
        currency="USD",
        provider="IVOL",
        strike_factor_verified=True,
    )


def _r(delta: float | None) -> OptionDailyRow:
    return OptionDailyRow(
        date=_D,
        open=None,
        high=None,
        low=None,
        close=None,
        bid=1.0,
        ask=1.5,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=None,
        mid=1.25,
        iv_stored=0.25,
        delta_stored=delta,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=5000.0,
    )


_TARGET = -0.10


# A chain WITH: a duplicate-instrument_id symbol (two rows, one contract_id), a
# NULL-delta symbol, and a UNIQUE closest symbol (dist 0 → no boundary tie, so
# the parity assertion is immune to tie-break edits).  Dropping the ``abs`` in
# match_by_delta's primary key would flip its winner to "P-0.30" (signed distance
# -0.20 < 0.0) while the reference stays "P-0.10" → the parity test goes RED.
def _chain() -> list[tuple[OptionContractDoc, OptionDailyRow]]:
    return [
        (_c("C+0.05", 5200.0), _r(0.05)),  # dist 0.15
        (_c("P-0.30", 4000.0), _r(-0.30)),  # dup instrument_ids of one symbol
        (_c("P-0.30", 4000.0), _r(-0.32)),  # best_dist 0.20
        (_c("P-0.11", 4310.0), _r(-0.11)),  # dist 0.01
        (_c("P-0.10", 4300.0), _r(-0.10)),  # dist 0.00 → UNIQUE winner
        (_c("P-null", 4500.0), _r(None)),  # NULLS LAST
    ]


def _match_winner(rows) -> str | None:
    res = match_by_delta(
        rows,
        [r.delta_stored for _c, r in rows],
        target=_TARGET,
        tolerance=1.0,
        strict=False,
        chain_size=len(rows),
    )
    return None if res.contract is None else res.contract.contract_id


class TestReferenceMatchesMatchByDelta:
    def test_rank1_symbol_equals_match_by_delta_winner(self):
        rows = _chain()
        top1 = symbol_delta_rank(rows, _TARGET, k=1)
        rank1_symbols = {c.contract_id for c, _r in top1}
        assert len(rank1_symbols) == 1
        # PRIMARY-metric coupling: the reference's rank-1 symbol IS the contract
        # match_by_delta picks.  A change to the PRIMARY distance metric on either
        # side (dropping abs / signed / relative-with-reorder) diverges these.
        assert rank1_symbols == {_match_winner(rows)} == {"P-0.10"}

    def test_winner_always_retained_in_topk(self):
        rows = _chain()
        winner = _match_winner(rows)
        for k in (1, 2, 3, 8):
            kept = {c.contract_id for c, _r in symbol_delta_rank(rows, _TARGET, k)}
            assert winner in kept, f"winner {winner} dropped at k={k}"

    def test_duplicate_instrument_ids_of_kept_symbol_all_returned(self):
        """Symbol-granular: every row of a retained symbol surfaces (the ~2.68%
        dup-instrument_id quirk) so _row_for_contract's first-pick is byte-stable."""
        rows = _chain()
        # Ranked by best_dist: P-0.10 (0.0), P-0.11 (0.01), C+0.05 (0.15),
        # P-0.30 (0.20), P-null (last).  k=4 retains P-0.30 → both dup rows appear.
        kept = symbol_delta_rank(rows, _TARGET, k=4)
        dup_rows = [(c, r) for c, r in kept if c.contract_id == "P-0.30"]
        assert len(dup_rows) == 2

    def test_all_null_delta_symbols_sort_last(self):
        rows = _chain()
        # With k=5 (all non-null symbols = 4), the NULL symbol is NOT retained.
        kept = {c.contract_id for c, _r in symbol_delta_rank(rows, _TARGET, k=4)}
        assert "P-null" not in kept
        # match_by_delta over ONLY the null symbol → missing_delta_no_compute.
        null_only = [(c, r) for c, r in rows if c.contract_id == "P-null"]
        res = match_by_delta(
            null_only,
            [r.delta_stored for _c, r in null_only],
            target=_TARGET,
            tolerance=1.0,
            strict=False,
            chain_size=1,
        )
        assert res.error_code == "missing_delta_no_compute"


class TestPushdownOverflowDetection:
    """audit_d3 INV-2/3: >k symbols sharing the rank-1 (best_dist, best_strike)
    pair is the ONE regime the SQL ``option_symbol`` tie-break could resolve
    differently from match_by_delta's instrument_id order.  The detector runs on a
    ``srn <= k+1`` fetch: an overflow group has ≥ k+1 symbols whose (k+1)th shares
    the rank-1 (dist, strike); non-overflow groups that fetched k+1 symbols mark
    the surplus one for discard (byte-identity with top-k)."""

    def test_overflow_detected_when_kplus1th_shares_dist_and_strike(self):
        # k=1, two symbols at the SAME dist AND the SAME strike -> the ONLY regime
        # where top-k can drop match_by_delta's pick (only the tertiary key differs:
        # SQL option_symbol vs match_by_delta's instrument_id order).
        results = {
            _D: [
                (_c("P-A", 4300.0), _r(_TARGET)),
                (_c("P-B", 4300.0), _r(_TARGET)),
            ]
        }
        overflow, drop = _pushdown_overflow_groups(results, _TARGET, k=1)
        assert overflow == {(_EXP, _D)}
        assert drop == {}

    def test_no_overflow_when_tie_has_distinct_strikes(self):
        # k=1, SAME dist but DISTINCT strikes (the expiry-day delta≈0 regime):
        # match_by_delta AND SQL both pick the lowest strike -> NOT an overflow,
        # the surplus higher-strike symbol is simply discarded (speedup preserved).
        results = {
            _D: [
                (_c("P-lo", 4200.0), _r(_TARGET)),  # dist 0.00, low strike -> rank-1
                (_c("P-hi", 4400.0), _r(_TARGET)),  # dist 0.00, high strike -> surplus
            ]
        }
        overflow, drop = _pushdown_overflow_groups(results, _TARGET, k=1)
        assert overflow == set()
        assert drop == {(_EXP, _D): "P-hi"}  # the higher-strike surplus symbol

    def test_no_overflow_when_kplus1th_farther(self):
        # k=1, the (k+1)th symbol is strictly farther -> discard it, not overflow.
        results = {
            _D: [
                (_c("P-A", 4200.0), _r(_TARGET)),  # dist 0.00 (rank-1)
                (_c("P-B", 4400.0), _r(_TARGET + 0.05)),  # dist 0.05 (surplus)
            ]
        }
        overflow, drop = _pushdown_overflow_groups(results, _TARGET, k=1)
        assert overflow == set()
        assert drop == {(_EXP, _D): "P-B"}  # the farther, alphabetically-later one

    def test_no_overflow_when_only_k_symbols(self):
        # Only k symbols returned (no surplus (k+1)th fetched) -> nothing to judge.
        results = {_D: [(_c("P-A", 4200.0), _r(_TARGET))]}
        overflow, drop = _pushdown_overflow_groups(results, _TARGET, k=1)
        assert overflow == set()
        assert drop == {}

    def test_all_null_rank1_never_overflow(self):
        # A rank-1 best_dist of None (all-NULL delta) has no real winner.
        results = {
            _D: [
                (_c("P-A", 4200.0), _r(None)),
                (_c("P-B", 4400.0), _r(None)),
            ]
        }
        overflow, _drop = _pushdown_overflow_groups(results, _TARGET, k=1)
        assert overflow == set()
