"""Provider resolution: pick the right data provider for a collection.

Single function with a clear resolution chain. No fallback chains,
no cross-provider merging -- one provider per request.
"""

from __future__ import annotations

from tcg.types.errors import DataNotFoundError

from tcg.data._providers.defaults import PROVIDER_DEFAULTS


def resolve_provider(
    collection: str,
    available_providers: list[str],
    requested: str | None = None,
) -> str:
    """Determine which provider to use for *collection*.

    Resolution chain
    ----------------
    1. *requested* is not None and in *available_providers* -> return it.
    2. *requested* is not None but NOT in *available_providers* -> raise.
    3. *requested* is None -> look up config default:
       a. Exact match on collection name in PROVIDER_DEFAULTS.
       b. Prefix match (e.g. "FUT_VIX" matches "FUT_" entry).
       c. If config default is in *available_providers* -> return it.
       d. Otherwise fall through.
    4. Return first available provider.
    5. If *available_providers* is empty -> raise.
    """
    if not available_providers:
        raise DataNotFoundError(
            f"No providers available for collection '{collection}'"
        )

    # Step 1 & 2: explicit request
    if requested is not None:
        if requested in available_providers:
            return requested
        raise DataNotFoundError(
            f"Provider '{requested}' not available for collection "
            f"'{collection}'. Available: {available_providers}"
        )

    # Step 3: config default lookup
    config_default = _lookup_default(collection)
    if config_default is not None and config_default in available_providers:
        return config_default

    # Step 4: first available
    return available_providers[0]


def _lookup_default(collection: str) -> str | None:
    """Look up the config default for *collection*.

    Checks exact match first, then prefix match. Prefix entries end
    with ``_`` (e.g. ``"FUT_"``).
    """
    # Exact match
    if collection in PROVIDER_DEFAULTS:
        return PROVIDER_DEFAULTS[collection]

    # Prefix match -- iterate only prefix keys (those ending with "_")
    for prefix, provider in PROVIDER_DEFAULTS.items():
        if prefix.endswith("_") and collection.startswith(prefix):
            return provider

    return None
