"""SQL-backed market data adapters (PostgreSQL dwh) for Phase 1.

This module replaces tcg.data._mongo.instruments for the market-data read path
(list_collections, list_instruments, get_prices, get_continuous, get_available_cycles,
get_aligned_prices) and tcg.data.options.reader for options reads (get_option_contract,
query_options_chain, list_roots, list_expirations*).

Every query is read-only and enforces point-in-time bounds (no look-ahead).
All gotchas from the recon document are handled here at the boundary.
"""

from __future__ import annotations
