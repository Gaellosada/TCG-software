"""Verify the vendored backtester library imports successfully."""

import pytest


def test_all_modules_import():
    """All lib submodules should be importable."""
    from tcg.backtester.lib import (
        aliases,
        compile,
        constants,
        data,
        data_load,
        diagnostics,
        engine,
        indicators,
        metrics,
        mongo,
        options,
        plotting,
        snippets_registry,
        types,
        validate,
    )

    # Verify they're actual modules, not None
    assert all(
        m is not None
        for m in [
            aliases,
            compile,
            constants,
            data,
            data_load,
            diagnostics,
            engine,
            indicators,
            metrics,
            mongo,
            options,
            plotting,
            snippets_registry,
            types,
            validate,
        ]
    )


def test_top_level_reexport():
    """tcg.backtester should re-export from tcg.backtester.lib."""
    import tcg.backtester

    assert hasattr(tcg.backtester, "engine")
    assert hasattr(tcg.backtester, "indicators")
    assert hasattr(tcg.backtester, "metrics")


def test_strategy_exports():
    """tcg.backtester should re-export strategy entry points."""
    import tcg.backtester

    assert hasattr(tcg.backtester, "run_strategy")
    assert hasattr(tcg.backtester, "StrategyContext")
    assert hasattr(tcg.backtester, "BacktestResult")
