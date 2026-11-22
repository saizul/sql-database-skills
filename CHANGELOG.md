# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-22

### Added

- `sql-query-review`: SQL linter for PostgreSQL and MySQL/MariaDB (SELECT *, functions and
  casts on columns, leading-wildcard LIKE, OR chains, optional filters, NOT IN subqueries,
  = NULL, DML without WHERE, ORDER BY RAND(), large OFFSET, implicit casts with optional
  schema types, comma joins, INSERT without columns, UNION, large IN lists, N+1 detection
  in query logs) with inline suppressions.
- `explain-plan-analysis`: analyser for PostgreSQL JSON and text plans, MySQL FORMAT=JSON,
  MariaDB ANALYZE FORMAT=JSON and MySQL EXPLAIN ANALYZE trees: self-time hotspots, scans,
  scans in loops, root-cause misestimates, disk sorts/hashes, filesorts, temporary tables,
  low filtered ratios, lossy bitmaps, heap fetches, JIT.
- `index-advisor`: composite / covering index suggestions (equality, sort, range), join
  columns, existing index detection (served / extend), duplicate and redundant indexes,
  PostgreSQL and MySQL DDL output.
- `safe-migrations`: migration linter for locking and rewriting operations, CONCURRENTLY
  in transactions, NOT VALID constraints, lock timeouts, destructive changes and backfills;
  zero-downtime patterns, expand/contract and monitoring references.
- `schema-design-review`: schema dump reviewer for keys, types, normalization and naming.
- `slow-query-log-triage`: MySQL slow log, PostgreSQL duration log and pg_stat_statements
  summariser with fingerprints, p95, hints and time filters.
- `backup-restore-drills`: dump checker (completeness, gzip integrity, versions, table data,
  restore traps, expected counts) and restore drill row-count comparison; dump/restore,
  PITR and drill references.
- `sql-security`: SQL injection scanner for seven languages and a MySQL/PostgreSQL grants
  and roles reviewer with row-level security checks.
- Claude Code plugin marketplace manifest, `tools/validate_skills.py`, CI and release workflows.

[Unreleased]: https://github.com/saizul/sql-database-skills/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/sql-database-skills/releases/tag/v0.1.0
