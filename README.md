# TCG Software

Financial simulation and exploration platform for volatility trading strategies, replacing a legacy Java platform.

## Tech Stack

- **Backend:** Python 3.12+ / FastAPI / psycopg3-async / NumPy
- **Frontend:** React 18 / Vite / Plotly.js / React Router

## Prerequisites

- Python 3.12+
- Node 18+
- [uv](https://docs.astral.sh/uv/) (Python env/dependency manager)
- Network access to the `dwh` PostgreSQL warehouse (both roles below live in the same database)

## Data Stores

TCG reads and writes a single `dwh` PostgreSQL database, split across two schemas with two distinct roles:

| Schema | Role | Access | Contents |
|---|---|---|---|
| `tcg_instruments` | `tcg_read` | read-only | Market data: instruments, prices, options, greeks |
| `tcg_app_data` | `tcg_app_rw` | read-write | App persistence: indicators, signals, portfolios, baskets (JSONB) |

There is no legacy document-store dependency, no SSM tunnel, and no bastion — the warehouse is reachable directly from a WSL/Linux dev machine.

## Quick Start

The simplest way to run everything is:

```bash
cp .env.example .env   # fill in DWH_* and APP_DB_* credentials
./start.sh
```

`start.sh` syncs Python deps (`uv sync`), installs frontend deps (`npm install`), starts the backend (`python -m tcg.core`, default `http://127.0.0.1:8000`), waits for its `/health` probe, then starts the Vite dev server. Ctrl-C stops both.

### Backend only

```bash
# Install (editable, with dev dependencies)
uv sync --extra dev

# Configure the database connection
cp .env.example .env   # then fill in DWH_* and APP_DB_* credentials

# Run
uv run python -m tcg.core --host 127.0.0.1 --port 8000
```

### Frontend only

```bash
cd frontend
npm install
npm run dev        # Dev server (proxies /api to backend)
npm run build      # Production build to dist/
npm test           # Vitest unit tests
```

### Tests

```bash
# Unit tests only (no database required)
pytest -m "not integration"

# All tests (requires a reachable dwh)
pytest

# Module boundary check
import-linter
```

## Environment Variables

See `.env.example` for the full, commented list. In short:

- `DWH_HOST` / `DWH_USER` / `DWH_PASSWORD` (required), `DWH_PORT` (default 5432), `DWH_DB` (default `dwh`), `DWH_SSLMODE` (default `require`) — read-only connection to `tcg_instruments`.
- `APP_DB_USER` / `APP_DB_PASSWORD` (required), `APP_DB_SCHEMA` (default `tcg_app_data`) — read-write connection to app persistence. Host/port/db default to the `DWH_*` values (same RDS, same database).
- `TCG_CORS_ORIGINS` (optional; defaults to the Vite dev origin).

Never commit `.env`.

## Desktop wrapper (optional)

A Tauri desktop wrapper bundles the backend as a PyInstaller sidecar. It's optional — the web app via `./start.sh` is the primary run target. See `desktop/README.md` for building it locally; tagging `desktop-v*` on `main` publishes a GitHub Release with installers for Windows/Linux/macOS.

## Dev Sharing

See [`dev/README.md`](dev/README.md) for exposing the app via a Cloudflare Tunnel with password protection (client demos).

## Project Structure

```
tcg/                    Python backend package
  core/                 FastAPI app, config, API routers
  data/                 dwh PostgreSQL adapters (read-only), caching, continuous futures rolling
  engine/               Portfolio/signal computation, metrics, return aggregation
  persistence/          tcg_app_data read-write adapters (indicators/signals/portfolios/baskets)
  types/                Domain types, error hierarchy, protocols
frontend/               React SPA
  src/pages/            Data, Indicators, Signals, Portfolio, Tickets, Settings, Help
  src/components/       Chart, Sidebar, PillToggle, TimeRangeSlider
  src/hooks/            useAsync, useTheme, useChartPreference
  src/api/              Backend API client wrappers
  src/utils/            Chart theming, formatting, OHLC helpers
tests/                  pytest suite (unit + integration)
dev/                    Developer tools (share.py for tunneled demos)
desktop/                Tauri desktop wrapper (optional; see desktop/README.md)
docs/                   Architecture and design documentation
```

Module boundaries are enforced by `import-linter` (`.import-linter.cfg`): `tcg.types` has no internal deps; `tcg.data` and `tcg.engine` both depend on `tcg.types` and are independent of each other; `tcg.core` assembles everything; only `tcg.persistence` may touch the app-data write client.
