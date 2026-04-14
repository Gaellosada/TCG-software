"""Provider resolution for market data collections."""

from tcg.data._providers.defaults import PROVIDER_DEFAULTS
from tcg.data._providers.resolver import resolve_provider

__all__ = ["PROVIDER_DEFAULTS", "resolve_provider"]
