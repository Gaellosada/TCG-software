"""Module 6 — options chain snapshot assembly.

Spec reference: §3.6.

Assembles a ``ChainSnapshot`` from raw rows: widens stored values to
``ComputeResult(source="stored", ...)`` (the only place in the system
where this widening happens — see Appendix C.3), optionally invokes
Module 2 (pricer) for missing Greeks when ``compute_missing=True``, and
computes ``K_over_S = strike / underlying_price`` fresh from the joined
underlying price.

Public surface::

    from tcg.engine.options.chain.protocol import OptionsChain
    from tcg.engine.options.chain.chain import DefaultOptionsChain

Independence contract: this package does NOT import from ``tcg.data.*``.
Mongo access is mediated by the local ports in ``_ports.py``.
"""

from tcg.engine.options.chain.chain import DefaultOptionsChain
from tcg.engine.options.chain.protocol import OptionsChain

__all__ = [
    "DefaultOptionsChain",
    "OptionsChain",
]
