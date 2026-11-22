# Reading plan nodes

## PostgreSQL

| Node / field | What it means | When it is a problem | Usual fix |
| --- | --- | --- | --- |
| Seq Scan | Reads the whole table | `Rows Removed by Filter` much larger than rows kept | Index on the filter columns (see index-advisor) |
| Index Scan | Seeks the index, then visits the heap per row | Many rows returned (random I/O) or `Rows Removed by Filter` after the index | Make the index match more of the WHERE; covering index |
| Index Only Scan | Answers from the index | `Heap Fetches` high: visibility map stale | `VACUUM` the table; tune autovacuum |
| Bitmap Heap Scan | Collects matching TIDs, reads pages in order | `lossy` heap blocks, big `Rows Removed by Index Recheck` | More `work_mem` |
| Nested Loop | For each outer row, probe the inner side | Inner side is a Seq Scan or loops are in the thousands | Index the inner join column; fix estimates so the planner picks a hash join |
| Hash Join / Hash | Builds a hash table of the inner side | `Batches` > 1 (spilled to disk) | `work_mem` / `hash_mem_multiplier`, smaller input |
| Merge Join | Joins two sorted inputs | Needs sorts on both sides | Indexes that provide the order |
| Sort | Sorts rows | `Sort Method: external merge  Disk: ...` | Index for ORDER BY, LIMIT, more `work_mem` |
| Incremental Sort | Sorts groups of a presorted prefix | Usually fine | - |
| HashAggregate / GroupAggregate | GROUP BY | Disk usage (PostgreSQL 13+ reports it) | `work_mem`, index on GROUP BY columns |
| Gather / Gather Merge | Parallel query | `Workers Launched` < `Workers Planned` | `max_parallel_workers` exhausted |
| Memoize | Caches inner results of a nested loop | Low hit rate | Usually fine |
| CTE Scan | Materialised CTE (before 12 always; after 12 only if referenced twice or `MATERIALIZED`) | Prevents predicate push-down | `NOT MATERIALIZED` or inline |
| SubPlan | Correlated subquery executed per row | `loops` large | Rewrite as JOIN / LATERAL |

**Timings:** `actual time=a..b` is per loop; multiply by `loops`. Parallel workers report
averages per worker. `Buffers: shared read` = pages from disk/OS cache, `hit` = shared buffers.

**Estimates:** `rows=` in `(cost=...)` is the estimate per loop. A factor of 10 or more
between estimate and actual at the bottom of the plan is the usual root cause of bad plans.
Overestimates under `Limit` are expected (execution stops early).

## MySQL / MariaDB

| access_type (JSON) / tree node | Meaning | Problem when | Fix |
| --- | --- | --- | --- |
| `ALL` / `Table scan on t` | Full table scan | Large `rows_examined_per_scan`, especially on the inner table of a join | Index the WHERE / join columns |
| `index` / `Index scan on t using k` | Full index scan | Large tables | Usually ORDER BY without a selective WHERE; add a selective prefix |
| `range` | Index range | `filtered` low | Extend the index with filtered columns |
| `ref` / `eq_ref` | Index lookup (non-unique / unique) | - | Good |
| `const` / `system` | At most one row | - | Good |
| `using_filesort` / `Sort:` | Sort not provided by an index | Many rows | Index with equality columns then ORDER BY columns |
| `using_temporary_table` / `Temporary table` | GROUP BY / DISTINCT / UNION in a temp table | Large groups (may go to disk) | Index the GROUP BY columns |
| `using_join_buffer` (hash join, BNL) | Join without index on the inner table | Large inner table | Index the join column |
| `dependent: true` / DEPENDENT SUBQUERY | Correlated subquery per row | Many outer rows | Rewrite as JOIN / EXISTS |
| `filtered` | % of examined rows kept by the remaining conditions | < 10% | Better index |
| MariaDB `r_rows`, `r_filtered` | Actual rows / filtered | Far from `rows` / `filtered` | `ANALYZE TABLE`, histograms |
