"""DefaultMarketDataServiceV2 — read service over the ``tcg_instruments_v2`` star.

Mirrors :class:`tcg.data.service.DefaultMarketDataService` in structure: it
composes the v2 SQL reader (:class:`SqlInstrumentReaderV2`), the UNCHANGED
:class:`ContinuousSeriesBuilder` (futures), and the v2-native options-continuous
resolver (settlement-value selection). It shares the SAME read-only ``tcg_read``
pool as v1 — the schema is bound per-query in the reader, not by a new pool.

Everything is read-only. Fact-table choice is always dispatched off
``serie.type``.
"""

from __future__ import annotations

from datetime import date

from tcg.data._options_continuous_v2 import resolve_options_continuous_v2
from tcg.data._rolling import ContinuousSeriesBuilder
from tcg.data._sql.connection import DwhConnectionPool
from tcg.data._sql.instruments_v2 import FACT_DISPATCH, SqlInstrumentReaderV2
from tcg.data._utils import date_to_int, filter_date_range
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import (
    ContinuousRollConfig,
    ContinuousSeries,
    OptionsContinuousV2,
)


class DefaultMarketDataServiceV2:
    """Read-only market data over the v2 star schema."""

    def __init__(self, dwh_pool: DwhConnectionPool) -> None:
        self._reader = SqlInstrumentReaderV2(dwh_pool)
        self._roller = ContinuousSeriesBuilder()

    # ------------------------------------------------------------------ #
    # Object / contract / serie browsing
    # ------------------------------------------------------------------ #
    async def list_objects(self) -> list[dict]:
        """List every v2 object (all kinds) with root metadata."""
        return await self._reader.list_objects()

    async def get_object_detail(self, object_id: int) -> dict:
        """Return ``{object}`` for one object — metadata only.

        Contracts and series are NOT included: on the large option roots that
        was 96 106 contracts + 200 672 series in a single 38 239 859-byte
        response taking ~36 s (measured twice on object 12, byte-identical).
        Both now come from :meth:`list_object_series` (filtered + paginated)
        and :meth:`get_object_facets` (aggregated).

        Raises ``DataNotFoundError`` if the object does not exist.
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        return {"object": obj}

    async def get_object_facets(self, object_id: int) -> dict:
        """Return the filterable dimensions of one object (for the filter form).

        Raises ``DataNotFoundError`` if the object does not exist.
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        facets = await self._reader.fetch_object_facets(object_id)
        return {"object_id": object_id, "kind": obj["kind"], **facets}

    async def list_object_series(
        self,
        object_id: int,
        *,
        expiration_min: date | None = None,
        expiration_max: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        option_type: str = "both",
        serie_type: str = "any",
        freq: str = "any",
        skip: int = 0,
        limit: int = 50,
    ) -> dict:
        """One filtered, paginated page of an object's series.

        Returns the ``PaginatedResult`` shape (``items``/``total``/``skip``/
        ``limit``). Raises ``DataNotFoundError`` if the object does not exist —
        but a filter matching nothing is NOT an error: it returns an empty
        ``items`` with ``total: 0``.
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        items, total = await self._reader.list_series_filtered(
            object_id,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
            option_type=option_type,
            serie_type=serie_type,
            freq=freq,
            skip=skip,
            limit=limit,
        )
        return {"items": items, "total": total, "skip": skip, "limit": limit}

    async def get_series(
        self,
        serie_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict:
        """Read one serie's facts, fact table dispatched by ``serie.type``.

        Returns ``{serie_id, type, grain, fields, points:{ts, <field>...}}``.
        ``grain`` is ``"daily"`` or ``"intraday"``, resolved from ``serie.freq``.
        Raises ``DataNotFoundError`` if the serie does not exist.
        """
        serie = await self._reader.get_serie(serie_id)
        if serie is None:
            raise DataNotFoundError(f"Serie {serie_id} not found in v2")
        serie_type = serie["type"]
        grain, ts_values, cols = await self._reader.read_serie_facts(
            serie_id, serie_type, freq=serie.get("freq"), start=start, end=end
        )
        fields = list(FACT_DISPATCH[serie_type][1])
        points: dict[str, list] = {"ts": ts_values}
        points.update({f: cols[f] for f in fields})
        return {
            "serie_id": serie_id,
            "type": serie_type,
            "grain": grain,
            "fields": fields,
            "points": points,
        }

    # ------------------------------------------------------------------ #
    # Continuous futures (reused ContinuousSeriesBuilder)
    # ------------------------------------------------------------------ #
    async def get_continuous_future(
        self,
        object_id: int,
        roll_config: ContinuousRollConfig,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> ContinuousSeries | None:
        """Build a continuous futures series for a v2 future object.

        Validates the object is a ``future``, fetches its contract bars, and
        feeds them to the UNCHANGED roller. Date-range filtered like v1.
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        if obj["kind"] != "future":
            raise ValidationError(
                f"Object {object_id} ({obj['symbol']}) is kind "
                f"'{obj['kind']}', not a future"
            )

        contracts = await self._reader.fetch_future_contract_bars(
            object_id, obj.get("cycle")
        )
        if not contracts:
            return None

        # v2 carries no per-contract cycle; the object-level cycle already
        # scoped the contracts. Pass the config through unchanged (the roller
        # uses roll_config for strategy/adjustment/rank).
        result = self._roller.build(contracts, roll_config, collection=obj["symbol"])
        if len(result.prices) == 0:
            return None

        if start is not None or end is not None:
            filtered_prices = filter_date_range(result.prices, start, end)
            if filtered_prices is None:
                return None
            start_int = date_to_int(start) if start is not None else 0
            end_int = date_to_int(end) if end is not None else 99999999
            filtered_roll_dates = tuple(
                rd for rd in result.roll_dates if start_int <= rd <= end_int
            )
            result = ContinuousSeries(
                collection=result.collection,
                roll_config=result.roll_config,
                prices=filtered_prices,
                roll_dates=filtered_roll_dates,
                contracts=result.contracts,
            )
        return result

    async def get_future_cycles(self, object_id: int) -> list[str]:
        """Return available listing cycles for a future object."""
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        return await self._reader.fetch_future_cycles(object_id)

    # ------------------------------------------------------------------ #
    # Continuous options (v2-native settlement selection)
    # ------------------------------------------------------------------ #
    async def get_continuous_options(
        self,
        object_id: int,
        *,
        criterion: str,
        target: float,
        option_type: str,
        start: date | None = None,
        end: date | None = None,
    ) -> OptionsContinuousV2:
        """Build a v2 continuous options settlement stream.

        Validates the object is an ``option`` then delegates to the resolver.
        ``criterion='delta'`` raises ``ValidationError`` (→ 400).
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        if obj["kind"] != "option":
            raise ValidationError(
                f"Object {object_id} ({obj['symbol']}) is kind "
                f"'{obj['kind']}', not an option"
            )
        return await resolve_options_continuous_v2(
            self._reader,
            obj,
            criterion=criterion,
            target=target,
            option_type=option_type,
            start=start,
            end=end,
        )
