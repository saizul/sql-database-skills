# Enabling slow query data (show these to the user; they apply them)

All of these change server configuration. Explain the overhead and get confirmation.

## PostgreSQL: pg_stat_statements (recommended, low overhead)

```ini
# postgresql.conf (requires a restart for shared_preload_libraries)
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.max = 10000
pg_stat_statements.track = top
track_io_timing = on            # adds I/O timing to plans and statements; check pg_test_timing first
```
```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;   -- in each database you query it from
SELECT query, calls, total_exec_time, mean_exec_time, rows
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
```
Managed services: RDS/Aurora (parameter group), Cloud SQL (`cloudsql.enable_pg_stat_statements`
is on by default in recent versions), Azure (server parameter), Supabase/Neon (available by default).
PostgreSQL 12 and older name the columns `total_time` / `mean_time`; the script handles both.

## PostgreSQL: duration logging

```ini
log_min_duration_statement = 250ms   # 0 logs everything: only briefly, it is expensive
log_line_prefix = '%m [%p] %q%u@%d '  # the script reads user@db from this prefix
log_lock_waits = on
log_temp_files = 0
```
Reload: `SELECT pg_reload_conf();`. Logs are in `log_directory` (often
`/var/lib/postgresql/16/main/log` or `/var/log/postgresql`).

## MySQL / MariaDB slow log

```sql
SET GLOBAL slow_query_log = ON;
SET GLOBAL long_query_time = 0.5;          -- seconds, applies to new connections
SET GLOBAL log_queries_not_using_indexes = OFF;  -- noisy; enable briefly if needed
SELECT @@slow_query_log_file;
```
Persist in `my.cnf` (`[mysqld] slow_query_log = 1`, `long_query_time = 0.5`) or with
`SET PERSIST` (MySQL 8). RDS/Aurora: parameter group plus `log_output = FILE` and download
from the console, or `TABLE` and query `mysql.slow_log`.

Rotate the log (`logrotate` with `copytruncate`, or `FLUSH SLOW LOGS`), and treat it as
sensitive: it contains literal values from queries (emails, tokens).

## Sampling instead of everything

- MySQL 8: `log_slow_rate_limit` (Percona/MariaDB), or a low `long_query_time` for a short window
- PostgreSQL 13+: `log_min_duration_sample` + `log_statement_sample_rate`
