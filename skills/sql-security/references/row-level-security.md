# Row-level security for multi-tenant PostgreSQL

```sql
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices FORCE ROW LEVEL SECURITY;   -- applies to the table owner too

CREATE POLICY tenant_isolation ON invoices
  USING (tenant_id = current_setting('app.tenant_id')::bigint)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::bigint);
```
The app sets the tenant per transaction (safe with connection pools):
```sql
BEGIN;
SELECT set_config('app.tenant_id', '42', true);   -- true = local to this transaction
SELECT * FROM invoices;                            -- only tenant 42
COMMIT;
```

## Rules
- The app role must not own the tables and must not have `BYPASSRLS` or `SUPERUSER`
  (both bypass policies). `grants_review.py` reports both.
- Every tenant table gets RLS; check with
  `SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relkind = 'r' AND relnamespace = 'app'::regnamespace;`
- Missing setting: `current_setting('app.tenant_id')` raises an error (good: fail closed).
  `current_setting('app.tenant_id', true)` returns NULL and the policy hides all rows.
- Index `tenant_id` (usually as the first column of composite indexes).
- Policies on views: views run with the owner's rights unless `security_invoker = true`
  (PostgreSQL 15+).
- Functions marked `SECURITY DEFINER` run as their owner: set `search_path` explicitly and
  keep them minimal.

## Test it
```sql
SET ROLE app;
SELECT set_config('app.tenant_id', '1', false);
SELECT count(*) FROM invoices WHERE tenant_id <> 1;                 -- must be 0
INSERT INTO invoices (tenant_id, total) VALUES (2, 10);              -- must fail (WITH CHECK)
RESET ROLE;
```
Add this as an automated test per tenant table.

## MySQL
MySQL has no row-level security. Options: tenant-filtered views with
`SQL SECURITY DEFINER` and grants only on the views, a schema per tenant, or strict
enforcement in the data-access layer (a mandatory tenant scope in the ORM) plus tests.
