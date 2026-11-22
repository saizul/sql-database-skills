# Composite index design rules

## Equality, Sort, Range (ESR)

A B-tree index is sorted by its first column, then the second within equal first values,
and so on. A query can seek on a prefix of the index as long as every earlier column is
constrained by equality.

```sql
SELECT id, total FROM orders
WHERE customer_id = $1 AND status = 'paid' AND created_at >= now() - interval '30 days'
ORDER BY created_at DESC LIMIT 20;
```

| Order | Column | Reason |
| --- | --- | --- |
| 1 | `customer_id` | equality |
| 2 | `status` | equality (order between equality columns only matters for *other* queries) |
| 3 | `created_at` | serves both ORDER BY and the range; the index delivers rows already sorted |

`(customer_id, status, created_at)` reads exactly 20 index entries. `(created_at, customer_id)`
would scan all of the last 30 days.

- **Range before sort** breaks the sort: `(customer_id, total, created_at)` for
  `WHERE customer_id = ? AND total > 100 ORDER BY created_at` must sort. Choose ESR when
  there is a LIMIT, ER(S) when the range is very selective.
- **IN lists** behave like equality for seeking but the rows come out in several sorted
  runs: an ORDER BY after an IN column usually still sorts (MySQL), or needs a merge
  (PostgreSQL can sometimes avoid it).
- **Only one range column** can be used for seeking; later columns are filtered in the index
  (MySQL index condition pushdown, PostgreSQL index filter), which still helps.
- **Mixed sort directions** (`ORDER BY a ASC, b DESC`) need `(a, b DESC)`. Uniform direction
  can be scanned backwards, so plain ascending is fine.
- **Joins:** the inner table of a nested loop needs an index on its join column. The driving
  table needs an index on its filter columns.
- **GROUP BY** columns in index order let the database stream groups (no hash/sort).

## Covering indexes

When every column the query touches is in the index, PostgreSQL can do an Index Only Scan
(if the visibility map is current) and MySQL reports `Using index`.

```sql
-- PostgreSQL 11+: payload columns do not affect sort order and are not used for seeking
CREATE INDEX CONCURRENTLY orders_cust_created_idx ON orders (customer_id, created_at) INCLUDE (total, status);
-- MySQL: append the columns (the primary key is already included in every secondary index)
ALTER TABLE orders ADD INDEX idx_cust_created (customer_id, created_at, total, status), ALGORITHM=INPLACE, LOCK=NONE;
```
Use covering indexes for hot, read-heavy paths only: every extra column makes the index
bigger and every UPDATE of those columns more expensive (and blocks HOT updates in PostgreSQL).

## Partial and expression indexes (PostgreSQL)

```sql
-- queue table: only a few rows are pending
CREATE INDEX CONCURRENTLY jobs_pending_idx ON jobs (run_at) WHERE status = 'pending';
-- case-insensitive lookups
CREATE INDEX CONCURRENTLY users_lower_email_idx ON users (lower(email));
```
The query must repeat the predicate / expression for the planner to use them.
MySQL 8.0.13+ supports functional key parts: `ALTER TABLE users ADD INDEX ((lower(email)))`.

## When not to add an index

- The table is small (a few thousand rows) or the query returns most of it
- Write-heavy tables where the query is rare: each index slows every write and adds WAL/binlog
- A wider existing index already starts with the same columns (extend or reuse it)
- Low-cardinality columns alone (`is_active`, `status`) unless combined or partial

## Redundant and duplicate indexes

- **Duplicate:** same columns, same order, same uniqueness. Keep one.
- **Redundant:** `(a)` is a left prefix of `(a, b)`. The wider index serves queries on `a`
  (slightly bigger to scan). Keep `(a)` if it is UNIQUE or backs a constraint, or if `(a, b)`
  is much wider and `a`-only lookups are very hot.
- MySQL InnoDB requires an index whose first columns are the foreign key columns; dropping
  it fails or another index must take over.
