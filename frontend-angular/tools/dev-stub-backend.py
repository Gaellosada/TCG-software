"""Dev-only stub backend for the Angular dev-harness.

DEV USE ONLY — never ship in production. This script serves canned JSON
responses for the FastAPI endpoints the Angular library touches, so the
dev-harness can render against a reachable backend in environments where
MongoDB / real FastAPI is unavailable (the WSL Wave 0+ scenario).

Usage:
    python frontend-angular/tools/dev-stub-backend.py [--port 8000]

Endpoints served:
    GET  /api/health                              → {"status":"ok"}
    GET  /api/data/collections                    → {"collections":[...]}
    GET  /api/data/{collection}                   → {"items":[...]}
    GET  /api/data/{collection}/{instrument_id}   → OHLCV series
    GET  /api/data/continuous/{coll}/cycles       → {"cycles":[...]}
    GET  /api/data/continuous/{coll}              → continuous series stub
    GET  /api/options/roots                       → {"roots":[...]}
    GET  /api/options/expirations?root=...        → {"root":"...","expirations":[...]}
    GET  /api/options/chain?...                   → {"rows":[...],"date":"...","underlying_price":{...}}
    GET  /api/options/contract/{coll}/{id}        → {"contract":{...},"rows":[...]}
    GET  /api/options/chain-snapshot?...          → {"series":[...],"underlying_price":{...}}
    GET  /api/options/select?q=...                → {"contract_id":"...","collection":"..."}
    GET  /api/options/stream/progress/{taskId}    → {"done":1,"total":1,"fraction":1.0}
    POST /api/options/stream                      → {"task_id":"...","rows":[]}
    GET  /api/persistence/baskets?category=X      → []
    GET  /api/persistence/signals?category=X      → []
    GET  /api/persistence/portfolios?category=X   → []

Workers B + C extend the route map as needed. Permissive CORS so the
dev-harness on :4200 can hit it on :8000 without proxy config.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse


SAMPLE_COLLECTIONS = ["INDEX", "ETF", "FUT_ES", "FUT_NQ", "OPT_SP_500"]

SAMPLE_INSTRUMENTS: dict[str, list[dict[str, Any]]] = {
    "INDEX": [
        {"symbol": "SPX", "instrument_id": "SPX", "display_name": "S&P 500"},
        {"symbol": "NDX", "instrument_id": "NDX", "display_name": "Nasdaq 100"},
    ],
    "ETF": [
        {"symbol": "SPY", "instrument_id": "SPY", "display_name": "SPDR S&P 500"},
        {"symbol": "QQQ", "instrument_id": "QQQ", "display_name": "Invesco QQQ"},
        {"symbol": "AAPL", "instrument_id": "AAPL", "display_name": "Apple"},
    ],
    "FUT_ES": [
        {"symbol": "ESH24", "instrument_id": "ESH24"},
        {"symbol": "ESM24", "instrument_id": "ESM24"},
    ],
    "FUT_NQ": [
        {"symbol": "NQH24", "instrument_id": "NQH24"},
    ],
    "OPT_SP_500": [],
}

SAMPLE_OPTION_ROOTS = [
    {
        "name": "OPT_SP_500",
        "collection": "OPT_SP_500",
        "has_greeks": True,
        "stored_greeks_ratio": 0.95,
        "has_computed_greeks": True,
        "last_trade_date": "2026-05-19",
        "expiration_last": "2026-12-19",
    },
    {
        "name": "OPT_VIX",
        "collection": "OPT_VIX",
        "has_greeks": True,
        "stored_greeks_ratio": 0.80,
        "has_computed_greeks": True,
        "last_trade_date": "2026-05-19",
        "expiration_last": "2026-09-17",
    },
]

SAMPLE_EXPIRATIONS = ["2026-06-20", "2026-07-18", "2026-08-15", "2026-09-19", "2026-12-19"]

SAMPLE_CHAIN_ROW_TEMPLATE: dict[str, Any] = {
    "expiration": "2026-06-20",
    "expiration_cycle": "M",
    "type": "C",
    "bid": None,
    "mid": None,
    "ask": None,
    "open_interest": None,
    "iv": {"value": 0.18, "source": "stored"},
    "delta": {"value": 0.50, "source": "stored"},
    "gamma": {"value": 0.01, "source": "stored"},
    "theta": {"value": -0.05, "source": "stored"},
    "vega": {"value": 0.10, "source": "stored"},
}

SAMPLE_CHAIN_ROWS = [
    {**SAMPLE_CHAIN_ROW_TEMPLATE, "contract_id": f"SPX_{exp}_{t}_{strike:05d}",
     "expiration": exp, "expiration_cycle": "M", "type": t, "strike": strike,
     "bid": round(max(0, (strike - 5400) * (1 if t == "P" else -1) * 0.5 + 10), 2),
     "mid": round(max(0, (strike - 5400) * (1 if t == "P" else -1) * 0.5 + 12), 2),
     "ask": round(max(0, (strike - 5400) * (1 if t == "P" else -1) * 0.5 + 14), 2),
     "iv": {"value": round(0.15 + abs(strike - 5400) * 0.0001, 4), "source": "stored"},
     "delta": {"value": round(0.5 + (5400 - strike) * 0.0005 * (1 if t == "C" else -1), 4), "source": "stored"},
     "gamma": {"value": 0.002, "source": "stored"},
     "theta": {"value": -0.03, "source": "stored"},
     "vega": {"value": 0.12, "source": "stored"},
    }
    for exp in ["2026-06-20", "2026-09-19"]
    for t in ["C", "P"]
    for strike in [5300, 5350, 5400, 5450, 5500]
]

SAMPLE_CONTRACT_SERIES = {
    "contract": {
        "contract_id": "SPX_2026-06-20_C_05400",
        "strike": 5400,
        "type": "C",
        "expiration": "2026-06-20",
        "expiration_cycle": "M",
        "root_underlying": "SPX",
        "provider": "stub",
    },
    "rows": [
        {
            "date": d,
            "mid": round(50.0 + i * 2.5, 2),
            "volume": 1000 + i * 100,
            "underlying_price_stored": round(5380.0 + i * 5.0, 2),
            "iv": {"value": round(0.18 + i * 0.002, 4), "source": "stored"},
            "delta": {"value": round(0.48 + i * 0.01, 4), "source": "stored"},
            "gamma": {"value": 0.002, "source": "stored"},
            "theta": {"value": -0.03, "source": "stored"},
            "vega": {"value": 0.12, "source": "stored"},
        }
        for i, d in enumerate(["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04", "2026-04-07"])
    ],
}

SAMPLE_CHAIN_SNAPSHOT = {
    "underlying_price": {"value": 5400.0, "source": "stored"},
    "series": [
        {
            "expiration": exp,
            "points": [
                {
                    "strike": strike,
                    "K_over_S": round(strike / 5400.0, 4),
                    "expiration_cycle": "M",
                    "value": {"value": round(0.15 + abs(strike - 5400) * 0.0001, 4), "source": "stored"},
                }
                for strike in [5200, 5250, 5300, 5350, 5400, 5450, 5500, 5550, 5600]
            ],
        }
        for exp in ["2026-06-20", "2026-09-19"]
    ],
}

# Tiny canned price series — 5 trading days.
SAMPLE_PRICES = {
    "dates": [
        "2026-01-02",
        "2026-01-03",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
    ],
    "open": [100.0, 101.5, 102.0, 100.5, 101.2],
    "high": [102.0, 102.5, 102.8, 101.8, 102.4],
    "low": [99.5, 100.5, 100.8, 99.8, 100.7],
    "close": [101.5, 102.0, 101.0, 101.2, 102.0],
    "volume": [1_000_000, 1_200_000, 900_000, 1_100_000, 1_050_000],
}


class StubHandler(BaseHTTPRequestHandler):
    def _set_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, Accept",
        )
        self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        self._json({"detail": f"stub: no route for {self.path}"}, status=404)

    def do_OPTIONS(self) -> None:  # noqa: N802 — http.server convention
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/api/health":
            return self._json({"status": "ok"})

        if path == "/api/data/collections":
            return self._json(
                {"collections": [{"name": c, "display_name": c} for c in SAMPLE_COLLECTIONS]}
            )

        m = re.match(r"^/api/data/continuous/([^/]+)/cycles$", path)
        if m:
            return self._json({"cycles": ["M", "Q"]})

        m = re.match(r"^/api/data/continuous/([^/]+)$", path)
        if m:
            return self._json(
                {
                    "dates": SAMPLE_PRICES["dates"],
                    "close": SAMPLE_PRICES["close"],
                    "rolls": [],
                }
            )

        m = re.match(r"^/api/data/([^/]+)/([^/]+)$", path)
        if m:
            return self._json(SAMPLE_PRICES)

        m = re.match(r"^/api/data/([^/]+)$", path)
        if m:
            coll = m.group(1)
            items = SAMPLE_INSTRUMENTS.get(coll, [])
            return self._json(
                {"items": items, "total": len(items), "skip": 0, "limit": 500}
            )

        if path == "/api/options/roots":
            return self._json({"roots": SAMPLE_OPTION_ROOTS})

        if path == "/api/options/expirations":
            parsed = urlparse(self.path)
            return self._json({"root": "OPT_SP_500", "expirations": SAMPLE_EXPIRATIONS})

        if path == "/api/options/chain":
            return self._json({"rows": SAMPLE_CHAIN_ROWS, "date": "2026-05-19",
                                "underlying_price": {"value": 5400.0, "source": "stored"}})

        m = re.match(r"^/api/options/contract/([^/]+)/([^/]+)$", path)
        if m:
            return self._json(SAMPLE_CONTRACT_SERIES)

        if path == "/api/options/chain-snapshot":
            return self._json(SAMPLE_CHAIN_SNAPSHOT)

        if path == "/api/options/select":
            return self._json({"contract_id": "SPX_2026-06-20_C_05400", "collection": "OPT_SP_500"})

        if path.startswith("/api/options/stream/progress/"):
            return self._json({"done": 1, "total": 1, "fraction": 1.0})

        if path == "/api/options/stream":
            return self._json({"task_id": "stub-task-1", "rows": []})

        if path.startswith("/api/persistence/"):
            # Empty list / object for any GET (list / get-by-id).
            if "?" in self.path or path.endswith(("signals", "portfolios", "baskets")):
                return self._json([])
            return self._json({})

        return self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/options/stream"):
            return self._json({"task_id": "stub-task-1", "rows": []}, status=200)
        # Echo + 201 for any POST under /api/persistence/.
        if self.path.startswith("/api/persistence/"):
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            return self._json(body, status=201)
        return self._not_found()

    def do_PUT(self) -> None:  # noqa: N802
        if self.path.startswith("/api/persistence/"):
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            return self._json(body, status=200)
        return self._not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/api/persistence/"):
            self.send_response(204)
            self._set_cors_headers()
            self.end_headers()
            return
        return self._not_found()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib name
        # Quieter logging — only print method+path on a single line.
        print(f"[stub] {self.address_string()} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()
    server = HTTPServer((args.host, args.port), StubHandler)
    print(f"[stub] dev-stub-backend listening on http://{args.host}:{args.port}")
    print("[stub] DEV USE ONLY — serves canned responses for the Angular dev-harness.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[stub] shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
