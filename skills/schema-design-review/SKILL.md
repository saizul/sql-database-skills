---
name: schema-design-review
description: Review a relational schema (pg_dump --schema-only, mysqldump --no-data, migration DDL) for PostgreSQL and MySQL/MariaDB design problems - missing primary keys, 32-bit integer keys, unindexed foreign keys, foreign key type mismatches, FLOAT for money, timestamp without time zone, utf8mb3 and MyISAM, lists in text columns, repeating column groups, all-nullable tables, quoted mixed-case or reserved names, mixed naming styles - and propose normalised, constrained DDL. Use when the user shares a schema or CREATE TABLE statements, designs new tables, asks for a data model or normalization review, or wants to know which types, keys and constraints to use.
license: MIT
compatibility: Python 3.8+ standard library. Reads a schema file; never connects to a database.
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# Schema design review

Make the schema enforce the data rules (keys, constraints, types) so bad data cannot be
stored, and keep it easy to query and evolve.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- "Review my schema / data model", a new `CREATE TABLE`, an ERD turned into DDL
- Choosing types (money, time, ids, text), keys or constraints for a new feature
- Auditing an inherited database before adding features or migrating engines
- Normalization questions: "should tags be a column or a table?"

## When not to use

- Query performance on an existing schema: `sql-query-review`, `explain-plan-analysis`, `index-advisor`
- How to apply the changes to a live database: `safe-migrations`
- Permissions and row-level security: `sql-security`
- Analytics warehouses (star schemas intentionally denormalise)

## Workflow

1. **Get the DDL.** `pg_dump --schema-only --no-owner dbname > schema.sql`,
   `mysqldump --no-data --routines=false dbname > schema.sql`, or the migration files.
   Ask about the domain: what each table represents, expected row counts, which data
   must never be missing or duplicated, and multi-tenancy.
2. **Run the reviewer.**
   ```bash
   python3 scripts/schema_review.py schema.sql
   python3 scripts/schema_review.py schema.sql --dialect mysql --min-severity low
   python3 scripts/schema_review.py schema.sql --json
   ```
   Exit 1 = findings at or above `--fail-on` (default `medium`), 2 = no CREATE TABLE found.
3. **Review the model yourself** with [references/checklist.md](references/checklist.md):
   - every table has a primary key; natural unique keys have UNIQUE constraints too
   - every relationship has a foreign key with a deliberate `ON DELETE` behaviour
   - required columns are `NOT NULL`; enumerated values have CHECK constraints or lookup tables
   - one fact in one place (no copied customer names on orders unless it is a snapshot by design)
   - many-to-many through junction tables with a composite primary key
   - soft deletes, audit columns, tenant columns applied consistently
4. **Propose DDL**, not just comments: corrected `CREATE TABLE` statements or the
   `ALTER TABLE` steps, with type choices from [references/types.md](references/types.md).
   Note when a change rewrites a big table and route it through `safe-migrations`.
5. **Prioritise:** data integrity first (keys, NOT NULL, money types, utf8mb4), then
   performance (FK indexes, key sizes), then naming consistency.

## Decision points

- **Surrogate vs natural key:** use a `bigint` identity (or UUIDv7 for distributed
  inserts) as the primary key and a UNIQUE constraint on the natural key (email, sku).
- **JSON columns:** fine for truly schemaless attributes; not for data you filter, join or
  constrain on. Promote those to columns.
- **Arrays / delimited lists:** a junction table unless you never join, constrain or
  update individual values.
- **ENUM vs lookup table vs CHECK:** CHECK (`status IN (...)`) or PostgreSQL enum for small
  stable sets; a lookup table when values carry attributes or change often.

## Output

```
Schema review: 12 tables (PostgreSQL)
HIGH   event_log has no primary key -> add id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
HIGH   orders.total double precision -> numeric(12,2)
MEDIUM orders.customer_id FK has no index -> CREATE INDEX CONCURRENTLY ... (customer_id)
MEDIUM orders.tag_ids text -> order_tags (order_id, tag_id) junction table
LOW    customers.created_at timestamp -> timestamptz
Proposed DDL: (corrected CREATE TABLE / ALTER statements)
Migration notes: numeric and timestamptz changes rewrite orders (40M rows): plan with safe-migrations.
```
