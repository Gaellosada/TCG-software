-- tickets.sql — DDL for the app-data "tickets" table.
--
-- A ticket is a single free-text note a user records when they hit an
-- issue. It is INTENTIONALLY outside the uniform 7-column app-data model
-- (id/type/category/locked/payload/created_at/updated_at): exactly three
-- columns, no JSONB, no soft-delete category. Editing a ticket is an
-- in-place UPDATE of `text`; deletion is a HARD `DELETE FROM` (this row
-- does NOT follow the project's uniform `category='DELETED'` soft-delete).
--
-- Target schema : tcg_app_data  (the app-data schema; same dwh RDS/database).
-- Run as        : a role WITH CREATE privilege on the schema. The runtime
--                 app role `tcg_app_rw` is DML-only (SELECT/INSERT/UPDATE/
--                 DELETE) and may LACK DDL rights — run this with an
--                 owner/admin role, then ensure `tcg_app_rw` has DML grants
--                 on the new table (see the GRANT below).
-- Applied       : MANUALLY. There is no migration framework in this project;
--                 the operator runs this file once against the dwh database.
--
-- Idempotent: re-running is a no-op (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS tcg_app_data.tickets (
  id         text PRIMARY KEY,
  text       text NOT NULL,
  created_at timestamptz NOT NULL
);

-- Grants. Confirmed role names (adjust if your deployment differs):
--   read-write app role = tcg_app_rw  (APP_DB_USER — the role the backend
--                                       connects as; REQUIRED for the feature)
--   read-only role      = tcg_read    (DWH_USER)
-- USAGE on the schema is required before the table-level grants take effect
-- (tcg_read, the market-data role, normally lacks USAGE on tcg_app_data).
GRANT USAGE ON SCHEMA tcg_app_data TO tcg_app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON tcg_app_data.tickets TO tcg_app_rw;

GRANT USAGE ON SCHEMA tcg_app_data TO tcg_read;
GRANT SELECT ON tcg_app_data.tickets TO tcg_read;
