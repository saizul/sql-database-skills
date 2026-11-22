# Beyond B-tree

| Need | PostgreSQL | MySQL / MariaDB |
| --- | --- | --- |
| `LIKE '%abc%'`, fuzzy match | `CREATE EXTENSION pg_trgm; CREATE INDEX ... USING gin (col gin_trgm_ops)` | `FULLTEXT` (word based, not substring) |
| Full-text search | `tsvector` column + GIN index, `to_tsquery` | `FULLTEXT` + `MATCH ... AGAINST` |
| JSON containment `@>`, key exists `?` | GIN on `jsonb` (`jsonb_path_ops` for `@>` only) | multi-valued index on `JSON` arrays (8.0.17+), generated columns |
| Arrays `@>`, `&&` | GIN | - |
| Ranges, exclusion constraints, geometry | GiST (`btree_gist` for mixed) | `SPATIAL` (InnoDB) |
| Huge append-only time series | BRIN on the timestamp | partitioning |
| Equality only, big keys | hash index (WAL-logged since 10) | - |

GIN indexes are slower to update; `fastupdate` and `gin_pending_list_limit` trade write
cost for read latency. Always check the operator in the query matches the operator class.
