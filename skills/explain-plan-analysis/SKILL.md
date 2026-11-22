---
name: explain-plan-analysis
description: Analyse PostgreSQL EXPLAIN (ANALYZE, BUFFERS) plans in JSON or text form and MySQL/MariaDB EXPLAIN FORMAT=JSON, EXPLAIN ANALYZE and FORMAT=TREE output - find the nodes with the most self time, sequential and full table scans with selective filters, scans repeated inside nested loops, row misestimates, sorts and hashes spilling to disk, filesorts, temporary tables, low filtered ratios, lossy bitmaps, heap fetches and JIT overhead - and turn them into concrete fixes. Use when the user pastes EXPLAIN or EXPLAIN ANALYZE output, a query plan, auto_explain log entries, or asks why a query is slow, why the planner picked a seq scan, or whether an index is used.
license: MIT
compatibility: Python 3.8+ standard library. Reads plan text; never connects to a database.
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# EXPLAIN plan analysis

Read the plan, find where the time actually goes, explain why the planner chose it and
what single change fixes it. The script parses plans; it never runs queries.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- The user pastes `EXPLAIN`, `EXPLAIN ANALYZE`, `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`,
  `EXPLAIN FORMAT=JSON`, MySQL `EXPLAIN ANALYZE` / `FORMAT=TREE` or `auto_explain` output
- "Why is this query slow?", "why is it not using my index?", "compare these two plans"
- Verifying a new index or a rewritten query (before / after)

## When not to use

- No plan yet: ask for one (step 1) or review the SQL text with `sql-query-review`
- Finding which queries are slow overall: use `slow-query-log-triage`
- Designing the index the plan needs: hand the findings to `index-advisor`

## Workflow

1. **Get a plan with real numbers.** Ask the user to run (on production-like data):
   ```sql
   -- PostgreSQL (best: JSON keeps every field)
   EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT ...;
   -- psql: \o plan.json  then the EXPLAIN, then \o
   -- MySQL 8.0.18+ / MariaDB
   EXPLAIN ANALYZE SELECT ...;              -- tree with actual rows/time (MySQL)
   EXPLAIN FORMAT=JSON SELECT ...;          -- estimates, filtered %, filesort/temporary flags
   ANALYZE FORMAT=JSON SELECT ...;          -- MariaDB: estimates and r_rows actuals
   ```
   `ANALYZE` executes the statement. For `INSERT/UPDATE/DELETE` tell the user to wrap it:
   `BEGIN; EXPLAIN ANALYZE ...; ROLLBACK;` (PostgreSQL) or use a copy. Never run it on
   production writes yourself.
2. **Run the analyser.**
   ```bash
   python3 scripts/explain_analyze.py plan.json
   python3 scripts/explain_analyze.py plan.txt --rows-threshold 50000   # bigger tables
   pbpaste | python3 scripts/explain_analyze.py -                       # from clipboard
   python3 scripts/explain_analyze.py plan.json --json
   ```
   psql decorations (`QUERY PLAN`, `----`, `(12 rows)`, trailing `+`) are stripped.
   Exit 1 = findings at or above `--fail-on` (default `medium`), 2 = unrecognised input.
3. **Explain the top hotspot first.** "Self time" is the node's time minus its children,
   multiplied by loops. Fix the biggest one, re-run, repeat. Typical causes and fixes are in
   [references/plan-nodes.md](references/plan-nodes.md).
4. **Check the estimates.** A misestimate is reported at the deepest node where it starts
   (parents inherit it). Bad estimates cause bad join orders and nested loops over
   thousands of rows. Fix with `ANALYZE table`, a higher statistics target, or
   `CREATE STATISTICS` for correlated columns (PostgreSQL); `ANALYZE TABLE` and histograms
   (MySQL 8). See [references/tuning.md](references/tuning.md).
5. **Propose one change at a time** (index, rewrite, statistics, `work_mem` for one query)
   and ask the user to re-run the same EXPLAIN. Compare before/after self time, rows and
   buffers. Never change server-wide settings without the user's confirmation.

## Decision points

- **Seq scan is fine** when the table is small or most rows are returned. It matters when
  `Rows Removed by Filter` dwarfs the rows kept (selective filter, no usable index).
- **Index exists but is not used:** function or cast on the column, implicit type cast,
  leading wildcard, low selectivity, or stale statistics. Cross-check with `sql-query-review`.
- **Nested loop with a scan inside:** the inner side needs an index on the join column.
- **Sort/Hash spilled to disk:** add an index for the ORDER BY, add LIMIT, or raise
  `work_mem` for that query only (`SET LOCAL work_mem = '64MB'` inside a transaction).
- **MySQL `filtered` < 10%:** the index finds rows the WHERE then throws away; extend it
  with the filtered columns.

## Output

```
Plan: PostgreSQL, ANALYZE, execution 870 ms
Hotspots: Seq Scan on orders 521 ms (60%), Sort 221 ms (25%)

1. Seq Scan on orders reads 1,000,000 rows to keep 48,210 (filter status = 'pending').
   Fix: CREATE INDEX CONCURRENTLY orders_status_created_at_idx ON orders (status, created_at);
   (also serves the ORDER BY created_at, which removes the 9 MB disk sort)
2. Estimate 1,200 rows vs 48,210 actual on orders: run ANALYZE orders;
3. Hash spilled into 4 batches: resolved by 1; otherwise SET LOCAL work_mem = '32MB'.
Re-run: EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ... and compare.
```
