"""Typed errors raised by the ``tcg_instruments_v2`` compatibility layer.

Every one of these is a CLIENT-CORRECTABLE condition: "the request as written
cannot be served by the data source you selected". They must surface as HTTP
**400** (not 502), so each message names the offending value AND the action that
fixes it — mapping spec §11.

Frozen public API (Wave 2a and Wave 2b implement it independently; the merge
keeps ONE version, so the names and semantics below must not drift):

===========================  ==========================================
``V2DataUnavailable``        base — subclasses the project ``DataAccessError``
``V2CollectionUnavailable``  §11 E1 — collection has no v2 mapping
``V2InstrumentUnavailable``  §11 E2 — instrument outside v2's coverage
``V2UnsupportedCycle``       §11 E3/E4 — monthly/empty cycle, or no cycle filter
``V2UnsupportedField``       §11 E5 — a stream/field v2 does not store at all
``V2SymbolError``            a symbol that does not parse under the v1 grammar
===========================  ==========================================

Note on naming: spec §11 gives the error *conditions* finer-grained names
(``V2CycleUnavailable`` / ``V2CycleFilterRequired`` → ``V2UnsupportedCycle``;
``V2StreamUnavailable`` → ``V2UnsupportedField``). The class set above is the
agreed cross-worker contract; the spec's distinctions survive in the message
text, which is what the user actually reads.
"""

from __future__ import annotations

from tcg.types.errors import DataAccessError


class V2DataUnavailable(DataAccessError):
    """Base: the v2 warehouse cannot serve this request as written.

    Never a transport failure — a genuine dwh outage still raises the ordinary
    ``OptionsDataAccessError`` / ``DataAccessError``. This one always means the
    caller can fix the request (or switch to ``data_source="v1"``).
    """


class V2CollectionUnavailable(V2DataUnavailable):
    """§11 E1 — the requested collection has no counterpart in v2."""


class V2InstrumentUnavailable(V2DataUnavailable):
    """§11 E2 — the collection maps, but this instrument is outside v2."""


class V2UnsupportedCycle(V2DataUnavailable):
    """§11 E3/E4 — the expiration cycle cannot be served by v2.

    Two conditions share this class:

    * E3 — the tag set contains ``"M"`` or ``""``. v2 has no monthly
      (3rd-Friday) E-mini options at all, only the weekly EW1-EW4 series.
    * E4 — no cycle filter was supplied on a chain/selection call. Serving the
      weeklies union would silently answer a *different* question from the one
      v1 answers for the same request, so it fails loudly instead.
    """


class V2UnsupportedField(V2DataUnavailable):
    """§11 E5 — v2 stores no data at all for this stream/field.

    ``mid`` / ``volume`` / ``open_interest`` on options: v2 carries end-of-day
    settlement only, with no bid/ask book. Substituting settlement for ``mid``
    would fabricate agreement between the two sources, so it is an error.
    """


class V2SymbolError(V2DataUnavailable):
    """A symbol does not parse under the v1 grammar the adapter speaks."""
