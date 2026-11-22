# Dump and restore commands

Credentials come from `~/.pgpass` / `PGPASSFILE` (PostgreSQL) or an option file such as
`~/.my.cnf` with `[client] user=... password=...` and mode 600 (MySQL). Never put
passwords on the command line (visible in `ps`).

## PostgreSQL

```bash
# custom format: compressed, selective and parallel restore
pg_dump -h db1 -U backup -d shop -Fc -Z 6 -f /backups/shop-$(date +%F).dump.tmp \
  && mv /backups/shop-$(date +%F).dump.tmp /backups/shop-$(date +%F).dump
# directory format with parallel dump (big databases)
pg_dump -d shop -Fd -j 4 -f /backups/shop-$(date +%F).dir
# roles, tablespaces (not included in pg_dump!)
pg_dumpall --globals-only --no-role-passwords -f /backups/globals-$(date +%F).sql
# plain SQL (readable, restorable with psql)
pg_dump -d shop --no-owner | gzip > /backups/shop-$(date +%F).sql.gz
```
Use the pg_dump of the *newest* major version involved (dumping a 16 server with pg_dump 14 fails).

Restore into a new database:
```bash
createdb -T template0 shop_restore
pg_restore -d shop_restore --no-owner --role=app -j 4 --exit-on-error /backups/shop.dump
# plain SQL
gunzip -c /backups/shop.sql.gz | psql -v ON_ERROR_STOP=1 -d shop_restore
# inspect / select objects
pg_restore --list shop.dump > shop.toc      # edit, then: pg_restore -L shop.toc ...
pg_restore -d shop_restore -t orders shop.dump   # one table
```
After restore: `ANALYZE;` (statistics are not in dumps) and check sequences
(`SELECT setval(...)` are included in dumps).

## MySQL / MariaDB

```bash
# consistent InnoDB snapshot without locking
mysqldump --defaults-extra-file=/etc/mysql/backup.cnf \
  --single-transaction --quick --routines --triggers --events \
  --set-gtid-purged=OFF --hex-blob --default-character-set=utf8mb4 \
  --databases shop | gzip > /backups/shop-$(date +%F).sql.gz
# MariaDB
mariadb-dump --single-transaction --quick --routines --triggers --events --databases shop \
  | gzip > /backups/shop-$(date +%F).sql.gz
# record the binlog position for PITR (MySQL 8.0.26+: --source-data=2)
mysqldump --single-transaction --source-data=2 ...
```
`--set-gtid-purged=OFF` for copies to other servers; keep `AUTO`/`ON` when seeding a
replica. MyISAM tables are not consistent with `--single-transaction`.

Faster for big data: MySQL Shell `util.dumpInstance()` / `util.loadDump()` (parallel),
`mydumper`/`myloader`, or physical backups (Percona XtraBackup, MariaDB `mariabackup`).

Restore:
```bash
mysql -e "CREATE DATABASE shop_restore CHARACTER SET utf8mb4"
gunzip -c /backups/shop-2026-11-21.sql.gz | sed 's/`shop`/`shop_restore`/g' | mysql shop_restore
# (if the dump used --databases it contains USE `shop`; the sed redirects it - review first)
```

## Backup script skeleton

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
out=/backups/shop-$(date +%F_%H%M).dump
pg_dump -d shop -Fc -f "$out.tmp"
mv "$out.tmp" "$out"
pg_restore --list "$out" > "$out.toc"                  # fails if the archive is unreadable
python3 /opt/skills/backup-restore-drills/scripts/dump_inspect.py "$out.toc" --fail-on high
age -r "$BACKUP_AGE_RECIPIENT" -o "$out.age" "$out" && rm "$out"
# copy off-site (rclone/restic/aws s3 cp), then prune: keep 7 daily, 4 weekly, 12 monthly
```
Alert on failure (systemd `OnFailure=`, healthchecks.io ping, cron MAILTO).
