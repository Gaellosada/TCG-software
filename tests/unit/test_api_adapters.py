"""Unit tests for tcg/core/api/_adapters.py::build_roll_config."""

from __future__ import annotations

import pytest

from tcg.core.api._adapters import build_roll_config
from tcg.types.market import AdjustmentMethod, ContinuousRollConfig, RollStrategy


class TestBuildRollConfigHappyPath:
    def test_none_adjustment(self):
        config = build_roll_config("none", None, 0)
        assert isinstance(config, ContinuousRollConfig)
        assert config.adjustment == AdjustmentMethod.NONE
        assert config.strategy == RollStrategy.FRONT_MONTH
        assert config.cycle is None
        assert config.roll_offset_days == 0

    def test_ratio_adjustment(self):
        config = build_roll_config("ratio", None, 0)
        assert config.adjustment == AdjustmentMethod.RATIO

    def test_difference_adjustment(self):
        config = build_roll_config("difference", None, 0)
        assert config.adjustment == AdjustmentMethod.DIFFERENCE

    def test_with_cycle(self):
        config = build_roll_config("none", "HMUZ", 0)
        assert config.cycle == "HMUZ"

    def test_with_roll_offset(self):
        config = build_roll_config("ratio", None, 3)
        assert config.roll_offset_days == 3

    def test_with_all_params(self):
        config = build_roll_config("difference", "HMUZ", 2)
        assert config.adjustment == AdjustmentMethod.DIFFERENCE
        assert config.cycle == "HMUZ"
        assert config.roll_offset_days == 2

    def test_empty_cycle_becomes_none(self):
        # cycle='' → `cycle or None` → None
        config = build_roll_config("none", "", 0)
        assert config.cycle is None

    def test_strategy_defaults_to_front_month(self):
        """When ``strategy`` is omitted, the config defaults to FRONT_MONTH
        (preserves every existing positional caller)."""
        for adj in ("none", "ratio", "difference"):
            config = build_roll_config(adj, None, 0)
            assert config.strategy == RollStrategy.FRONT_MONTH


class TestBuildRollConfigStrategy:
    """Issue #3 (trap a): ``build_roll_config`` must thread the chosen roll
    ``strategy`` instead of hardcoding FRONT_MONTH — otherwise END_OF_MONTH
    silently no-ops for the signals + indicators paths that route through this
    adapter."""

    def test_end_of_month_threaded(self):
        config = build_roll_config("none", None, 0, strategy="end_of_month")
        assert config.strategy == RollStrategy.END_OF_MONTH

    def test_explicit_front_month_threaded(self):
        config = build_roll_config("ratio", "HMUZ", 2, strategy="front_month")
        assert config.strategy == RollStrategy.FRONT_MONTH
        # The other params are still honoured alongside the strategy.
        assert config.adjustment == AdjustmentMethod.RATIO
        assert config.cycle == "HMUZ"
        assert config.roll_offset_days == 2

    def test_invalid_strategy_raises(self):
        with pytest.raises(ValueError, match=r"[Ii]nvalid.*strategy|strategy"):
            build_roll_config("none", None, 0, strategy="bogus")

    def test_end_of_month_with_all_params(self):
        config = build_roll_config("difference", "HMUZ", 3, strategy="end_of_month")
        assert config.strategy == RollStrategy.END_OF_MONTH
        assert config.adjustment == AdjustmentMethod.DIFFERENCE
        assert config.cycle == "HMUZ"
        assert config.roll_offset_days == 3


class TestBuildRollConfigInvalidAdjustment:
    def test_unknown_adjustment_raises_value_error(self):
        with pytest.raises(ValueError):
            build_roll_config("xyz", None, 0)

    def test_error_message_mentions_unknown_adjustment(self):
        with pytest.raises(ValueError, match=r"unknown adjustment method"):
            build_roll_config("back_adjust", None, 0)

    def test_error_message_includes_the_bad_value(self):
        with pytest.raises(ValueError, match=r"'bad_value'"):
            build_roll_config("bad_value", None, 0)

    def test_empty_adjustment_string_raises(self):
        with pytest.raises(ValueError, match=r"unknown adjustment method"):
            build_roll_config("", None, 0)

    def test_case_sensitive_none_vs_None(self):
        # "None" (capital) is not in ADJUSTMENT_MAP — only "none" is.
        with pytest.raises(ValueError, match=r"unknown adjustment method"):
            build_roll_config("None", None, 0)


class TestBuildRollConfigEdgeCases:
    def test_roll_offset_zero_explicit(self):
        config = build_roll_config("none", None, 0)
        assert config.roll_offset_days == 0

    def test_roll_offset_cast_to_int(self):
        # roll_offset is cast via int() in the implementation
        config = build_roll_config("none", None, 5)
        assert config.roll_offset_days == 5
        assert isinstance(config.roll_offset_days, int)

    def test_cycle_none_explicit(self):
        config = build_roll_config("none", None, 0)
        assert config.cycle is None
