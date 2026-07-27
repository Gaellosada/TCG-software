"""Typed errors for the v2 compatibility adapter.

Every one of these means the same thing: *the request as written cannot be
served by the v2 warehouse*. That is a client-correctable bad request, so each
message names the offending value AND the action that fixes it (spec §11).

None of these is ever downgraded to a warning and none falls back to v1 — a
portfolio that silently mixed sources would produce a result attributable to
neither warehouse, defeating the whole point of the comparison.

HTTP status
-----------
``tcg.core.api.errors.STATUS_MAP`` maps to a status code by the ``error_type``
STRING, not by the exception class. These errors therefore declare
``error_type="validation_error"`` so they surface as **400**, while still
subclassing :class:`DataAccessError` as the frozen cross-worker API requires.

That pairing is deliberate but is the one wart here: the class says
"storage backend failure" while the payload says "validation error". The two
places in ``tcg`` that catch ``DataAccessError`` — ``tcg/data/_sql/instruments.py``
and ``tcg/data/_v2_compat/_sql_v2.py`` — only re-raise it, preserving the
subclass, so the mismatch is inert today. Adding a handler that SWALLOWS or
downgrades a ``DataAccessError`` on a v2 path would turn an actionable 400 into
a 502, which is what makes this sentence worth keeping true. The clean fix is
a dedicated ``"v2_unavailable": 400`` entry in ``STATUS_MAP`` — an edit to an
existing file, and therefore Wave 3a's call, not this module's.
"""

from __future__ import annotations

from tcg.types.errors import DataAccessError, TCGError

# These are *client* errors ("v2 cannot serve this request"), not backend
# failures, so they must render as HTTP 400. STATUS_MAP is keyed on this
# string. See the module docstring for why the class base differs.
_V2_ERROR_TYPE = "validation_error"


class V2DataUnavailable(DataAccessError):
    """Base: the v2 warehouse cannot serve this request.

    Subclasses :class:`DataAccessError` (frozen cross-worker API) but reports
    ``error_type="validation_error"`` so the existing handler returns 400
    rather than 502.
    """

    def __init__(self, message: str) -> None:
        # Deliberately bypasses DataAccessError.__init__, which would hardcode
        # error_type="data_access_error" (→ HTTP 502).
        TCGError.__init__(self, message, _V2_ERROR_TYPE)


class V2CollectionUnavailable(V2DataUnavailable):
    """Spec §11 E1 — the collection has no v2 representation at all."""

    def __init__(self, collection: str) -> None:
        super().__init__(
            f'Data source "v2" does not have data for collection '
            f"'{collection}'. The v2 warehouse currently covers only "
            f"IND_SP_500 (INDEX), FUT_SP_500 and the weekly OPT_SP_500 "
            f'options. Switch this run to data source "v1", or remove the '
            f"'{collection}' leg."
        )
        self.collection = collection


class V2InstrumentUnavailable(V2DataUnavailable):
    """Spec §11 E2 — the collection is served but this instrument is not."""

    def __init__(self, symbol: str, collection: str = "INDEX") -> None:
        super().__init__(
            f"Data source \"v2\" has no data for instrument '{symbol}'. "
            f"Within {collection}, v2 covers only IND_SP_500. Switch this run "
            f'to data source "v1", or change the instrument.'
        )
        self.symbol = symbol
        self.collection = collection


class V2FuturesContractUnavailable(V2InstrumentUnavailable):
    """A v1 ES futures symbol names an expiration v2 does not list.

    The two warehouses do NOT agree on every ES expiration: 84 of the 86 v2
    contracts round-trip, but v1 lists ``20260618``/``20270617`` where v2 lists
    ``20260619``/``20270618`` — an off-by-one *listing* difference, not missing
    data (live-verified 2026-07-27). v1 additionally lists 18 pre-2010
    contracts that predate v2's history entirely.

    Because the adapter keys futures identity on the expiration date, such a
    symbol resolved to zero rows and the request answered ``None``. In a
    v1-vs-v2 COMPARISON feature that is the worst failure mode: the user reads
    the hole as a strategy or coverage effect rather than an identity
    mismatch. So it raises, and names the nearest v2 expiration to make the
    off-by-one legible.

    Deliberately NOT fuzzy-matched: snapping ``20260618`` onto v2's
    ``20260619`` would fabricate an identity the warehouses do not share and
    would mask genuine gaps.

    Subclasses :class:`V2InstrumentUnavailable` (so existing handlers and
    ``pytest.raises`` sites keep working) but builds its own message — the
    parent's is INDEX-specific.
    """

    def __init__(self, symbol: str, nearest: int | None = None) -> None:
        hint = (
            f" The nearest v2 contract expires {nearest} "
            f"(symbol 'FUT_SP_500_EMINI_{nearest}') — the two warehouses list "
            f"this contract one day apart."
            if nearest is not None
            else ""
        )
        # Skips V2InstrumentUnavailable.__init__ (INDEX wording) on purpose.
        V2DataUnavailable.__init__(
            self,
            f"Data source \"v2\" has no futures contract '{symbol}': the v2 "
            f"warehouse lists no ES contract with that expiration date.{hint} "
            f'Switch this run to data source "v1", or use a contract v2 lists.',
        )
        self.symbol = symbol
        self.collection = "FUT_SP_500"
        self.nearest = nearest


class V2UnsupportedCycle(V2DataUnavailable):
    """Spec §11 E3 — a non-weekly expiration cycle (``M`` / ``''``).

    v2 has no monthly 3rd-Friday S&P 500 options. Serving only the weekly half
    of a ``("M", "W3 Friday")`` expansion would silently compare two different
    strategies, so this fails loudly instead.
    """

    def __init__(self, cycle: str) -> None:
        super().__init__(
            f'Data source "v2" has no monthly (3rd-Friday) S&P 500 options — '
            f"it covers only the weekly EW1-EW4 series. This leg requests "
            f"expiration cycle '{cycle}'. Choose one of the weekly cycles "
            f"'W1 Friday', 'W2 Friday', 'W3 Friday', 'W4 Friday'"
            f', or switch this run to data source "v1".'
        )
        self.cycle = cycle


class V2MissingCycleFilter(V2DataUnavailable):
    """Spec §11 E4 — an option leg carries NO cycle filter at all.

    Distinct from :class:`V2UnsupportedCycle`, which names an offending cycle
    VALUE. This one takes no argument precisely so it cannot be handed a whole
    sentence that the sibling's ``'{cycle}'`` slot would then nest inside its
    own message.
    """

    def __init__(self) -> None:
        super().__init__(
            'Data source "v2" requires an explicit weekly expiration cycle on '
            "an option leg. With no cycle filter, v1 returns monthly AND "
            "weekly contracts while v2 can only return weeklies — the two "
            "results would not be comparable. Choose one of 'W1 Friday', "
            "'W2 Friday', 'W3 Friday', 'W4 Friday', or switch this run to "
            'data source "v1".'
        )


class V2UnsupportedField(V2DataUnavailable):
    """Spec §11 E5 — a requested field/stream has no v2 source.

    v2 stores end-of-day settlement only: no bid/ask quotes, no volume, no
    open interest on options.
    """

    def __init__(self, field: str, detail: str | None = None) -> None:
        super().__init__(
            f'Data source "v2" has no {field} data for S&P 500 options — v2 '
            f"stores end-of-day settlement only, with no bid/ask quotes, "
            f'volume or open interest. Use the "close" stream (settlement) or '
            f'"bs_mid" (Black-76 from stored IV), or switch this run to data '
            f'source "v1".' + (f" ({detail})" if detail else "")
        )
        self.field = field


class V2SymbolError(V2DataUnavailable):
    """A symbol does not parse under the v1 grammar the adapter speaks.

    Raised by the ``_mapping`` round-trip helpers. Not one of the spec's E1-E9
    user-facing cases — it means a caller handed the adapter a string that is
    not a v1 S&P 500 symbol at all, so the message names the expected grammar.
    """

    def __init__(self, symbol: str, expected: str) -> None:
        super().__init__(
            f"Symbol '{symbol}' is not a valid v1 S&P 500 symbol. Expected the "
            f"form {expected}. Check the leg definition, or switch this run to "
            f'data source "v1".'
        )
        self.symbol = symbol
        self.expected = expected
