#!/usr/bin/env python3
"""Scan SQL migrations for operations that lock tables, rewrite them or break running code.

PostgreSQL checks: CREATE/DROP INDEX without CONCURRENTLY, CONCURRENTLY inside a
transaction, ADD COLUMN with a volatile default or NOT NULL without a default, column
type changes, SET NOT NULL, foreign keys / CHECK constraints without NOT VALID, UNIQUE
or PRIMARY KEY constraints built under lock, REINDEX / REFRESH MATERIALIZED VIEW without
CONCURRENTLY, VACUUM FULL, CLUSTER, LOCK TABLE.

MySQL / MariaDB checks: column type changes (MODIFY / CHANGE), CONVERT TO CHARACTER SET,
primary key changes, FULLTEXT / SPATIAL indexes, OPTIMIZE TABLE, ALTER without an
explicit ALGORITHM / LOCK clause.

Both: missing lock timeout, RENAME of tables/columns, DROP COLUMN / DROP TABLE /
TRUNCATE, data backfills (UPDATE / DELETE) inside the schema migration.

Operations on tables created earlier in the same file are not reported (they are
empty). Suppress a finding with a comment in the statement:
  -- migration-lint: ignore drop_column

The script only reads files. Exit codes: 0 = no findings at or above --fail-on,
1 = findings, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass

SEVERITIES = ["info", "low", "medium", "high"]
VOLATILE = {
    "CLOCK_TIMESTAMP",
    "RANDOM",
    "GEN_RANDOM_UUID",
    "UUID_GENERATE_V4",
    "TIMEOFDAY",
    "NEXTVAL",
    "UUID",
}


@dataclass
class Tok:
    kind: str
    value: str
    line: int

    @property
    def up(self) -> str:
        return self.value.upper() if self.kind == "word" else self.value


@dataclass
class Finding:
    check: str
    severity: str
    title: str
    file: str
    line: int
    fix: str = ""


TOKEN_RE = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z)|(?<![^\n])\#[^\n]*)"
    r"|(?P<string>[EeNn]?'(?:[^'\\]|''|\\.)*')"
    r"|(?P<dollar>\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)?\$.*?\$(?P=tag)\$)"
    r"|(?P<qident>\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`)"
    r"|(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<op>::|<>|!=|<=|>=|[-+*/%=<>(),;.\[\]~!@&|^:#?$])"
    r"|(?P<other>.)",
    re.S,
)


def tokenize(text: str) -> list[Tok]:
    out, line = [], 1
    for m in TOKEN_RE.finditer(text):
        kind, value = m.lastgroup, m.group(0)
        if kind == "dollar":
            kind = "string"
        if kind != "ws":
            if kind == "qident":
                kind, value = "word", value[1:-1]
            out.append(Tok("op" if kind == "other" else kind, value, line))
        line += value.count("\n")
    return out


def split_statements(tokens: list[Tok]) -> list[list[Tok]]:
    stmts, cur = [], []
    for t in tokens:
        if t.kind == "op" and t.value == ";":
            if cur:
                stmts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts


def detect_dialect(text: str) -> str:
    if re.search(r"`|\bENGINE\s*=|\bAUTO_INCREMENT\b|\bMODIFY\s+(COLUMN\s+)?\w|\bALGORITHM\s*=", text, re.I):
        return "mysql"
    return "postgres"


def table_after(code: list[Tok], i: int) -> tuple[str, int]:
    ups = [t.up for t in code]
    while i < len(code) and ups[i] in ("ONLY", "IF", "EXISTS", "NOT", "TABLE"):
        i += 1
    if i >= len(code):
        return "", i
    name = code[i].value
    i += 1
    while i + 1 < len(code) and code[i].value == ".":
        name = code[i + 1].value
        i += 2
    return name.lower(), i


def split_actions(code: list[Tok], start: int) -> list[list[Tok]]:
    actions, cur, depth = [], [], 0
    for t in code[start:]:
        if t.value == "(":
            depth += 1
        elif t.value == ")":
            depth -= 1
        if depth == 0 and t.value == ",":
            actions.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        actions.append(cur)
    return actions


class MigrationLinter:
    def __init__(self, file: str, dialect: str, transaction: str, ignore: set[str]):
        self.file = file
        self.dialect = dialect
        self.in_tx = transaction == "yes"
        self.created: set[str] = set()
        self.lock_timeout = False
        self.ignore = ignore
        self.findings: list[Finding] = []
        self.lock_warned = False
        self._stmt_ignore: set[str] | None = set()

    def add(self, check: str, severity: str, tok: Tok, title: str, fix: str) -> None:
        if check in self.ignore:
            return
        if self._stmt_ignore is None or check in self._stmt_ignore:
            return
        self.findings.append(Finding(check, severity, title, self.file, tok.line, fix))

    def need_lock_timeout(self, tok: Tok, table: str) -> None:
        if self.lock_timeout or self.lock_warned or table in self.created:
            return
        self.lock_warned = True
        fix = (
            "SET lock_timeout = '5s'; at the top (and retry on failure) so a blocked ALTER "
            "does not queue every query behind it"
            if self.dialect == "postgres"
            else "SET SESSION lock_wait_timeout = 5; at the top so a blocked ALTER fails fast"
        )
        self.add("missing_lock_timeout", "medium", tok, f"no lock timeout set before locking {table}", fix)

    def lint(self, text: str) -> list[Finding]:
        for stmt in split_statements(tokenize(text)):
            comments = [t for t in stmt if t.kind == "comment"]
            code = [t for t in stmt if t.kind != "comment"]
            if not code:
                continue
            self._stmt_ignore = set()
            for c in comments:
                m = re.search(r"migration-lint:\s*ignore\b([\w\s,-]*)", c.value)
                if m:
                    names = {x for x in re.split(r"[\s,]+", m.group(1)) if x}
                    self._stmt_ignore = (self._stmt_ignore | names) if names else None
                    if self._stmt_ignore is None:
                        break
            self.statement(code)
        return self.findings

    def statement(self, code: list[Tok]) -> None:
        ups = [t.up for t in code]
        joined = " ".join(ups)
        first = code[0]
        if ups[0] in ("BEGIN", "START") and (len(ups) == 1 or ups[1] in ("TRANSACTION", "WORK", "ISOLATION")):
            self.in_tx = True
            return
        if ups[0] in ("COMMIT", "END", "ROLLBACK"):
            self.in_tx = False
            return
        if ups[0] == "SET":
            if re.search(r"\b(LOCK_TIMEOUT|LOCK_WAIT_TIMEOUT|INNODB_LOCK_WAIT_TIMEOUT)\b", joined):
                self.lock_timeout = True
            return
        if ups[0] == "CREATE" and "TABLE" in ups[1:5] and "AS" not in ups[:8]:
            name, _ = table_after(code, ups.index("TABLE") + 1)
            self.created.add(name)
            return
        if ups[0] == "CREATE" and "INDEX" in ups[1:4]:
            self.create_index(code, ups)
        elif ups[0] == "DROP" and ups[1:2] == ["INDEX"]:
            if self.dialect == "postgres":
                if "CONCURRENTLY" not in ups:
                    self.add(
                        "drop_index_not_concurrently",
                        "medium",
                        first,
                        "DROP INDEX takes an ACCESS EXCLUSIVE lock on the table",
                        "DROP INDEX CONCURRENTLY (outside a transaction)",
                    )
                elif self.in_tx:
                    self.concurrently_tx(first)
        elif ups[0] == "DROP" and ups[1:2] == ["TABLE"]:
            name, _ = table_after(code, 2)
            if name not in self.created:
                self.add(
                    "drop_table",
                    "high",
                    first,
                    f"DROP TABLE {name}: irreversible, and running code that still reads it will fail",
                    "deploy code that no longer uses the table first; take a backup; "
                    "consider renaming it and dropping in a later release",
                )
        elif ups[0] == "TRUNCATE":
            self.add(
                "truncate",
                "high",
                first,
                "TRUNCATE deletes all rows and takes an ACCESS EXCLUSIVE / metadata lock",
                "make sure this is intended; back up first; never in a routine migration",
            )
        elif ups[0] == "ALTER" and ups[1:2] == ["TABLE"]:
            self.alter_table(code, ups)
        elif ups[0] == "RENAME" and ups[1:2] == ["TABLE"]:
            self.rename(first, "table")
        elif ups[0] in ("UPDATE", "DELETE"):
            has_where = "WHERE" in ups
            self.add(
                "backfill_in_migration",
                "medium" if has_where else "high",
                first,
                f"{ups[0]} {'with' if has_where else 'without'} WHERE inside a schema migration: "
                "one long transaction holding row locks (and replica lag)",
                "run backfills separately in batches of 1-10k rows by primary key range, committing "
                "each batch (see references/patterns.md)",
            )
        elif ups[0] == "VACUUM" and "FULL" in ups[:3]:
            self.add(
                "vacuum_full",
                "high",
                first,
                "VACUUM FULL rewrites the table under ACCESS EXCLUSIVE",
                "use pg_repack or pg_squeeze for online compaction",
            )
        elif ups[0] == "CLUSTER":
            self.add("cluster", "high", first, "CLUSTER rewrites the table under ACCESS EXCLUSIVE", "use pg_repack")
        elif ups[0] == "REINDEX" and "CONCURRENTLY" not in ups:
            self.add(
                "reindex_not_concurrently",
                "medium",
                first,
                "REINDEX blocks writes (and reads using the index)",
                "REINDEX ... CONCURRENTLY (PostgreSQL 12+)",
            )
        elif ups[0] == "REFRESH" and "CONCURRENTLY" not in ups:
            self.add(
                "refresh_matview",
                "low",
                first,
                "REFRESH MATERIALIZED VIEW blocks reads of the view",
                "REFRESH MATERIALIZED VIEW CONCURRENTLY (needs a unique index on the view)",
            )
        elif ups[0] == "LOCK":
            self.add(
                "lock_table",
                "medium",
                first,
                "explicit LOCK TABLE in a migration",
                "keep the locked section tiny and set lock_timeout first",
            )
        elif ups[0] == "OPTIMIZE":
            self.add(
                "optimize_table",
                "medium",
                first,
                "OPTIMIZE TABLE rebuilds the table (heavy I/O, replica lag)",
                "run off-peak or use pt-online-schema-change / gh-ost",
            )

    def concurrently_tx(self, tok: Tok) -> None:
        self.add(
            "concurrently_in_transaction",
            "high",
            tok,
            "CONCURRENTLY cannot run inside a transaction block (the migration will fail)",
            "run it in its own non-transactional migration (Rails: disable_ddl_transaction!, "
            "Django: atomic = False, Flyway: separate script, Alembic: autocommit_block())",
        )

    def create_index(self, code: list[Tok], ups: list[str]) -> None:
        on = ups.index("ON") if "ON" in ups else None
        if on is None:
            return
        table, _ = table_after(code, on + 1)
        first = code[0]
        if self.dialect == "postgres":
            if "CONCURRENTLY" in ups:
                if self.in_tx:
                    self.concurrently_tx(first)
                return
            if table in self.created:
                return
            self.add(
                "create_index_not_concurrently",
                "high",
                first,
                f"CREATE INDEX on {table} blocks all writes until the index is built",
                "CREATE INDEX CONCURRENTLY ... (outside a transaction); if it fails, "
                "DROP INDEX CONCURRENTLY the INVALID index and retry",
            )
            self.need_lock_timeout(first, table)
        else:
            if table in self.created:
                return
            if "FULLTEXT" in ups[:3] or "SPATIAL" in ups[:3]:
                self.add(
                    "mysql_fulltext_index",
                    "medium",
                    first,
                    f"{ups[1]} index on {table}: the first one rebuilds the table and cannot use LOCK=NONE",
                    "build it off-peak or with gh-ost / pt-online-schema-change",
                )
            elif "ALGORITHM" not in ups:
                self.mysql_no_algorithm(first, table)

    def mysql_no_algorithm(self, tok: Tok, table: str) -> None:
        self.add(
            "mysql_no_algorithm",
            "low",
            tok,
            f"ALTER on {table} without ALGORITHM/LOCK: MySQL silently falls back to a blocking table copy",
            "add ALGORITHM=INSTANT (or ALGORITHM=INPLACE, LOCK=NONE) so the statement fails instead of copying",
        )

    def rename(self, tok: Tok, what: str) -> None:
        self.add(
            f"rename_{what}",
            "high",
            tok,
            f"renaming a {what} breaks every running app instance that uses the old name",
            "expand/contract: add the new name, write both, backfill, switch reads, drop the old one later",
        )

    def alter_table(self, code: list[Tok], ups: list[str]) -> None:
        table, i = table_after(code, 2)
        first = code[0]
        new_table = table in self.created
        actions = split_actions(code, i)
        locking = False
        has_algorithm = "ALGORITHM" in ups
        mysql_needs_algorithm = False
        for act in actions:
            a = [t.up for t in act]
            if not a:
                continue
            tok = act[0]
            s = " ".join(a)
            locking = True
            if a[0] == "ADD" and (
                a[1:2] == ["COLUMN"]
                or (
                    len(a) > 1
                    and a[1]
                    not in (
                        "CONSTRAINT",
                        "PRIMARY",
                        "UNIQUE",
                        "INDEX",
                        "KEY",
                        "FOREIGN",
                        "CHECK",
                        "FULLTEXT",
                        "SPATIAL",
                    )
                )
            ):
                if new_table:
                    continue
                default_i = a.index("DEFAULT") if "DEFAULT" in a else None
                if default_i is not None and a[default_i + 1] in VOLATILE and self.dialect == "postgres":
                    self.add(
                        "add_column_volatile_default",
                        "high",
                        tok,
                        f"ADD COLUMN with volatile DEFAULT {a[default_i + 1].lower()}() rewrites {table} "
                        "under ACCESS EXCLUSIVE",
                        "add the column without a default (or with a constant), backfill in batches, then SET DEFAULT",
                    )
                if "NOT NULL" in s and default_i is None and self.dialect == "postgres" and "GENERATED" not in a:
                    self.add(
                        "add_column_not_null_no_default",
                        "high",
                        tok,
                        f"ADD COLUMN ... NOT NULL without DEFAULT fails if {table} has rows",
                        "add it nullable (or with a constant DEFAULT), backfill, then add NOT NULL "
                        "via a NOT VALID check constraint",
                    )
                if self.dialect == "mysql":
                    mysql_needs_algorithm = True
                    if "AFTER" in a or "FIRST" in a:
                        self.add(
                            "mysql_column_position",
                            "low",
                            tok,
                            "ADD COLUMN ... AFTER/FIRST is INSTANT only on MySQL 8.0.29+ (older: table rebuild)",
                            "add the column at the end, or confirm the server version",
                        )
            elif a[0] == "ALTER" and ("TYPE" in a or "SET DATA TYPE" in s):
                if not new_table:
                    self.add(
                        "alter_column_type",
                        "high",
                        tok,
                        f"changing a column type on {table} rewrites the table and its indexes under ACCESS EXCLUSIVE",
                        "add a new column, dual-write, backfill in batches, swap; (varchar(n) -> larger n "
                        "or text is metadata-only)",
                    )
            elif a[0] == "ALTER" and "SET NOT NULL" in s:
                if not new_table:
                    self.add(
                        "set_not_null",
                        "medium",
                        tok,
                        f"SET NOT NULL scans all of {table} while holding ACCESS EXCLUSIVE",
                        "ADD CONSTRAINT c CHECK (col IS NOT NULL) NOT VALID; VALIDATE CONSTRAINT c; "
                        "then SET NOT NULL (PostgreSQL 12+ skips the scan); DROP the check",
                    )
            elif a[0] in ("MODIFY", "CHANGE"):
                if not new_table:
                    self.add(
                        "mysql_modify_column",
                        "high",
                        tok,
                        f"{a[0]} COLUMN on {table} usually copies the whole table (writes blocked on big tables)",
                        "use gh-ost or pt-online-schema-change, or ALGORITHM=INPLACE/INSTANT when the change allows it",
                    )
                    if a[0] == "CHANGE":
                        self.rename(tok, "column")
            elif a[0] == "CONVERT" and "CHARACTER" in a:
                self.add(
                    "mysql_convert_charset",
                    "high",
                    tok,
                    f"CONVERT TO CHARACTER SET rebuilds {table} and every text index",
                    "use gh-ost / pt-online-schema-change; check index length limits for utf8mb4",
                )
            elif a[0] == "ADD" and ("FOREIGN" in a):
                if not new_table and self.dialect == "postgres" and "NOT VALID" not in s:
                    self.add(
                        "add_fk_without_not_valid",
                        "medium",
                        tok,
                        f"adding a foreign key validates every row of {table} while locking both tables",
                        "ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID; then in a separate step "
                        "ALTER TABLE ... VALIDATE CONSTRAINT ...",
                    )
                if self.dialect == "mysql":
                    mysql_needs_algorithm = True
            elif a[0] == "ADD" and "CHECK" in a:
                if not new_table and self.dialect == "postgres" and "NOT VALID" not in s:
                    self.add(
                        "add_check_without_not_valid",
                        "medium",
                        tok,
                        f"adding a CHECK constraint scans {table} under ACCESS EXCLUSIVE",
                        "ADD CONSTRAINT ... CHECK (...) NOT VALID; then VALIDATE CONSTRAINT in a separate step",
                    )
            elif a[0] == "ADD" and ("PRIMARY" in a or "UNIQUE" in a):
                if new_table:
                    continue
                if self.dialect == "postgres" and "USING INDEX" not in s:
                    self.add(
                        "add_unique_constraint",
                        "medium",
                        tok,
                        f"ADD {'PRIMARY KEY' if 'PRIMARY' in a else 'UNIQUE'} builds an index on {table} "
                        "while blocking writes",
                        "CREATE UNIQUE INDEX CONCURRENTLY idx ...; then ADD CONSTRAINT c "
                        "PRIMARY KEY/UNIQUE USING INDEX idx",
                    )
                elif self.dialect == "mysql" and "PRIMARY" in a:
                    self.add(
                        "mysql_primary_key_change",
                        "high",
                        tok,
                        f"changing the primary key rebuilds {table} (clustered index)",
                        "use gh-ost / pt-online-schema-change",
                    )
                elif self.dialect == "mysql":
                    mysql_needs_algorithm = True
            elif a[0] == "ADD" and a[1:2] in (["FULLTEXT"], ["SPATIAL"]):
                if not new_table:
                    self.add(
                        "mysql_fulltext_index",
                        "medium",
                        tok,
                        f"{a[1]} index on {table}: the first one rebuilds the table and cannot use LOCK=NONE",
                        "build it off-peak or with gh-ost / pt-online-schema-change",
                    )
            elif a[0] == "ADD" and a[1:2] in (["INDEX"], ["KEY"]):
                mysql_needs_algorithm = True
            elif a[0] == "DROP" and a[1:2] == ["PRIMARY"] and self.dialect == "mysql":
                self.add(
                    "mysql_primary_key_change",
                    "high",
                    tok,
                    f"dropping the primary key rebuilds {table}",
                    "use gh-ost / pt-online-schema-change",
                )
            elif a[0] == "DROP" and (
                a[1:2] == ["COLUMN"]
                or (len(a) > 1 and a[1] not in ("CONSTRAINT", "INDEX", "KEY", "PRIMARY", "FOREIGN", "DEFAULT", "CHECK"))
            ):
                if not new_table:
                    col = act[2].value if a[1:2] == ["COLUMN"] and len(act) > 2 else act[1].value
                    if col.upper() in ("IF", "EXISTS") and len(act) > 4:
                        col = act[4].value
                    self.add(
                        "drop_column",
                        "medium",
                        tok,
                        f"DROP COLUMN {table}.{col}: running code (and ORM caches) that select it will fail",
                        "first deploy code that ignores the column (Rails: ignored_columns), then drop; "
                        "take a backup - this is irreversible",
                    )
            elif a[0] == "RENAME":
                if a[1:2] in (["COLUMN"],) or (len(a) > 2 and a[2] == "TO" and a[1] not in ("TO",)):
                    self.rename(tok, "column")
                elif a[1:2] in (["TO"], ["AS"]):
                    self.rename(tok, "table")
        if locking and not new_table:
            self.need_lock_timeout(first, table)
        if self.dialect == "mysql" and mysql_needs_algorithm and not has_algorithm and not new_table:
            self.mysql_no_algorithm(first, table)


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _d, names in os.walk(p):
                files += [os.path.join(dirpath, n) for n in sorted(names) if n.endswith(".sql")]
        else:
            files.append(p)
    return sorted(files) if len(paths) == 1 and os.path.isdir(paths[0]) else files


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Scan SQL migrations for locking, rewriting and breaking operations (read-only).",
        epilog="Exit codes: 0 = clean, 1 = findings at or above --fail-on, 2 = error.",
    )
    p.add_argument("paths", nargs="+", help="migration .sql files or directories, or - for stdin")
    p.add_argument("--dialect", choices=["auto", "postgres", "mysql"], default="auto")
    p.add_argument(
        "--transaction",
        choices=["auto", "yes"],
        default="auto",
        help="yes = the framework wraps each file in a transaction (Rails/Django/Alembic on PostgreSQL)",
    )
    p.add_argument("--fail-on", choices=SEVERITIES, default="medium")
    p.add_argument("--ignore", default="", help="comma-separated check names to skip")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    ignore = {x.strip() for x in args.ignore.split(",") if x.strip()}
    findings: list[Finding] = []
    files = 0
    try:
        for path in collect(args.paths):
            if path == "-":
                text, name = sys.stdin.read(), "<stdin>"
            else:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text, name = fh.read(), path
            dialect = detect_dialect(text) if args.dialect == "auto" else args.dialect
            findings += MigrationLinter(name, dialect, args.transaction, ignore).lint(text)
            files += 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    findings.sort(key=lambda f: (f.file, f.line, -SEVERITIES.index(f.severity)))
    counts = Counter(f.severity for f in findings)
    if args.json:
        print(json.dumps({"files": files, "findings": [asdict(f) for f in findings]}, indent=2))
    else:
        summary = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "no findings"
        print(f"migration-lint: {summary} ({files} file(s))")
        for f in findings:
            print(f"{f.severity.upper():<7} {f.file}:{f.line}  {f.check}  {f.title}")
            print(f"        fix: {f.fix}")
    fail = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) >= fail for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
