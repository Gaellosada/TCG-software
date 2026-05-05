"""Verify the vendored backtester library imports successfully."""

import pytest


def test_all_modules_import():
    """All lib submodules should be importable."""
    from tcg.backtester.lib import (
        aliases,
        compile,
        constants,
        data_load,
        diagnostics,
        engine,
        metrics,
        mongo,
        options,
        plotting,
        signals,
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
            data_load,
            diagnostics,
            engine,
            metrics,
            mongo,
            options,
            plotting,
            signals,
            snippets_registry,
            types,
            validate,
        ]
    )


def test_top_level_reexport():
    """tcg.backtester should re-export from tcg.backtester.lib."""
    import tcg.backtester

    assert hasattr(tcg.backtester, "engine")
    assert hasattr(tcg.backtester, "signals")
    assert hasattr(tcg.backtester, "metrics")
