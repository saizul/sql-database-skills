# Point-in-time recovery

## PostgreSQL

Requirements: a base backup plus continuous WAL archiving.

- Tools: **pgBackRest** (recommended: parallel, compression, encryption, retention,
  verify), **WAL-G**, or `pg_basebackup` + `archive_command`.
- `postgresql.conf`: `wal_level = replica`, `archive_mode = on`, `archive_command` /
  `archive_library` from the tool, `archive_timeout = 60` for a bounded RPO on quiet systems.
- Monitor `pg_stat_archiver` (`failed_count`, `last_failed_time`): a failing archive
  command means no PITR and a filling `pg_wal` directory.

Recovery to a new server (PostgreSQL 12+):
```bash
pgbackrest --stanza=main --type=time --target="2026-11-21 09:41:00+00" \
  --target-action=promote --delta restore
# or manually: restore base backup, then in postgresql.conf
#   restore_command = 'cp /archive/%f %p'
#   recovery_target_time = '2026-11-21 09:41:00+00'
#   recovery_target_action = 'pause'      # inspect before promoting
# touch recovery.signal; start; verify; SELECT pg_wal_replay_resume(); or pg_promote()
```
Find the target: the timestamp just before the bad statement (application logs,
`log_statement = 'ddl'` output) or a transaction id / LSN (`pg_waldump`).

## MySQL / MariaDB

Requirements: a full backup with its binlog position, plus retained binary logs.

- `log_bin = ON`, `binlog_format = ROW`, `binlog_expire_logs_seconds` long enough to
  cover the interval between full backups, `sync_binlog = 1`, `gtid_mode = ON` (MySQL).
- Full backup: mysqldump `--source-data=2` (position in a comment), XtraBackup
  (`xtrabackup_binlog_info`) or mariabackup.

Recovery on a new server:
```bash
# 1. restore the full backup
# 2. find the bad event
mysqlbinlog --base64-output=decode-rows -vv --start-datetime="2026-11-21 09:30:00" binlog.000123 | less
# 3. replay up to just before it
mysqlbinlog --start-position=<from backup> --stop-position=<before bad event> binlog.000123 binlog.000124 \
  | mysql
# GTID variant: --exclude-gtids='uuid:1234' to skip the bad transaction
```

## Drill both

Run a PITR drill at least quarterly: restore to a timestamp, verify a known row exists
and a known later change does not. Record how long WAL/binlog replay took.
