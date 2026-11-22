# Statistics and per-query tuning

Always prefer fixing the query or index. Session settings are for single expensive
queries (reports, batch jobs); server-wide changes need the user's confirmation and a
capacity check (`work_mem` is per sort/hash node per connection).

## PostgreSQL

```sql
-- refresh statistics for one table
ANALYZE orders;
-- skewed column: more histogram buckets (default 100)
ALTER TABLE orders ALTER COLUMN status SET STATISTICS 1000;
ANALYZE orders;
-- correlated columns (city and zip, tenant_id and status)
CREATE STATISTICS orders_tenant_status (dependencies, ndistinct, mcv) ON tenant_id, status FROM orders;
ANALYZE orders;
-- per-query memory for sorts/hashes
BEGIN;
SET LOCAL work_mem = '64MB';
SELECT ...;
COMMIT;
-- JIT costs more than it saves on short queries
SET jit = off;   -- or raise jit_above_cost
```

Check autovacuum is keeping up:
```sql
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20;
```

Capture plans of slow queries automatically with `auto_explain`
(`auto_explain.log_min_duration = '500ms'`, `auto_explain.log_analyze = on`,
`auto_explain.log_buffers = on`, `auto_explain.log_format = json`); `log_analyze` adds
overhead to every query, so sample with `auto_explain.sample_rate`.

## MySQL 8 / MariaDB

```sql
ANALYZE TABLE orders;
-- histograms for non-indexed, skewed columns (MySQL 8.0)
ANALYZE TABLE orders UPDATE HISTOGRAM ON status WITH 64 BUCKETS;
-- MariaDB: persistent statistics
ANALYZE TABLE orders PERSISTENT FOR ALL;
-- per-session sort buffer for one report
SET SESSION sort_buffer_size = 8 * 1024 * 1024;
-- see which index the optimizer considered
SET optimizer_trace = 'enabled=on';
SELECT ...;
SELECT * FROM information_schema.OPTIMIZER_TRACE\G
SET optimizer_trace = 'enabled=off';
```

Index hints (`FORCE INDEX`, `USE INDEX`) are a last resort: they freeze today's data
distribution into the query.
