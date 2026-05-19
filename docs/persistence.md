# Persistence (Write) Layer

This document describes the safety model behind the write layer that
backs indicators, signals, and portfolios. Read-side data access
remains unchanged and is documented in `data-model.md`.

## Two-layer isolation

The write layer enforces "only ever touch
`tcg-app-data.2026-app-data`" twice, independently:

> **Defense in depth — dedicated database.** The write namespace lives
> in its OWN database (`tcg-app-data`), not in the legacy
> `tcg-instrument` database that the read-only data layer uses. This
> makes the scoped Mongo user invisible to `tcg-instrument` entirely:
> `list_database_names()` on the scoped client returns only
> `['tcg-app-data']`, and every operation against any collection in
> `tcg-instrument` (including `listCollections`) is rejected with
> `OperationFailure` code 13.

### Layer 1 — Mongo role (server-side)

A dedicated database user `app-writer` (provisioned in `tcg-app-data`)
holds a single custom role named `appDataWriter` (also defined in
`tcg-app-data`). That role grants exactly:

```text
{
  privileges: [{
    resource:   { db: "tcg-app-data", collection: "2026-app-data" },
    actions:    [find, insert, update, remove, createIndex, listIndexes]
  }],
  inherited roles: []
}
```

No `dbAdmin`, no `userAdmin`, no `readAnyDatabase`. Any operation
outside this single namespace — including `listCollections` on either
`tcg-app-data` or `tcg-instrument` — fails with `OperationFailure`
code 13 / `Unauthorized`.

Evidence of the live privilege check (run at provisioning time and
re-runnable from `tests/integration/test_persistence_scope_rejection.py`)
is captured in
`workspace/tasks/persistence-layer/output/migration-scope-check-evidence.json`.

### Layer 2 — `WriteRepository` (application-side)

`tcg.persistence.WriteRepository.__init__` binds the collection handle
exactly once:

```python
self._coll = client["tcg-app-data"]["2026-app-data"]
```

The class exposes **only** the following public methods:

- `create(doc)` — insert + stamp `created_at`/`updated_at`.
- `get_by_id(doc_type, doc_id)` — filter by `(_id, type)`.
- `list_by_type("indicator")` — list active (non-deleted) indicators.
- `list_by_type_and_category(doc_type, category)` — list signals or
  portfolios in a given workflow stage.
- `update(doc)` — full-document replace by `(_id, type)`; raises
  `KeyError` if missing.
- `archive(doc_type, doc_id)` — soft-delete (indicators flip
  `deleted=True`; signals/portfolios move to `Category.ARCHIVE`).

No public method accepts a collection name. The repository has no
`__getattr__` escape hatch and no public attribute that exposes a
collection or database handle. The static checks in
`tests/unit/test_persistence_api_surface.py` enforce this on every
test run.

### Belt-and-suspenders

If layer 1 is mis-edited (e.g. the role is widened), layer 2 still
prevents the application from issuing writes outside the bound
collection. If layer 2 is regressed (someone adds an escape hatch),
layer 1 still rejects the write at the server. Both layers must fail
before data can be written outside the authorised namespace.

Layer 2 is a defence-in-depth convention, not a Python-level
invariant. `WriteRepository` uses `__slots__` + a `__setattr__`
guard that blocks ordinary attribute rebinding after
construction (e.g. `repo._coll = other` raises). `object.__setattr__`
is still reachable from a determined caller — at that point only
layer 1 stands, and an integration test
(`test_object_setattr_bypass_rejected_by_mongo_role`) proves
that bypass is still caught by the server with `OperationFailure`
code 13.

## Request lifecycle and error mapping

The HTTP layer maps repository / Mongo / validation errors to clean
status codes so internal exceptions never leak as 500s:

| Condition | Status | Source |
|---|---|---|
| Validation error (`extra` field, `_id` pattern, oversize `description`, unknown `rebalance`, depth > 16, serialized payload > 1 MB) | 400 (via `RequestValidationError` handler) | Pydantic constraints in `tcg/core/api/persistence.py` |
| Duplicate `_id` on create | 409 | `pymongo.errors.DuplicateKeyError` → `HTTPException(409)` |
| Concurrent update (CAS miss on `updated_at`) | 409 | `ConcurrentUpdateError` → `HTTPException(409)` |
| Request body > 4 MB (`Content-Length` or chunked) | 413 | `BodySizeLimitMiddleware` in `tcg/core/app.py` |
| Document too large after encoding (rare; 16 MB BSON limit) | 413 | `pymongo.errors.DocumentTooLarge` → `DocumentTooLargeError` → `HTTPException(413)` |
| Doc not found (`update` / `archive` on missing `_id`) | 404 | `KeyError` → `HTTPException(404)` |
| Any other PyMongo error (`OperationFailure` non-duplicate, `ServerSelectionTimeoutError`, network etc.) | 503 | App-level `pymongo.errors.PyMongoError` handler — message sanitised, no server topology / credentials leaked |

### Concurrency model

`update` uses optimistic concurrency on `updated_at`:

1. Route reads pre-image via `get_by_id`, capturing `existing.updated_at`.
2. Route calls `repo.update(..., expected_updated_at=existing.updated_at)`.
3. Repo filters `{_id, type, updated_at: expected_updated_at}` and
   `replace_one`. On 0-matched, it probes existence: doc gone →
   `KeyError` (404); doc present with different `updated_at` →
   `ConcurrentUpdateError` (409).

This means two readers racing to update the same doc cannot silently
lose an update — the second writer hits 409 and can refetch + retry.

### Body size cap

`BodySizeLimitMiddleware` enforces a 4 MB cap (well below Mongo's
16 MB BSON limit) on every request. It honours `Content-Length` for
the fast path; for chunked / HTTP/2 transports without a declared
length it counts bytes as they stream and short-circuits with 413
once the threshold is exceeded. This bounds the memory cost of
any single request to roughly `4 MB + one chunk`.

## Environment variable

The scoped client is built from a single environment variable:

```
MONGO_APP_WRITE_URI=mongodb://app-writer:<password>@<host>:27017/?...&authSource=tcg-app-data
```

This URI is the **only** secret needed by the write layer. The
admin URI is used **once** at provisioning time to create the role and
user and is not referenced by application code.

Lookup order is identical to the read-side config:

1. Real OS environment variable
2. `TCG-software/.env`

If neither is set, `tcg.persistence._client.build_write_client` raises
`ValueError` rather than silently falling back to an unscoped URI.

## Import-linter contract

A `persistence-write-boundary` contract in `.import-linter.cfg`
forbids `tcg.types`, `tcg.data`, `tcg.engine`, and
`tcg.core.indicators` from importing `tcg.persistence`. The only
allowed seams are `tcg.core.api._persistence_wiring` (the FastAPI
dependency) and `tcg.core.api.persistence` (the HTTP router). Both
sit under `tcg.core`, which is intentionally not in the forbidden
source list.

## Adding a new persisted entity type

The persistence module owns *structure*, not *interpretation*: inner
payloads (e.g. `IndicatorDoc.definition`, `SignalDoc.blocks`) are
opaque dicts so new payload fields do not require schema changes here.
To add a wholly new entity type:

1. **Dataclass.** Add a `@dataclass(frozen=True, slots=True)` to
   `tcg/types/persistence.py` with the `type` discriminator literal,
   server-stamped `created_at` / `updated_at`, and either a `category`
   field (workflow stage) or a `deleted` flag (boolean soft-delete).
2. **Discriminator union.** Add the new class to `PersistenceDoc` and
   to `_TYPE_TO_CLASS` so `from_mongo_dict` can route it.
3. **Endpoints.** Add Pydantic wire models and a small set of
   handlers to `tcg/core/api/persistence.py`. Mirror the existing
   indicator / signal / portfolio shape: `POST /xxx`, `GET /xxx`,
   `GET /xxx/{id}`, `PUT /xxx/{id}`, `DELETE /xxx/{id}`.
4. **Property test.** Add a strategy + round-trip test to
   `tests/property/test_persistence_serialization.py`.

The `WriteRepository` interface does NOT need to change — new types
go through the same `create / get_by_id / update / archive` surface.

## Failure modes

- **Missing env var.** `build_write_client` raises `ValueError`. The
  FastAPI dependency surfaces this as a 500 to the caller (a startup
  / config bug, not a runtime condition).
- **Auth failure mid-request.** Motor raises `OperationFailure`, which
  the app-level `PyMongoError` handler maps to **503** with a
  sanitised message. In-band auth failures indicate the server-side
  role was mutated and require operator attention.
- **Update / archive on a missing doc.** `WriteRepository.update` /
  `archive` raise `KeyError`. The HTTP handlers translate to **404**.
- **Concurrent update conflict.** `update` raises
  `ConcurrentUpdateError` when the on-disk `updated_at` no longer
  matches the value read before the write. HTTP returns **409**;
  the client should refetch and retry.
- **Type/id collision.** `get_by_id(doc_type, doc_id)` filters by
  both keys, so an indicator and a signal sharing the same `_id`
  return only their respective doc.
- **Oversize request body.** Requests > 4 MB are rejected with
  **413** by the body-size middleware (both `Content-Length` and
  chunked transports).
- **Malformed input.** Pydantic rejects payloads that fail the
  validation constraints (`_id` pattern, `description` length,
  `rebalance` enum, nesting depth, extra fields) with **400** via
  the project's `RequestValidationError` handler.

## Out of scope (for now)

- Auth / multi-user authorisation at the HTTP layer.
- Audit trail beyond `updated_at` (no event log).
- Hard delete. Archive is the only delete operation exposed.
- Schema migration / versioning of payload dicts. Consumers must
  tolerate forward-compatible payload shapes.
