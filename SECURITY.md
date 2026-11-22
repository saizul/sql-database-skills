# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## What counts as a vulnerability

These skills guide AI agents working on real databases, so unsafe guidance is treated like
a code vulnerability:

- A script that connects to a database, executes SQL, writes outside stdout, or makes a
  network call
- A script that prints a secret: passwords, password hashes, connection strings with
  credentials, or literal values from slow logs when `--samples` was not requested
- A `SKILL.md` workflow that tells the agent to run DDL, DML, GRANT/REVOKE, a restore or a
  destructive maintenance command without asking the user first
- Advice that silently causes data loss or long locks (for example a "safe" migration
  pattern that rewrites a table under an exclusive lock)
- Crafted input files (SQL, plans, logs, dumps, source code) that make a script execute
  something or hang indefinitely

A check that misses a problem (false negative) or reports a non-problem is a normal bug:
please open an issue.

## Reporting

Please **do not** open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/saizul/sql-database-skills/security/advisories/new)
with the affected skill, version, impact and steps to reproduce. Remove credentials,
hostnames and personal data from examples.

You will get an answer within 5 working days. Fixes are released as soon as possible and
credited in the changelog unless you prefer otherwise.
