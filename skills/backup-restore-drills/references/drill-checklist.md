# Restore drill checklist and log

## Before
- [ ] Scratch target ready (separate server/instance or a new database name), enough disk (2-3x dump size)
- [ ] Same major version as production (or the target version for an upgrade test)
- [ ] Source row counts captured at (or near) the dump time
- [ ] Roles/globals available (`pg_dumpall --globals-only` restored first, or `--no-owner`)

## During
- [ ] Note start time
- [ ] Restore with stop-on-error (`--exit-on-error`, `ON_ERROR_STOP=1`); keep the log
- [ ] `ANALYZE` (PostgreSQL) / `ANALYZE TABLE` on big tables
- [ ] Note end time = measured RTO

## Verify
- [ ] `dump_inspect.py --compare-counts source.tsv restored.tsv` passes
- [ ] Application smoke test passes against the restored copy
- [ ] Extensions, functions, triggers, views, sequences present
- [ ] PITR drills: target time verified with a known before/after row

## After
- [ ] Drop the scratch database (confirm the name first)
- [ ] Record the result

## Drill log template

```
Date:            2026-11-21
Operator:        <name>
Backup used:     shop-2026-11-21.dump (created 02:00 UTC, 4.1 GB)
Target:          drill-db-1, PostgreSQL 16.4
Restore time:    18 min (RTO target 60 min)
Row counts:      48/48 tables match
Smoke test:      ok (login, order history, nightly report)
Issues:          role "reporting" missing -> add globals dump to the backup job
Next drill:      2026-12-21
```
