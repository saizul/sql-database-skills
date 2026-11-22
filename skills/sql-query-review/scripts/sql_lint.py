#!/usr/bin/env python3
"""Lint SQL queries (PostgreSQL / MySQL / MariaDB) for performance and correctness anti-patterns.

Reads .sql files, directories of .sql files, stdin, or query logs (--log). Only DML
statements (SELECT, WITH, INSERT, UPDATE, DELETE, REPLACE) are checked; DDL is skipped
(use safe-migrations / schema-design-review for DDL). The linter never connects to a
database.

Checks: SELECT *, functions / casts / arithmetic on columns in WHERE and JOIN ON,
leading-wildcard LIKE, OR across different columns, optional-filter ORs, NOT IN
(subquery), = NULL, UPDATE/DELETE without WHERE, ORDER BY RAND(), large OFFSET,
implicit type casts (exact with --schema, name-based otherwise), comma joins,
INSERT without a column list, UNION without ALL, huge IN lists and repeated
statements that look like N+1 queries.

Suppress a finding with a comment inside the statement:
  -- sql-lint: ignore select_star,or_different_columns
  -- sql-lint: ignore            (all checks for that statement)

Exit codes: 0 = no findings at or above --fail-on, 1 = findings, 2 = error.
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

KEYWORDS = set(
    """
    ALL AND ANY AS ASC BETWEEN BY CASE CAST COLLATE CROSS CURRENT_DATE CURRENT_TIME
    CURRENT_TIMESTAMP DAY DEFAULT DELETE DESC DISTINCT DO ELSE END ESCAPE EXCEPT EXISTS
    FALSE FETCH FIRST FOR FROM FULL GROUP HAVING HOUR ILIKE IN INNER INSERT INTERSECT
    INTERVAL INTO IS JOIN LATERAL LEFT LIKE LIMIT MINUTE MONTH NATURAL NEXT NOT NULL
    NULLS OF OFFSET ON ONLY OR ORDER OUTER OVER PARTITION RECURSIVE REGEXP REPLACE
    RETURNING RIGHT RLIKE ROW ROWS SECOND SELECT SET SIMILAR SOME STRAIGHT_JOIN TABLE
    THEN TO TRUE UNION UNKNOWN UPDATE USING VALUES WEEK WHEN WHERE WINDOW WITH YEAR DATE
    TIME TIMESTAMP INTEGER INT BIGINT SMALLINT TEXT VARCHAR CHAR NUMERIC DECIMAL BOOLEAN
    JSON JSONB UUID SIGNED UNSIGNED CONFLICT DUPLICATE KEY IGNORE LOCK SHARE NOWAIT SKIP
    LOCKED MODE QUARTER EPOCH DOW ISODOW DOY
    """.split()
)
WRAP_FUNCS = set(
    """
    LOWER UPPER TRIM LTRIM RTRIM DATE YEAR MONTH DAY HOUR TIME DATE_FORMAT TO_CHAR
    DATE_TRUNC EXTRACT SUBSTRING SUBSTR LEFT RIGHT CONCAT COALESCE IFNULL NVL ISNULL
    CAST CONVERT LENGTH CHAR_LENGTH ABS ROUND FLOOR CEIL MD5 UNIX_TIMESTAMP
    FROM_UNIXTIME DATE_ADD DATE_SUB TO_DATE TO_TIMESTAMP REPLACE JSON_EXTRACT
    JSON_UNQUOTE WEEKDAY DAYOFWEEK DAYOFMONTH STR_TO_DATE
    """.split()
)
CMP = {"=", "<", ">", "<=", ">=", "<>", "!=", "<=>"}
CMP_WORDS = {"LIKE", "ILIKE", "IN", "BETWEEN", "IS", "NOT", "REGEXP", "RLIKE", "SIMILAR"}
CLAUSES = {
    "SELECT": "select",
    "FROM": "from",
    "JOIN": "from",
    "WHERE": "where",
    "ON": "on",
    "HAVING": "having",
    "GROUP": "group",
    "ORDER": "order",
    "LIMIT": "limit",
    "OFFSET": "limit",
    "SET": "set",
    "VALUES": "values",
    "RETURNING": "select",
    "UNION": "other",
    "INTERSECT": "other",
    "EXCEPT": "other",
    "WINDOW": "other",
    "INTO": "other",
    "USING": "other",
    "FOR": "other",
}
PREDICATE = {"where", "on", "having"}
DML = {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "REPLACE", "("}
STRING_TYPES = {
    "CHAR",
    "VARCHAR",
    "TEXT",
    "CHARACTER",
    "NCHAR",
    "NVARCHAR",
    "TINYTEXT",
    "MEDIUMTEXT",
    "LONGTEXT",
    "CITEXT",
    "ENUM",
    "UUID",
}
NUMERIC_TYPES = {
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "MEDIUMINT",
    "SERIAL",
    "BIGSERIAL",
    "SMALLSERIAL",
    "DECIMAL",
    "NUMERIC",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "INT2",
    "INT4",
    "INT8",
}
STRINGY_NAME = re.compile(
    r"(^|_)(phone|mobile|zip|zipcode|postcode|postal_code|sku|code|isbn|iban|ssn|uuid|guid|ref|reference)$"
    r"|_(no|number|num)$",
    re.I,
)


@dataclass
class Tok:
    kind: str  # word, qident, string, number, param, op, comment
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
    snippet: str = ""


def token_re(dialect: str) -> re.Pattern:
    hash_comment = {"mysql": r"|\#[^\n]*", "postgres": "", "auto": r"|(?<![^\n])\#[^\n]*|\#(?=[ \t])[^\n]*"}
    return re.compile(
        r"(?P<ws>\s+)"
        r"|(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z)" + hash_comment[dialect] + r")"
        r"|(?P<string>[EeNnBbXx]?'(?:[^'\\]|''|\\.)*')"
        r"|(?P<dollar>\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)?\$.*?\$(?P=tag)\$)"
        r"|(?P<qident>\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`)"
        r"|(?P<number>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|\.\d+)"
        r"|(?P<param>\$\d+|\?|%s|%\([A-Za-z_]\w*\)s|(?<!:):[A-Za-z_]\w*)"
        r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)"
        r"|(?P<op><=>|<>|!=|<=|>=|::|\|\||->>|->|[-+*/%=<>(),;.\[\]{}~!@&|^:#])"
        r"|(?P<other>.)",
        re.S,
    )


def tokenize(text: str, dialect: str = "auto") -> list[Tok]:
    out: list[Tok] = []
    line = 1
    for m in token_re(dialect).finditer(text):
        kind = m.lastgroup
        value = m.group(0)
        if kind == "dollar":
            kind = "string"
        if kind != "ws":
            out.append(Tok(kind if kind != "other" else "op", value, line))
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


def fingerprint(code: list[Tok]) -> str:
    parts = []
    for t in code:
        if t.kind in ("string", "number", "param"):
            parts.append("?")
        elif t.kind == "word":
            parts.append(t.value.upper() if t.value.upper() in KEYWORDS else t.value.lower())
        elif t.kind == "qident":
            parts.append(t.value[1:-1].lower())
        elif t.kind != "comment":
            parts.append(t.value)
    s = " ".join(parts)
    s = re.sub(r"\(\s*\?(?:\s*,\s*\?)*\s*\)", "(?+)", s)
    s = re.sub(r"(\(\?\+\))(?:\s*,\s*\(\?\+\))+", r"\1, ...", s)
    s = re.sub(r"\s*([(),.])\s*", lambda m: {"(": " (", ")": ") ", ",": ", ", ".": "."}[m.group(1)], s)
    s = re.sub(r"\s+([,)])", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- schema


def parse_schema_types(text: str) -> dict[str, str]:
    """Return {column_name_lower: TYPE} from CREATE TABLE statements (conflicts -> '?')."""
    types: dict[str, str] = {}
    for stmt in split_statements(tokenize(text)):
        code = [t for t in stmt if t.kind != "comment"]
        ups = [t.up for t in code]
        if len(code) < 4 or ups[0] != "CREATE" or "TABLE" not in ups[:6]:
            continue
        try:
            start = ups.index("(")
        except ValueError:
            continue
        depth, item = 0, []
        for t in code[start + 1 :]:
            if t.value == "(":
                depth += 1
            elif t.value == ")":
                if depth == 0:
                    _add_column(item, types)
                    break
                depth -= 1
            if depth == 0 and t.value == ",":
                _add_column(item, types)
                item = []
            else:
                item.append(t)
    return types


def _add_column(item: list[Tok], types: dict[str, str]) -> None:
    if len(item) < 2 or item[0].kind not in ("word", "qident"):
        return
    if item[0].up in {"CONSTRAINT", "PRIMARY", "UNIQUE", "KEY", "INDEX", "FOREIGN", "CHECK", "FULLTEXT"}:
        return
    name = item[0].value.strip('"`').lower()
    typ = item[1].value.upper()
    if name in types and types[name] != typ:
        types[name] = "?"
    else:
        types[name] = typ


# --------------------------------------------------------------------------- linter


class StatementLinter:
    def __init__(self, stmt: list[Tok], file: str, opts: argparse.Namespace, schema: dict[str, str]):
        self.file = file
        self.opts = opts
        self.schema = schema
        self.comments = [t for t in stmt if t.kind == "comment"]
        self.code = [t for t in stmt if t.kind != "comment"]
        self.findings: list[Finding] = []
        n = len(self.code)
        self.depth = [0] * n
        self.match: dict[int, int] = {}
        self.clause = ["other"] * n
        stack_open: list[int] = []
        clause_stack = ["other"]
        for i, t in enumerate(self.code):
            if t.kind == "op" and t.value == "(":
                self.depth[i] = len(stack_open)
                self.clause[i] = clause_stack[-1]
                stack_open.append(i)
                nxt = self.code[i + 1].up if i + 1 < n else ""
                inherit = clause_stack[-1] if clause_stack[-1] in PREDICATE and nxt != "SELECT" else "other"
                clause_stack.append(inherit)
                continue
            if t.kind == "op" and t.value == ")":
                if stack_open:
                    self.match[stack_open.pop()] = i
                    clause_stack.pop()
                self.depth[i] = len(stack_open)
                self.clause[i] = clause_stack[-1]
                continue
            self.depth[i] = len(stack_open)
            if t.kind == "word" and t.up in CLAUSES:
                new = CLAUSES[t.up]
                nxt = self.code[i + 1].up if i + 1 < n else ""
                if t.up == "ON" and nxt in ("CONFLICT", "DUPLICATE", "DELETE", "UPDATE"):
                    new = "other"
                clause_stack[-1] = new
            self.clause[i] = clause_stack[-1]

    # helpers ---------------------------------------------------------------
    def tok(self, i: int) -> Tok | None:
        return self.code[i] if 0 <= i < len(self.code) else None

    def up(self, i: int) -> str:
        t = self.tok(i)
        return t.up if t else ""

    def is_column(self, i: int) -> bool:
        t = self.tok(i)
        if t is None or t.kind not in ("word", "qident"):
            return False
        if t.kind == "word" and t.up in KEYWORDS:
            return False
        return self.up(i + 1) != "("

    def colname(self, i: int) -> str:
        return self.code[i].value.strip('"`').lower()

    def add(self, check: str, severity: str, i: int, title: str, fix: str) -> None:
        t = self.tok(i) or self.code[0]
        lo = max(0, i - 4)
        snippet = " ".join(x.value for x in self.code[lo : i + 6])
        self.findings.append(Finding(check, severity, title, self.file, t.line, fix, snippet[:160]))

    def ignored(self) -> set[str] | None:
        names: set[str] = set()
        found = False
        for c in self.comments:
            m = re.search(r"sql-lint:\s*ignore\b([\w\s,-]*)", c.value)
            if m:
                found = True
                names |= {x for x in re.split(r"[\s,]+", m.group(1)) if x}
        if not found:
            return set()
        return names or None  # None = ignore everything

    # checks ----------------------------------------------------------------
    def run(self) -> list[Finding]:
        if not self.code or self.up(0) not in DML:
            return []
        self.check_select_star()
        self.check_functions_on_columns()
        self.check_like()
        self.check_or_groups()
        self.check_misc()
        self.check_implicit_casts()
        ign = self.ignored()
        if ign is None:
            return []
        seen, out = set(), []
        for f in self.findings:
            key = (f.check, f.line)
            if f.check in ign or f.check in self.opts.ignore or key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out

    def check_select_star(self) -> None:
        for i, t in enumerate(self.code):
            if t.value != "*" or t.kind != "op" or self.clause[i] != "select":
                continue
            prev = self.tok(i - 1)
            if prev is None or not (prev.up in ("SELECT", "DISTINCT", "ALL") or prev.value in (",", ".")):
                continue
            j = i - 1
            while j >= 0 and not (self.up(j) == "SELECT" and self.depth[j] == self.depth[i]):
                j -= 1
            if j >= 2 and self.up(j - 1) == "(" and self.up(j - 2) == "EXISTS":
                continue
            self.add(
                "select_star",
                "medium",
                i,
                "SELECT * reads every column (no index-only scans, breaks when columns change)",
                "list the columns the caller uses",
            )
            return

    def check_functions_on_columns(self) -> None:
        for i, t in enumerate(self.code):
            if self.clause[i] not in PREDICATE:
                continue
            if t.kind == "word" and t.up in WRAP_FUNCS and self.up(i + 1) == "(":
                j = self.match.get(i + 1)
                if j is None or self.up(i + 2) == "SELECT":
                    continue
                if not any(self.is_column(k) for k in range(i + 2, j)):
                    continue
                if self.up(j + 1) in CMP or self.up(j + 1) in CMP_WORDS or self.up(i - 1) in CMP:
                    self.add(
                        "function_on_column",
                        "medium",
                        i,
                        f"{t.up}(column) in a predicate prevents a plain index on that column from being used",
                        "rewrite as a range on the bare column (col >= x AND col < y), "
                        "or add an expression/functional index",
                    )
            elif t.value == "::" and self.is_column(i - 1):
                k = i + 1
                if self.up(k + 1) == "(":
                    k = self.match.get(k + 1, k)
                if self.up(k + 1) in CMP:
                    self.add(
                        "function_on_column",
                        "medium",
                        i,
                        f"cast {self.code[i - 1].value}::{self.up(i + 1).lower()} in a predicate prevents index use",
                        "cast the parameter instead of the column, or index the expression",
                    )
            elif (
                self.is_column(i)
                and self.up(i + 1) in ("+", "-", "*", "/")
                and self.tok(i + 2) is not None
                and self.code[i + 2].kind in ("number", "param")
                and self.up(i + 3) in CMP
                and self.up(i - 1) in ("WHERE", "AND", "OR", "(", "ON", "NOT")
            ):
                self.add(
                    "function_on_column",
                    "low",
                    i,
                    f"arithmetic on column {t.value} in a predicate prevents index use",
                    "move the arithmetic to the other side: col = ? - 1",
                )

    def check_like(self) -> None:
        for i, t in enumerate(self.code):
            if t.up not in ("LIKE", "ILIKE"):
                continue
            nxt = self.tok(i + 1)
            lead = nxt is not None and nxt.kind == "string" and re.match(r"^[EeNn]?'%", nxt.value)
            if nxt is not None and nxt.up == "CONCAT" and self.up(i + 2) == "(":
                arg = self.tok(i + 3)
                lead = lead or (arg is not None and arg.kind == "string" and arg.value.startswith("'%"))
            if lead:
                self.add(
                    "leading_wildcard",
                    "medium",
                    i,
                    "LIKE with a leading % cannot use a B-tree index (full scan)",
                    "PostgreSQL: pg_trgm GIN index; MySQL: FULLTEXT index; or anchor the pattern ('abc%')",
                )

    def check_or_groups(self) -> None:
        groups: list[dict] = []

        def close(depth: int) -> None:
            while groups and groups[-1]["depth"] >= depth:
                self._evaluate_or_group(groups.pop())

        for i, t in enumerate(self.code):
            d = self.depth[i]
            if t.kind == "word" and t.up in CLAUSES:
                close(d)
                if self.clause[i] in PREDICATE:
                    groups.append({"depth": d, "terms": [[]], "start": i})
                continue
            if t.value == "(" and t.kind == "op":
                if self.clause[i] in PREDICATE and self.up(i + 1) != "SELECT":
                    if groups and groups[-1]["depth"] == d:
                        groups[-1]["terms"][-1].append(i)
                    groups.append({"depth": d + 1, "terms": [[]], "start": i})
                continue
            if t.value == ")" and t.kind == "op":
                close(d + 1)
                continue
            if groups and groups[-1]["depth"] == d:
                if t.up == "OR":
                    groups[-1]["terms"].append([])
                else:
                    groups[-1]["terms"][-1].append(i)
        close(0)

    def _evaluate_or_group(self, g: dict) -> None:
        terms = [t for t in g["terms"] if t]
        if len(terms) < 2:
            return
        cols, ops = [], []
        for term in terms:
            col, op = None, None
            for pos, i in enumerate(term):
                nxt = term[pos + 1] if pos + 1 < len(term) else None
                if self.is_column(i) and nxt is not None and (self.up(nxt) in CMP or self.up(nxt) in CMP_WORDS):
                    col, op = self.colname(i), self.up(nxt)
                    break
            first = self.tok(term[0])
            if first is not None and first.kind == "param" and self.up(term[0] + 1) == "IS":
                self.add(
                    "optional_filter",
                    "low",
                    term[0],
                    "'(col = ? OR ? IS NULL)' catch-all filter: one cached plan cannot use the index well",
                    "build the WHERE clause dynamically with only the filters that are set",
                )
                return
            cols.append(col)
            ops.append(op)
        known = [c for c in cols if c]
        if len(known) < 2:
            return
        if len(set(known)) == 1 and all(o == "=" for o in ops):
            self.add(
                "or_same_column",
                "low",
                g["start"],
                f"OR chain on {known[0]}: {known[0]} = a OR {known[0]} = b ...",
                f"use {known[0]} IN (a, b, ...)",
            )
        elif len(set(known)) > 1:
            self.add(
                "or_different_columns",
                "medium",
                g["start"],
                "OR across different columns (" + ", ".join(sorted(set(known))) + ") usually forces a scan",
                "split into UNION ALL of index-friendly queries, or index each column "
                "(PostgreSQL BitmapOr / MySQL index_merge)",
            )

    def check_misc(self) -> None:
        code, n = self.code, len(self.code)
        first = self.up(0)
        # DML without WHERE
        verb_i = next((i for i, t in enumerate(code) if self.depth[i] == 0 and t.up in ("UPDATE", "DELETE")), None)
        if verb_i is not None and (first in ("UPDATE", "DELETE") or first == "WITH"):
            if not any(self.depth[i] == 0 and t.up == "WHERE" for i, t in enumerate(code) if i > verb_i):
                self.add(
                    "dml_without_where",
                    "high",
                    verb_i,
                    f"{code[verb_i].up} without WHERE touches every row",
                    "add a WHERE clause; for intentional full-table changes batch it "
                    "(see safe-migrations) or use TRUNCATE",
                )
        comma_done = False
        for i, t in enumerate(code):
            u = t.up
            if u == "NOT" and self.up(i + 1) == "IN" and self.up(i + 2) == "(" and self.up(i + 3) == "SELECT":
                self.add(
                    "not_in_subquery",
                    "medium",
                    i,
                    "NOT IN (subquery) returns no rows if the subquery yields a NULL, and plans poorly",
                    "use NOT EXISTS (SELECT 1 FROM ... WHERE ...)",
                )
            if (
                u in ("=", "<>", "!=")
                and (self.up(i + 1) == "NULL" or self.up(i - 1) == "NULL")
                and self.clause[i] in PREDICATE
            ):
                self.add(
                    "null_comparison",
                    "high",
                    i,
                    f"'{u} NULL' is never true (NULL compares as unknown)",
                    "use IS NULL / IS NOT NULL",
                )
            if u == "ORDER" and self.up(i + 1) == "BY" and self.up(i + 2) in ("RAND", "RANDOM"):
                self.add(
                    "order_by_random",
                    "medium",
                    i,
                    "ORDER BY RAND()/RANDOM() sorts the whole result to pick a few rows",
                    "pick random ids in the application, or TABLESAMPLE (PostgreSQL)",
                )
            if u == "OFFSET" and self.tok(i + 1) is not None and code[i + 1].kind == "number":
                if float(code[i + 1].value) >= self.opts.offset_threshold:
                    self.add(
                        "large_offset",
                        "low",
                        i,
                        f"OFFSET {code[i + 1].value}: the database reads and discards every skipped row",
                        "use keyset pagination: WHERE (created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC",
                    )
            if (
                u == "LIMIT"
                and self.tok(i + 3) is not None
                and code[i + 1].kind == "number"
                and code[i + 2].value == ","
                and float(code[i + 1].value) >= self.opts.offset_threshold
            ):
                self.add(
                    "large_offset",
                    "low",
                    i,
                    f"LIMIT {code[i + 1].value}, n: the database reads and discards every skipped row",
                    "use keyset pagination on an indexed sort key",
                )
            if t.value == "," and self.clause[i] == "from" and not comma_done:
                comma_done = True
                self.add(
                    "comma_join",
                    "low",
                    i,
                    "implicit comma join: a forgotten join condition silently becomes a cross join",
                    "use explicit JOIN ... ON",
                )
            if u == "INTO" and first in ("INSERT", "REPLACE"):
                k = i + 1
                while self.tok(k) is not None and (code[k].kind in ("word", "qident") or code[k].value == "."):
                    if code[k].up in ("VALUES", "VALUE", "SELECT"):
                        break
                    k += 1
                if self.up(k) in ("VALUES", "VALUE", "SELECT"):
                    self.add(
                        "insert_without_columns",
                        "low",
                        i,
                        "INSERT without a column list breaks when a column is added or reordered",
                        "name the columns: INSERT INTO t (a, b) VALUES (...)",
                    )
            if u == "UNION" and self.up(i + 1) != "ALL":
                self.add(
                    "union_distinct",
                    "info",
                    i,
                    "UNION removes duplicates (extra sort/hash)",
                    "use UNION ALL when the branches cannot overlap",
                )
            if u == "IN" and self.up(i + 1) == "(":
                j = self.match.get(i + 1, n)
                items = sum(1 for k in range(i + 2, j) if code[k].kind in ("number", "string", "param"))
                if items > self.opts.in_threshold:
                    self.add(
                        "large_in_list",
                        "low",
                        i,
                        f"IN list with {items} values (parse cost, plan cache churn)",
                        "PostgreSQL: = ANY($1::bigint[]); MySQL: join a temporary table or batch the list",
                    )

    def check_implicit_casts(self) -> None:
        code = self.code
        for i, _t in enumerate(code):
            if self.clause[i] not in PREDICATE or not self.is_column(i):
                continue
            nxt = self.up(i + 1)
            lit_i = None
            if nxt in CMP:
                lit_i = i + 2
            elif nxt == "IN" and self.up(i + 2) == "(":
                lit_i = i + 3
            if lit_i is None or self.tok(lit_i) is None:
                continue
            lit = code[lit_i]
            name = self.colname(i)
            ctype = self.schema.get(name)
            base = re.sub(r"\(.*", "", ctype or "")
            if lit.kind == "number":
                if base in STRING_TYPES:
                    self.add(
                        "implicit_cast",
                        "medium",
                        i,
                        f"{name} is {ctype} but compared to a number: MySQL casts every row "
                        "(no index use); PostgreSQL raises an error",
                        f"quote the value: {name} = '{lit.value}' (and bind it as a string)",
                    )
                elif ctype is None and STRINGY_NAME.search(name):
                    self.add(
                        "implicit_cast",
                        "low",
                        i,
                        f"{name} looks like a string column but is compared to a number (pass --schema to confirm)",
                        "compare against a string literal / bind the parameter as a string",
                    )
            elif lit.kind == "string" and base in NUMERIC_TYPES and re.fullmatch(r"'-?\d+(\.\d+)?'", lit.value):
                self.add(
                    "implicit_cast",
                    "info",
                    i,
                    f"{name} is {ctype} but compared to a quoted number (the literal is cast; "
                    "check the driver binds the right type)",
                    f"use {lit.value.strip(chr(39))} without quotes",
                )


def lint_text(text: str, file: str, opts: argparse.Namespace, schema: dict[str, str]) -> tuple[list, int]:
    findings: list[Finding] = []
    stmts = split_statements(tokenize(text, opts.dialect))
    prints: Counter = Counter()
    first_line: dict[str, int] = {}
    count = 0
    for stmt in stmts:
        linter = StatementLinter(stmt, file, opts, schema)
        if not linter.code or linter.up(0) not in DML:
            continue
        count += 1
        findings += linter.run()
        if any(t.kind in ("number", "string", "param") for t in linter.code) and "WHERE" in [t.up for t in linter.code]:
            fp = fingerprint(linter.code)
            prints[fp] += 1
            first_line.setdefault(fp, linter.code[0].line)
    for fp, c in prints.items():
        if c >= opts.repeat_threshold and fp.startswith("SELECT"):
            findings.append(
                Finding(
                    "n_plus_one",
                    "medium",
                    f"same query shape executed {c} times (possible N+1)",
                    file,
                    first_line[fp],
                    "load all rows in one query: WHERE fk IN (...) / = ANY(?) or a JOIN; "
                    "check the ORM for eager loading (select_related, includes, with)",
                    fp[:160],
                )
            )
    return findings, count


LOG_PATTERNS = [
    re.compile(r"(?:statement|execute [^:]*):\s*(.*)$"),  # PostgreSQL log_statement / auto_explain
    re.compile(r"\d+\s+Query\t(.*)$"),  # MySQL general log: <time>\t<id> Query\t<sql>
]


def log_to_sql(text: str) -> str:
    out = []
    for raw in text.splitlines():
        stmt = raw
        for pat in LOG_PATTERNS:
            m = pat.search(raw)
            if m:
                stmt = m.group(1)
                break
        stmt = stmt.strip()
        if stmt:
            out.append(stmt.rstrip(";") + ";")
    return "\n".join(out)


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _dirs, names in os.walk(p):
                files += [os.path.join(dirpath, n) for n in sorted(names) if n.endswith(".sql")]
        else:
            files.append(p)
    return files


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Lint SQL queries for performance and correctness anti-patterns (read-only).",
        epilog="Exit codes: 0 = clean, 1 = findings at or above --fail-on, 2 = error.",
    )
    p.add_argument("paths", nargs="+", help=".sql files, directories, or - for stdin")
    p.add_argument("--dialect", choices=["auto", "postgres", "mysql"], default="auto")
    p.add_argument("--schema", action="append", default=[], help="schema dump (CREATE TABLE) for exact type checks")
    p.add_argument("--log", action="store_true", help="input is a query log (one statement per line)")
    p.add_argument("--fail-on", choices=SEVERITIES, default="medium")
    p.add_argument("--min-severity", choices=SEVERITIES, default="info", help="hide findings below this")
    p.add_argument("--ignore", default="", help="comma-separated check names to skip")
    p.add_argument("--repeat-threshold", type=int, default=5, help="N+1: repeats of one query shape (default 5)")
    p.add_argument("--offset-threshold", type=int, default=1000, help="flag OFFSET >= this (default 1000)")
    p.add_argument("--in-threshold", type=int, default=500, help="flag IN lists longer than this (default 500)")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    args.ignore = {x.strip() for x in args.ignore.split(",") if x.strip()}

    schema: dict[str, str] = {}
    try:
        for s in args.schema:
            with open(s, encoding="utf-8", errors="replace") as fh:
                schema.update(parse_schema_types(fh.read()))
        findings: list[Finding] = []
        statements = 0
        for path in collect(args.paths):
            if path == "-":
                text, name = sys.stdin.read(), "<stdin>"
            else:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text, name = fh.read(), path
            if args.log:
                text = log_to_sql(text)
            f, n = lint_text(text, name, args, schema)
            findings += f
            statements += n
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    floor = SEVERITIES.index(args.min_severity)
    findings = [f for f in findings if SEVERITIES.index(f.severity) >= floor]
    findings.sort(key=lambda f: (-SEVERITIES.index(f.severity), f.file, f.line))
    counts = Counter(f.severity for f in findings)
    if args.json:
        print(json.dumps({"statements": statements, "findings": [asdict(f) for f in findings]}, indent=2))
    else:
        summary = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "no findings"
        print(f"sql-lint: {summary} ({statements} statements checked)")
        for f in findings:
            print(f"{f.severity.upper():<7} {f.file}:{f.line}  {f.check}  {f.title}")
            print(f"        fix: {f.fix}")
    fail = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) >= fail for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
