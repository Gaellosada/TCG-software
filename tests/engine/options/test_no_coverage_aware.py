"""Item C — regression lock: the ``coverage_aware`` capability is fully removed.

``coverage_aware`` had zero production callers (both entry points defaulted it
False; no API model exposed it) and was deleted end-to-end.  This test pins the
removal so it cannot silently creep back via a re-added parameter.
"""

from __future__ import annotations

import inspect

from tcg.engine.options.series import stream_resolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream


def test_resolve_option_stream_has_no_coverage_aware_param():
    params = inspect.signature(resolve_option_stream).parameters
    assert "coverage_aware" not in params


def test_module_defines_no_coverage_symbols():
    for name in (
        "_coverage_candidates",
        "_COVERAGE_MAX_CANDIDATES",
        "_COVERAGE_DTE_WINDOW_DAYS",
    ):
        assert not hasattr(stream_resolver, name), name
