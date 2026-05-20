# `frontend-angular/tools/`

Developer-only utilities for the Angular dev-harness. None of these files
ship to production — they exist so contributors can run the dev-harness
against a reachable stub in environments where the real FastAPI / MongoDB
stack isn't available (the WSL case noted in `PROBLEMS.md` from Wave 0).

## `dev-stub-backend.py`

A minimal stdlib-only HTTP server that returns canned JSON for the
endpoints the Angular library hits. Permissive CORS so the dev-harness
on `:4200` can talk to it on `:8000` without a proxy.

### Run

```bash
python frontend-angular/tools/dev-stub-backend.py            # :8000 by default
python frontend-angular/tools/dev-stub-backend.py --port 8001
```

### Verify

```bash
curl -fsS http://localhost:8000/api/health
# → {"status":"ok"}

curl -fsS http://localhost:8000/api/data/collections
# → {"collections":[{"name":"INDEX","display_name":"INDEX"}, ...]}

curl -fsS http://localhost:8000/api/data/ETF
# → {"items":[{"symbol":"SPY",...},...],"total":...,"skip":0,"limit":500}
```

### Boundary

The stub is intentionally tiny. When Workers B + C (or any later wave)
need an endpoint that isn't here, they should add it to `StubHandler` —
not work around the gap. Stub responses are flagged with `[stub]` in
the access log so it's obvious when the dev-harness is talking to the
stub vs a real FastAPI.

### Integration

The dev-harness in `projects/dev-harness/` is wired to talk to
`http://localhost:8000` by default. Override via the `TCG_API_BASE_URL`
provider in `projects/dev-harness/src/app/app.config.ts` if you want to
point at a real FastAPI instance instead.
