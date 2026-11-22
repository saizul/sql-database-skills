---
name: sql-query-review
description: Review SQL queries for PostgreSQL and MySQL/MariaDB performance and correctness anti-patterns - SELECT *, functions or casts on indexed columns, leading-wildcard LIKE, OR chains across columns, NOT IN with NULLs, = NULL, UPDATE/DELETE without WHERE, ORDER BY RAND(), deep OFFSET pagination, implicit type casts, comma joins, N+1 query bursts in logs - with a linter for .sql files, query logs and stdin, then rewrite them. Use when the user asks to review, optimise or lint a SQL query or .sql file, asks why a query is slow or not using an index, pastes ORM-generated SQL, or wants a SQL check in CI.
license: MIT
compatibility: Python 3.8+ standard library. Works offline on SQL text; no database connection needed.
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# SQL query review

Find the query patterns that defeat indexes or return wrong results, explain each one and
give a rewritten query. The linter only reads text; it never connects to a database.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- "Review this query", "why doesn't this use my index?", "make this SQL faster"
- A `.sql` file, a migration's data query, ORM-generated SQL (Django, Rails, Prisma,
  Hibernate, Eloquent) or a query log the user pastes
- Adding a SQL lint step to CI for a `queries/` or `sql/` directory
- Hunting N+1 queries in a PostgreSQL / MySQL general log or `log_statement` output

## When not to use

- The user already has `EXPLAIN` output: use `explain-plan-analysis`
- Choosing which index to create: use `index-advisor` (this skill finds the anti-pattern,
  that one designs the index)
- DDL and migrations (`ALTER TABLE`, `CREATE INDEX`): use `safe-migrations`
- SQL injection in application code: use `sql-security`
- Warehouse dialects (BigQuery, Snowflake): most rules apply, but the cost model differs

## Workflow

1. **Collect the query and context.** Ask (or infer) the engine and version, table sizes,
   the indexes that exist, and how the query is called (once per request? per row?).
   If the user can share `\d table` (psql) or `SHOW CREATE TABLE`, put it in a schema file.
2. **Run the linter.**
   ```bash
   python3 scripts/sql_lint.py query.sql                       # one file
   python3 scripts/sql_lint.py sql/ --dialect postgres          # directory, recursive
   python3 scripts/sql_lint.py query.sql --schema schema.sql    # exact implicit-cast checks
   python3 scripts/sql_lint.py general.log --log                # N+1 detection in a query log
   echo "SELECT * FROM t WHERE lower(email) = 'a'" | python3 scripts/sql_lint.py -
   python3 scripts/sql_lint.py sql/ --json --fail-on high       # CI
   ```
   Exit code 1 = findings at or above `--fail-on` (default `medium`), 2 = unreadable input.
   Suppress an accepted finding inside the statement: `-- sql-lint: ignore select_star`.
3. **Read the query yourself** for what a linter cannot know
   (details in [references/anti-patterns.md](references/anti-patterns.md)):
   - correlated subqueries in the SELECT list that run once per row
   - `DISTINCT` hiding a join that multiplies rows; `GROUP BY` on a non-unique key
   - `COUNT(*)` used only to test existence (use `EXISTS`)
   - missing `LIMIT` on endpoints that list data; unbounded `IN (subquery)`
   - pagination by `OFFSET`; time zones in date predicates; `BETWEEN` on timestamps
     (inclusive upper bound)
   - transactions held open across network calls; `SELECT ... FOR UPDATE` without an
     index on the predicate (locks many rows)
4. **Rewrite.** For every finding give the corrected SQL, not just advice. Keep semantics
   identical (watch NULL handling when turning `NOT IN` into `NOT EXISTS`, and inclusive
   vs exclusive range bounds when replacing `DATE(col) = x`).
   Use [references/rewrites.md](references/rewrites.md) for before/after pairs.
5. **Prove it.** Ask the user to run `EXPLAIN (ANALYZE, BUFFERS)` (PostgreSQL) or
   `EXPLAIN ANALYZE` (MySQL 8.0.18+) for the old and new query on realistic data, and
   analyse both with the `explain-plan-analysis` skill. For writes, wrap in
   `BEGIN; ... ROLLBACK;` or run on a copy. Never run the user's write queries yourself.

## Decision points

- **Function on a column** (`DATE(created_at) = ...`, `lower(email) = ...`): prefer a range
  rewrite; if the function is essential (case-insensitive email), add an expression index
  (PostgreSQL `CREATE INDEX ... (lower(email))`, MySQL 8.0.13+ functional index) or a
  `citext` / case-insensitive collation column.
- **OR across columns:** if each column has its own index, PostgreSQL can BitmapOr and
  MySQL can index_merge; otherwise rewrite as `UNION ALL` with a dedup predicate.
- **N+1:** the fix lives in application code (eager loading / batching), not in SQL.
  Name the ORM call to change (`select_related`, `prefetch_related`, `includes`, `with`).

## Output

```
SQL review: 2 high, 3 medium, 1 low (14 statements)

1. HIGH  orders.sql:12  UPDATE without WHERE touches every row
   Before: UPDATE orders SET status = 'archived';
   After:  UPDATE orders SET status = 'archived' WHERE created_at < now() - interval '2 years';
           (run in batches, see safe-migrations)
2. MEDIUM orders.sql:4  DATE(created_at) prevents index use
   After:  WHERE created_at >= '2026-01-15' AND created_at < '2026-01-16'
...
Not checked automatically: correlated subqueries, DISTINCT over joins (reviewed: none found)
Next: run EXPLAIN (ANALYZE, BUFFERS) on the old and new versions.
```
