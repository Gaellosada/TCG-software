"""Tests for tcg.types — construction, immutability, enum values, utilities."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pytest

from tcg.types import (
    AdjustmentMethod,
    AssetClass,
    ContinuousRollConfig,
    ContinuousSeries,
    ContractSpec,
    DataAccessError,
    DataNotFoundError,
    DataVersion,
    EngineType,
    EquityCurve,
    ErrorResponse,
    InstrumentId,
    MetricsSuite,
    MongoConfig,
    PaginatedResult,
    PortfolioResult,
    PortfolioSpec,
    PriceSeries,
    Provenance,
    PositionSizingConfig,
    RebalanceFreq,
    ResultSource,
    RollStrategy,
    SimConfig,
    SimResult,
    SimulationError,
    SimulationRequest,
    SizingMethod,
    StrategyDefinition,
    StrategyExecutionError,
    StrategyMeta,
    StrategyStage,
    TCGError,
    Trade,
    ValidationError,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_price_series(n: int = 5) -> PriceSeries:
    """Create a minimal PriceSeries with n bars."""
    return PriceSeries(
        dates=np.arange(20240101, 20240101 + n, dtype=np.int64),
        open=np.ones(n, dtype=np.float64),
        high=np.ones(n, dtype=np.float64) * 1.1,
        low=np.ones(n, dtype=np.float64) * 0.9,
        close=np.ones(n, dtype=np.float64),
        volume=np.ones(n, dtype=np.float64) * 1000,
    )


def _make_instrument_id() -> InstrumentId:
    return InstrumentId(
        symbol="SPX",
        asset_class=AssetClass.INDEX,
        collection="index_prices",
    )


def _make_provenance() -> Provenance:
    return Provenance(
        source=ResultSource.ON_THE_FLY,
        engine="vectorized-0.1.0",
        data_version=DataVersion(
            source="mongodb",
            snapshot_date=date(2024, 1, 1),
        ),
        computed_at=datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
        config_hash="abc123",
        strategy_hash="def456",
    )


# ── Enum values ──────────────────────────────────────────────────────


class TestEnums:
    def test_asset_class_values(self):
        assert AssetClass.EQUITY == "equity"
        assert AssetClass.INDEX == "index"
        assert AssetClass.FUTURE == "future"
        assert len(AssetClass) == 3

    def test_roll_strategy_values(self):
        assert RollStrategy.FRONT_MONTH == "front_month"

    def test_adjustment_method_values(self):
        assert AdjustmentMethod.NONE == "none"
        assert AdjustmentMethod.PROPORTIONAL == "proportional"
        assert AdjustmentMethod.DIFFERENCE == "difference"
        assert len(AdjustmentMethod) == 3

    def test_engine_type_values(self):
        assert EngineType.VECTORIZED == "vectorized"
        assert EngineType.EVENT_DRIVEN == "event_driven"

    def test_sizing_method_values(self):
        assert SizingMethod.FIXED_FRACTIONAL == "fixed_fractional"
        assert SizingMethod.VOL_TARGET == "vol_target"

    def test_rebalance_freq_values(self):
        assert RebalanceFreq.NONE == "none"
        assert RebalanceFreq.DAILY == "daily"
        assert RebalanceFreq.WEEKLY == "weekly"
        assert RebalanceFreq.MONTHLY == "monthly"
        assert RebalanceFreq.QUARTERLY == "quarterly"
        assert RebalanceFreq.ANNUALLY == "annually"
        assert len(RebalanceFreq) == 6

    def test_result_source_values(self):
        assert ResultSource.LEGACY == "legacy"
        assert ResultSource.PRECOMPUTED == "precomputed"
        assert ResultSource.ON_THE_FLY == "on_the_fly"

    def test_strategy_stage_values(self):
        assert StrategyStage.TRIAL == "trial"
        assert StrategyStage.VALIDATION == "validation"
        assert StrategyStage.PROD == "prod"
        assert StrategyStage.ARCHIVE == "archive"
        assert len(StrategyStage) == 4


# ── Market types ─────────────────────────────────────────────────────


class TestMarketTypes:
    def test_instrument_id_construction(self):
        iid = _make_instrument_id()
        assert iid.symbol == "SPX"
        assert iid.asset_class == AssetClass.INDEX
        assert iid.exchange is None

    def test_instrument_id_with_exchange(self):
        iid = InstrumentId(
            symbol="ES",
            asset_class=AssetClass.FUTURE,
            collection="futures",
            exchange="CME",
        )
        assert iid.exchange == "CME"

    def test_contract_spec_construction(self):
        iid = _make_instrument_id()
        spec = ContractSpec(instrument_id=iid, multiplier=50.0)
        assert spec.multiplier == 50.0
        assert spec.expiration is None

    def test_price_series_len(self):
        ps = _make_price_series(10)
        assert len(ps) == 10

    def test_price_series_len_empty(self):
        ps = _make_price_series(0)
        assert len(ps) == 0

    def test_continuous_roll_config_defaults(self):
        cfg = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        assert cfg.adjustment == AdjustmentMethod.NONE
        assert cfg.cycle is None

    def test_continuous_series_construction(self):
        ps = _make_price_series(5)
        roll_cfg = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
            cycle="HMUZ",
        )
        cs = ContinuousSeries(
            collection="vix_futures",
            roll_config=roll_cfg,
            prices=ps,
            roll_dates=(20240102, 20240104),
            contracts=("VXF24", "VXG24", "VXH24"),
        )
        assert cs.collection == "vix_futures"
        assert len(cs.roll_dates) == 2
        assert len(cs.contracts) == 3


# ── Simulation types ─────────────────────────────────────────────────


class TestSimulationTypes:
    def test_sim_config_defaults(self):
        cfg = SimConfig()
        assert cfg.initial_capital == 100_000.0
        assert cfg.commission_pct == 0.001
        assert cfg.sizing.method == SizingMethod.FIXED_FRACTIONAL

    def test_position_sizing_config_defaults(self):
        psc = PositionSizingConfig()
        assert psc.lookback == 60
        assert psc.target_vol is None

    def test_simulation_request_construction(self):
        iid = _make_instrument_id()
        req = SimulationRequest(
            code="def strategy(ctx): return 1.0",
            instruments={"spx": iid},
        )
        assert req.code.startswith("def strategy")
        assert "spx" in req.instruments
        assert req.start is None
        assert req.end is None

    def test_simulation_request_with_continuous_roll(self):
        """instruments dict accepts ContinuousRollConfig as union type."""
        iid = _make_instrument_id()
        roll = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        req = SimulationRequest(
            code="pass",
            instruments={"index": iid, "futures": roll},
        )
        assert isinstance(req.instruments["index"], InstrumentId)
        assert isinstance(req.instruments["futures"], ContinuousRollConfig)

    def test_trade_construction(self):
        trade = Trade(
            date="2024-01-15",
            instrument="spx",
            action="BUY",
            quantity=100.0,
            price=4800.0,
            cost=4.80,
            signal=1.0,
        )
        assert trade.action == "BUY"
        assert trade.cost == 4.80

    def test_equity_curve_construction(self):
        ec = EquityCurve(
            dates=("2024-01-01", "2024-01-02"),
            values=(100_000.0, 100_500.0),
        )
        assert len(ec.dates) == 2
        assert ec.leg_benchmarks == {}

    def test_sim_result_construction(self):
        ec = EquityCurve(
            dates=("2024-01-01",),
            values=(100_000.0,),
        )
        prov = _make_provenance()
        cfg = SimConfig()
        result = SimResult(
            equity_curve=ec,
            trades=(),
            signals={},
            provenance=prov,
            config=cfg,
        )
        assert result.provenance.source == ResultSource.ON_THE_FLY
        assert len(result.trades) == 0


# ── Metrics ──────────────────────────────────────────────────────────


class TestMetrics:
    def test_metrics_suite_construction(self):
        m = MetricsSuite(
            total_return=0.2,
            annualized_return=0.15,
            sharpe_ratio=1.5,
            max_drawdown=-0.10,
            calmar_ratio=1.5,
            cvar_5=-0.02,
            time_underwater_days=30,
            annualized_volatility=0.18,
            sortino_ratio=2.0,
            num_trades=50,
        )
        assert m.total_return == 0.2
        assert m.annualized_volatility == 0.18
        assert m.sortino_ratio == 2.0
        assert m.win_rate is None

    def test_metrics_suite_with_win_rate(self):
        m = MetricsSuite(
            total_return=0.2,
            annualized_return=0.15,
            sharpe_ratio=1.5,
            max_drawdown=-0.10,
            calmar_ratio=1.5,
            cvar_5=-0.02,
            time_underwater_days=30,
            annualized_volatility=0.18,
            sortino_ratio=2.0,
            num_trades=50,
            win_rate=0.55,
        )
        assert m.win_rate == 0.55


# ── Portfolio ────────────────────────────────────────────────────────


class TestPortfolio:
    def test_portfolio_spec_construction(self):
        spec = PortfolioSpec(
            name="60/40",
            legs={"stocks": "SPX index", "bonds": "TLT"},
            weights={"stocks": 0.6, "bonds": 0.4},
        )
        assert spec.rebalance == RebalanceFreq.NONE
        assert spec.weights["stocks"] == 0.6

    def test_portfolio_result_construction(self):
        result = PortfolioResult(
            dates=("2024-01-01",),
            portfolio_equity=(100_000.0,),
            leg_equities={"stocks": (60_000.0,)},
            portfolio_returns=np.array([0.01]),
            leg_returns={"stocks": np.array([0.015])},
        )
        assert result.metrics is None
        assert result.provenance is None


# ── Provenance ───────────────────────────────────────────────────────


class TestProvenance:
    def test_data_version_construction(self):
        dv = DataVersion(
            source="mongodb",
            snapshot_date=date(2024, 1, 1),
            vendor_version="yahoo-v2",
            preprocessing=("split_adjust", "dividend_adjust"),
            collections_accessed=("index_prices",),
        )
        assert len(dv.preprocessing) == 2

    def test_data_version_defaults(self):
        dv = DataVersion(source="parquet", snapshot_date=None)
        assert dv.vendor_version is None
        assert dv.preprocessing == ()
        assert dv.collections_accessed == ()

    def test_provenance_construction(self):
        prov = _make_provenance()
        assert prov.source == ResultSource.ON_THE_FLY
        assert prov.engine == "vectorized-0.1.0"


# ── Strategy ─────────────────────────────────────────────────────────


class TestStrategy:
    def test_strategy_meta_construction(self):
        now = datetime.now(tz=UTC)
        meta = StrategyMeta(
            id="strat-001",
            name="SMA Crossover",
            description="Simple moving average crossover",
            stage=StrategyStage.TRIAL,
            created_at=now,
        )
        assert meta.tags == ()
        assert meta.legacy_name is None

    def test_strategy_definition_construction(self):
        now = datetime.now(tz=UTC)
        meta = StrategyMeta(
            id="strat-001",
            name="Test",
            description="Test strategy",
            stage=StrategyStage.PROD,
            created_at=now,
        )
        defn = StrategyDefinition(
            meta=meta,
            code="def strategy(ctx): return 1.0",
            config={"fast_period": 10, "slow_period": 50},
        )
        assert defn.config["fast_period"] == 10


# ── Config ───────────────────────────────────────────────────────────


class TestConfig:
    def test_mongo_config_construction(self):
        cfg = MongoConfig(uri="mongodb://localhost:27017")
        assert cfg.db_name == "tcg-instrument"

    def test_mongo_config_custom_db(self):
        cfg = MongoConfig(uri="mongodb://localhost:27017", db_name="custom")
        assert cfg.db_name == "custom"


# ── Errors ───────────────────────────────────────────────────────────


class TestErrors:
    def test_tcg_error_base(self):
        err = TCGError("something broke", "generic_error")
        assert err.message == "something broke"
        assert err.error_type == "generic_error"
        assert str(err) == "something broke"

    def test_data_not_found_error(self):
        err = DataNotFoundError("SPX not found")
        assert err.error_type == "data_not_found"
        assert isinstance(err, TCGError)

    def test_data_access_error(self):
        err = DataAccessError("MongoDB timeout")
        assert err.error_type == "data_access_error"

    def test_strategy_execution_error(self):
        err = StrategyExecutionError("syntax error on line 5")
        assert err.error_type == "strategy_execution_error"

    def test_simulation_error(self):
        err = SimulationError("NaN in equity curve")
        assert err.error_type == "simulation_error"

    def test_validation_error(self):
        err = ValidationError("initial_capital must be positive")
        assert err.error_type == "validation_error"

    def test_error_response_construction(self):
        resp = ErrorResponse(
            error_type="data_not_found",
            message="SPX not found",
            details={"collection": "index_prices"},
        )
        assert resp.details is not None

    def test_error_response_no_details(self):
        resp = ErrorResponse(
            error_type="validation_error",
            message="bad input",
        )
        assert resp.details is None

    def test_errors_are_catchable_as_exception(self):
        with pytest.raises(TCGError):
            raise DataNotFoundError("missing")

        with pytest.raises(Exception):
            raise SimulationError("broken")


# ── Common ───────────────────────────────────────────────────────────


class TestCommon:
    def test_paginated_result_construction(self):
        result = PaginatedResult(
            items=("a", "b", "c"),
            total=10,
            skip=0,
            limit=3,
        )
        assert len(result.items) == 3
        assert result.total == 10

    def test_paginated_result_generic_with_dataclass(self):
        """PaginatedResult works with domain types."""
        iid = _make_instrument_id()
        result = PaginatedResult(
            items=(iid,),
            total=1,
            skip=0,
            limit=10,
        )
        assert result.items[0].symbol == "SPX"

    def test_paginated_result_empty(self):
        result = PaginatedResult(items=(), total=0, skip=0, limit=10)
        assert len(result.items) == 0


# ── Frozen immutability ──────────────────────────────────────────────


class TestFrozenImmutability:
    def test_instrument_id_frozen(self):
        iid = _make_instrument_id()
        with pytest.raises(AttributeError):
            iid.symbol = "AAPL"  # type: ignore[misc]

    def test_sim_config_frozen(self):
        cfg = SimConfig()
        with pytest.raises(AttributeError):
            cfg.initial_capital = 200_000.0  # type: ignore[misc]

    def test_metrics_suite_frozen(self):
        m = MetricsSuite(
            total_return=0.2,
            annualized_return=0.15,
            sharpe_ratio=1.5,
            max_drawdown=-0.10,
            calmar_ratio=1.5,
            cvar_5=-0.02,
            time_underwater_days=30,
            annualized_volatility=0.18,
            sortino_ratio=2.0,
            num_trades=50,
        )
        with pytest.raises(AttributeError):
            m.sharpe_ratio = 2.0  # type: ignore[misc]

    def test_provenance_frozen(self):
        prov = _make_provenance()
        with pytest.raises(AttributeError):
            prov.source = ResultSource.LEGACY  # type: ignore[misc]

    def test_trade_frozen(self):
        trade = Trade(
            date="2024-01-15",
            instrument="spx",
            action="BUY",
            quantity=100.0,
            price=4800.0,
            cost=4.80,
            signal=1.0,
        )
        with pytest.raises(AttributeError):
            trade.price = 5000.0  # type: ignore[misc]

    def test_error_response_frozen(self):
        resp = ErrorResponse(error_type="err", message="msg")
        with pytest.raises(AttributeError):
            resp.message = "new"  # type: ignore[misc]

    def test_paginated_result_frozen(self):
        result = PaginatedResult(items=(), total=0, skip=0, limit=10)
        with pytest.raises(AttributeError):
            result.total = 5  # type: ignore[misc]
