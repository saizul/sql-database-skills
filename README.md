# sql-database-skills

[![CI](https://github.com/saizul/sql-database-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/sql-database-skills/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/saizul/sql-database-skills?sort=semver)](https://github.com/saizul/sql-database-skills/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/saizul/sql-database-skills?style=social)](https://github.com/saizul/sql-database-skills/stargazers)

**Agent Skills that turn Claude Code and Codex into a careful DBA for PostgreSQL and MySQL/MariaDB: find the slow query, read the plan, design the index, ship the migration without locking production.**

Eight skills for the database work every backend team does: reviewing queries, reading
`EXPLAIN`, choosing indexes, zero-downtime migrations, schema design, slow log triage,
backups that are proven by restore drills, and SQL security. Each skill is a step-by-step
workflow plus a tested, standard-library-only Python script, so the agent works from
deterministic analysis instead of guessing.

- **Never touches your database.** Every script reads text you give it: `.sql` files,
  `EXPLAIN` output, logs, schema dumps, grants. The workflows tell the agent to show any
  statement that changes data, schema or privileges and wait for your confirmation.
- **PostgreSQL and MySQL/MariaDB.** JSON and text plans, MySQL tree plans, `pg_dump` and
  `mysqldump` formats, pg_stat_statements and slow logs, `CONCURRENTLY` and
  `ALGORITHM=INSTANT`.
- **Tested.** 110+ tests with fixtures for every script, on Python 3.11-3.13 in CI.
- **No dependencies.** Python 3.8+ standard library. Copy a script anywhere and run it.

## Skills

| Skill | What it does | Try asking |
| --- | --- | --- |
| [sql-query-review](skills/sql-query-review/SKILL.md) | Lints SQL for `SELECT *`, functions on indexed columns, leading-wildcard LIKE, OR chains, `NOT IN` + NULL, `= NULL`, UPDATE/DELETE without WHERE, deep OFFSET, implicit casts, N+1 bursts in query logs; rewrites them | "Why doesn't this query use my index?" |
| [explain-plan-analysis](skills/explain-plan-analysis/SKILL.md) | Parses PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` JSON/text and MySQL `FORMAT=JSON` / `EXPLAIN ANALYZE`: self-time hotspots, seq scans, scans in nested loops, misestimates, disk sorts and hashes, filesorts, temp tables | "Here's the EXPLAIN ANALYZE output, what's slow?" |
| [index-advisor](skills/index-advisor/SKILL.md) | Suggests composite and covering indexes from queries + schema (equality, sort, range), detects indexes that already serve a query or should be extended, and duplicate/redundant ones | "Which index should I add for these three queries?" |
| [safe-migrations](skills/safe-migrations/SKILL.md) | Scans migrations for blocking locks and rewrites: index without `CONCURRENTLY`, NOT NULL without default, type changes, FKs without `NOT VALID`, missing lock_timeout, renames, drops; expand/contract and batched backfill patterns | "Review this migration before I run it on a 50M row table" |
| [schema-design-review](skills/schema-design-review/SKILL.md) | Reviews schema dumps: missing primary keys, int keys, unindexed FKs, FK type mismatches, FLOAT money, timestamp without time zone, utf8mb3, MyISAM, lists in columns, naming | "Review our schema before we launch" |
| [slow-query-log-triage](skills/slow-query-log-triage/SKILL.md) | Groups MySQL slow logs, PostgreSQL duration logs and pg_stat_statements by fingerprint; ranks by total time with p95, rows examined, lock time, temp spills, chatty N+1 queries | "Which queries should we optimise first?" |
| [backup-restore-drills](skills/backup-restore-drills/SKILL.md) | pg_dump/pg_restore and mysqldump recipes, PITR, plus a checker for truncated dumps, broken gzip, version mismatch, missing table data and source vs restored row counts | "Prove last night's backup actually restores" |
| [sql-security](skills/sql-security/SKILL.md) | Finds SQL injection in Python, JS/TS, PHP, Ruby, Go, Java and C#; reviews GRANTs and roles for least privilege; row-level security checks | "Audit our database permissions and look for SQL injection" |

## Install

### Claude Code: plugin marketplace (recommended)

```text
/plugin marketplace add saizul/sql-database-skills
/plugin install sql-database-skills@sql-database-skills
```

Or from your shell:

```bash
claude plugin marketplace add saizul/sql-database-skills
claude plugin install sql-database-skills@sql-database-skills
```

Plugin skills are namespaced, e.g. `/sql-database-skills:explain-plan-analysis`. Claude
also picks them up automatically when your request matches a skill's description. Update
later with `/plugin marketplace update sql-database-skills`.

### Claude Code: copy the skills

```bash
git clone https://github.com/saizul/sql-database-skills.git
# all projects (personal skills)
mkdir -p ~/.claude/skills && cp -r sql-database-skills/skills/* ~/.claude/skills/
# or just one project
mkdir -p .claude/skills && cp -r sql-database-skills/skills/safe-migrations .claude/skills/
```

Each release also has one zip per skill and an all-skills zip under
[Releases](https://github.com/saizul/sql-database-skills/releases).

### Codex CLI

Codex reads skills from `~/.agents/skills` (all repositories) and `.agents/skills` in a
repository (from the current directory up to the repository root):

```bash
git clone https://github.com/saizul/sql-database-skills.git
mkdir -p ~/.agents/skills && cp -r sql-database-skills/skills/* ~/.agents/skills/
# or per repository
mkdir -p .agents/skills && cp -r sql-database-skills/skills/index-advisor .agents/skills/
```

Codex also ships a `$skill-installer` skill that can install skills from a GitHub
repository; ask it to install from `saizul/sql-database-skills`. Skill locations and the
installer are described in the [Codex skills documentation](https://developers.openai.com/codex/skills);
check it if your Codex version behaves differently. [AGENTS.md](AGENTS.md) explains how
Codex and other agents should use these skills.

### Any other agent

The skills follow the open [Agent Skills format](https://agentskills.io/specification):
a folder with `SKILL.md` (YAML frontmatter + instructions), `scripts/` and `references/`.
Point your agent at the folder, or run the scripts yourself; every script has `--help`.

## Usage examples

```text
> Our API got slow after yesterday's deploy. pg_stat_statements export is in pgss.csv.
  (slow-query-log-triage ranks the shapes -> the top one examines 50k rows per row ->
   explain-plan-analysis on its plan -> index-advisor proposes (customer_id, status, created_at)
   -> safe-migrations wraps it in CREATE INDEX CONCURRENTLY with a lock timeout.)

> Review migrations/20261101_add_region.sql, orders has 40M rows.
  (Flags NOT NULL without default and a FK without NOT VALID, then writes a
   two-release plan with a batched backfill.)

> Is this pg_dump from last night complete? And check it against prod row counts.
  (dump_inspect.py checks the end marker and versions, then compares counts.)
```

Run the scripts directly too:

```bash
python3 skills/sql-query-review/scripts/sql_lint.py queries/ --dialect postgres
python3 skills/explain-plan-analysis/scripts/explain_analyze.py plan.json
python3 skills/index-advisor/scripts/index_advisor.py queries.sql --schema schema.sql
python3 skills/safe-migrations/scripts/migration_lint.py db/migrations/ --transaction yes
python3 skills/schema-design-review/scripts/schema_review.py schema.sql
python3 skills/slow-query-log-triage/scripts/slowlog_triage.py /var/log/mysql/mysql-slow.log --top 10
python3 skills/backup-restore-drills/scripts/dump_inspect.py /backups/shop.sql.gz --max-age-hours 26
python3 skills/sql-security/scripts/sqli_scan.py src/ && python3 skills/sql-security/scripts/grants_review.py grants.txt
```

The linters exit 1 on findings, so they also work as CI gates (`--fail-on high`, `--json`).

## How skills work

At startup the agent loads only each skill's `name` and `description` (about 100 tokens
each). When your request matches a description, it reads that skill's `SKILL.md`: the
workflow, decision points and safety rules. Longer material lives in `references/` and is
read only when a step needs it. Scripts run as normal commands, so their output is exact
and repeatable.

```
skills/<skill-name>/
  SKILL.md          frontmatter (name, description) + workflow
  scripts/          standalone Python 3 helpers (stdlib only, --help, --json)
  references/       checklists, patterns, copy-paste SQL
```

## FAQ

**Will the agent run statements against my database?**
The scripts cannot: none of them opens a database connection. The workflows ask the agent
to give you read-only queries to run (EXPLAIN, catalog queries, exports), and to show any
DDL, DML, GRANT or restore and wait for your explicit yes. Your agent's permission
settings still apply on top.

**Does anything leave my machine?**
No. No script makes network calls or sends telemetry. Slow logs and dumps can contain
personal data, so the triage output normalises literals (`?`) by default.

**Which versions are supported?**
PostgreSQL 12-18 and MySQL 8.0/8.4, MariaDB 10.6+. Version-specific advice (fast column
defaults in PostgreSQL 11, `SET NOT NULL` shortcut in 12, `INSTANT` columns in MySQL
8.0.29) is called out where it matters.

**What about SQLite, SQL Server, Oracle, or warehouses?**
The query review rules mostly apply, but plans, locks and DDL differ. They are on the
roadmap; contributions welcome.

**How accurate is the index advisor?**
It applies the same rules a DBA uses (equality, sort, range; join columns; covering) to the
SQL text. It does not know your data distribution, so the workflow always ends with
`EXPLAIN` before and after.

## Roadmap

Ideas for new skills and checks (great first contributions; see [CONTRIBUTING.md](CONTRIBUTING.md)):

- `replication-health-check` - parse `pg_stat_replication` / `SHOW REPLICA STATUS` for lag, slots, errors
- `postgres-vacuum-bloat` - autovacuum settings, dead tuples, wraparound risk from catalog exports
- `connection-pooling` - PgBouncer / ProxySQL sizing from `max_connections` and app pool settings
- `mysql-config-review` - `SHOW VARIABLES` review (buffer pool, redo log, binlog, sql_mode)
- `partitioning-planner` - range/list partition plans and retention for big time-series tables
- `sqlite-review` - WAL mode, `PRAGMA` settings and query review for SQLite
- `explain-plan-analysis`: plan diff mode (`--compare before.json after.json`)
- `safe-migrations`: generate the expand/contract steps for a rename automatically

## Contributing

Contributions are welcome: new checks, new skills, fixes, better docs and anonymised
fixtures from real-world plans and logs. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the
skill template, the quality bar and how to run the validator and tests:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python tools/validate_skills.py && ruff check . && ruff format --check . && pytest
```

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md) and report security issues
privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) (c) Saizul Amin
