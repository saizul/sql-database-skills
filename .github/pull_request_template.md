## What does this pull request do?

<!-- One or two sentences. Link the issue: "Closes #123". -->

## Type

- [ ] New skill
- [ ] New check or option in an existing script
- [ ] Workflow / reference improvement
- [ ] Bug fix
- [ ] Repository tooling (validator, CI)

## Checklist

- [ ] `python tools/validate_skills.py` passes
- [ ] `ruff check . && ruff format --check .` passes
- [ ] `pytest` passes; new script behaviour has tests with fixtures in `tests/fixtures/<skill>/`
- [ ] Scripts use only the Python standard library (or POSIX shell), support `--help`, have a
      `--json` option where output is structured, and exit non-zero on findings
- [ ] Read-only by default; any statement that changes a database (DDL, DML, GRANT, restore) is
      shown to the user and needs confirmation in the SKILL.md workflow
- [ ] No secrets, password hashes, real customer data, hostnames or IPs in fixtures (use
      example.com and 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24)
- [ ] New skills: listed in `.claude-plugin/marketplace.json`, the README skills table and CHANGELOG.md
