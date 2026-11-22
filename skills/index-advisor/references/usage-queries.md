# Index usage and size queries (read-only)

Run on the primary and on every replica: usage statistics are per server, and replicas
often serve the read queries that use an index.

## PostgreSQL

```sql
-- indexes never or rarely scanned since the last statistics reset, biggest first
SELECT s.schemaname, s.relname AS table, s.indexrelname AS index,
       s.idx_scan, pg_size_pretty(pg_relation_size(s.indexrelid)) AS size,
       i.indisunique, i.indisprimary
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan < 50
ORDER BY pg_relation_size(s.indexrelid) DESC;

-- when were statistics reset? (idx_scan counts since then)
SELECT stats_reset FROM pg_stat_database WHERE datname = current_database();

-- invalid indexes left by a failed CREATE INDEX CONCURRENTLY
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;

-- index definitions for a table
SELECT indexdef FROM pg_indexes WHERE tablename = 'orders';
```

## MySQL 8 / MariaDB

```sql
-- unused indexes (sys schema, counters since server start)
SELECT * FROM sys.schema_unused_indexes WHERE object_schema = 'shop';
-- redundant indexes as MySQL sees them
SELECT * FROM sys.schema_redundant_indexes WHERE table_schema = 'shop';
-- sizes
SELECT database_name, table_name, index_name,
       ROUND(stat_value * @@innodb_page_size / 1024 / 1024, 1) AS size_mb
FROM mysql.innodb_index_stats
WHERE stat_name = 'size' AND database_name = 'shop'
ORDER BY size_mb DESC;
SHOW INDEX FROM orders;
```

Safer than dropping straight away: MySQL 8 `ALTER TABLE orders ALTER INDEX idx_x INVISIBLE;`
(instant, reversible with `VISIBLE`). PostgreSQL has no invisible indexes; keep the
`CREATE INDEX` statement ready to rebuild.
