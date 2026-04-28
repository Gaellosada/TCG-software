"""Module 3 — options selection.

Spec reference: §3.3.

Resolves a (root, date, type, criterion, maturity) request into a single
``SelectionResult``.  Three criteria: ``ByDelta``, ``ByMoneyness``,
``ByStrike``.  Five maturity rules forwarded to Module 4.

Public surface::

    from tcg.engine.options.selection.protocol import OptionsSelector
    from tcg.engine.options.selection.selector import (
        DefaultOptionsSelector,
        UnderlyingPriceResolver,
    )

Independence contract: this package does NOT import from ``tcg.data.*``.
The chain reader is injected via the local ``ChainReaderPort`` Protocol
defined in ``_ports.py``.  See ``selector.py`` module docstring for full
rationale and import-linter implications.
"""

from tcg.engine.options.selection.protocol import OptionsSelector
from tcg.engine.options.selection.selector import (
    DefaultOptionsSelector,
    UnderlyingPriceResolver,
)

__all__ = [
    "DefaultOptionsSelector",
    "OptionsSelector",
    "UnderlyingPriceResolver",
]
