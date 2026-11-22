# Expand / contract (rename, split, retype)

Running app instances keep the old schema in their code (and ORM caches) until they are
replaced. Every migration must work with the *previous* and the *next* app release.

## Rename a column (`total` -> `amount`)

| Step | Database | App |
| --- | --- | --- |
| 1 expand | `ADD COLUMN amount` (same type, nullable) | - |
| 2 dual write | trigger copying `total` -> `amount`, or app writes both | release A writes both, reads `total` |
| 3 backfill | batched `UPDATE ... SET amount = total WHERE amount IS NULL` | - |
| 4 switch reads | constraints/indexes on `amount` (CONCURRENTLY, NOT VALID) | release B reads `amount`, still writes both |
| 5 stop old writes | drop the trigger | release C writes only `amount`; Rails `ignored_columns = ["total"]` |
| 6 contract | `ALTER TABLE ... DROP COLUMN total` (after backup) | - |

PostgreSQL shortcut for small teams: a view or generated column can bridge one release,
but only for reads.

## Rename a table

Create the new table name as a view over the old (PostgreSQL: simple views are
updatable), switch the app, then rename the table and drop the view in one short
transaction. MySQL `RENAME TABLE` is atomic but still breaks code using the old name.

## Drop a column

1. Release code that no longer reads or writes it (Rails `ignored_columns`, Django: remove
   the field and deploy with `SeparateDatabaseAndState`, Prisma: remove from schema and
   deploy before the migration).
2. Back up the data if it might be needed (`COPY (SELECT id, col FROM t) TO ...`).
3. `ALTER TABLE ... DROP COLUMN` with a lock timeout (metadata-only in PostgreSQL; INSTANT
   in MySQL 8.0.29+).

## Split a table / change a key type

Same pattern: new structure, dual write, backfill, verify counts (see
`backup-restore-drills` for count queries), switch reads, stop old writes, drop.
