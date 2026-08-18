"""SQL read adapter for the dwh v2 star schema ``tcg_instruments_v2``.

The v2 warehouse is a star: ``object -> contract -> serie -> fact_*``. A serie's
``type`` (``value`` / ``bar`` / ``greeks`` / ``bbba``) selects exactly one fact
table — nothing in the DB enforces the mapping, so every read here reads
``serie.type`` first and dispatches. All queries are read-only, parameterized,
and reuse the EXISTING ``tcg_read`` read pool (:class:`DwhConnectionPool`); the
schema is bound per-query (``V2_SCHEMA``) rather than by a second pool.

Partition/BRIN gotcha (honoured): every multi-row fact query bounds ``ts`` with a
constant ``>= lower AND < upper`` range so the planner can prune / BRIN-scan
rather than reading the whole fact table.

Decimal → float coercion happens at this boundary (NumPy/engine want floats).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np

from tcg.data._sql.connection import DwhConnectionPool, to_float, to_float_or
from tcg.data._utils import date_to_int
from tcg.types.errors import DataAccessError
from tcg.types.market import ContractPriceData, PriceSeries

logger = logging.getLogger(__name__)

# Schema for every v2 query. Bound per-statement (never a second pool) so the
# same read-only ``tcg_read`` pool serves both v1 and v2.
V2_SCHEMA = "tcg_instruments_v2"

# Sentinel ``ts`` bounds when the caller leaves start/end open. Kept inside a
# generous span so the constant range still lets the planner prune.
_MIN_DATE = date(1900, 1, 1)
_MAX_DATE = date(2100, 12, 31)

# Which fact table + value columns each serie.type dispatches to. Read
# ``serie.type`` first, then look this up — the DB does not enforce the mapping.
FACT_DISPATCH: dict[str, tuple[str, tuple[str, ...]]] = {
    "bar": ("fact_bar", ("open", "high", "low", "close", "volume", "open_interest")),
    "value": ("fact_value", ("value",)),
    "greeks": (
        "fact_greeks",
        ("delta", "gamma", "theta", "vega", "rho", "implied_vol"),
    ),
    "bbba": (
        "fact_bbba",
        ("best_bid_value", "best_bid_volume", "best_ask_value", "best_ask_volume"),
    ),
}


def _ts_to_int(ts: datetime) -> int:
    """timestamptz → YYYYMMDD int (in UTC — the dwh stores daily bars at 00:00Z)."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return date_to_int(ts.date())


def _ts_to_iso(ts: datetime) -> str:
    """timestamptz → ISO 8601 in UTC, ``Z``-suffixed.

    Used for intraday series, where the time-of-day IS the data point. A naive
    ts is assumed UTC (the dwh stores timestamptz; psycopg returns aware
    datetimes, but be defensive).
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


#: ``serie.freq`` values that carry no intraday component. Only ``daily`` and
#: ``1m`` exist in v2 today (761 039 and 244 324 series respectively).
_DAILY_FREQS = frozenset({"daily"})

#: The same set as a stable, ordered list for binding into ``freq = ANY(%s)``
#: predicates (psycopg adapts a Python list to a Postgres array; a tuple would
#: become a record). Sorting only makes the bound value deterministic. This is
#: the single source the roller-feed queries share with :func:`grain_for_freq`,
#: so a future daily synonym added to ``_DAILY_FREQS`` cannot silently diverge
#: "what counts as daily" between the two.
_DAILY_FREQ_LIST = sorted(_DAILY_FREQS)


def grain_for_freq(freq: str | None) -> str:
    """Return ``"daily"`` (ts → YYYYMMDD int) or ``"intraday"`` (ts → ISO 8601).

    Anything that is not explicitly a daily frequency is treated as intraday.
    That default is deliberate: emitting a full timestamp loses nothing, while
    collapsing one to a date destroys information — precisely the defect this
    fixes. A future ``5m``/``1h`` frequency therefore cannot reintroduce it.
    """
    return "daily" if (freq or "").strip().lower() in _DAILY_FREQS else "intraday"


def _bounds(start: date | None, end: date | None) -> tuple[date, date]:
    """Return an inclusive [lower, upper_exclusive) pair of date bounds.

    ``upper`` is the day AFTER *end* so the SQL uses ``ts < upper`` and captures
    an inclusive end date regardless of intraday ts. That is load-bearing, not
    defensive: v2 DOES hold intraday facts. Only ``daily`` facts are stored at
    00:00Z; ``1m`` facts carry a real time-of-day, verified live against the
    warehouse — a FUT_SP_500 minute-bar contract's busiest UTC date holds ~1 400
    rows with as many distinct times-of-day, and an OPT_SP_500_EW2 ``bbba`` serie
    71 of 71. See ``test_intraday_facts_carry_a_real_time_of_day_live`` in
    ``tests/integration/data/test_instruments_v2_integration.py``. Were ``upper``
    *end* itself, ``ts < end`` would drop every intraday row ON the end date.
    """
    lower = start if start is not None else _MIN_DATE
    end_incl = end if end is not None else _MAX_DATE
    return lower, end_incl + timedelta(days=1)


class SqlInstrumentReaderV2:
    """Read-only SQL adapter for the ``tcg_instruments_v2`` star schema."""

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ #
    # Dimension reads
    # ------------------------------------------------------------------ #
    async def list_objects(self) -> list[dict[str, Any]]:
        """List every object (all kinds) with its root metadata."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT object_id, kind, symbol, name, cycle,
                                   underlying_object_id
                            FROM {V2_SCHEMA}.object
                            ORDER BY kind, symbol"""
                    )
                    return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"v2 SQL error listing objects: {exc}") from exc

    async def get_object(self, object_id: int) -> dict[str, Any] | None:
        """Return one object row (or ``None``)."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT object_id, kind, symbol, name, cycle,
                                   underlying_object_id
                            FROM {V2_SCHEMA}.object
                            WHERE object_id = %s""",
                        (object_id,),
                    )
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading object {object_id}: {exc}"
            ) from exc

    async def list_series(self, object_id: int) -> list[dict[str, Any]]:
        """List an object's series (metadata only) — every serie, unfiltered.

        No join to ``contract`` at all, which is exactly why this survives with
        no production caller: it is the independent oracle in
        ``test_series_page_lists_object_level_series_live``. That test checks
        ``list_series_filtered``'s LEFT JOIN has not become semantically INNER,
        a regression that presents as "this object has no data" and that no
        assertion on an option root can see. An oracle sharing the join under
        test would collapse with it and stay green, so it has to come from a
        query shaped like this one. Do not "tidy" this away.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT serie_id, contract_id, type, freq, source
                            FROM {V2_SCHEMA}.serie
                            WHERE object_id = %s
                            ORDER BY serie_id""",
                        (object_id,),
                    )
                    return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error listing series for object {object_id}: {exc}"
            ) from exc

    async def fetch_object_facets(self, object_id: int) -> dict[str, Any]:
        """Aggregate the filterable dimensions of one object.

        Cheap by design — this is what the filter form is built from, so it must
        never scan a fact table. Three grouped reads over ``contract`` and
        ``serie`` only (measured 0.33 s + 0.37 s on object 12, the largest).
        Objects without contracts (index / rate) yield empty ``expirations`` and
        ``None`` strike bounds; that is a normal answer, not an error.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT expiration, COUNT(*) AS contracts
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = %s AND expiration IS NOT NULL
                            GROUP BY expiration
                            ORDER BY expiration DESC""",
                        (object_id,),
                    )
                    expirations = [
                        {
                            "expiration": r["expiration"].isoformat(),
                            "contracts": int(r["contracts"]),
                        }
                        for r in await cur.fetchall()
                    ]

                    await cur.execute(
                        f"""SELECT MIN(strike) AS strike_min,
                                   MAX(strike) AS strike_max,
                                   COUNT(*) AS contracts,
                                   ARRAY_AGG(DISTINCT option_type)
                                     FILTER (WHERE option_type IS NOT NULL)
                                     AS option_types
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = %s""",
                        (object_id,),
                    )
                    agg = await cur.fetchone() or {}

                    await cur.execute(
                        f"""SELECT type, freq, COUNT(*) AS series
                            FROM {V2_SCHEMA}.serie
                            WHERE object_id = %s
                            GROUP BY type, freq
                            ORDER BY type, freq""",
                        (object_id,),
                    )
                    serie_types = [
                        {
                            "type": r["type"],
                            "freq": r["freq"],
                            "series": int(r["series"]),
                        }
                        for r in await cur.fetchall()
                    ]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading facets for object {object_id}: {exc}"
            ) from exc

        return {
            "expirations": expirations,
            "strike_min": to_float(agg.get("strike_min")),
            "strike_max": to_float(agg.get("strike_max")),
            "option_types": sorted(agg.get("option_types") or []),
            "serie_types": serie_types,
            "totals": {
                "contracts": int(agg.get("contracts") or 0),
                "series": sum(s["series"] for s in serie_types),
            },
        }

    #: Whitelisted filter enum values. Validated before reaching SQL; the
    #: *values* are still bound as parameters, never interpolated.
    _SERIE_TYPES = frozenset(FACT_DISPATCH) | {"any"}
    _FREQS = frozenset({"1m", "daily", "any"})
    _OPTION_TYPES = frozenset({"call", "put", "both"})

    async def list_series_filtered(
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
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one filtered page of an object's series, plus the total count.

        LEFT JOIN, not INNER: object-level series (``contract_id IS NULL`` —
        index and rate objects) must still list. Contract metadata is returned
        joined so the caller needs no second round-trip and the frontend needs
        no contract_id → contract map.

        Ordering is ``expiration, strike, option_type, serie_id`` — a TOTAL
        order. This is correctness, not presentation: under a non-deterministic
        ORDER BY, LIMIT/OFFSET paging can repeat or skip rows between pages.
        ``serie_id`` is the unique tiebreaker, and it is load-bearing here:
        object 12 carries up to four series per contract (``value``/``greeks``
        daily, ``bar``/``bbba`` 1m), so the leading three keys are NOT unique.

        The count and the page share one ``WHERE`` clause string, so a filter can
        never apply to only one of them.
        """
        if serie_type not in self._SERIE_TYPES:
            raise DataAccessError(f"v2 unknown serie_type filter {serie_type!r}")
        if freq not in self._FREQS:
            raise DataAccessError(f"v2 unknown freq filter {freq!r}")
        if option_type not in self._OPTION_TYPES:
            raise DataAccessError(f"v2 unknown option_type filter {option_type!r}")

        where = ["s.object_id = %s"]
        params: list[Any] = [object_id]
        if serie_type != "any":
            where.append("s.type = %s")
            params.append(serie_type)
        if freq != "any":
            where.append("s.freq = %s")
            params.append(freq)
        if option_type != "both":
            where.append("c.option_type = %s")
            params.append(option_type)
        if expiration_min is not None:
            where.append("c.expiration >= %s")
            params.append(expiration_min)
        if expiration_max is not None:
            where.append("c.expiration <= %s")
            params.append(expiration_max)
        if strike_min is not None:
            where.append("c.strike >= %s")
            params.append(strike_min)
        if strike_max is not None:
            where.append("c.strike <= %s")
            params.append(strike_max)
        clause = " AND ".join(where)

        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT COUNT(*) AS total
                            FROM {V2_SCHEMA}.serie s
                            LEFT JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            WHERE {clause}""",
                        tuple(params),
                    )
                    row = await cur.fetchone()
                    total = int(row["total"]) if row else 0

                    await cur.execute(
                        f"""SELECT s.serie_id, s.contract_id, s.type, s.freq,
                                   s.source, c.contract_code, c.expiration,
                                   c.strike, c.option_type
                            FROM {V2_SCHEMA}.serie s
                            LEFT JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            WHERE {clause}
                            ORDER BY c.expiration NULLS FIRST,
                                     c.strike NULLS FIRST,
                                     c.option_type NULLS FIRST,
                                     s.serie_id
                            LIMIT %s OFFSET %s""",
                        (*params, limit, skip),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error listing filtered series for object "
                f"{object_id}: {exc}"
            ) from exc

        items = [
            {
                "serie_id": r["serie_id"],
                "contract_id": r["contract_id"],
                "type": r["type"],
                "freq": r["freq"],
                "source": r["source"],
                "contract_code": r["contract_code"],
                "expiration": r["expiration"].isoformat() if r["expiration"] else None,
                "strike": to_float(r["strike"]),
                "option_type": r["option_type"],
            }
            for r in rows
        ]
        return items, total

    async def get_serie(self, serie_id: int) -> dict[str, Any] | None:
        """Return one serie row (incl. ``type`` for fact-table dispatch)."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT serie_id, object_id, contract_id, type, freq, source
                            FROM {V2_SCHEMA}.serie
                            WHERE serie_id = %s""",
                        (serie_id,),
                    )
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading serie {serie_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Fact reads (dispatched by serie.type)
    # ------------------------------------------------------------------ #
    async def read_serie_facts(
        self,
        serie_id: int,
        serie_type: str,
        *,
        freq: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[str, list[int] | list[str], dict[str, list[float | None]]]:
        """Read one serie's facts from the fact table its ``type`` dispatches to.

        Returns ``(grain, ts, {field: [values...]})``. ``grain`` is
        ``"daily"`` (``ts`` are ``YYYYMMDD`` ints) or ``"intraday"`` (``ts`` are
        ISO 8601 strings) as decided by :func:`grain_for_freq` from *freq*.
        Collapsing an intraday ts to a date is what made minute series plot on a
        single abscissa, so the grain is resolved here, once. ``ts`` is bounded
        with a constant range so the planner prunes / BRIN-scans. Raises
        ``DataAccessError`` on an unknown ``serie_type`` (should never happen —
        the CHECK constrains it).
        """
        dispatch = FACT_DISPATCH.get(serie_type)
        if dispatch is None:
            raise DataAccessError(
                f"v2 unknown serie.type {serie_type!r} for serie {serie_id}"
            )
        table, fields = dispatch
        lower, upper = _bounds(start, end)
        col_list = ", ".join(fields)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT ts, {col_list}
                            FROM {V2_SCHEMA}.{table}
                            WHERE serie_id = %s
                              AND ts >= %s AND ts < %s
                            ORDER BY ts""",
                        (serie_id, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading {table} for serie {serie_id}: {exc}"
            ) from exc

        grain = grain_for_freq(freq)
        to_ts = _ts_to_int if grain == "daily" else _ts_to_iso
        ts_out: list[Any] = []
        cols: dict[str, list[float | None]] = {f: [] for f in fields}
        for r in rows:
            ts_out.append(to_ts(r["ts"]))
            for f in fields:
                cols[f].append(to_float(r[f]))
        return grain, ts_out, cols

    # ------------------------------------------------------------------ #
    # Futures continuous feed (for the reused ContinuousSeriesBuilder)
    # ------------------------------------------------------------------ #
    async def fetch_future_contract_bars(
        self,
        object_id: int,
        object_cycle: str | None,
    ) -> list[ContractPriceData]:
        """Fetch every future contract's DAILY bar series → ``ContractPriceData``.

        One :class:`ContractPriceData` per contract, sorted ascending by
        expiration (the ``ContinuousSeriesBuilder`` requires that ordering). Only
        ``bar``-type series are joined (a future's price lives in ``fact_bar``),
        pinned to the daily frequencies (``freq = ANY(_DAILY_FREQ_LIST)``) for the
        same reason as
        :meth:`fetch_future_front_closes`: FUT_SP_500 also carries ``bar:1m``
        series, and ``PriceSeries.dates`` are ``YYYYMMDD`` ints, so minute rows
        would both blow the statement timeout (this is called with an unbounded
        ``ts`` range) and collapse onto duplicate dates inside a contract —
        corrupt input to the roller, not merely slow input. The whole
        per-contract history is pulled (the roller trims); ``ts`` is still
        constant-bounded to the sentinel span so the planner can BRIN-scan.
        ``expiration_cycle`` is stamped from the object's single cycle (v2 has no
        per-contract cycle) so END_OF_MONTH collapse behaves.
        """
        lower, upper = _bounds(None, None)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT c.contract_code, c.expiration,
                                   f.ts, f.open, f.high, f.low, f.close, f.volume
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_bar f
                              ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'bar'
                              AND s.freq = ANY(%s)
                              AND c.expiration IS NOT NULL
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY c.expiration, c.contract_code, f.ts""",
                        (object_id, _DAILY_FREQ_LIST, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error fetching future bars for object {object_id}: {exc}"
            ) from exc

        grouped: dict[str, dict[str, Any]] = {}
        for r in rows:
            code = r["contract_code"]
            bucket = grouped.get(code)
            if bucket is None:
                bucket = {
                    "expiration": date_to_int(r["expiration"]),
                    "dates": [],
                    "open": [],
                    "high": [],
                    "low": [],
                    "close": [],
                    "volume": [],
                }
                grouped[code] = bucket
            bucket["dates"].append(_ts_to_int(r["ts"]))
            bucket["close"].append(to_float_or(r["close"], 0.0))
            bucket["open"].append(to_float_or(r["open"], 0.0))
            bucket["high"].append(to_float_or(r["high"], 0.0))
            bucket["low"].append(to_float_or(r["low"], 0.0))
            bucket["volume"].append(to_float_or(r["volume"], 0.0))

        contracts: list[ContractPriceData] = []
        for code, b in grouped.items():
            if not b["dates"]:
                continue
            contracts.append(
                ContractPriceData(
                    contract_id=code,
                    expiration=b["expiration"],
                    expiration_cycle=object_cycle,
                    prices=PriceSeries(
                        dates=np.array(b["dates"], dtype=np.int64),
                        open=np.array(b["open"], dtype=np.float64),
                        high=np.array(b["high"], dtype=np.float64),
                        low=np.array(b["low"], dtype=np.float64),
                        close=np.array(b["close"], dtype=np.float64),
                        volume=np.array(b["volume"], dtype=np.float64),
                    ),
                )
            )
        contracts.sort(key=lambda c: c.expiration)
        return contracts

    async def fetch_future_cycles(self, object_id: int) -> list[str]:
        """Return the object's listing cycle(s).

        v2 carries a single ``cycle`` per object (not per contract), so this is
        ``[object.cycle]`` when set, else an empty list.
        """
        obj = await self.get_object(object_id)
        if obj is None or not obj.get("cycle"):
            return []
        return [obj["cycle"]]

    # ------------------------------------------------------------------ #
    # Options selection reads (settlement values)
    # ------------------------------------------------------------------ #
    async def fetch_option_settlements(
        self,
        object_id: int,
        option_type: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch per-date settlement rows for one option object + option_type.

        Returns rows ``{ts_int, contract_id, contract_code, expiration_int,
        strike, value}`` for every ``value``-serie whose contract matches
        *option_type*, over the ``[start, end]`` window. ``ts`` is constant-
        bounded (BRIN/prune). Zero/NULL settlements are NOT filtered here — the
        resolver applies the ``> 0`` guard so it can surface dropped dates.
        """
        lower, upper = _bounds(start, end)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.ts, c.contract_id, c.contract_code,
                                   c.expiration, c.strike, f.value
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_value f
                              ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'value'
                              AND c.option_type = %s
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY f.ts, c.expiration, c.strike""",
                        (object_id, option_type, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error fetching option settlements for object "
                f"{object_id}: {exc}"
            ) from exc

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "ts_int": _ts_to_int(r["ts"]),
                    "contract_id": r["contract_id"],
                    "contract_code": r["contract_code"],
                    "expiration_int": date_to_int(r["expiration"]),
                    "strike": to_float(r["strike"]),
                    "value": to_float(r["value"]),
                }
            )
        return out

    async def fetch_option_expirations(
        self,
        object_id: int,
        option_type: str,
    ) -> list[int]:
        """Return sorted distinct contract expirations (YYYYMMDD ints).

        Reads the ``contract`` dimension for one option object + option_type —
        the tradeable expiration *chain*, independent of whether settlement
        facts exist on any given day. The options-continuous resolver uses this
        to determine the active (front) expiration per date, so a settlement
        data hole in the true front contract cannot spuriously advance (or
        rewind) the AtExpiry roll. The whole chain is returned (no ts window):
        the front for a date near a window edge may be an expiration outside the
        windowed settlement set.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT DISTINCT expiration
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = %s
                              AND option_type = %s
                              AND expiration IS NOT NULL
                            ORDER BY expiration""",
                        (object_id, option_type),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error fetching option expirations for object "
                f"{object_id}: {exc}"
            ) from exc
        return [date_to_int(r["expiration"]) for r in rows]

    async def fetch_future_front_closes(
        self,
        object_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch every DAILY future bar row (ts, expiration, close) for spot lookup.

        Feeds the options-continuous *moneyness* spot: the resolver picks, per
        date, the front future (nearest expiration >= that date) close. Pinned to
        the daily frequencies (``freq = ANY(_DAILY_FREQ_LIST)``) — FUT_SP_500 also
        carries ``bar:1m`` series, and
        without the pin this scans minute bars (timeout) and makes the per-date
        front close the 00:00 bar rather than the daily close. Only
        ``close > 0`` rows are returned (false-zero guard). ``ts``
        constant-bounded.
        """
        lower, upper = _bounds(start, end)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.ts, c.expiration, f.close
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_bar f
                              ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'bar'
                              AND s.freq = ANY(%s)
                              AND c.expiration IS NOT NULL
                              AND f.close > 0
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY f.ts, c.expiration""",
                        (object_id, _DAILY_FREQ_LIST, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error fetching future front closes for object "
                f"{object_id}: {exc}"
            ) from exc

        return [
            {
                "ts_int": _ts_to_int(r["ts"]),
                "expiration_int": date_to_int(r["expiration"]),
                "close": to_float_or(r["close"], 0.0),
            }
            for r in rows
        ]
