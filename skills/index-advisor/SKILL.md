---
name: index-advisor
description: Recommend PostgreSQL and MySQL/MariaDB B-tree indexes from real queries and the schema - composite indexes ordered by the equality, sort, range rule, covering indexes (INCLUDE or appended columns), join-column indexes, and whether an existing index or the primary key already serves a query or should be extended - plus duplicate and redundant left-prefix indexes to drop. Prints ready CREATE INDEX CONCURRENTLY / ALTER TABLE ... ALGORITHM=INPLACE statements with reasoning. Use when the user asks which index to add, why an index is not helping, to design a composite or covering index, to clean up unused or duplicate indexes, or shares slow queries together with a schema.
license: MIT
compatibility: Python 3.8+ standard library. Reads .sql files (queries and schema dumps); never connects to a database.
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# Index advisor

Design the smallest set of indexes that serves the workload, explain the column order and
deploy them without blocking writes. Fewer, wider indexes beat many single-column ones.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- "Which index should I add for this query?", "why is my index not used?"
- A slow query (from `slow-query-log-triage` or `explain-plan-analysis`) and its tables
- Index hygiene: duplicate, redundant or unused indexes slowing down writes
- Reviewing a pull request that adds an index

## When not to use

- Full-text search, JSON containment, geometry, arrays: GIN / GiST / FULLTEXT / SPATIAL
  indexes are outside the script; see [references/index-types.md](references/index-types.md)
- The query itself is the problem (functions on columns, OR chains): run
  `sql-query-review` first, then index the rewritten query
- Applying the index to production: follow `safe-migrations`

## Workflow

1. **Collect inputs.**
   - Queries: the real SQL with representative literals or placeholders, ideally the
     top entries from `slow-query-log-triage`. Several queries on the same table give
     better (consolidated) advice.
   - Schema: `pg_dump --schema-only -t orders -t customers dbname > schema.sql` or
     `mysqldump --no-data dbname orders customers > schema.sql`.
   - Size and write rate of the tables (an index costs on every INSERT/UPDATE/DELETE).
2. **Run the advisor.**
   ```bash
   python3 scripts/index_advisor.py queries.sql --schema schema.sql
   python3 scripts/index_advisor.py queries.sql --schema schema.sql --dialect mysql
   python3 scripts/index_advisor.py queries/ --schema schema.sql --json
   ```
   Output per query and table: `suggest`, `extend <existing index>` or
   `already served by <index>`, the reasoning, then a consolidated list of proposed
   indexes and the duplicate/redundant ones. Exit 1 = something to propose, 0 = nothing.
3. **Sanity-check each proposal** with the rules in
   [references/design-rules.md](references/design-rules.md):
   - equality columns first, then the ORDER BY / GROUP BY columns, then one range column
   - put the most selective equality column first only when other queries also filter by it
   - covering (INCLUDE) only for hot, read-heavy queries; it makes the index bigger
   - skip low-cardinality single columns (`status` alone) unless the value is rare -
     then a partial index (`WHERE status = 'pending'`) is smaller and better (PostgreSQL)
   - MySQL InnoDB secondary indexes already contain the primary key: drop `id` from
     covering columns
4. **Check usage before dropping.** Redundant or unused indexes may back a constraint or a
   rare report. Ask the user to run the usage queries in
   [references/usage-queries.md](references/usage-queries.md) on the primary and every
   replica (statistics are per server) before any `DROP INDEX`.
5. **Hand over deploy-safe DDL.** PostgreSQL `CREATE INDEX CONCURRENTLY` outside a
   transaction, MySQL `ALGORITHM=INPLACE, LOCK=NONE`; set a lock timeout; build during
   low traffic; verify with `explain-plan-analysis` that the new index is used. For
   "extend", create the wider index first and drop the old one only after verification.
   Never create or drop indexes yourself; give the statements to the user.

## Output

```
Proposed indexes (2):
1. orders (customer_id, status, created_at)  -- replaces orders_customer_id_idx
   why: customer_id = ? and status = ? (equality), then ORDER BY created_at with LIMIT
        (no sort); serves queries 1 and 4
   CREATE INDEX CONCURRENTLY orders_customer_id_status_created_at_idx
       ON orders (customer_id, status, created_at);
   covering variant (hot path only): ... INCLUDE (total)
2. order_items (order_id) - join column, looked up once per order
Drop after checking pg_stat_user_indexes on primary and replicas:
   orders_status_idx duplicates orders_status_dup_idx
Verify: EXPLAIN (ANALYZE, BUFFERS) before and after.
```
