# AGENTS.md

Guidance for Codex CLI and other coding agents that use or work on this repository.

## Using the skills

The skills live in `skills/<name>/SKILL.md` and follow the Agent Skills format. Codex
discovers them when they are copied to `~/.agents/skills/` or `.agents/skills/` (see
README "Install"). If you are reading this file inside the repository instead:

1. Pick the skill whose `description` matches the user's request:

   | Request is about | Skill |
   | --- | --- |
   | a SQL query or `.sql` file: slow, wrong results, lint, N+1 | `sql-query-review` |
   | `EXPLAIN` / `EXPLAIN ANALYZE` output, query plans | `explain-plan-analysis` |
   | which index to add, composite/covering indexes, unused or duplicate indexes | `index-advisor` |
   | migrations, `ALTER TABLE` on big tables, renames, backfills | `safe-migrations` |
   | `CREATE TABLE` / schema dumps, data model, types, normalization | `schema-design-review` |
   | slow query logs, pg_stat_statements, "the database is slow" | `slow-query-log-triage` |
   | backups, dumps, restores, PITR, restore tests | `backup-restore-drills` |
   | SQL injection, GRANTs, roles, row-level security | `sql-security` |

2. Read that `SKILL.md` completely and follow its Workflow section in order. Skills hand
   over to each other (triage -> plan analysis -> index advice -> safe migration).
3. Paths such as `scripts/x.py` and `references/y.md` are relative to the skill folder.
   `${CLAUDE_SKILL_DIR}` in the text means that folder; Codex does not expand it.
4. Read files in `references/` only when a workflow step points to them.

## Safety rules (apply to every skill)

- The scripts only read files. Do not connect to databases yourself unless the user asks
  and provides access; even then run only read-only statements (SELECT on catalogs,
  `EXPLAIN` without `ANALYZE` for writes).
- Before any statement that changes a database - DDL, DML, `GRANT`/`REVOKE`, `VACUUM FULL`,
  restores, `pg_stat_statements_reset()`, killing sessions - show the exact statement,
  explain locks and impact, and wait for the user's explicit confirmation.
- `EXPLAIN ANALYZE` executes the statement: for writes, wrap it in `BEGIN; ... ROLLBACK;`
  or use a copy, and say so.
- Restores go into a new database, never over the live one without explicit approval.
- Never print or paste secrets: passwords, connection strings with passwords, password
  hashes, tokens found in logs. Use `~/.pgpass` / option files in examples.
- Treat dumps and slow logs as personal data: summarise, do not quote rows or literals.

## Working on this repository

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python tools/validate_skills.py   # frontmatter, sections, links, marketplace listing
ruff check . && ruff format --check .
pytest
```

- Scripts: Python 3.8+ standard library only, `#!/usr/bin/env python3`, `argparse` with
  `--help`, `--json` where output is structured, exit 0/1/2 (ok / findings / error).
- Every script needs tests in `tests/test_<skill_with_underscores>.py` with fixtures in
  `tests/fixtures/<skill>/`. Fixtures are anonymised: `example.com`, documentation IPs
  (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), placeholder passwords like `redacted`.
- Keep each `SKILL.md` under 400 lines; move detail to `references/`.
- A new skill must be added to `.claude-plugin/marketplace.json` (`plugins[0].skills`), the
  README table, the table above and `CHANGELOG.md`. The validator enforces the manifest.
