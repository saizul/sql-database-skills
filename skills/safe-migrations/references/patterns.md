# Zero-downtime patterns

## Lock timeouts and retries (always)

A blocked `ALTER TABLE` waits for its lock *and* makes every later query on the table wait
behind it. Fail fast and retry instead.

```sql
-- PostgreSQL
SET lock_timeout = '5s';
SET statement_timeout = '30min';   -- for long CONCURRENTLY builds use 0 or a large value
-- MySQL / MariaDB (metadata lock wait)
SET SESSION lock_wait_timeout = 5;
```
Retry with backoff in the deploy tool (3-5 attempts). Rails: the `strong_migrations` gem;
Django: `django-pg-zero-downtime-migrations`; generic: wrap the migration runner.

## Add a column

| Engine | Safe | Unsafe |
| --- | --- | --- |
| PostgreSQL 11+ | `ADD COLUMN c text`, `ADD COLUMN c int DEFAULT 0` (constant or stable default, metadata only) | volatile default (`clock_timestamp()`, `gen_random_uuid()`, `random()`) -> rewrite; `NOT NULL` without default -> fails with rows |
| MySQL 8.0.12+ | `ADD COLUMN c ..., ALGORITHM=INSTANT` (8.0.29+ any position) | positioned `AFTER` on older versions, adding to tables with FULLTEXT indexes -> rebuild |

## NOT NULL on an existing column (PostgreSQL)

```sql
ALTER TABLE orders ADD CONSTRAINT orders_region_nn CHECK (region IS NOT NULL) NOT VALID;  -- instant
-- after the backfill:
ALTER TABLE orders VALIDATE CONSTRAINT orders_region_nn;   -- scans, but only SHARE UPDATE EXCLUSIVE
ALTER TABLE orders ALTER COLUMN region SET NOT NULL;        -- PG 12+: uses the valid check, no scan
ALTER TABLE orders DROP CONSTRAINT orders_region_nn;
```

## Indexes

```sql
-- PostgreSQL: must be outside a transaction block
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_status_idx ON orders (status);
-- failed build leaves an INVALID index: drop and retry
DROP INDEX CONCURRENTLY IF EXISTS orders_status_idx;
-- MySQL: online, but heavy I/O and replica lag on big tables
ALTER TABLE orders ADD INDEX idx_status (status), ALGORITHM=INPLACE, LOCK=NONE;
```
Framework switches: Rails `disable_ddl_transaction!` + `algorithm: :concurrently`;
Django `atomic = False` + `AddIndexConcurrently`; Alembic
`with op.get_context().autocommit_block():`; Prisma: hand-edit the migration SQL and run it
separately (Prisma wraps migrations in a transaction on PostgreSQL).

## Unique constraints and primary keys (PostgreSQL)

```sql
CREATE UNIQUE INDEX CONCURRENTLY orders_public_id_key ON orders (public_id);
ALTER TABLE orders ADD CONSTRAINT orders_public_id_key UNIQUE USING INDEX orders_public_id_key;
```

## Foreign keys and CHECK constraints

```sql
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk
  FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;   -- brief lock, no scan
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_fk;         -- separate migration
```
Index the referencing column first (`orders.customer_id`) or every delete on `customers`
scans `orders`.

## Batched backfill

Run outside the schema migration (a job, a management command, a script) so each batch
commits and releases its locks.

```sql
-- PostgreSQL: repeat until 0 rows updated; sleep between batches to protect replicas
UPDATE orders SET region = 'eu'
WHERE id IN (
  SELECT id FROM orders WHERE region IS NULL ORDER BY id LIMIT 5000
  FOR UPDATE SKIP LOCKED
);
-- MySQL: walk the primary key in ranges
UPDATE orders SET region = 'eu' WHERE id BETWEEN 1 AND 5000 AND region IS NULL;
```
```bash
# shell loop (PostgreSQL), stop when nothing is left
while :; do
  n=$(psql "$DATABASE_URL" -qtAc "WITH b AS (SELECT id FROM orders WHERE region IS NULL LIMIT 5000)
        UPDATE orders o SET region = 'eu' FROM b WHERE o.id = b.id RETURNING 1" | wc -l)
  [ "$n" -eq 0 ] && break
  sleep 0.5
done
```
Watch replication lag (`pg_stat_replication.replay_lag`, `SHOW REPLICA STATUS`) and pause
when it grows.

## Changing a column type

- PostgreSQL metadata-only: `varchar(n)` -> larger `varchar(m)` or `text`, `numeric(p,s)` ->
  larger precision with the same scale. Everything else rewrites the table and its indexes.
- Safe path: add the new column, dual-write (trigger or app), backfill in batches, add
  indexes concurrently, swap names in one short transaction (or switch reads in the app),
  drop the old column later.
- MySQL: `MODIFY COLUMN` is usually `ALGORITHM=COPY` (blocks writes). Exceptions:
  increasing `VARCHAR` length within the same length-byte range (<= 255 bytes or > 255),
  some ENUM additions at the end. Use `gh-ost` / `pt-online-schema-change` for the rest.

## Online schema change tools (MySQL)

```bash
gh-ost --host=db1 --database=shop --table=orders \
  --alter="MODIFY COLUMN total DECIMAL(14,2) NOT NULL" \
  --max-load=Threads_running=25 --critical-load=Threads_running=100 \
  --chunk-size=1000 --cut-over=default --execute      # run without --execute first (dry run)

pt-online-schema-change --alter "MODIFY COLUMN total DECIMAL(14,2) NOT NULL" \
  D=shop,t=orders --max-lag=5 --dry-run               # then --execute
```
Both need a primary key (or unique key); triggers (pt-osc) and foreign keys need care.

## Operations to avoid during traffic

`VACUUM FULL`, `CLUSTER`, `REINDEX` without `CONCURRENTLY`, `LOCK TABLE`, `TRUNCATE`,
`OPTIMIZE TABLE`, `ALTER TABLE ... CONVERT TO CHARACTER SET`. Use `pg_repack` /
`pg_squeeze` (PostgreSQL) and gh-ost / pt-osc (MySQL) for online rebuilds.
