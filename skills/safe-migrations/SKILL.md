---
name: safe-migrations
description: Plan and review zero-downtime schema migrations for PostgreSQL and MySQL/MariaDB - add columns nullable, backfill in batches, CREATE INDEX CONCURRENTLY, NOT VALID constraints, lock_timeout / lock_wait_timeout, expand and contract renames, ALGORITHM=INSTANT/INPLACE, gh-ost and pt-online-schema-change - with a linter that scans migration SQL for table rewrites, blocking locks, CONCURRENTLY inside transactions, NOT NULL without default, type changes, destructive DROP/TRUNCATE/RENAME and in-migration backfills. Use when the user writes or reviews a database migration (Rails, Django, Laravel, Prisma, Flyway, Liquibase, Alembic, Knex, golang-migrate), plans an ALTER TABLE on a large or busy table, or asks how to rename, retype or add a column without downtime.
license: MIT
compatibility: Python 3.8+ standard library. Reads .sql files; ORM migrations need their generated SQL (sqlmigrate, rails db:migrate:status + structure.sql, prisma migrate diff, alembic --sql).
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# Safe migrations

Change the schema of a live database without blocking traffic or breaking app instances
that are still running the previous release. Every risky step is shown to the user, and
nothing runs against a database without their confirmation.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- Writing or reviewing a migration file or the SQL it generates
- "Add a NOT NULL column to a 50M row table", "rename this column", "change int to bigint"
- Planning an index or constraint on a busy table; a migration that timed out or locked the site
- A CI check for `migrations/*.sql`

## When not to use

- Designing which index to add: `index-advisor`; reviewing the resulting schema: `schema-design-review`
- Restoring after a failed migration: `backup-restore-drills`
- Empty databases or tables created in the same migration (nothing to lock)

## Workflow

1. **Get the SQL that will actually run.** ORMs hide locking details. Generate it:
   Django `python manage.py sqlmigrate app 0042`, Rails `rails db:migrate` with
   `structure.sql` diff or `ActiveRecord::Base.logger`, Laravel `php artisan migrate --pretend`,
   Prisma `prisma migrate diff --script`, Alembic `alembic upgrade head --sql`,
   Flyway/Liquibase/golang-migrate: the `.sql` files. Ask for table sizes and write rates
   and the engine version (PostgreSQL 11+ fast defaults, 12+ NOT NULL shortcut; MySQL
   8.0.12+ INSTANT, 8.0.29+ INSTANT at any column position).
2. **Lint it.**
   ```bash
   python3 scripts/migration_lint.py migrations/20261101_add_region.sql
   python3 scripts/migration_lint.py db/migrate/sql/ --dialect postgres
   python3 scripts/migration_lint.py migration.sql --transaction yes   # framework wraps in BEGIN
   python3 scripts/migration_lint.py migrations/ --json --fail-on high  # CI
   ```
   Use `--transaction yes` for Rails, Django, Alembic and Prisma on PostgreSQL (each
   migration runs in a transaction unless disabled). Exit 1 = findings at or above
   `--fail-on`. Accept a finding explicitly with `-- migration-lint: ignore <check>` and a reason.
3. **Rewrite risky steps** with the patterns in [references/patterns.md](references/patterns.md):
   - one lock-taking statement per migration, preceded by `SET lock_timeout = '5s'`
     (PostgreSQL) / `SET SESSION lock_wait_timeout = 5` (MySQL), with retries
   - indexes `CONCURRENTLY` in a non-transactional migration
   - constraints `NOT VALID` first, `VALIDATE CONSTRAINT` in a later migration
   - backfills outside the schema migration, in batches, committed per batch
   - renames and type changes as expand/contract across several deploys
4. **Order the deploy.** Write the plan as numbered releases (see
   [references/expand-contract.md](references/expand-contract.md)): which migration ships
   with which app version, and what must be true before the next step (backfill finished,
   no code reads the old column). Destructive steps (DROP, TRUNCATE) come last, after a
   verified backup.
5. **Rehearse.** Ask the user to run the migration against a production-sized copy or
   staging with traffic, time it, and watch for blocked sessions
   ([references/monitoring.md](references/monitoring.md)). Do not run migrations against
   production yourself; give the commands and wait for the user.

## Decision points

- **Big MySQL table and the change is not INSTANT/INPLACE-online:** use `gh-ost` or
  `pt-online-schema-change` instead of a direct ALTER (and check replica lag throttling).
- **PostgreSQL type change int -> bigint on a large table:** new column + trigger or
  dual-write + batched backfill + swap in one short transaction; never `ALTER COLUMN TYPE`.
- **Lock timeout hit:** something holds a long transaction on the table. Find it
  (monitoring queries), do not raise the timeout blindly.
- **Rollback:** every step should have a down path; for DROP it is "restore from backup",
  so make the drop the last, separate step.

## Output

```
Migration review: 20261101_orders_changes.sql (PostgreSQL 16, orders ~40M rows)
HIGH  line 3  CREATE INDEX without CONCURRENTLY blocks writes for the whole build
HIGH  line 6  ADD COLUMN region text NOT NULL fails on a non-empty table
MED   line 9  FOREIGN KEY without NOT VALID locks orders and customers during validation

Safe plan:
  Release 1 (migration, no transaction):
    SET lock_timeout = '5s';
    ALTER TABLE orders ADD COLUMN region text;
    CREATE INDEX CONCURRENTLY orders_status_idx ON orders (status);
    ALTER TABLE orders ADD CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id)
      REFERENCES customers (id) NOT VALID;
  Job: backfill region in 5k-row batches (script in references/patterns.md)
  Release 2: ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_fk;
             NOT NULL via CHECK ... NOT VALID -> VALIDATE -> SET NOT NULL
Rehearse on staging; watch pg_stat_activity for lock waits.
```
