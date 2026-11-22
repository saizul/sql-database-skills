---
name: backup-restore-drills
description: Set up and prove PostgreSQL and MySQL/MariaDB backups - pg_dump/pg_restore (custom and directory formats, parallel jobs), pg_dumpall globals, mysqldump/mariadb-dump with --single-transaction, physical backups and point-in-time recovery (pgBackRest, WAL archiving, binlogs), retention and encryption - and run restore drills that check a dump is complete (end marker, truncated gzip, version mismatch, missing table data) and compare row counts between source and restored copy. Use when the user asks to back up a database, restore a dump, test or verify backups, recover to a point in time, migrate data between servers, or after a failed restore.
license: MIT
compatibility: Python 3.8+ standard library for the checker. pg_dump/pg_restore or mysqldump/mariadb-dump are needed on the machine running the backups.
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# Backup and restore drills

A backup counts only after it has been restored. Build backups that can be restored,
check every dump automatically, and run a restore drill with row-count verification.
Never restore over an existing database without explicit confirmation.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- "Back up my Postgres/MySQL database", "how do I restore this dump?"
- "Are my backups any good?", a scheduled restore test, an audit asking for proof
- Point-in-time recovery after a bad deploy or an accidental DELETE
- Moving a database to a new server or major version

## When not to use

- Schema changes: `safe-migrations` (but take a backup first)
- Whole-server file backups (restic, borg) without database consistency: dump first, then
  back up the dump
- Managed databases where the provider handles PITR (RDS, Cloud SQL): use their
  snapshot/PITR tooling, but still run restore drills with the checker

## Workflow

1. **Define the targets** with the user: RPO (how much data may be lost) and RTO (how long
   a restore may take), database size, engine version, where backups go, retention, and
   who gets alerted. Logical dumps suit databases up to tens of GB; beyond that, or for
   RPO under a day, add physical backups with WAL/binlog archiving
   ([references/pitr.md](references/pitr.md)).
2. **Use the right dump commands** from [references/commands.md](references/commands.md):
   PostgreSQL `pg_dump -Fc` (or `-Fd -j 4`) plus `pg_dumpall --globals-only`; MySQL
   `mysqldump --single-transaction --routines --triggers --events` (InnoDB only).
   Scripts must fail loudly: `set -euo pipefail`, check exit codes, write to a temp file and
   rename on success, then encrypt and copy off-site.
3. **Check every dump** right after it is written (cron/CI):
   ```bash
   python3 scripts/dump_inspect.py /backups/shop-2026-11-21.sql.gz --max-age-hours 26
   pg_restore --list /backups/shop.dump > /tmp/shop.toc && python3 scripts/dump_inspect.py /tmp/shop.toc
   python3 scripts/dump_inspect.py /backups/shop.sql.gz --expected prod_counts.tsv --tolerance 0.01
   python3 scripts/dump_inspect.py /backups/*.sql.gz --json
   ```
   It reports truncated dumps (missing end marker, broken gzip), tiny files, stale
   backups, pg_dump older than the server, tables without data, DEFINER / GTID_PURGED /
   OWNER traps. Exit 1 = findings at or above `--fail-on` (default `medium`).
4. **Run a restore drill** (monthly at least) into a scratch database or server - never
   into production:
   - restore with the commands in [references/commands.md](references/commands.md)
     (`pg_restore -d drill_db --no-owner -j 4`, `mysql drill_db < dump.sql`)
   - collect row counts on source and restored copy with
     [references/verify-queries.md](references/verify-queries.md)
   - compare: `python3 scripts/dump_inspect.py --compare-counts source.tsv restored.tsv`
   - run a smoke query the application depends on, and time the whole restore (that is your real RTO)
   - record the result using the drill log template in [references/drill-checklist.md](references/drill-checklist.md)
5. **Point-in-time recovery:** follow [references/pitr.md](references/pitr.md). Always
   restore to a new instance, find the exact target time/LSN/binlog position, verify, then
   switch or copy data back. Confirm every destructive step with the user.

## Safety rules

- Restore into a new database name (`shop_restore_20261121`), never `DROP DATABASE` or
  restore over the live one without the user's explicit, specific approval.
- Never print or commit passwords: use `~/.pgpass` (mode 600), `PGPASSFILE`, or
  `mysql_config_editor` / an option file with mode 600.
- Dumps contain all the data: encrypt them (`age`, `gpg`, restic) and restrict access.

## Output

```
Backup check: shop-2026-11-21.sql.gz (mysqldump 8.0.40, 1.2 GB, 3 h old)
  complete: yes   tables: 48 (45 with data)
  INFO: GTID_PURGED set - use --set-gtid-purged=OFF for copies to other servers
Restore drill 2026-11-21 into drill-db-1:
  restore time 14 min (RTO target 60 min: ok)
  row counts: 48/48 tables match (tolerance 0)
  smoke test: SELECT count(*) FROM orders WHERE created_at > now() - interval 1 day -> 1,204
Next drill: 2026-12-21. Gaps: no binlog archiving yet (RPO = 24 h).
```
