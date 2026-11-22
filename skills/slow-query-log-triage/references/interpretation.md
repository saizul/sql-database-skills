# Interpreting the summary

| Signal | Meaning | Next step |
| --- | --- | --- |
| Top group has a large share of total time | Fixing it helps the whole server | Start here even if each call is fast |
| Rows examined / rows sent > 100 (MySQL) | The query reads far more than it returns: missing/unused index, or a filter that cannot use the index | `explain-plan-analysis`, `index-advisor` |
| Many calls, mean < 5 ms, big share | Chatty access pattern: N+1 from an ORM loop, polling, missing cache | Batch / eager-load / cache in the application |
| Max much larger than mean | Occasional stalls: lock waits, I/O spikes, plan flips with bind parameters, checkpoints | Correlate times with locks, deploys, backups; check `log_lock_waits` |
| Lock time share high (MySQL `Lock_time`) | Waiting on row or metadata locks | Find the long transaction or hot row; shorten transactions |
| Temp blocks written (pgss) | Sorts or hashes spill to disk | Index the ORDER BY / GROUP BY, or raise `work_mem` for that query |
| Low cache hit ratio (pgss) | Data not in shared buffers / OS cache | Better index (fewer pages), more RAM, or the query reads too much |
| Same shape with different literals in many groups | Literals inside comments or dynamic identifiers break fingerprinting | Normalise in the app (bind parameters) |
| `COMMIT` / `BEGIN` near the top | fsync bound or synchronous replication waits | Check disk latency, `synchronous_commit`, group commit settings |

## Caveats

- MySQL slow logs only contain queries over `long_query_time`; frequent fast queries are
  invisible. pg_stat_statements sees everything.
- `Query_time` includes lock wait; PostgreSQL `duration` is wall time including waits.
- pg_stat_statements is cumulative since the last reset or restart: compare two snapshots
  (export, wait an hour, export again, subtract) for a time window.
- Replicas have their own statistics and logs.
