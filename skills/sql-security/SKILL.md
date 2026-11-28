---
name: sql-security
description: Secure PostgreSQL and MySQL/MariaDB access end to end - scan application code (Python, JS/TS, PHP, Ruby, Go, Java, C#) for SQL injection via string interpolation and concatenation (with a baseline so CI fails only on new findings), review GRANTs, roles and users for least privilege (ALL ON *.*, SUPER/FILE, accounts on '%', superuser app roles, GRANT TO PUBLIC, login roles that own tables), check row-level security policies, and design separate migration, app and read-only roles. Use when the user asks for a SQL injection review, a database permissions or security audit, least-privilege roles, row-level security for multi-tenant data, or shares SHOW GRANTS output, pg_dumpall --roles-only, or raw SQL built in code.
license: MIT
compatibility: Python 3.8+ standard library. Reads source files and privilege dumps; never connects to a database.
metadata:
  author: Saizul Amin
  version: "0.1.0"
---

# SQL security

Two jobs: make sure user input can never become SQL, and make sure each database account
can do only what it needs. Findings are reported with masked secrets; no change is made
to users, grants or code without the user's confirmation.

Paths below are relative to this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`).

## When to use

- "Check my code for SQL injection", a pull request that adds raw SQL
- "Audit database permissions", "create a read-only user", "least privilege for the app"
- Multi-tenant data isolation with PostgreSQL row-level security
- Hardening before a pen test or compliance audit (SOC 2, ISO 27001, PCI)

## When not to use

- Network exposure, TLS and firewall of the database host: server hardening, not SQL
- Query performance: `sql-query-review`
- Secrets management in general (vaults, rotation): mention it, but it is out of scope here

## Workflow

1. **Scan the code for injection.**
   ```bash
   python3 scripts/sqli_scan.py src/
   python3 scripts/sqli_scan.py app/ lib/ --json --fail-on high     # CI
   python3 scripts/sqli_scan.py src/ --write-baseline sqli-baseline.json  # legacy code: record once
   python3 scripts/sqli_scan.py src/ --baseline sqli-baseline.json        # CI: fail on new findings only
   ```
   A baseline matches on file path, check and the code of the line (not the line
   number); run it from the same directory with the same paths. It is a way to stop new
   injections while the old ones are fixed, not an accepted-risk list: keep it shrinking.
   High = interpolated SQL that reaches an execute/query call or uses request input;
   medium = interpolated SQL elsewhere. The scanner is line based: also search manually
   for query builders fed with raw fragments (`whereRaw`, `orderByRaw`, `.extra()`,
   `Arel.sql`, `sequelize.literal`) and for dynamic identifiers. Accepted lines get a
   `sqli-scan: ignore` comment with the reason (for example an allow-listed column name).
2. **Fix with parameters, not escaping.** Values become bind parameters; identifiers
   (column, table, sort direction) go through an allow-list. Examples per language and
   ORM are in [references/injection.md](references/injection.md). Show the diff and let the
   user apply it.
3. **Collect privileges** (read-only; the user runs these):
   ```bash
   # MySQL / MariaDB: every account's grants
   mysql -N -e "SELECT CONCAT('SHOW GRANTS FOR ''', user, '''@''', host, ''';') FROM mysql.user" | mysql -N > grants.txt
   # PostgreSQL: roles and object privileges (no passwords)
   pg_dumpall --roles-only --no-role-passwords > roles.sql
   pg_dump --schema-only --no-comments shop > schema.sql && cat roles.sql schema.sql > pg_privs.sql
   ```
4. **Review them.**
   ```bash
   python3 scripts/grants_review.py grants.txt
   python3 scripts/grants_review.py pg_privs.sql --json
   ```
   Then compare with the target role model in
   [references/least-privilege.md](references/least-privilege.md): an owner/migration role
   (NOLOGIN in PostgreSQL), an app role with DML only, a read-only role for reporting,
   named personal accounts for humans, no shared superuser.
5. **Row-level security** (multi-tenant PostgreSQL): verify every tenant table has RLS
   enabled and FORCEd, a policy on `tenant_id`, and that the app does not connect as the
   table owner or a BYPASSRLS role. Patterns and tests are in
   [references/row-level-security.md](references/row-level-security.md).
6. **Propose changes as SQL** (`REVOKE`, `ALTER ROLE ... NOSUPERUSER`, new roles) in a
   rollout order that does not break the running app: create new roles and grants first,
   switch the app's credentials, then revoke. The user applies them.

## Output

```
SQL security review
Injection (sqli_scan.py, 214 files): 3 high, 2 medium
  HIGH app/views.py:41  f-string into cursor.execute -> cur.execute("... WHERE name = %s", [name])
  HIGH web/orders.ts:12 template literal into pool.query -> pool.query('... = $1', [id])
Privileges (grants_review.py, PostgreSQL):
  HIGH   role app is SUPERUSER
  MEDIUM login role app owns 31 tables (bypasses RLS, can DROP)
  MEDIUM invoices has RLS but is not FORCEd
Plan: 1) create app_owner NOLOGIN, transfer ownership  2) app gets DML grants only
      3) ALTER TABLE invoices FORCE ROW LEVEL SECURITY  4) ALTER ROLE app NOSUPERUSER
```
