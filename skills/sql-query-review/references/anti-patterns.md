# SQL anti-patterns: what the linter reports and why

Check names match `sql_lint.py` output. Severity is the default the script assigns.

| Check | Severity | Why it hurts | Typical fix |
| --- | --- | --- | --- |
| `select_star` | medium | Reads every column: no index-only scans, more I/O and network, breaks callers when columns are added or reordered. `EXISTS (SELECT * ...)` is not reported. | List the columns the caller uses. |
| `function_on_column` | medium | `f(col) = x` cannot use a B-tree index on `col`; the database evaluates `f` for every row. Includes casts (`col::date`) and arithmetic (`col + 1 > x`, low). | Range on the bare column, move arithmetic to the constant side, or an expression index. |
| `leading_wildcard` | medium | `LIKE '%abc'` cannot seek in a B-tree. | PostgreSQL `pg_trgm` GIN index; MySQL `FULLTEXT`; or anchor the pattern. |
| `or_different_columns` | medium | One B-tree cannot serve `a = 1 OR b = 2`; planners fall back to scans unless both columns are indexed (BitmapOr / index_merge). | `UNION ALL` of two indexed queries, or index both columns. |
| `or_same_column` | low | `a = 1 OR a = 2 OR ...` is fine for the planner but noisy. | `a IN (1, 2, ...)`. |
| `optional_filter` | low | `(col = $1 OR $1 IS NULL)` makes one generic plan for all cases; with prepared statements it is usually a scan. | Build the WHERE clause dynamically. |
| `not_in_subquery` | medium | If the subquery returns a NULL, `NOT IN` returns no rows. PostgreSQL also cannot turn it into an anti-join. | `NOT EXISTS (SELECT 1 ... WHERE ...)`. |
| `null_comparison` | high | `col = NULL` is never true. | `IS NULL` / `IS NOT NULL` (`IS DISTINCT FROM` for nullable comparisons). |
| `dml_without_where` | high | Touches every row, holds locks, generates huge WAL/binlog. | Add WHERE; batch big changes. |
| `order_by_random` | medium | Sorts the whole table to return a few rows. | Random ids in the app, `TABLESAMPLE`, or a precomputed random column. |
| `large_offset` | low | `OFFSET 50000` reads and throws away 50,000 rows on every page. | Keyset pagination: `WHERE (created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC LIMIT 20`. |
| `implicit_cast` | medium/low/info | MySQL compares `varchar_col = 123` numerically: every row is cast, no index. PostgreSQL raises an error for text = integer. Quoted numbers against numeric columns are harmless (info) but hint at wrong driver types. | Compare like with like; bind parameters with the right type. |
| `comma_join` | low | `FROM a, b` with a forgotten condition is a cross join. | Explicit `JOIN ... ON`. |
| `insert_without_columns` | low | Breaks when columns are added or reordered. | Name the columns. |
| `union_distinct` | info | `UNION` sorts/hashes to remove duplicates. | `UNION ALL` when branches cannot overlap. |
| `large_in_list` | low | Huge literal lists: parse cost and plan cache churn. | `= ANY($1::bigint[])` (PostgreSQL), a temp table join, or batching. |
| `n_plus_one` | medium | The same query shape runs many times (one per parent row). | Eager loading / one query with `IN` or a JOIN. |

## Things to check by hand

- **Correlated subqueries in SELECT** (`SELECT (SELECT count(*) FROM items i WHERE i.order_id = o.id) ...`):
  one execution per row; rewrite as a `LEFT JOIN` on a grouped subquery or a `LATERAL` join.
- **DISTINCT over joins:** usually a join that multiplies rows; fix the join (or use `EXISTS`).
- **Existence checks:** `SELECT count(*) ... > 0` counts everything; `SELECT EXISTS (SELECT 1 ...)` stops at the first row.
- **Inclusive timestamp ranges:** `BETWEEN '2026-01-01' AND '2026-01-31'` misses the last day after midnight; use `>= start AND < next_day`.
- **Time zones:** `created_at::date` in PostgreSQL uses the session time zone; be explicit with `AT TIME ZONE`.
- **Locking reads:** `SELECT ... FOR UPDATE` on an unindexed predicate locks many rows (MySQL next-key locks); add the index or narrow the predicate. Use `SKIP LOCKED` for job queues.
- **Large IN (subquery) on MySQL 5.7:** may be executed as a dependent subquery; MySQL 8 semijoin handles most cases.
- **Type of parameters:** JDBC/ORMs binding strings for bigint ids, or `numeric` vs `integer`, can block index use in MySQL.

## Known false positives

- `select_star` on tiny lookup tables or in ad-hoc analytics: suppress with `-- sql-lint: ignore select_star`.
- `function_on_column` when a matching expression index exists (check `\d table` / `SHOW INDEX`).
- `implicit_cast` name heuristics (`phone`, `zip`, `*_code`) without `--schema`: confirm the real type.
- `n_plus_one` in batch scripts that intentionally loop: raise `--repeat-threshold`.
