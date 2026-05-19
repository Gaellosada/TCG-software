# Persistence (Write) Layer

This document describes the safety model behind the write layer that
backs indicators, signals, and portfolios. Read-side data access
remains unchanged and is documented in `data-model.md`.

## Two-layer isolation

The write layer enforces "only ever touch
`tcg-instrument.2026-app-data`" twice, independently:

### Layer 1 — Mongo role (server-side)

A dedicated database user `app-writer` holds a single custom role
named `appDataWriter`. That role grants exactly:

```text
{
  privileges: [{
    resource:   { db: "tcg-instrument", collection: "2026-app-data" },
    actions:    [find, insert, update, remove, createIndex, listIndexes]
  }],
  inherited roles: []
}
```

No `dbAdmin`, no `userAdmin`, no `readAnyDatabase`. Any operation
outside this single namespace — including `listCollections` on the
parent database — fails with `OperationFailure` code 13 / `Unauthorized`.

Evidence of the live privilege check (run at provisioning time and
re-runnable from `tests/integration/test_persistence_scope_rejection.py`)
is captured in
`workspace/tasks/persistence-layer/output/scope-check-evidence.json`.

### Layer 2 — `WriteRepository` (application-side)

`tcg.persistence.WriteRepository.__init__` binds the collection handle
exactly once:

```python
self._coll = client["tcg-instrument"]["2026-app-data"]
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

## Environment variable

The scoped client is built from a single environment variable:

```
MONGO_APP_WRITE_URI=mongodb://app-writer:<password>@<host>:27017/?...&authSource=tcg-instrument
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
  FastAPI dependency surfaces this as a 500 to the caller.
- **Auth failure mid-request.** Motor raises `OperationFailure`
  (re-thrown as 500). The role is configured at provisioning time;
  in-band auth failures indicate the server-side role was mutated.
- **Update / archive on a missing doc.** `WriteRepository.update` /
  `archive` raise `KeyError`. The HTTP handlers translate to 404.
- **Type/id collision.** `get_by_id(doc_type, doc_id)` filters by
  both keys, so an indicator and a signal sharing the same `_id`
  return only their respective doc.

## Out of scope (for now)

- Auth / multi-user authorisation at the HTTP layer.
- Audit trail beyond `updated_at` (no event log).
- Hard delete. Archive is the only delete operation exposed.
- Schema migration / versioning of payload dicts. Consumers must
  tolerate forward-compatible payload shapes.
