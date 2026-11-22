#!/usr/bin/env python3
"""Review a schema dump (pg_dump --schema-only, mysqldump --no-data, or migration DDL).

Reads CREATE TABLE, ALTER TABLE ... ADD CONSTRAINT and CREATE INDEX statements and
reports design problems:

  keys         tables without a primary key, 32-bit integer primary keys, foreign keys
               without a supporting index (PostgreSQL), foreign key / referenced column
               type mismatches
  types        FLOAT/DOUBLE/REAL for money, timestamp without time zone (PostgreSQL),
               utf8/utf8mb3 and MyISAM (MySQL), serial instead of identity (info),
               varchar(255) everywhere (info), ENUM columns (info)
  normalization  repeating column groups (phone1, phone2), lists stored in text columns
               (tag_ids, tags), tables where every column is nullable
  naming       quoted mixed-case identifiers, reserved words, mixed naming styles

It only reads the file. Exit codes: 0 = no findings at or above --fail-on,
1 = findings, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

SEVERITIES = ["info", "low", "medium", "high"]
STOP = {
    "NOT",
    "NULL",
    "DEFAULT",
    "PRIMARY",
    "UNIQUE",
    "REFERENCES",
    "CHECK",
    "CONSTRAINT",
    "COLLATE",
    "GENERATED",
    "AUTO_INCREMENT",
    "COMMENT",
    "CHARSET",
    "ON",
    "IDENTITY",
    "AS",
    "KEY",
}
RESERVED = {
    "user",
    "order",
    "group",
    "table",
    "select",
    "key",
    "desc",
    "from",
    "where",
    "limit",
    "check",
    "column",
    "default",
    "index",
    "references",
    "range",
    "rank",
    "window",
}
MONEY = re.compile(r"(price|amount|cost|total|balance|fee|salary|tax|money|revenue|paid|discount)", re.I)
LIST_COL = re.compile(r"(_ids|_list|_csv)$|^(tags|categories|roles|emails|phones)$", re.I)


@dataclass
class Tok:
    kind: str
    value: str
    line: int
    quoted: bool = False

    @property
    def up(self) -> str:
        return self.value.upper() if self.kind == "word" else self.value


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    line: int
    quoted: bool = False
    default: str = ""
    charset: str = ""
    original: str = ""


@dataclass
class Table:
    name: str
    line: int
    columns: dict[str, Column] = field(default_factory=dict)
    pk: list[str] = field(default_factory=list)
    uniques: list[list[str]] = field(default_factory=list)
    indexes: list[list[str]] = field(default_factory=list)
    fks: list[tuple[list[str], str, list[str], int]] = field(default_factory=list)
    engine: str = ""
    charset: str = ""
    quoted: bool = False


@dataclass
class Finding:
    check: str
    severity: str
    table: str
    title: str
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
        if kind not in ("ws", "comment"):
            quoted = False
            if kind == "qident":
                kind, value, quoted = "word", value[1:-1], True
            out.append(Tok("op" if kind == "other" else kind, value, line, quoted))
        line += value.count("\n")
    return out


def split_statements(tokens: list[Tok]) -> list[list[Tok]]:
    stmts, cur = [], []
    for t in tokens:
        if t.value == ";" and t.kind == "op":
            if cur:
                stmts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts


def paren_items(toks: list[Tok], start: int) -> tuple[list[list[Tok]], int]:
    items, cur, depth, i = [], [], 0, start
    while i < len(toks):
        t = toks[i]
        if t.value == "(" and t.kind == "op":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif t.value == ")" and t.kind == "op":
            depth -= 1
            if depth == 0:
                if cur:
                    items.append(cur)
                return items, i
        if depth == 1 and t.value == ",":
            items.append(cur)
            cur = []
        else:
            cur.append(t)
        i += 1
    return items, i


def col_list(toks: list[Tok], start: int) -> tuple[list[str], int]:
    items, end = paren_items(toks, start)
    return [it[0].value.lower() for it in items if it], end


def qualified(toks: list[Tok], i: int) -> tuple[str, int, bool]:
    ups = [t.up for t in toks]
    while i < len(toks) and ups[i] in ("ONLY", "IF", "NOT", "EXISTS", "TABLE"):
        i += 1
    name, quoted = toks[i].value, toks[i].quoted
    i += 1
    while i + 1 < len(toks) and toks[i].value == ".":
        name, quoted = toks[i + 1].value, toks[i + 1].quoted
        i += 2
    return name, i, quoted


def parse(text: str) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for stmt in split_statements(tokenize(text)):
        ups = [t.up for t in stmt]
        if len(stmt) < 3:
            continue
        if ups[0] == "CREATE" and "TABLE" in ups[1:5] and "(" in ups:
            parse_create(stmt, ups, tables)
        elif ups[0] == "ALTER" and ups[1] == "TABLE":
            parse_alter(stmt, ups, tables)
        elif ups[0] == "CREATE" and "INDEX" in ups[1:4] and "ON" in ups:
            on = ups.index("ON")
            name, i, _ = qualified(stmt, on + 1)
            t = tables.get(name.lower())
            while i < len(stmt) and stmt[i].value != "(":
                i += 1
            if t is not None and i < len(stmt):
                cols, _ = col_list(stmt, i)
                (t.uniques if "UNIQUE" in ups[:3] else t.indexes).append(cols)
    return tables


def parse_create(stmt: list[Tok], ups: list[str], tables: dict[str, Table]) -> None:
    name, i, quoted = qualified(stmt, ups.index("TABLE") + 1)
    if i >= len(stmt) or stmt[i].value != "(":
        return
    t = Table(name.lower(), stmt[0].line, quoted=quoted and name != name.lower())
    tables[t.name] = t
    items, end = paren_items(stmt, i)
    for it in items:
        if not it:
            continue
        head = [x.up for x in it]
        if head[0] in (
            "CONSTRAINT",
            "PRIMARY",
            "UNIQUE",
            "KEY",
            "INDEX",
            "FOREIGN",
            "CHECK",
            "FULLTEXT",
            "SPATIAL",
        ):
            table_constraint(it, head, t)
            continue
        if head[0] == "LIKE":
            continue
        col = column_def(it, head, t)
        t.columns[col.name] = col
    rest = " ".join(x.up for x in stmt[end + 1 :])
    m = re.search(r"ENGINE\s*=\s*(\w+)", rest)
    if m:
        t.engine = m.group(1).upper()
    m = re.search(r"(?:CHARSET|CHARACTER SET)\s*=?\s*(\w+)", rest)
    if m:
        t.charset = m.group(1).lower()


def column_def(it: list[Tok], head: list[str], t: Table) -> Column:
    name = it[0].value
    k = 1
    typ = []
    depth = 0
    while k < len(it):
        u = head[k]
        if depth == 0 and (u in STOP or (u == "CHARACTER" and k + 1 < len(it) and head[k + 1] == "SET")):
            break
        if it[k].value == "(":
            depth += 1
        elif it[k].value == ")":
            depth -= 1
        typ.append(it[k].value)
        k += 1
    type_s = re.sub(r"\s*([(),])\s*", r"\1", " ".join(typ)).lower()
    rest = " ".join(head[k:])
    col = Column(
        name.lower(),
        type_s,
        "NOT NULL" not in rest and "PRIMARY KEY" not in rest,
        it[0].line,
        it[0].quoted and name != name.lower(),
        original=name,
    )
    m = re.search(r"DEFAULT\s+(\S+)", rest)
    if m:
        col.default = m.group(1)
    m = re.search(r"CHARACTER SET\s+(\w+)|CHARSET\s+(\w+)", rest)
    if m:
        col.charset = (m.group(1) or m.group(2)).lower()
    if "PRIMARY KEY" in rest:
        t.pk = [col.name]
    elif re.search(r"\bUNIQUE\b", rest):
        t.uniques.append([col.name])
    if "REFERENCES" in head[k:]:
        r = k + head[k:].index("REFERENCES")
        ref, j, _ = qualified(it, r + 1)
        refcols = []
        if j < len(it) and it[j].value == "(":
            refcols, _ = col_list(it, j)
        t.fks.append(([col.name], ref.lower(), refcols, it[0].line))
    return col


def table_constraint(it: list[Tok], head: list[str], t: Table) -> None:
    k = 2 if head[0] == "CONSTRAINT" else 0
    kind = head[k] if k < len(head) else ""
    paren = next((j for j in range(k, len(it)) if it[j].value == "("), None)
    if paren is None:
        return
    cols, end = col_list(it, paren)
    if kind == "PRIMARY":
        t.pk = cols
    elif kind == "UNIQUE":
        t.uniques.append(cols)
    elif kind in ("KEY", "INDEX"):
        t.indexes.append(cols)
    elif kind == "FOREIGN":
        rest = [x.up for x in it[end + 1 :]]
        if "REFERENCES" in rest:
            r = end + 1 + rest.index("REFERENCES")
            ref, j, _ = qualified(it, r + 1)
            refcols = []
            if j < len(it) and it[j].value == "(":
                refcols, _ = col_list(it, j)
            t.fks.append((cols, ref.lower(), refcols, it[0].line))


def parse_alter(stmt: list[Tok], ups: list[str], tables: dict[str, Table]) -> None:
    name, i, _ = qualified(stmt, 2)
    t = tables.get(name.lower())
    if t is None:
        return
    # split actions on top-level commas
    depth, cur, actions = 0, [], []
    for tok in stmt[i:]:
        if tok.value == "(":
            depth += 1
        elif tok.value == ")":
            depth -= 1
        if depth == 0 and tok.value == ",":
            actions.append(cur)
            cur = []
        else:
            cur.append(tok)
    actions.append(cur)
    for act in actions:
        a = [x.up for x in act]
        if not a or a[0] != "ADD":
            continue
        body, head = act[1:], a[1:]
        if head and head[0] in ("CONSTRAINT", "PRIMARY", "UNIQUE", "KEY", "INDEX", "FOREIGN"):
            table_constraint(body, head, t)
        elif head:
            if head[0] == "COLUMN":
                body, head = body[1:], head[1:]
            if body:
                col = column_def(body, head, t)
                t.columns[col.name] = col


def base_type(type_s: str) -> str:
    return re.sub(r"\(.*", "", type_s).strip()


def review(tables: dict[str, Table], dialect: str) -> list[Finding]:
    out: list[Finding] = []
    styles: Counter = Counter()
    varchar_lengths: Counter = Counter()
    for t in tables.values():

        def add(check: str, sev: str, title: str, line: int, fix: str, _t: Table = t) -> None:
            out.append(Finding(check, sev, _t.name, title, line, fix))

        if not t.pk:
            add(
                "no_primary_key",
                "high",
                f"{t.name} has no primary key",
                t.line,
                "add a surrogate key (bigint identity / BIGINT AUTO_INCREMENT) or a natural PRIMARY KEY; "
                "replication, ORMs and online schema tools need one",
            )
        elif len(t.pk) == 1:
            pkcol = t.columns.get(t.pk[0])
            if pkcol and base_type(pkcol.type) in (
                "int",
                "integer",
                "serial",
                "int4",
                "mediumint",
                "smallint",
                "int unsigned",
            ):
                add(
                    "int_primary_key",
                    "low",
                    f"{t.name}.{pkcol.name} is {pkcol.type}: 32-bit keys run out at ~2.1 billion "
                    "(4.2 billion unsigned)",
                    pkcol.line,
                    "use bigint for tables that grow; migrating later is a full rewrite",
                )
        if t.engine == "MYISAM":
            add(
                "mysql_myisam",
                "high",
                f"{t.name} uses MyISAM (no transactions, no foreign keys, table-level locks, not crash safe)",
                t.line,
                "ALTER TABLE ... ENGINE=InnoDB (use an online schema tool on big tables)",
            )
        if t.charset in ("utf8", "utf8mb3"):
            add(
                "mysql_utf8mb3",
                "medium",
                f"{t.name} uses {t.charset} (3-byte UTF-8: emoji and some CJK characters fail)",
                t.line,
                "CONVERT TO CHARACTER SET utf8mb4 (plan it as an online migration)",
            )
        nullable = [c for c in t.columns.values() if c.nullable and c.name not in t.pk]
        if len(t.columns) >= 4 and len(nullable) == len([c for c in t.columns.values() if c.name not in t.pk]):
            add(
                "all_nullable",
                "info",
                f"every non-key column of {t.name} is nullable",
                t.line,
                "mark required columns NOT NULL; NULLs complicate queries, uniqueness and aggregates",
            )
        if t.quoted:
            add(
                "quoted_identifier",
                "low",
                f'table "{t.name}" uses a quoted mixed-case name',
                t.line,
                "use lower_snake_case so it never needs quoting",
            )
        if t.name in RESERVED:
            add(
                "reserved_word",
                "low",
                f"table name {t.name} is a reserved word",
                t.line,
                "rename (e.g. app_user, orders)",
            )
        styles["camel" if re.search(r"[a-z][A-Z]", t.name) else "snake"] += 1
        groups: dict[str, list[str]] = defaultdict(list)
        for c in t.columns.values():
            typ = base_type(c.type)
            if typ in ("float", "double", "real", "double precision", "float4", "float8") and MONEY.search(c.name):
                add(
                    "float_money",
                    "high",
                    f"{t.name}.{c.name} is {c.type}: binary floating point cannot store money exactly",
                    c.line,
                    "use numeric(12,2) / DECIMAL(12,2), or integer minor units (cents)",
                )
            if dialect != "mysql" and (typ == "timestamp" or "without time zone" in c.type):
                add(
                    "timestamp_without_tz",
                    "low",
                    f"{t.name}.{c.name} is timestamp without time zone",
                    c.line,
                    "use timestamptz (stores an absolute instant; displays in the session time zone)",
                )
            if typ in ("serial", "bigserial", "smallserial"):
                add(
                    "serial_column",
                    "info",
                    f"{t.name}.{c.name} uses {typ}",
                    c.line,
                    "prefer bigint GENERATED ALWAYS AS IDENTITY (standard SQL, clearer permissions)",
                )
            if c.charset in ("utf8", "utf8mb3"):
                add(
                    "mysql_utf8mb3",
                    "medium",
                    f"{t.name}.{c.name} uses {c.charset}",
                    c.line,
                    "use utf8mb4",
                )
            if typ == "enum":
                add(
                    "enum_column",
                    "info",
                    f"{t.name}.{c.name} is an ENUM",
                    c.line,
                    "fine for stable sets; for values that change use a lookup table with a foreign key",
                )
            if LIST_COL.search(c.name) and typ in (
                "text",
                "varchar",
                "character varying",
                "mediumtext",
                "longtext",
                "char",
            ):
                add(
                    "list_in_column",
                    "medium",
                    f"{t.name}.{c.name} ({c.type}) looks like a delimited list",
                    c.line,
                    "use a junction table (one row per value) with foreign keys; "
                    "PostgreSQL arrays only if you never join on the values",
                )
            m = re.match(r"^(.*?)_?(\d+)$", c.name)
            if m and m.group(1):
                groups[m.group(1)].append(c.name)
            if c.quoted:
                add(
                    "quoted_identifier",
                    "low",
                    f'{t.name}."{c.original}" uses a quoted mixed-case name',
                    c.line,
                    "use lower_snake_case",
                )
            if c.name in RESERVED:
                add(
                    "reserved_word",
                    "low",
                    f"{t.name}.{c.name} is a reserved word",
                    c.line,
                    "rename the column",
                )
            if typ in ("varchar", "character varying"):
                varchar_lengths[c.type] += 1
            styles["camel" if re.search(r"[a-z][A-Z]", c.original or c.name) else "snake"] += 1
        for base, cols in groups.items():
            if len(cols) >= 2:
                add(
                    "repeating_group",
                    "low",
                    f"{t.name} has a repeating column group: {', '.join(sorted(cols))}",
                    t.columns[cols[0]].line,
                    f"move {base} values to a child table ({t.name}_{base}s) with one row per value",
                )
        # foreign keys
        indexed = [t.pk, *t.uniques, *t.indexes]
        for cols, ref, refcols, line in t.fks:
            if dialect != "mysql" and not any(ix[: len(cols)] == cols for ix in indexed if ix):
                add(
                    "fk_without_index",
                    "medium",
                    f"foreign key {t.name}({', '.join(cols)}) -> {ref} has no index starting with those columns",
                    line,
                    f"CREATE INDEX CONCURRENTLY ON {t.name} ({', '.join(cols)}); without it every "
                    f"DELETE/UPDATE on {ref} scans {t.name}, and joins are slow",
                )
            target = tables.get(ref)
            if target is not None:
                rcols = refcols or target.pk
                for c, rc in zip(cols, rcols):
                    a, b = t.columns.get(c), target.columns.get(rc)
                    if a and b and _norm_int(a.type) != _norm_int(b.type):
                        add(
                            "fk_type_mismatch",
                            "medium",
                            f"{t.name}.{c} is {a.type} but references {ref}.{rc} ({b.type})",
                            line,
                            "use the same type on both sides (implicit casts, and int columns cannot hold bigint ids)",
                        )
    if styles["camel"] and styles["snake"] and min(styles.values()) >= 2:
        out.append(
            Finding(
                "mixed_naming",
                "low",
                "*",
                f"mixed naming styles: {styles['camel']} camelCase and {styles['snake']} snake_case identifiers",
                1,
                "pick lower_snake_case for every table and column",
            )
        )
    total_varchar = sum(varchar_lengths.values())
    v255 = sum(n for typ, n in varchar_lengths.items() if typ.endswith("(255)"))
    if total_varchar >= 5 and v255 / total_varchar > 0.6:
        out.append(
            Finding(
                "varchar_255",
                "info",
                "*",
                f"{v255} of {total_varchar} varchar columns are varchar(255)",
                1,
                "size columns for their data (and add CHECK constraints); in PostgreSQL text is equivalent",
            )
        )
    return out


def _norm_int(type_s: str) -> str:
    b = base_type(type_s).replace(" unsigned", "")
    return {
        "serial": "integer",
        "int": "integer",
        "int4": "integer",
        "bigserial": "bigint",
        "int8": "bigint",
        "smallserial": "smallint",
        "int2": "smallint",
        "character varying": "varchar",
    }.get(b, b)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Review a SQL schema dump for key, type, normalization and naming problems (read-only).",
        epilog="Exit codes: 0 = clean, 1 = findings at or above --fail-on, 2 = error.",
    )
    p.add_argument("path", help="schema dump (.sql), or - for stdin")
    p.add_argument("--dialect", choices=["auto", "postgres", "mysql"], default="auto")
    p.add_argument("--fail-on", choices=SEVERITIES, default="medium")
    p.add_argument("--min-severity", choices=SEVERITIES, default="info")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    try:
        if args.path == "-":
            text = sys.stdin.read()
        else:
            with open(args.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    dialect = args.dialect
    if dialect == "auto":
        dialect = "mysql" if re.search(r"`|ENGINE\s*=|AUTO_INCREMENT", text, re.I) else "postgres"
    tables = parse(text)
    if not tables:
        print("error: no CREATE TABLE statements found", file=sys.stderr)
        return 2
    floor = SEVERITIES.index(args.min_severity)
    findings = [f for f in review(tables, dialect) if SEVERITIES.index(f.severity) >= floor]
    findings.sort(key=lambda f: (-SEVERITIES.index(f.severity), f.table, f.line))
    counts = Counter(f.severity for f in findings)
    if args.json:
        print(
            json.dumps(
                {"dialect": dialect, "tables": len(tables), "findings": [asdict(f) for f in findings]},
                indent=2,
            )
        )
    else:
        summary = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "no findings"
        print(f"schema review ({dialect}, {len(tables)} tables): {summary}")
        for f in findings:
            print(f"{f.severity.upper():<7} line {f.line:<5} {f.check}  {f.title}")
            print(f"        fix: {f.fix}")
    fail = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) >= fail for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
