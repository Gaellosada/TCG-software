# TCG Software

Financial simulation and exploration platform for volatility trading strategies, replacing a legacy Java platform.

## Tech Stack

- **Backend:** Python 3.14+ / FastAPI / Motor (async MongoDB) / NumPy
- **Frontend:** React 18 / Vite / Plotly.js / React Router

## Prerequisites

- Python 3.14+
- Node 18+
- MongoDB instance with legacy `tcg-instrument` database

## SSM Tunnel (private MongoDB)

If the MongoDB instance is in a private subnet behind an AWS bastion, the app can open an SSM port-forwarding tunnel automatically.

### Additional prerequisites (tunnel mode only)

- [AWS CLI v2](https://aws.amazon.com/cli/)
- [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
- AWS credentials with `ssm:StartSession` permission on the bastion instance
- Network egress to AWS endpoints (HTTPS 443)

### Setup

1. In `.env`, set `SSM_TUNNEL_ENABLED=true`
2. Fill in `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`
3. Fill in `MONGO_USER`, `MONGO_PASSWORD`, and `MONGO_DB`
4. (Optional) Fill in `MONGO_APP_WRITE_USER` and `MONGO_APP_WRITE_PASSWORD` for write access
5. Start the app normally — the tunnel opens and closes automatically

The tunnel connects `localhost:27017` → bastion → MongoDB private IP. If the tunnel drops, the backend restarts it automatically with exponential backoff.

## Quick Start

### Backend

```bash
# Install (editable, with dev dependencies)
pip install -e ".[dev]"

# Configure MongoDB connection
cp .env.example .env   # then edit MONGO_URI and MONGO_DB_NAME

# Run
uvicorn tcg.core.app:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev        # Dev server (proxies /api to backend)
npm run build      # Production build to dist/
npm test           # Vitest unit tests
```

### Tests

```bash
# Unit tests only (no MongoDB required)
pytest -m "not integration"

# All tests (requires live MongoDB)
pytest
```

## Mongo connection — replicaSet & auth required

The production `tcg-instrument` Mongo runs as a single-node replica set
with authentication enabled. A bare `mongodb://host:port` URI will
**not** connect — Motor reports a server-selection timeout because the
client cannot discover the replica set topology.

Use the full form:

```
mongodb://<user>:<pass>@<host>:<port>/?directConnection=true&replicaSet=<rs_name>
```

For local dev, store the URI in `.env` (copy from `.env.example`) and
export `MONGO_URI` + `MONGO_DB_NAME=tcg-instrument` when running
scripts that talk to Mongo directly (the FastAPI app reads `.env` via
the config loader; ad-hoc scripts do not).

Symptom checklist when Mongo connections time out:

- URI is missing the `replicaSet=<name>` query param → add it.
- Authentication is failing silently (Motor's default behavior) →
  confirm the URI carries the `<user>:<pass>@` prefix.
- Wrong replica set name → `tcg-rs` in the current dev environment.
- Network reachability — the dev Mongo lives behind a private network;
  confirm the host is reachable before debugging the URI.

Do not commit the credentialed URI to the repo.

## Dev Sharing

See [`dev/README.md`](dev/README.md) for exposing the app via a Cloudflare Tunnel with password protection (client demos).

## Project Structure

```
tcg/                    Python backend package
  core/                 FastAPI app, config, API routers
  data/                 MongoDB adapters, caching, continuous futures rolling
  engine/               Portfolio computation, metrics, return aggregation
  types/                Domain types, error hierarchy, protocols
frontend/               React SPA
  src/pages/            Data, Portfolio, Research, Settings, Help
  src/components/       Chart, Sidebar, PillToggle, TimeRangeSlider
  src/hooks/            useAsync, useTheme, useChartPreference
  src/api/              Backend API client wrappers
  src/utils/            Chart theming, formatting, OHLC helpers
tests/                  pytest suite (unit + integration)
dev/                    Developer tools (share.py for tunneled demos)
docs/                   Architecture and design documentation
```
