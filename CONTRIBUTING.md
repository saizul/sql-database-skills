# Contributing to sql-database-skills

Thank you for helping! New checks, new skills, fixes, anonymised fixtures from real-world
plans, logs and schemas, and clearer docs are all welcome.

- [Ways to contribute](#ways-to-contribute)
- [Good first issues](#good-first-issues)
- [Development setup](#development-setup)
- [Repository layout](#repository-layout)
- [Write a new skill](#write-a-new-skill)
- [Quality bar](#quality-bar)
- [Pull request process](#pull-request-process)

## Ways to contribute

| Size | Idea |
| --- | --- |
| 10 minutes | Fix a typo, clarify a workflow step, add a FAQ entry |
| 30 minutes | Add a missing test case or a fixture that triggers a false positive |
| 1-2 hours | Add a check to an existing script (see below) |
| Half a day | Write a new skill from the template |

Comment on an issue before you start so nobody else picks the same one.

## Good first issues

Each of these is self-contained and touches one script and its test file.

1. **sql-query-review: `SELECT DISTINCT` over joins.** Report `low` when `DISTINCT` is used
   in a query with a JOIN (often hides row multiplication); add good/bad fixtures.
2. **sql-query-review: `COUNT(*)` for existence.** Detect `SELECT COUNT(*) ... ` compared with
   `> 0` in the same statement (`CASE WHEN (SELECT count(*) ...) > 0`) and suggest `EXISTS`.
3. **explain-plan-analysis: plan diff.** Add `--compare before.json after.json` that prints
   execution time, buffers and node type changes side by side.
4. **explain-plan-analysis: `Memoize` and `Incremental Sort`.** Report low Memoize hit rates
   (`Hits` vs `Misses`) from PostgreSQL 14+ plans.
5. **index-advisor: MySQL covering columns.** With `--dialect mysql`, drop primary key columns
   from covering variants (InnoDB secondary indexes already contain the PK).
6. **index-advisor: partial index hint.** When a query filters `status = 'pending'` (a literal on
   a low-cardinality column), propose a PostgreSQL partial index as an alternative.
7. **safe-migrations: `ALTER TYPE ... ADD VALUE`.** Report `info` that it cannot run in a
   transaction before PostgreSQL 12 and that new values are unusable until commit.
8. **safe-migrations: multiple ALTERs on one MySQL table.** Report `info` when a file has several
   `ALTER TABLE` statements on the same table (each may rebuild it; combine them).
9. **schema-design-review: missing `ON DELETE`.** Report `info` for foreign keys without an
   explicit `ON DELETE` clause, so the choice is deliberate.
10. **slow-query-log-triage: pg_stat_statements snapshots.** Accept two CSVs (`--baseline old.csv`)
    and report the difference, so a time window can be analysed.
11. **backup-restore-drills: directory-format dumps.** Detect a `pg_dump -Fd` directory
    (`toc.dat`) and point to `pg_restore --list`.
12. **sql-security: Python `executescript` and `cursor.mogrify`.** Add tests and patterns for
    sqlite3 `executescript` with interpolation and misuse of `mogrify` output.
13. **New skill: `replication-health-check`** (see the roadmap in the README).

## Development setup

```bash
git clone https://github.com/saizul/sql-database-skills.git
cd sql-database-skills
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

python tools/validate_skills.py   # SKILL.md frontmatter, sections, links, marketplace listing
ruff check . && ruff format --check .
pytest
```

No database is needed: every script works on text fixtures. To try a skill against a
real database, use a disposable local container, never production.

To try your changes in Claude Code, either copy a skill to `~/.claude/skills/` or add your
checkout as a local marketplace:

```bash
claude plugin marketplace add ./sql-database-skills
claude plugin install sql-database-skills@sql-database-skills
```

For Codex, copy the skill folder to `~/.agents/skills/`.

## Repository layout

```
.claude-plugin/
  marketplace.json      marketplace + plugin entry listing every skill
  plugin.json           plugin manifest (name, version, author)
skills/<name>/
  SKILL.md              frontmatter + workflow (the part agents read)
  scripts/*.py          standalone helpers, Python 3.8+ stdlib only
  references/*.md       detail loaded on demand
templates/skill/        copy this to start a new skill
tests/
  test_<skill>.py       one test module per skill
  fixtures/<skill>/     inputs for those tests
tools/validate_skills.py
```

## Write a new skill

1. Open a "New skill proposal" issue first so we can agree on scope.
2. Copy the template:
   ```bash
   cp -r templates/skill skills/my-new-skill
   mv skills/my-new-skill/scripts/your_script.py skills/my-new-skill/scripts/my_check.py
   ```
3. Edit `SKILL.md`:
   - `name` equals the folder name: lowercase letters, digits, single hyphens, max 64 chars.
   - `description` (max 1024 chars) says **what** it does with concrete keywords and **when**
     to use it, starting the second part with "Use when ...". This text decides whether
     the agent picks the skill, so list the phrases users actually type.
   - Required sections: `## When to use`, `## When not to use`, `## Workflow`, `## Output`.
   - Workflow steps are imperative and numbered, with exact commands, decision points and
     the point where the agent must ask before changing anything.
   - Keep it under 400 lines; move tables, runbooks and long configs to `references/`.
4. Write the script: `argparse` with a useful `--help`, `--json` output, exit codes
   0 (ok) / 1 (findings) / 2 (error), read-only, no network and no database connections.
5. Add `tests/test_my_new_skill.py` and fixtures in `tests/fixtures/my-new-skill/`. Cover
   a "bad" input, a "good" input that produces no findings, edge cases and the CLI exit codes.
6. Add the skill to `.claude-plugin/marketplace.json` (`plugins[0].skills`), the README
   skills table, the table in `AGENTS.md` and `CHANGELOG.md` under "Unreleased".
7. Run the validator, ruff and pytest.

## Quality bar

- **Safety first.** Scripts never connect to a database. A skill must never tell the agent
  to run DDL, DML, GRANT/REVOKE, restores or destructive maintenance without showing the
  statement and getting confirmation. Locking behaviour must be stated correctly per engine
  and version.
- **Deterministic where it matters.** If a step is "parse this and find X", write a script
  and test it rather than relying on the model reading a 5,000-line plan or log.
- **Anonymised fixtures.** No real customer data, hostnames, IPs, password hashes or
  credentials. Use `example.com`, documentation IP ranges (192.0.2.0/24, 198.51.100.0/24,
  203.0.113.0/24) and placeholders like `redacted`.
- **Low false positives.** If a check is often wrong, make it `info` or drop it, and
  document known false positives in the skill's references.
- **Both engines.** PostgreSQL and MySQL/MariaDB behave differently (locks, DDL, plans);
  say which one a rule applies to and note version differences.

## Pull request process

1. Fork, create a branch, make focused commits.
2. Make sure CI passes locally (validator, ruff, pytest).
3. Fill in the pull request template.
4. A maintainer reviews within a few days. Small, focused pull requests are merged fastest.

By contributing you agree that your contributions are licensed under the MIT License.
