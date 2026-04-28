"""Unit tests for ``DefaultOptionsChain.snapshot``.

Module 6 assembles a ``ChainSnapshot`` from raw rows: it widens stored
values to ``source="stored"``, optionally invokes the pricer for missing
Greeks, and computes K/S fresh from ``strike / underlying_price``.

Cardinal invariants under test:

- Module 6 NEVER calls Module 2 unless ``compute_missing=True``.
- Module 6 NEVER fabricates a ``source="computed"`` ComputeResult — only
  the pricer (Module 2) emits ``"computed"``.
- ``source="stored"`` widening is exclusive to Module 6.
- ``K_over_S`` uses the joined underlying price; never reads stored
  ``moneyness`` (guardrail #3).
- OPT_VIX with ``compute_missing=True`` cascades the gate — every Greek
  remains ``source="missing"`` (guardrail #6).
- Empty chain query → empty rows + a note.
- ``expiration_min > expiration_max`` raises ``OptionsValidationError``.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from tcg.engine.options.chain.chain import DefaultOptionsChain
from tcg.types.errors import OptionsValidationError
from tcg.types.options import (
    ComputedGreeks,
    ComputeResult,
    OptionContractDoc,
    OptionDailyRow,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_contract(
    *,
    collection: str = "OPT_SP_500",
    contract_id: str = "C1|M",
    strike: float = 100.0,
    type_: str = "C",
    expiration: date = date(2024, 6, 21),
    root_underlying: str = "IND_SP_500",
    underlying_ref: str | None = "FUT_SP_500_EMINI_20240621",
) -> OptionContractDoc:
    return OptionContractDoc(
        collection=collection,
        contract_id=contract_id,
        root_underlying=root_underlying,
        underlying_ref=underlying_ref,
        underlying_symbol="ES",
        expiration=expiration,
        expiration_cycle="M",
        strike=strike,
        type=type_,  # type: ignore[arg-type]
        contract_size=50.0,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _make_row(
    *,
    row_date: date = date(2024, 6, 14),
    bid: float | None = 1.0,
    ask: float | None = 1.5,
    mid: float | None = 1.25,
    iv_stored: float | None = 0.20,
    delta_stored: float | None = 0.50,
    gamma_stored: float | None = 0.01,
    theta_stored: float | None = -0.05,
    vega_stored: float | None = 0.10,
    underlying_price_stored: float | None = None,
) -> OptionDailyRow:
    return OptionDailyRow(
        date=row_date,
        open=None,
        high=None,
        low=None,
        close=None,
        bid=bid,
        ask=ask,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=100.0,
        mid=mid,
        iv_stored=iv_stored,
        delta_stored=delta_stored,
        gamma_stored=gamma_stored,
        theta_stored=theta_stored,
        vega_stored=vega_stored,
        underlying_price_stored=underlying_price_stored,
    )


def _make_data_port(
    pairs: list[tuple[OptionContractDoc, OptionDailyRow]],
) -> AsyncMock:
    port = AsyncMock()
    port.query_chain.return_value = pairs
    return port


def _make_index_port(value: float | None = None) -> AsyncMock:
    port = AsyncMock()
    port.get_index_value_on_date.return_value = value
    return port


def _make_futures_port(value: float | None = 100.0) -> AsyncMock:
    port = AsyncMock()
    port.get_futures_close_on_date.return_value = value
    return port


def _make_chain(
    *,
    data_port: AsyncMock,
    pricer: MagicMock | None = None,
    index_port: AsyncMock | None = None,
    futures_port: AsyncMock | None = None,
) -> DefaultOptionsChain:
    return DefaultOptionsChain(
        data_port=data_port,
        pricer=pricer if pricer is not None else MagicMock(),
        index_port=index_port if index_port is not None else _make_index_port(),
        futures_port=futures_port if futures_port is not None else _make_futures_port(),
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestSnapshotFullStored:
    """When all stored values are present, every Greek is source='stored'."""

    @pytest.mark.asyncio
    async def test_three_rows_all_stored(self) -> None:
        contracts = [_make_contract(contract_id=f"C{i}|M", strike=90.0 + 5 * i) for i in range(3)]
        rows = [_make_row() for _ in range(3)]
        data_port = _make_data_port(list(zip(contracts, rows)))
        pricer = MagicMock()
        chain = _make_chain(data_port=data_port, pricer=pricer)

        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
        )

        assert snap.root == "OPT_SP_500"
        assert snap.date == date(2024, 6, 14)
        assert len(snap.rows) == 3
        for cr in snap.rows:
            assert cr.iv.source == "stored"
            assert cr.delta.source == "stored"
            assert cr.gamma.source == "stored"
            assert cr.theta.source == "stored"
            assert cr.vega.source == "stored"
        # Pricer must NOT be called when all stored values are present
        # (and compute_missing defaults to False anyway).
        pricer.compute.assert_not_called()


class TestSnapshotMissingNoCompute:
    """compute_missing=False + missing stored → source='missing', error_code='not_stored'."""

    @pytest.mark.asyncio
    async def test_missing_delta_surfaces_not_stored(self) -> None:
        contract = _make_contract()
        row = _make_row(delta_stored=None)
        data_port = _make_data_port([(contract, row)])
        pricer = MagicMock()
        chain = _make_chain(data_port=data_port, pricer=pricer)

        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
            compute_missing=False,
        )

        assert len(snap.rows) == 1
        assert snap.rows[0].delta.source == "missing"
        assert snap.rows[0].delta.error_code == "not_stored"
        assert snap.rows[0].delta.value is None
        # Other Greeks still 'stored'.
        assert snap.rows[0].iv.source == "stored"
        # Pricer still not invoked.
        pricer.compute.assert_not_called()


class TestSnapshotMissingWithCompute:
    """compute_missing=True + missing stored → pricer called → source='computed'."""

    @pytest.mark.asyncio
    async def test_missing_delta_filled_by_pricer(self) -> None:
        contract = _make_contract()
        row = _make_row(delta_stored=None)
        data_port = _make_data_port([(contract, row)])

        # Pricer returns a fully-computed ComputedGreeks (delta only is what
        # we care about — but Module 2 always returns all five fields).
        computed = ComputedGreeks(
            iv=ComputeResult(value=0.20, source="computed", model="Black-76", inputs_used={"r": 0.0}),
            delta=ComputeResult(value=0.42, source="computed", model="Black-76", inputs_used={"r": 0.0}),
            gamma=ComputeResult(value=0.01, source="computed", model="Black-76", inputs_used={"r": 0.0}),
            theta=ComputeResult(value=-0.05, source="computed", model="Black-76", inputs_used={"r": 0.0}),
            vega=ComputeResult(value=0.10, source="computed", model="Black-76", inputs_used={"r": 0.0}),
        )
        pricer = MagicMock()
        pricer.compute.return_value = computed

        chain = _make_chain(data_port=data_port, pricer=pricer)
        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
            compute_missing=True,
        )

        assert len(snap.rows) == 1
        # Stored fields stay stored; missing field is filled with computed.
        assert snap.rows[0].iv.source == "stored"  # stored present, takes precedence
        assert snap.rows[0].delta.source == "computed"
        assert snap.rows[0].delta.value == 0.42
        # The pricer was called exactly once (per row).
        pricer.compute.assert_called_once()


class TestSnapshotOptVixCascade:
    """OPT_VIX with compute_missing=True still surfaces source='missing' per guardrail #6."""

    @pytest.mark.asyncio
    async def test_opt_vix_compute_missing_true_no_widening_to_computed(self) -> None:
        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        # All stored Greeks missing — typical OPT_VIX shape.
        row = _make_row(
            iv_stored=None,
            delta_stored=None,
            gamma_stored=None,
            theta_stored=None,
            vega_stored=None,
        )
        data_port = _make_data_port([(contract, row)])

        # Module 2 returns "missing" for every Greek with the OPT_VIX gate.
        gated = ComputedGreeks(
            iv=ComputeResult(value=None, source="missing", error_code="missing_forward_vix_curve", missing_inputs=("forward_vix_curve",)),
            delta=ComputeResult(value=None, source="missing", error_code="missing_forward_vix_curve", missing_inputs=("forward_vix_curve",)),
            gamma=ComputeResult(value=None, source="missing", error_code="missing_forward_vix_curve", missing_inputs=("forward_vix_curve",)),
            theta=ComputeResult(value=None, source="missing", error_code="missing_forward_vix_curve", missing_inputs=("forward_vix_curve",)),
            vega=ComputeResult(value=None, source="missing", error_code="missing_forward_vix_curve", missing_inputs=("forward_vix_curve",)),
        )
        pricer = MagicMock()
        pricer.compute.return_value = gated

        chain = _make_chain(
            data_port=data_port,
            pricer=pricer,
            index_port=_make_index_port(value=18.0),  # IND_VIX value joins ok
        )

        snap = await chain.snapshot(
            root="OPT_VIX",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
            compute_missing=True,
        )

        assert len(snap.rows) == 1
        for greek in (snap.rows[0].iv, snap.rows[0].delta, snap.rows[0].gamma, snap.rows[0].theta, snap.rows[0].vega):
            assert greek.source == "missing"
            assert greek.error_code == "missing_forward_vix_curve"


class TestSnapshotTypeFilter:
    """type='C' is forwarded to the data port (which performs filtering)."""

    @pytest.mark.asyncio
    async def test_type_filter_forwarded(self) -> None:
        contract = _make_contract(type_="C")
        row = _make_row()
        data_port = _make_data_port([(contract, row)])
        chain = _make_chain(data_port=data_port)

        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
        )

        # Verify the type was passed through.
        data_port.query_chain.assert_awaited_once()
        call_kwargs = data_port.query_chain.await_args.kwargs
        assert call_kwargs["type"] == "C"
        # And the result row's type is "C".
        assert all(r.type == "C" for r in snap.rows)


class TestSnapshotEmptyChain:
    """Empty chain query → rows=() and a note mentioning zero rows."""

    @pytest.mark.asyncio
    async def test_empty_chain_yields_note(self) -> None:
        data_port = _make_data_port([])
        chain = _make_chain(data_port=data_port)

        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="both",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
        )

        assert snap.rows == ()
        assert any("0 rows" in n or "zero" in n.lower() for n in snap.notes)


class TestSnapshotKOverS:
    """K_over_S = strike / underlying_price; None when underlying join fails."""

    @pytest.mark.asyncio
    async def test_k_over_s_with_known_underlying(self) -> None:
        contract = _make_contract(strike=100.0)
        row = _make_row()
        data_port = _make_data_port([(contract, row)])
        chain = _make_chain(
            data_port=data_port,
            futures_port=_make_futures_port(value=110.0),
        )

        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
        )

        assert snap.underlying_price == 110.0
        assert snap.rows[0].K_over_S == pytest.approx(100.0 / 110.0)

    @pytest.mark.asyncio
    async def test_k_over_s_none_when_underlying_join_fails(self) -> None:
        contract = _make_contract()
        row = _make_row()
        data_port = _make_data_port([(contract, row)])
        chain = _make_chain(
            data_port=data_port,
            futures_port=_make_futures_port(value=None),
        )

        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
        )

        assert snap.underlying_price is None
        assert snap.rows[0].K_over_S is None
        # The underlying-join failure is surfaced as a note.
        assert any("underlying" in n.lower() for n in snap.notes)


class TestSnapshotValidation:
    """expiration_min > expiration_max raises OptionsValidationError."""

    @pytest.mark.asyncio
    async def test_inverted_expiration_range_raises(self) -> None:
        data_port = _make_data_port([])
        chain = _make_chain(data_port=data_port)

        with pytest.raises(OptionsValidationError):
            await chain.snapshot(
                root="OPT_SP_500",
                date=date(2024, 6, 14),
                type="C",
                expiration_min=date(2025, 1, 1),
                expiration_max=date(2024, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_invalid_type_raises(self) -> None:
        data_port = _make_data_port([])
        chain = _make_chain(data_port=data_port)

        with pytest.raises(OptionsValidationError):
            await chain.snapshot(
                root="OPT_SP_500",
                date=date(2024, 6, 14),
                type="X",  # type: ignore[arg-type]
                expiration_min=date(2024, 6, 1),
                expiration_max=date(2024, 12, 31),
            )


class TestSnapshotStrikeBounds:
    """strike_min/strike_max forwarded to the data port."""

    @pytest.mark.asyncio
    async def test_strike_bounds_forwarded(self) -> None:
        data_port = _make_data_port([])
        chain = _make_chain(data_port=data_port)

        await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="both",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
            strike_min=80.0,
            strike_max=120.0,
        )

        call_kwargs = data_port.query_chain.await_args.kwargs
        assert call_kwargs["strike_min"] == 80.0
        assert call_kwargs["strike_max"] == 120.0


class TestSnapshotOptBTCJoinUsesRow:
    """OPT_BTC reads underlying price directly off the row (Decision H)."""

    @pytest.mark.asyncio
    async def test_opt_btc_underlying_from_row(self) -> None:
        contract = _make_contract(
            collection="OPT_BTC",
            root_underlying="BTC",
            underlying_ref=None,
        )
        row = _make_row(underlying_price_stored=7484.58)
        data_port = _make_data_port([(contract, row)])
        index_port = _make_index_port()
        futures_port = _make_futures_port(value=None)

        chain = _make_chain(
            data_port=data_port,
            index_port=index_port,
            futures_port=futures_port,
        )

        snap = await chain.snapshot(
            root="OPT_BTC",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
        )

        assert snap.underlying_price == 7484.58
        # Neither index nor futures port was consulted.
        index_port.get_index_value_on_date.assert_not_awaited()
        futures_port.get_futures_close_on_date.assert_not_awaited()


class TestSnapshotNeverFabricatesComputed:
    """Module 6 must NEVER label a stored value as 'computed'."""

    @pytest.mark.asyncio
    async def test_stored_value_keeps_stored_label_even_with_compute_missing(self) -> None:
        contract = _make_contract()
        row = _make_row()  # all stored values present
        data_port = _make_data_port([(contract, row)])

        # If the pricer were called we'd notice; but it should not be.
        pricer = MagicMock()
        pricer.compute.side_effect = AssertionError("pricer must not be called when all stored present")
        chain = _make_chain(data_port=data_port, pricer=pricer)

        snap = await chain.snapshot(
            root="OPT_SP_500",
            date=date(2024, 6, 14),
            type="C",
            expiration_min=date(2024, 6, 1),
            expiration_max=date(2024, 12, 31),
            compute_missing=True,
        )

        for cr in snap.rows:
            assert cr.iv.source == "stored"
            assert cr.delta.source == "stored"
