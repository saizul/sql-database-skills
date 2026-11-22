---
name: slow-query-log-triage
description: Triage database slowness from MySQL/MariaDB slow query logs (including .gz), PostgreSQL log_min_duration_statement logs and pg_stat_statements CSV exports - group queries by fingerprint, rank by total time with count, mean, p95 and max, rows examined per row sent, lock time, temp spills and cache hit ratio, and flag chatty N+1 queries - then pick the few queries worth fixing. Use when the user says the database is slow, shares a slow log, mysqld-slow.log, postgresql.log or pg_stat_statements output, asks which queries to optimise first, or wants to set up slow query logging.
license: MIT
compatibility: Python 3.8+ standard library. Reads log/CSV files; enabling logging on the server needs the user's approval.
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# Slow query log triage

Find the handful of query shapes that consume most of the database's time, and send each
to the right fix (index, rewrite, caching, batching). Total time matters more than the
single slowest query.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- "The database is slow / CPU is at 100%", "what should we optimise first?"
- A MySQL/MariaDB slow log, a PostgreSQL log with `duration:` lines, or a
  pg_stat_statements export
- Setting up slow query logging or pg_stat_statements in the first place

## When not to use

- One known slow query: go straight to `explain-plan-analysis` / `sql-query-review`
- Lock contention or a stuck migration right now: use the lock monitoring queries of the
  `safe-migrations` skill
- Server resource problems (disk full, OOM, swap): check the host first

## Workflow

1. **Get the data** (pick what exists; enabling anything is a config change - show the
   commands and let the user apply them, see [references/setup.md](references/setup.md)):
   - PostgreSQL, best: pg_stat_statements
     ```bash
     psql "$DATABASE_URL" -c "\copy (SELECT s.*, d.datname, r.rolname FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid JOIN pg_roles r ON r.oid = s.userid) TO 'pgss.csv' CSV HEADER"
     ```
   - PostgreSQL log with `log_min_duration_statement = 250ms`
   - MySQL/MariaDB: `slow_query_log = ON`, `long_query_time = 0.5`, then the file from
     `SELECT @@slow_query_log_file;`
2. **Summarise.**
   ```bash
   python3 scripts/slowlog_triage.py /var/log/mysql/mysql-slow.log
   python3 scripts/slowlog_triage.py mysql-slow.log.1.gz --since 2026-11-20T09:00 --until 2026-11-20T10:00
   python3 scripts/slowlog_triage.py postgresql-2026-11-20.log --db shop --top 15
   python3 scripts/slowlog_triage.py pgss.csv --sort mean
   python3 scripts/slowlog_triage.py pgss.csv --json
   ```
   Query text is normalised (literals become `?`), so emails or tokens in the log are not
   echoed; `--samples` prints one raw example per group - only use it when needed, and do
   not paste secrets from it into reports. `--fail-over-ms N` exits 1 when a group's mean
   exceeds N (for alerts / CI on staging).
3. **Classify each top group** (top 5-10 usually cover 80% of the time):
   - high rows examined per row sent / seq scans -> missing index: `index-advisor`
   - slow but well indexed -> inspect the plan: `explain-plan-analysis`
   - anti-patterns in the SQL -> `sql-query-review`
   - thousands of calls at < 5 ms -> N+1 or missing cache: fix in the application
   - lock time dominates -> long transactions or hot rows; look at what holds the locks
   - temp blocks / low cache hit ratio -> memory (`work_mem`, buffer pool) or I/O
   See [references/interpretation.md](references/interpretation.md).
4. **Report and plan** the top fixes with expected impact (share of total time), then
   verify after the change by re-running the same summary on a comparable period, or
   after `SELECT pg_stat_statements_reset();` (only with the user's consent - it clears
   history for everyone).

## Output

```
mysql slow log 2026-11-20 09:00-10:00: 11,240 executions, 37 shapes, 1,830 s total
#1 43%  6,120 calls  mean 129 ms  p95 410 ms
   SELECT id, total FROM orders WHERE status = ? AND customer_id = ? ORDER BY created_at DESC LIMIT ?
   examines 50,000 rows per row returned -> index (customer_id, status, created_at)  [index-advisor]
#2 39%  1 call  12 s  SELECT COUNT(*) FROM events WHERE kind IN (?+)  -> nightly report, move to replica
#3 15%  lock wait 80%  UPDATE inventory ... -> hot row; shorten the transaction around it
Plan: fix #1 (est. -40% DB time), move #2, investigate #3. Re-measure tomorrow same hour.
```
