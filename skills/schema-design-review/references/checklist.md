# Schema review checklist

## Keys and relationships
- [ ] Every table has a PRIMARY KEY (InnoDB clusters rows by it; logical replication,
      gh-ost and most ORMs require one)
- [ ] Surrogate keys are `bigint` (identity / AUTO_INCREMENT) or UUID (prefer UUIDv7 or ULID
      for index locality); never reuse ids
- [ ] Natural keys (email, sku, external ids) have UNIQUE constraints, case-normalised where needed
- [ ] Every reference is a FOREIGN KEY with the same type as the referenced column
- [ ] `ON DELETE` chosen deliberately: `RESTRICT` (default-ish), `CASCADE` for owned children,
      `SET NULL` for optional links
- [ ] PostgreSQL: every FK column has an index whose first column(s) are the FK
      (MySQL creates one automatically)
- [ ] Junction tables: composite PRIMARY KEY (a_id, b_id) plus an index on (b_id)

## Columns and constraints
- [ ] Required data is NOT NULL; defaults only where a default is a real business value
- [ ] Small fixed value sets have CHECK constraints (or enum / lookup table)
- [ ] Ranges and invariants as CHECK (`quantity > 0`, `ends_at > starts_at`)
- [ ] PostgreSQL exclusion constraints for "no overlapping bookings" (`EXCLUDE USING gist`)
- [ ] Money in `numeric(p,s)` / `DECIMAL(p,s)` or integer minor units, with a currency column
- [ ] Times as `timestamptz` (PostgreSQL) / `DATETIME` in UTC or `TIMESTAMP` (MySQL, 2038 limit);
      dates as `date`
- [ ] Text: PostgreSQL `text` + CHECK length when needed; MySQL `VARCHAR(n)` sized to data,
      `utf8mb4` everywhere
- [ ] Booleans as `boolean` (PostgreSQL) / `TINYINT(1)`/`BOOLEAN` (MySQL), not 'Y'/'N' strings

## Normalization
- [ ] No repeating groups (`phone1`, `phone2`) - child table
- [ ] No delimited lists in a column (`tag_ids = '1,5,9'`) - junction table
- [ ] Each non-key column depends on the whole key (2NF) and nothing but the key (3NF);
      deliberate denormalisations (order line price snapshot, counters) are documented
- [ ] No EAV (entity, attribute, value) tables unless the attributes are truly user-defined;
      consider `jsonb` with a GIN index instead

## Naming and consistency
- [ ] lower_snake_case, no quoted identifiers, no reserved words (`user`, `order`, `group`)
- [ ] One convention for table names (plural or singular) and FK names (`customer_id`)
- [ ] Constraint and index names are explicit and predictable (`orders_customer_id_fkey`,
      `orders_status_created_at_idx`)
- [ ] Audit columns (`created_at`, `updated_at`) and tenant columns (`tenant_id`) are
      present and NOT NULL wherever the convention says so

## Engine specifics
- [ ] MySQL: InnoDB only (no MyISAM), `utf8mb4` + `utf8mb4_0900_ai_ci` (or a deliberate
      collation), `sql_mode` includes STRICT_TRANS_TABLES
- [ ] PostgreSQL: identity columns instead of `serial`; `REVOKE CREATE ON SCHEMA public FROM PUBLIC`
- [ ] Partitioning only for very large, time-based tables with retention needs
