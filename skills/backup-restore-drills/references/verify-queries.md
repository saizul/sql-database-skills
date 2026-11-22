# Row counts for restore verification

Exact counts are slow on big tables but are the point of a drill. Run the same query on
the source (at the dump time, or on a replica paused at that point) and on the restored
copy, save the output as TSV and compare with `dump_inspect.py --compare-counts`.

## PostgreSQL (exact, one row per table)

```bash
psql -d shop -At -F $'\t' <<'SQL' > source.tsv
SELECT format('%s', c.relname),
       (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
SQL
```
Estimate only (fast): `SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY 1;`
(use `--tolerance 0.05`, estimates drift).

## MySQL / MariaDB

```bash
# generate exact COUNT(*) statements, then run them
mysql -N -e "SELECT CONCAT('SELECT ''', table_name, ''', COUNT(*) FROM \`', table_schema, '\`.\`', table_name, '\`;')
             FROM information_schema.tables WHERE table_schema = 'shop' AND table_type = 'BASE TABLE'" \
  | mysql -N shop > source.tsv
```
`information_schema.tables.table_rows` is an estimate for InnoDB; do not use it for drills.

## Compare

```bash
python3 scripts/dump_inspect.py --compare-counts source.tsv restored.tsv            # exact
python3 scripts/dump_inspect.py --compare-counts source.tsv restored.tsv --tolerance 0.001
```
Missing tables and lower counts are high severity; higher counts usually mean the source
counts were taken earlier than the dump.

## Beyond counts

- Checksums of a critical table: PostgreSQL `SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM orders t;`
  MySQL `CHECKSUM TABLE orders;` (engine-specific, compare same versions only)
- Application smoke test: log in, load the newest order, run the nightly report
- Constraints and indexes present: compare `pg_dump --schema-only` / `mysqldump --no-data`
  of both sides with `diff`
