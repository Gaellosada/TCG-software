"""Reusable benchmark harness for OPTIONS-based backtesting simulations.

Wave 1 (profiling) of optimize-options-simulation-perf.

Runs IN-PROCESS against the LIVE dwh (no HTTP, caches bypassed) and produces a
per-phase / per-query profile of ``resolve_option_stream`` — the shared hot path
for both signals (``POST /api/signals/compute``) and portfolios
(``POST /api/portfolio/compute``).

It is deliberately kept OUT of the ``tcg/`` package so it can never affect
engine byte-identity. It instruments the dwh round-trips by monkeypatching the
two SQL reader methods (``SqlOptionsDataReader.query_chain`` /
``query_chain_bulk``) — no edit to any file under ``tcg/`` is required for the
query-level metrics.

Phase attribution (for the matrix maturities used here):
  * ``list_option_expirations_*`` calls  -> Phase A prep (distinct index scan).
  * ``query_chain``  calls  -> Phase-B per-expiration-group strike-window PROBE
                               (stream_resolver.py:~1353). (NearestToTarget's
                               Phase-A probe is skipped because we pass
                               ``available_expirations``.)
  * ``query_chain_bulk`` calls -> Phase-B bulk chain fetch (the asyncio.gather
                               at stream_resolver.py:1456-1457).
  * compute (Phase C) = total wall - all dwh wall time (per-date selection is
                               pure CPU except ByMoneyness).

Usage:
    .venv/bin/python bench_options.py            # full matrix
    .venv/bin/python bench_options.py --quick    # short-range cells only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import date

# ── dwh wiring (via app config loaders; never reads .env directly) ──
from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data import create_services
from tcg.data._sql import options as _sql_options

from tcg.core.api._models import OptionStreamRef
from tcg.core.api._options_materialise import (
    materialise_option_streams,
    _business_dates_in_range,
    fetch_nearest_target_expirations_by_date,
)
from tcg.core.api._options_wiring import build_stream_resolver_wiring
from tcg.core.api.options import (
    _criterion_pydantic_to_dataclass,
    _maturity_pydantic_to_dataclass,
    _roll_offset_pydantic_to_dataclass,
)
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import expand_cycle


# ─────────────────────────────────────────────────────────────────────
# Query instrumentation (monkeypatch the SQL reader)
# ─────────────────────────────────────────────────────────────────────
@dataclass
class QueryEvent:
    method: str  # "query_chain" | "query_chain_bulk"
    t_start: float
    t_end: float
    n_dates: int  # 1 for query_chain, len(dates) for bulk
    n_rows: int  # total rows/contracts returned

    @property
    def dur(self) -> float:
        return self.t_end - self.t_start


@dataclass
class Recorder:
    events: list[QueryEvent] = field(default_factory=list)

    def reset(self) -> None:
        self.events = []

    # ---- aggregates ----
    def by_method(self, method: str) -> list[QueryEvent]:
        return [e for e in self.events if e.method == method]

    def dwh_wall_serialized(self) -> float:
        """Sum of individual query durations (what a serial loop would cost)."""
        return sum(e.dur for e in self.events)

    def dwh_wall_union(self) -> float:
        """Union of query time intervals (real dwh busy wall, accounts for
        parallel overlap under asyncio.gather)."""
        if not self.events:
            return 0.0
        ivs = sorted((e.t_start, e.t_end) for e in self.events)
        total = 0.0
        cur_s, cur_e = ivs[0]
        for s, e in ivs[1:]:
            if s > cur_e:
                total += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        total += cur_e - cur_s
        return total

    def max_parallelism(self) -> int:
        """Peak number of concurrently in-flight queries (detects pool
        serialization: if this stays 1, queries are NOT running in parallel)."""
        pts = []
        for e in self.events:
            pts.append((e.t_start, 1))
            pts.append((e.t_end, -1))
        pts.sort(key=lambda x: (x[0], x[1]))
        cur = mx = 0
        for _t, d in pts:
            cur += d
            mx = max(mx, cur)
        return mx

    def total_rows(self) -> int:
        return sum(e.n_rows for e in self.events)


REC = Recorder()


def install_instrumentation() -> None:
    Reader = _sql_options.SqlOptionsDataReader
    orig_chain = Reader.query_chain
    orig_bulk = Reader.query_chain_bulk

    async def timed_chain(self, *args, **kwargs):
        t0 = time.perf_counter()
        out = await orig_chain(self, *args, **kwargs)
        t1 = time.perf_counter()
        REC.events.append(QueryEvent("query_chain", t0, t1, 1, len(out) if out else 0))
        return out

    async def timed_bulk(self, *args, **kwargs):
        t0 = time.perf_counter()
        out = await orig_bulk(self, *args, **kwargs)
        t1 = time.perf_counter()
        n_rows = sum(len(v) for v in out.values()) if out else 0
        n_dates = len(out) if out else 0
        REC.events.append(QueryEvent("query_chain_bulk", t0, t1, n_dates, n_rows))
        return out

    Reader.query_chain = timed_chain  # type: ignore[assignment]
    Reader.query_chain_bulk = timed_bulk  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────
# Ref builders (representative option strategies)
# ─────────────────────────────────────────────────────────────────────
def put_ref(
    target_delta: float, *, cycle=None, hold=False, stream=None
) -> OptionStreamRef:
    """A short-Δ SPX put option-stream ref, NextThirdFriday monthly roll."""
    payload = {
        "type": "option_stream",
        "collection": "OPT_SP_500",
        "option_type": "P",
        "cycle": cycle,
        "maturity": {"kind": "next_third_friday", "offset_months": 1},
        "selection": {
            "kind": "by_delta",
            "target": target_delta,
            "tolerance": 0.1,
            "strict": False,
        },
    }
    if stream is not None:
        payload["stream"] = stream
    if hold:
        payload["hold_between_rolls"] = True
        payload["nav_times"] = 1.0
    return OptionStreamRef(**payload)


@dataclass
class Cell:
    name: str
    ref: OptionStreamRef
    start: date
    end: date
    hold: bool = False


# ─────────────────────────────────────────────────────────────────────
# Runners
# ─────────────────────────────────────────────────────────────────────
async def run_stream(svc, ref: OptionStreamRef, start: date, end: date, *, hold: bool):
    """Resolve one option stream through the real engine path, uncached.

    Non-hold -> materialise_option_streams (the /options/stream + indicators +
    portfolio-level-leg display path). Hold -> resolve_option_stream directly with
    hold_between_rolls=True (the portfolio premium-leg / signals P&L path)."""
    REC.reset()
    t0 = time.perf_counter()
    if not hold:
        result = await materialise_option_streams(
            [("bench", ref)], svc=svc, start_date=start, end_date=end
        )
        assert not isinstance(result, str), result
        _d, values, diags, _c = result["bench"]
    else:
        trade_dates = _business_dates_in_range(start, end)
        _cycle = expand_cycle(ref.cycle)
        # Arity-robust: iter1 (1b1a879) wiring returns a 4-tuple; iter2 (probe
        # removal) adds a 5th root_underlying_resolver.  Unpack the first four and
        # keep the optional 5th so this bench runs under BOTH for A/B.
        _wiring = build_stream_resolver_wiring(
            svc, underlying_prefetch_window=(trade_dates[0], trade_dates[-1])
        )
        chain_reader, mat_resolver, ul_resolver, bulk_reader = _wiring[:4]
        _root_ul_resolver = _wiring[4] if len(_wiring) > 4 else None
        import inspect as _inspect

        _extra_kw = (
            {"root_underlying_resolver": _root_ul_resolver}
            if "root_underlying_resolver"
            in _inspect.signature(resolve_option_stream).parameters
            else {}
        )
        all_exps = await svc.list_option_expirations_filtered(
            ref.collection, option_type=ref.option_type, cycle=_cycle
        )
        _maturity = _maturity_pydantic_to_dataclass(ref.maturity)
        by_date = await fetch_nearest_target_expirations_by_date(
            svc=svc,
            maturity=_maturity,
            collection=ref.collection,
            option_type=ref.option_type,
            cycle=_cycle,
            trade_dates=trade_dates,
        )
        hold_out: dict = {}
        values, diags, _c = await resolve_option_stream(
            dates=trade_dates,
            collection=ref.collection,
            option_type=ref.option_type,
            cycle=_cycle,
            maturity=_maturity,
            selection=_criterion_pydantic_to_dataclass(ref.selection),
            stream=ref.stream,
            roll_offset=_roll_offset_pydantic_to_dataclass(ref.roll_offset),
            chain_reader=chain_reader,
            maturity_resolver=mat_resolver,
            underlying_price_resolver=ul_resolver,
            bulk_chain_reader=bulk_reader,
            available_expirations=all_exps,
            available_expirations_by_date=by_date,
            hold_between_rolls=True,
            hold_roll_info_out=hold_out,
            **_extra_kw,
        )
    total = time.perf_counter() - t0
    n_ok = int(sum(1 for v in values if v == v))  # non-NaN
    return {
        "total": total,
        "events": list(REC.events),
        "n_dates": len(values),
        "n_ok": n_ok,
    }


def summarize(name: str, res: dict) -> dict:
    rec = Recorder(res["events"])
    chain = rec.by_method("query_chain")
    bulk = rec.by_method("query_chain_bulk")
    dwh_union = rec.dwh_wall_union()
    dwh_serial = rec.dwh_wall_serialized()
    probe_t = sum(e.dur for e in chain)
    bulk_t = sum(e.dur for e in bulk)
    total = res["total"]
    compute = max(total - dwh_union, 0.0)
    import statistics as _stat

    bulk_durs = sorted(e.dur for e in bulk)
    probe_durs = sorted(e.dur for e in chain)
    bulk_p50 = _stat.median(bulk_durs) if bulk_durs else 0.0
    bulk_max = bulk_durs[-1] if bulk_durs else 0.0
    probe_p50 = _stat.median(probe_durs) if probe_durs else 0.0
    return {
        "bulk_p50": bulk_p50,
        "bulk_max": bulk_max,
        "probe_p50": probe_p50,
        "name": name,
        "total": total,
        "n_dates": res["n_dates"],
        "n_ok": res["n_ok"],
        "n_probe": len(chain),
        "probe_t": probe_t,
        "n_bulk": len(bulk),
        "bulk_t": bulk_t,
        "dwh_union": dwh_union,
        "dwh_serial": dwh_serial,
        "compute": compute,
        "rows": rec.total_rows(),
        "max_par": rec.max_parallelism(),
        "events": res["events"],
    }


def print_row(s: dict) -> None:
    print(
        f"{s['name']:<34} tot={s['total']:6.2f}s  dwh(union)={s['dwh_union']:6.2f}s "
        f"({100 * s['dwh_union'] / s['total']:4.0f}%)  compute={s['compute']:5.2f}s  "
        f"probe={s['n_probe']:>3}q/{s['probe_t']:5.2f}s  bulk={s['n_bulk']:>3}q/{s['bulk_t']:5.2f}s  "
        f"rows={s['rows']:>7}  par={s['max_par']}  dates={s['n_dates']} ok={s['n_ok']}"
    )
    print(
        f"{'':<34}   latency/query: bulk p50={s['bulk_p50']:.2f}s max={s['bulk_max']:.2f}s "
        f"(avg={s['bulk_t'] / s['n_bulk'] if s['n_bulk'] else 0:.2f}s over {s['n_bulk']}q)  "
        f"probe p50={s['probe_p50']:.2f}s  |  serial-if-no-parallelism={s['dwh_serial']:.1f}s "
        f"-> union={s['dwh_union']:.1f}s (parallel savings {s['dwh_serial'] - s['dwh_union']:.0f}s)"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="short-range cells only")
    args = ap.parse_args()

    pool = DwhConnectionPool(**load_dwh_config())
    await pool.connect()
    svc = (await create_services(pool))["market_data"]
    install_instrumentation()

    Y1 = (date(2024, 1, 1), date(2024, 12, 31))
    Y10 = (date(2015, 1, 1), date(2025, 1, 1))

    cells: list[Cell] = [
        Cell("signal 10Δ put  1yr(2024)", put_ref(0.10), *Y1),
        Cell("signal 50Δ put  1yr(2024)", put_ref(0.50), *Y1),
    ]
    if not args.quick:
        cells += [
            Cell("signal 10Δ put  10yr(15-25)", put_ref(0.10), *Y10),
            Cell("signal 50Δ put  10yr(15-25)", put_ref(0.50), *Y10),
            Cell(
                "portfolio HOLD 25Δ put 1yr", put_ref(0.25, hold=True), *Y1, hold=True
            ),
            Cell(
                "portfolio HOLD 25Δ put 10yr", put_ref(0.25, hold=True), *Y10, hold=True
            ),
        ]

    summaries: list[dict] = []
    print("\n=== MATRIX (uncached, live dwh) ===")
    for c in cells:
        try:
            res = await run_stream(svc, c.ref, c.start, c.end, hold=c.hold)
            s = summarize(c.name, res)
            summaries.append(s)
            print_row(s)
        except Exception as exc:  # noqa: BLE001
            print(f"{c.name:<34} ERROR/TIMEOUT: {type(exc).__name__}: {exc}")

    # ── Iterative-dev scenario: 10Δ THEN 50Δ over the SAME underlying/range ──
    print("\n=== ITERATIVE-DEV (10Δ then 50Δ, same OPT_SP_500 range) ===")
    for label, rng in [("1yr(2024)", Y1)] + (
        [] if args.quick else [("10yr(15-25)", Y10)]
    ):
        r10 = summarize(
            f"iter 10Δ {label}", await run_stream(svc, put_ref(0.10), *rng, hold=False)
        )
        r50 = summarize(
            f"iter 50Δ {label}", await run_stream(svc, put_ref(0.50), *rng, hold=False)
        )
        print_row(r10)
        print_row(r50)
        overlap_iterative_dev(r10, r50, label)

    await pool.close()


def _bulk_signature_set(events: list[QueryEvent]) -> tuple[int, int, int]:
    """(n_bulk_queries, total_bulk_dates, total_bulk_rows) — the bulk fetch
    footprint. For two runs over the SAME range+collection+type+maturity, the
    EXPIRATION GROUPS and their date-sets are IDENTICAL (selection delta only
    changes the strike WINDOW, not which dates/expirations are fetched)."""
    bulk = [e for e in events if e.method == "query_chain_bulk"]
    return (len(bulk), sum(e.n_dates for e in bulk), sum(e.n_rows for e in bulk))


def overlap_iterative_dev(r10: dict, r50: dict, label: str) -> None:
    b10 = _bulk_signature_set(r10["events"])
    b50 = _bulk_signature_set(r50["events"])
    total_rt_10 = r10["n_probe"] + r10["n_bulk"]
    total_rt_50 = r50["n_probe"] + r50["n_bulk"]
    combined_rt = total_rt_10 + total_rt_50
    combined_t = r10["total"] + r50["total"]
    combined_rows = r10["rows"] + r50["rows"]
    # The bulk fetches address the SAME expiration groups / date-sets; a chain
    # cache keyed on (collection,type,expiration,date-set) would serve the 2nd
    # run's bulk (and its probe) from the 1st run's fetch. Redundant work in the
    # 2nd run = its ENTIRE dwh footprint (identical group structure).
    redundant_rt = total_rt_50
    redundant_t = r50["dwh_union"]
    redundant_rows = r50["rows"]
    print(
        f"  [{label}] round-trips: {total_rt_10}+{total_rt_50}={combined_rt}  "
        f"identical-groups(bulk sig 10Δ vs 50Δ): {b10} vs {b50}  match={b10[:2] == b50[:2]}"
    )
    print(
        f"  [{label}] iterative-dev re-fetch CEILING (2nd run fully cache-served): "
        f"round-trips {redundant_rt}/{combined_rt} = {100 * redundant_rt / combined_rt:.0f}%  |  "
        f"dwh-time {redundant_t:.2f}s/{combined_t:.2f}s total-wall = up to {100 * redundant_t / combined_t:.0f}%  |  "
        f"rows {redundant_rows}/{combined_rows} = {100 * redundant_rows / combined_rows:.0f}%"
    )


if __name__ == "__main__":
    asyncio.run(main())
