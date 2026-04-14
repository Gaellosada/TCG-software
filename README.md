# TCG Software

Financial simulation and exploration platform for volatility trading strategies, replacing a legacy Java platform.

## Tech Stack

- **Backend:** Python 3.14+ / FastAPI / Motor (async MongoDB) / NumPy
- **Frontend:** React 18 / Vite / Plotly.js / React Router

## Prerequisites

- Python 3.14+
- Node 18+
- MongoDB instance with legacy `tcg-instrument` database

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
