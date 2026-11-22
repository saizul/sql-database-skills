# Watching a migration (read-only queries)

## PostgreSQL

```sql
-- who is waiting on whom
SELECT pid, wait_event_type, wait_event, state, now() - xact_start AS xact_age,
       pg_blocking_pids(pid) AS blocked_by, left(query, 80) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND state <> 'idle'
ORDER BY xact_start;

-- long-running transactions that will block DDL
SELECT pid, now() - xact_start AS age, state, left(query, 80)
FROM pg_stat_activity WHERE xact_start < now() - interval '1 minute' ORDER BY age DESC;

-- progress of CREATE INDEX CONCURRENTLY (12+)
SELECT phase, blocks_done, blocks_total, tuples_done, tuples_total
FROM pg_stat_progress_create_index;

-- replica lag
SELECT application_name, replay_lag FROM pg_stat_replication;
```
Cancelling the user's own stuck migration: `SELECT pg_cancel_backend(<pid>);` - only after
the user confirms, never `pg_terminate_backend` on other sessions without explicit approval.

## MySQL / MariaDB

```sql
-- metadata lock waits (performance_schema must be enabled)
SELECT object_schema, object_name, lock_type, lock_status, owner_thread_id
FROM performance_schema.metadata_locks WHERE object_type = 'TABLE';
SELECT * FROM sys.schema_table_lock_waits\G
-- long transactions
SELECT trx_id, trx_started, trx_mysql_thread_id, left(trx_query, 80)
FROM information_schema.innodb_trx ORDER BY trx_started;
-- ALTER progress (stage events)
SELECT event_name, work_completed, work_estimated FROM performance_schema.events_stages_current;
SHOW REPLICA STATUS\G      -- Seconds_Behind_Source
```
