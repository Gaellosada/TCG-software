"""Module 2 — opt-in Black-76 pricing kernel + py_vollib IV inverter.

Phase 1B Wave B1.2. See spec §3.2 and ORDERS.md.
"""

from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.pricing.pricer import DefaultOptionsPricer
from tcg.engine.options.pricing.protocol import OptionsPricer, PricingKernel

__all__ = [
    "BS76Kernel",
    "DefaultOptionsPricer",
    "OptionsPricer",
    "PricingKernel",
]
