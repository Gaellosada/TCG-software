"""Collection discovery and prefix-based classification.

Discovered at startup from ``db.list_collection_names()``.
This is an implementation detail of the MongoDB adapter -- outside code
interacts only via ``MarketDataService.list_collections()``.
"""

from __future__ import annotations

from tcg.types.market import AssetClass


class CollectionRegistry:
    """Classifies MongoDB collection names by prefix convention.

    Prefix rules (from legacy Java platform):

    ========  ============  =========================
    Prefix    Asset Class   Examples
    ========  ============  =========================
    FUT_      FUTURE        FUT_VIX, FUT_SP_500
    OPT_      Options       OPT_VIX (deferred)
    INDEX     INDEX         Single collection
    ETF       EQUITY        Single collection
    FUND      EQUITY        Single collection
    FOREX     EQUITY        Single collection
    ========  ============  =========================

    Unknown collections (system collections, etc.) are silently ignored.
    """

    def __init__(self, raw_names: list[str]) -> None:
        self.futures: list[str] = []
        self.options: list[str] = []
        self.indexes: list[str] = []
        self.assets: list[str] = []  # ETF, FUND, FOREX

        for name in raw_names:
            if name.startswith("FUT_"):
                self.futures.append(name)
            elif name.startswith("OPT_"):
                self.options.append(name)
            elif name == "INDEX":
                self.indexes.append(name)
            elif name in ("ETF", "FUND", "FOREX"):
                self.assets.append(name)
            # else: ignore unknown collections

        # Sort for deterministic ordering
        self.futures.sort()
        self.options.sort()
        self.indexes.sort()
        self.assets.sort()

    def asset_class_for(self, collection: str) -> AssetClass | None:
        """Classify a collection name into its asset class.

        Returns ``None`` for unknown or deferred collection types (e.g. OPT_).
        """
        if collection.startswith("FUT_"):
            return AssetClass.FUTURE
        if collection in self.indexes:
            return AssetClass.INDEX
        if collection in self.assets:
            return AssetClass.EQUITY
        return None

    @property
    def all_active(self) -> list[str]:
        """All collections in scope (excludes options for now)."""
        return self.indexes + self.assets + self.futures

    def __contains__(self, collection: str) -> bool:
        return collection in self.all_active
