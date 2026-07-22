"""Ephemeral PostgreSQL fixture for REAL-SQL execution tests.

The other tests in this directory drive ``SqlOptionsDataReader`` through a *fake*
cursor that only string-asserts the emitted SQL; they cannot catch a semantic
drift between the real SQL and its Python reference (e.g. reverting the
``expiration_cycle`` predicate in ``query_held_rows`` leaves them green).  This
fixture spins up a throwaway PostgreSQL server, seeds a tiny chain reproducing
the dwh quirks that actually bit, and lets a test run the *real* queries so a
regression goes RED in unit CI.

Feasibility / portability
--------------------------
No Docker and no server binary is on ``PATH`` in this environment, and the
``pgserver`` wheel has no CPython-3.14 build, so ``pytest-postgresql`` /
``testcontainers`` cannot run here out of the box.  We instead *discover* a
PostgreSQL ``initdb``/``postgres`` pair and spin the server ourselves:

  1. ``$TCG_TEST_PG_BINDIR`` (explicit override), then
  2. ``~/.cache/tcg-test-pg/usr/lib/postgresql/*/bin`` (an extracted apt server
     tree — how this repo's dev box provides a server without root), then
  3. system ``/usr/lib/postgresql/*/bin`` and ``shutil.which("initdb")``.

When none is found the fixture ``pytest.skip``s the whole module (it degrades
cleanly, exactly like ``pytest-postgresql`` would).  This is a real *unit* test
(offline, no ``integration`` marker) wherever a server binary is discoverable.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date

import psycopg
import pytest


# --------------------------------------------------------------------------- #
# Server-binary discovery
# --------------------------------------------------------------------------- #
def _env_truthy(value: str | None) -> bool:
    """A CI-style truthy env test: unset/empty/0/false/no/off are falsey."""
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


def _candidate_bindirs() -> list[str]:
    cands: list[str] = []
    env = os.environ.get("TCG_TEST_PG_BINDIR")
    if env:
        cands.append(env)
    cands.extend(
        sorted(
            glob.glob(
                os.path.expanduser("~/.cache/tcg-test-pg/usr/lib/postgresql/*/bin")
            )
        )
    )
    cands.extend(sorted(glob.glob("/usr/lib/postgresql/*/bin")))
    which = shutil.which("initdb")
    if which:
        cands.append(os.path.dirname(which))
    # de-dupe, keep order, keep only dirs holding BOTH initdb and postgres
    seen: set[str] = set()
    out: list[str] = []
    for d in cands:
        d = os.path.abspath(d)
        if d in seen:
            continue
        seen.add(d)
        if os.path.isfile(os.path.join(d, "initdb")) and os.path.isfile(
            os.path.join(d, "postgres")
        ):
            out.append(d)
    return out


def _libdirs_for(bindir: str) -> list[str]:
    """Shared-lib dirs the extracted server binaries need at runtime."""
    libs: list[str] = []
    priv = os.path.normpath(os.path.join(bindir, "..", "lib"))
    if os.path.isdir(priv):
        libs.append(priv)
    m = re.match(r"(.*)/lib/postgresql/\d+/bin$", bindir)
    if m:
        ma = os.path.join(m.group(1), "lib", "x86_64-linux-gnu")
        if os.path.isdir(ma):
            libs.append(ma)
    return libs


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass(frozen=True)
class SeededDwh:
    """Connection params for the ephemeral server + the seed's identifiers.

    The seed constants travel on the fixture value (not module imports) because
    the ``tests/`` tree has no ``__init__.py`` — a test module cannot
    ``from .conftest import ...``.
    """

    host: str  # socket directory (libpq treats a leading-'/' host as a socket dir)
    port: int
    db: str
    user: str
    password: str
    root: str
    e1: date
    e2: date
    e3: date
    e4: date
    e1_dates: tuple[date, ...]
    e2_dates: tuple[date, ...]
    held_dates: tuple[date, ...]
    e4_dates: tuple[date, ...]


# --------------------------------------------------------------------------- #
# Seed — a ~30-row chain reproducing the quirks that bit (see module docstring
# of tcg/data/_sql/options.py + the test file).
# --------------------------------------------------------------------------- #
ROOT = "OPT_TEST"
E1 = date(2024, 3, 15)  # delta-pushdown group (2 trade dates)
E2 = date(2024, 4, 19)  # delta tie group
E3 = date(2024, 3, 22)  # held-rows cross-cycle group
E4 = date(2024, 5, 17)  # >k RANK-1 OVERFLOW group (item E)
E1_DATES = (date(2024, 3, 1), date(2024, 3, 6))
E2_DATES = (date(2024, 4, 1),)
HELD_DATES = (date(2024, 3, 5), date(2024, 3, 6))
E4_DATES = (date(2024, 5, 2),)

_DDL = """
CREATE SCHEMA tcg_instruments;

CREATE TABLE tcg_instruments.dim_instrument (
    instrument_id     bigint PRIMARY KEY,
    symbol            text NOT NULL,
    source_collection text NOT NULL,
    asset_class       text NOT NULL,
    root_symbol       text,
    underlying_symbol text,
    expiration        date,
    expiration_cycle  text,
    strike            numeric,
    option_type       text,
    contract_size     numeric,
    currency          text,
    provider          text
);

CREATE TABLE tcg_instruments.fact_price_eod (
    instrument_id bigint NOT NULL,
    trade_date    date   NOT NULL,
    bid           numeric,
    ask           numeric,
    close         numeric,
    volume        numeric,
    open_interest numeric,
    PRIMARY KEY (instrument_id, trade_date)
);

CREATE TABLE tcg_instruments.fact_option_greeks (
    instrument_id   bigint NOT NULL,
    trade_date      date   NOT NULL,
    delta           double precision,
    gamma           double precision,
    vega            double precision,
    theta           double precision,
    implied_vol     double precision,
    underlying_price double precision,
    PRIMARY KEY (instrument_id, trade_date)
);
"""


def _dim_rows() -> list[tuple]:
    # (iid, symbol, cycle, strike, expiration)
    spec = [
        # E1 delta group — winner SYM_W is a DUP symbol (two iids, same symbol/strike).
        # RANK-DISCRIMINATING seed: TEN non-null-delta symbols with DISTINCT
        # best-|delta-target| distances (target -0.10), so a k=2 pushdown (fetch
        # k+1=3) EXCLUDES seven of them and the correct pick is unambiguous.  This
        # is what makes ``test_delta_pushdown_matches_full_chain`` actually
        # discriminate the SQL ORDER BY: invert ``best_dist ASC``->``DESC`` and the
        # pushdown returns the three FARTHEST symbols (winner absent) -> RED.  With
        # only the old 3 non-null symbols and k+1=3 the pushdown returned ALL of
        # them regardless of rank order (rank-blind), so the test passed either way.
        (10, "OPT_TEST_4800P", "M", 4800, E1),  # delta -0.05, dist 0.05
        (12, "OPT_TEST_4820P", "M", 4820, E1),  # delta -0.12, dist 0.02 (2nd closest)
        (13, "OPT_TEST_4870P", "M", 4870, E1),  # delta -0.07, dist 0.03 (3rd closest)
        (14, "OPT_TEST_4700P", "M", 4700, E1),  # delta -0.17, dist 0.07
        (15, "OPT_TEST_4680P", "M", 4680, E1),  # delta -0.19, dist 0.09
        (16, "OPT_TEST_4650P", "M", 4650, E1),  # delta -0.23, dist 0.13
        (17, "OPT_TEST_4600P", "M", 4600, E1),  # delta -0.28, dist 0.18
        (18, "OPT_TEST_4550P", "M", 4550, E1),  # delta -0.35, dist 0.25 (farthest)
        (20, "OPT_TEST_4900P", "M", 4900, E1),  # delta -0.30, dist 0.20
        (30, "OPT_TEST_4950P", "M", 4950, E1),  # SYM_W near sibling (lower iid)
        (31, "OPT_TEST_4950P", "M", 4950, E1),  # SYM_W far sibling (same symbol)
        (40, "OPT_TEST_5000P", "M", 5000, E1),  # NULL-delta symbol
        # E2 delta TIE group (two symbols equidistant; lower strike wins).
        (50, "OPT_TEST_4850P", "M", 4850, E2),
        (60, "OPT_TEST_4870P", "M", 4870, E2),
        (70, "OPT_TEST_4900P_E2", "M", 4900, E2),
        # E3 held-rows CROSS-CYCLE dup: ONE symbol, two iids under M vs W3 Friday.
        (110, "OPT_TEST_4970P", "M", 4970, E3),  # lower iid -> first-by-iid on revert
        (111, "OPT_TEST_4970P", "W3 Friday", 4970, E3),
        # E4 >k OVERFLOW group (item E): TWO distinct symbols sharing the SAME
        # strike AND the SAME delta (an exact rank-1 tie).  This is the ONLY
        # regime where top-k can drop match_by_delta's pick: distinct-strike ties
        # never bite (both rank by lower-strike, so the winner is always kept),
        # but a same-strike tie is broken by option_symbol in SQL vs by input
        # (instrument_id) order in match_by_delta.  Here the iid order (ZZ=210 <
        # AA=220) is the OPPOSITE of the symbol-name order (AA < ZZ), so a naive
        # top-1 keeps AA while match_by_delta over the full chain picks ZZ.
        (210, "OPT_TEST_ZZ", "M", 4900, E4),  # lower iid -> full-chain winner
        (220, "OPT_TEST_AA", "M", 4900, E4),  # lower symbol -> SQL top-1 keeps this
    ]
    return [
        (
            iid,
            sym,
            ROOT,
            "option",
            "SPX",
            "SPX",
            exp,
            cyc,
            strike,
            "P",
            50,
            "USD",
            "TEST",
        )
        for (iid, sym, cyc, strike, exp) in spec
    ]


def _greeks_rows() -> list[tuple]:
    # (iid, delta) per trade date it trades on
    deltas = {
        10: -0.05,
        12: -0.12,  # dist 0.02 (2nd closest)
        13: -0.07,  # dist 0.03 (3rd closest)
        14: -0.17,  # dist 0.07
        15: -0.19,  # dist 0.09
        16: -0.23,  # dist 0.13
        17: -0.28,  # dist 0.18
        18: -0.35,  # dist 0.25 (farthest)
        20: -0.30,
        30: -0.10,  # SYM_W near: exact target -> unique winner
        31: -0.45,  # SYM_W far sibling
        40: None,  # NULL delta
        50: -0.09,  # tie (dist 0.01), lower strike 4850
        60: -0.11,  # tie (dist 0.01), strike 4870
        70: -0.50,
        110: -0.10,
        111: -0.10,
        210: -0.10,  # E4 overflow tie (exact target)
        220: -0.10,  # E4 overflow tie (exact target)
    }
    dates_for = {
        10: E1_DATES,
        12: E1_DATES,
        13: E1_DATES,
        14: E1_DATES,
        15: E1_DATES,
        16: E1_DATES,
        17: E1_DATES,
        18: E1_DATES,
        20: E1_DATES,
        30: E1_DATES,
        31: E1_DATES,
        40: E1_DATES,
        50: E2_DATES,
        60: E2_DATES,
        70: E2_DATES,
        110: HELD_DATES,
        111: HELD_DATES,
        210: E4_DATES,
        220: E4_DATES,
    }
    rows: list[tuple] = []
    for iid, d in deltas.items():
        for td in dates_for[iid]:
            rows.append((iid, td, d, 0.01, 1.0, -0.5, 0.20, 5000.0))
    return rows


def _price_rows() -> list[tuple]:
    # (iid, bid, ask, close) per trade date; mid = (bid+ask)/2
    quotes = {
        10: (5.0, 5.2),
        12: (6.0, 6.2),
        13: (6.5, 6.7),
        14: (8.0, 8.2),
        15: (8.5, 8.7),
        16: (9.0, 9.2),
        17: (10.0, 10.2),
        18: (11.0, 11.2),
        20: (30.0, 30.4),
        30: (12.0, 12.2),  # SYM_W near -> mid 12.1 (the resolved row)
        31: (40.0, 40.2),  # SYM_W far  -> mid 40.1
        40: (60.0, 60.4),
        50: (9.0, 9.2),
        60: (11.0, 11.2),
        70: (50.0, 50.4),
        110: (7.30, 7.50),  # M sibling  -> mid 7.40 (WRONG on revert)
        111: (4.10, 4.20),  # W3 Friday  -> mid 4.15 (correct)
        210: (7.0, 7.4),  # ZZ -> mid 7.20 (the correct full-chain pick)
        220: (3.0, 3.4),  # AA -> mid 3.20 (the WRONG naive-top-1 pick)
    }
    dates_for = {
        10: E1_DATES,
        12: E1_DATES,
        13: E1_DATES,
        14: E1_DATES,
        15: E1_DATES,
        16: E1_DATES,
        17: E1_DATES,
        18: E1_DATES,
        20: E1_DATES,
        30: E1_DATES,
        31: E1_DATES,
        40: E1_DATES,
        50: E2_DATES,
        60: E2_DATES,
        70: E2_DATES,
        110: HELD_DATES,
        111: HELD_DATES,
        210: E4_DATES,
        220: E4_DATES,
    }
    rows: list[tuple] = []
    for iid, (bid, ask) in quotes.items():
        for td in dates_for[iid]:
            rows.append((iid, td, bid, ask, (bid + ask) / 2.0, 100, 200))
    return rows


def _seed(conninfo: str) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            cur.executemany(
                "INSERT INTO tcg_instruments.dim_instrument "
                "(instrument_id, symbol, source_collection, asset_class, root_symbol, "
                " underlying_symbol, expiration, expiration_cycle, strike, option_type, "
                " contract_size, currency, provider) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                _dim_rows(),
            )
            cur.executemany(
                "INSERT INTO tcg_instruments.fact_price_eod "
                "(instrument_id, trade_date, bid, ask, close, volume, open_interest) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                _price_rows(),
            )
            cur.executemany(
                "INSERT INTO tcg_instruments.fact_option_greeks "
                "(instrument_id, trade_date, delta, gamma, vega, theta, implied_vol, "
                " underlying_price) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                _greeks_rows(),
            )


def _select_bindir() -> str:
    """Return the pg server bindir, else skip (or HARD-FAIL under the CI gate).

    Pure w.r.t. its inputs (env + ``_candidate_bindirs``) so it is unit-testable
    without spinning a server.  When no server binary is discoverable this
    normally ``pytest.skip``s the module (clean offline degradation); when
    ``$TCG_REQUIRE_PG_TESTS`` is truthy it ``pytest.fail``s instead, so a
    runner-image change that silently drops the pg binary cannot quietly re-open
    the real-SQL semantic-drift hole (a skip is green).
    """
    bindirs = _candidate_bindirs()
    if not bindirs:
        msg = (
            "no PostgreSQL server binary discoverable "
            "(set $TCG_TEST_PG_BINDIR or install a local server); "
            "real-SQL equivalence guard cannot run offline here"
        )
        if _env_truthy(os.environ.get("TCG_REQUIRE_PG_TESTS")):
            pytest.fail(msg + " [TCG_REQUIRE_PG_TESTS is set — refusing to skip]")
        pytest.skip(msg)
    return bindirs[0]


@pytest.fixture(scope="session")
def seeded_dwh() -> SeededDwh:  # type: ignore[misc]
    """Spin an ephemeral PostgreSQL, seed the fixture chain, yield conn params."""
    bindir = _select_bindir()

    env = dict(os.environ)
    libs = _libdirs_for(bindir)
    if libs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            libs + [env.get("LD_LIBRARY_PATH", "")]
        ).rstrip(os.pathsep)

    tmp = tempfile.mkdtemp(prefix="tcg-pgtest-")
    datadir = os.path.join(tmp, "data")
    sockdir = os.path.join(tmp, "sock")
    os.makedirs(sockdir, exist_ok=True)
    port = _free_port()
    user = "tcg_test"

    subprocess.run(
        [os.path.join(bindir, "initdb"), "-D", datadir, "-A", "trust", "-U", user],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    proc = subprocess.Popen(
        [
            os.path.join(bindir, "postgres"),
            "-D",
            datadir,
            "-k",
            sockdir,
            "-c",
            "listen_addresses=",
            "-p",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    conninfo = f"host={sockdir} port={port} dbname=postgres user={user}"
    # Wait for readiness (connect loop).
    deadline = time.time() + 30
    last_err: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("postgres exited during startup")
        try:
            with psycopg.connect(conninfo, connect_timeout=2):
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError(f"postgres never became ready: {last_err}")

    try:
        _seed(conninfo)
        yield SeededDwh(
            host=sockdir,
            port=port,
            db="postgres",
            user=user,
            password="",
            root=ROOT,
            e1=E1,
            e2=E2,
            e3=E3,
            e4=E4,
            e1_dates=E1_DATES,
            e2_dates=E2_DATES,
            held_dates=HELD_DATES,
            e4_dates=E4_DATES,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
