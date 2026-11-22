#!/usr/bin/env python3
"""Suggest composite and covering indexes from SQL queries (and an optional schema dump).

For every SELECT / UPDATE / DELETE the script finds, per table, the columns used for
equality filters, joins, ORDER BY / GROUP BY and ranges, and proposes one B-tree index
following the Equality -> Sort -> Range rule, plus a covering variant (PostgreSQL
INCLUDE, MySQL appended columns). With --schema it also reads CREATE TABLE / CREATE
INDEX / ALTER TABLE statements to

  * skip suggestions an existing index (or the primary key) already serves,
  * propose extending an existing index instead of adding a near-duplicate,
  * report duplicate and redundant (left-prefix) indexes that only cost writes.

Suggestions are heuristics: confirm each one with EXPLAIN before and after (see the
explain-plan-analysis skill). The script never connects to a database.

Exit codes: 0 = nothing to suggest, 1 = suggestions or redundant indexes, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field

KEYWORDS = set(
    """
    ALL AND ANY AS ASC BETWEEN BY CASE CAST CROSS DESC DISTINCT ELSE END EXISTS FALSE
    FETCH FIRST FOR FROM FULL GROUP HAVING ILIKE IN INNER INTERVAL INTO IS JOIN LATERAL
    LEFT LIKE LIMIT NATURAL NOT NULL NULLS OFFSET ON ONLY OR ORDER OUTER RIGHT SELECT
    SET STRAIGHT_JOIN THEN TRUE UNION UPDATE USING VALUES WHEN WHERE WITH DELETE INSERT
    RETURNING WINDOW OVER PARTITION LOCK SHARE NOWAIT SKIP LOCKED
    """.split()
)
CMP_EQ = {"="}
CMP_RANGE = {"<", ">", "<=", ">="}
STOP_FROM = {
    "WHERE",
    "GROUP",
    "ORDER",
    "LIMIT",
    "HAVING",
    "UNION",
    "WINDOW",
    "OFFSET",
    "FOR",
    "RETURNING",
    "SET",
}
JOIN_WORDS = {
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "FULL",
    "CROSS",
    "NATURAL",
    "OUTER",
    "STRAIGHT_JOIN",
    "LATERAL",
}


@dataclass
class Tok:
    kind: str
    value: str
    line: int

    @property
    def up(self) -> str:
        return self.value.upper() if self.kind == "word" else self.value


TOKEN_RE = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z)|(?<![^\n])\#[^\n]*)"
    r"|(?P<string>[EeNn]?'(?:[^'\\]|''|\\.)*')"
    r"|(?P<dollar>\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)?\$.*?\$(?P=tag)\$)"
    r"|(?P<qident>\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`)"
    r"|(?P<number>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
    r"|(?P<param>\$\d+|\?|%s|%\([A-Za-z_]\w*\)s|(?<!:):[A-Za-z_]\w*)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<op><=>|<>|!=|<=|>=|::|\|\||->>|->|[-+*/%=<>(),;.\[\]~!@&|^:#])"
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


def ident(v: str) -> str:
    return v.lower()


# --------------------------------------------------------------------------- schema


@dataclass
class Index:
    name: str
    table: str
    columns: list[str]
    unique: bool = False
    primary: bool = False
    partial: bool = False
    expression: bool = False
    method: str = "btree"
    include: list[str] = field(default_factory=list)


@dataclass
class Schema:
    columns: dict[str, list[str]] = field(default_factory=dict)  # table -> columns
    indexes: list[Index] = field(default_factory=list)

    def table_of(self, column: str, candidates: list[str]) -> str | None:
        owners = [t for t in candidates if column in self.columns.get(t, [])]
        return owners[0] if len(owners) == 1 else None


def _paren_items(toks: list[Tok], start: int) -> tuple[list[list[Tok]], int]:
    """Split the parenthesised list starting at toks[start] == '(' into items."""
    items, cur, depth, i = [], [], 0, start
    while i < len(toks):
        t = toks[i]
        if t.value == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif t.value == ")":
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


def _index_cols(items: list[list[Tok]]) -> tuple[list[str], bool]:
    cols, expr = [], False
    for it in items:
        if not it:
            continue
        if it[0].kind != "word" or (len(it) > 2 and it[1].value == "(" and it[2].kind != "number"):
            expr = True  # lower(email), (a + b); MySQL prefix lengths like title(100) are plain columns
        if it[0].value == "(":
            expr = True
            continue
        cols.append(ident(it[0].value))
    return cols, expr


def _table_name(toks: list[Tok], i: int) -> tuple[str, int]:
    """Read [schema.]table starting at i; return (table, next index)."""
    name = toks[i].value
    i += 1
    while i + 1 < len(toks) and toks[i].value == ".":
        name = toks[i + 1].value
        i += 2
    return ident(name), i


def parse_schema(text: str) -> Schema:
    sch = Schema()
    for stmt in split_statements(tokenize(text)):
        ups = [t.up for t in stmt]
        if len(stmt) < 3:
            continue
        if ups[0] == "CREATE" and "TABLE" in ups[1:4]:
            _parse_create_table(stmt, ups, sch)
        elif ups[0] == "CREATE" and "INDEX" in ups[1:4]:
            _parse_create_index(stmt, ups, sch)
        elif ups[0] == "ALTER" and ups[1] == "TABLE":
            _parse_alter_table(stmt, ups, sch)
    return sch


def _parse_create_table(stmt: list[Tok], ups: list[str], sch: Schema) -> None:
    i = ups.index("TABLE") + 1
    while i < len(stmt) and ups[i] in ("IF", "NOT", "EXISTS", "ONLY"):
        i += 1
    if i >= len(stmt):
        return
    table, i = _table_name(stmt, i)
    if i >= len(stmt) or stmt[i].value != "(":
        return
    items, _ = _paren_items(stmt, i)
    cols = sch.columns.setdefault(table, [])
    for it in items:
        if not it:
            continue
        head = it[0].up
        rest = [t.up for t in it]
        paren = next((k for k, t in enumerate(it) if t.value == "("), None)
        if head in ("PRIMARY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT", "FULLTEXT", "SPATIAL"):
            if paren is None or "FOREIGN" in rest or "CHECK" in rest or "EXCLUDE" in rest:
                continue
            sub, _ = _paren_items(it, paren)
            icols, expr = _index_cols(sub)
            primary = "PRIMARY" in rest
            unique = primary or "UNIQUE" in rest
            name = it[1].value if head in ("KEY", "INDEX") and paren > 1 else ""
            if head == "CONSTRAINT" or head == "UNIQUE" and paren > 1 and rest[1] not in ("KEY", "INDEX"):
                name = it[1].value
            elif head == "UNIQUE" and paren > 2:
                name = it[2].value
            if head in ("FULLTEXT", "SPATIAL"):
                continue
            sch.indexes.append(
                Index(name or f"{table}_{'pkey' if primary else '_'.join(icols)}", table, icols, unique, primary)
            )
            sch.indexes[-1].expression = expr
            continue
        col = ident(it[0].value)
        cols.append(col)
        joined = " ".join(rest)
        if "PRIMARY KEY" in joined:
            sch.indexes.append(Index(f"{table}_pkey", table, [col], True, True))
        elif re.search(r"\bUNIQUE\b", joined):
            sch.indexes.append(Index(f"{table}_{col}_key", table, [col], True))


def _parse_create_index(stmt: list[Tok], ups: list[str], sch: Schema) -> None:
    unique = "UNIQUE" in ups[:3]
    i = ups.index("INDEX") + 1
    while i < len(stmt) and ups[i] in ("CONCURRENTLY", "IF", "NOT", "EXISTS"):
        i += 1
    name = ""
    if ups[i] != "ON":
        name = stmt[i].value
        i += 1
        while i < len(stmt) and stmt[i].value == ".":
            name = stmt[i + 1].value
            i += 2
    if i >= len(stmt) or ups[i] != "ON":
        return
    i += 1
    if ups[i] == "ONLY":
        i += 1
    table, i = _table_name(stmt, i)
    method = "btree"
    if i < len(stmt) and ups[i] == "USING":
        method = stmt[i + 1].value.lower()
        i += 2
    if i >= len(stmt) or stmt[i].value != "(":
        return
    items, end = _paren_items(stmt, i)
    cols, expr = _index_cols(items)
    include: list[str] = []
    rest = ups[end + 1 :]
    if "INCLUDE" in rest:
        k = end + 1 + rest.index("INCLUDE") + 1
        inc_items, _ = _paren_items(stmt, k)
        include, _ = _index_cols(inc_items)
    idx = Index(name or f"{table}_{'_'.join(cols)}_idx", table, cols, unique, False, "WHERE" in rest, expr, method)
    idx.include = include
    sch.indexes.append(idx)


def _parse_alter_table(stmt: list[Tok], ups: list[str], sch: Schema) -> None:
    i = 2
    while i < len(stmt) and ups[i] in ("ONLY", "IF", "EXISTS"):
        i += 1
    table, i = _table_name(stmt, i)
    while i < len(stmt):
        if ups[i] == "ADD":
            j = i + 1
            name = ""
            if j < len(stmt) and ups[j] == "CONSTRAINT":
                name = stmt[j + 1].value
                j += 2
            kind = ups[j] if j < len(stmt) else ""
            if kind in ("PRIMARY", "UNIQUE", "INDEX", "KEY"):
                paren = next((k for k in range(j, len(stmt)) if stmt[k].value == "("), None)
                if paren is None:
                    return
                if not name and kind in ("INDEX", "KEY", "UNIQUE") and paren > j + 1:
                    cand = stmt[paren - 1].value
                    if cand.upper() not in ("KEY", "INDEX", "USING", "BTREE"):
                        name = cand
                items, end = _paren_items(stmt, paren)
                cols, expr = _index_cols(items)
                primary = kind == "PRIMARY"
                default = f"{table}_pkey" if primary else f"{table}_{'_'.join(cols)}_idx"
                idx = Index(name or default, table, cols, primary or kind == "UNIQUE", primary)
                idx.expression = expr
                sch.indexes.append(idx)
                i = end
            elif kind == "COLUMN" or (kind and kind not in ("FOREIGN", "CHECK", "CONSTRAINT")):
                k = j + 1 if kind == "COLUMN" else j
                if k < len(stmt):
                    sch.columns.setdefault(table, []).append(ident(stmt[k].value))
        i += 1


# --------------------------------------------------------------------------- query analysis


@dataclass
class TableUse:
    table: str
    alias: str
    position: int
    eq: list[str] = field(default_factory=list)
    inlist: list[str] = field(default_factory=list)
    join: list[str] = field(default_factory=list)
    range: list[str] = field(default_factory=list)
    sort: list[tuple[str, str]] = field(default_factory=list)
    group: list[str] = field(default_factory=list)
    selected: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


@dataclass
class Suggestion:
    table: str
    columns: list[str]
    include: list[str]
    reasons: list[str]
    sql: str
    covering_sql: str
    status: str  # new, extend, served
    existing: str = ""
    queries: list[str] = field(default_factory=list)
    n_eq: int = 0


def extract_blocks(code: list[Tok]) -> list[list[Tok]]:
    """Return the statement plus every parenthesised SELECT inside it, each flattened."""
    blocks, outer, i = [], [], 0
    while i < len(code):
        t = code[i]
        if t.value == "(" and i + 1 < len(code) and code[i + 1].up in ("SELECT", "WITH"):
            depth, j = 0, i
            while j < len(code):
                if code[j].value == "(":
                    depth += 1
                elif code[j].value == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            blocks += extract_blocks(code[i + 1 : j])
            outer.append(Tok("param", "?", t.line))
            i = j + 1
            continue
        outer.append(t)
        i += 1
    return [outer, *blocks]


def depth_map(code: list[Tok]) -> list[int]:
    d, out = 0, []
    for t in code:
        if t.value == "(":
            out.append(d)
            d += 1
        elif t.value == ")":
            d = max(d - 1, 0)
            out.append(d)
        else:
            out.append(d)
    return out


def analyse_block(code: list[Tok], schema: Schema) -> list[TableUse]:
    if not code:
        return []
    ups = [t.up for t in code]
    depth = depth_map(code)
    uses: dict[str, TableUse] = {}
    order: list[TableUse] = []

    def add_table(i: int) -> int:
        if i >= len(code) or code[i].kind != "word" or ups[i] in KEYWORDS:
            return i
        table, j = _table_name(code, i)
        alias = table
        if j < len(code) and ups[j] == "AS":
            j += 1
        if j < len(code) and code[j].kind == "word" and ups[j] not in KEYWORDS and ups[j] not in JOIN_WORDS:
            alias = ident(code[j].value)
            j += 1
        use = TableUse(table, alias, len(order))
        order.append(use)
        uses[alias] = use
        uses.setdefault(table, use)
        return j

    first = ups[0]
    i = 0
    if first == "UPDATE":
        add_table(1)
    while i < len(code):
        if depth[i] == 0 and ups[i] in ("FROM", "JOIN"):
            j = add_table(i + 1)
            while j < len(code) and code[j].value == "," and depth[j] == 0:
                j = add_table(j + 1)
            i = max(j, i + 1)
            continue
        i += 1
    if not order:
        return []

    def owner(k: int) -> tuple[TableUse | None, str, int]:
        """Resolve a column reference at k. Returns (table use, column, next index)."""
        t = code[k]
        if t.kind != "word" or ups[k] in KEYWORDS or (k + 1 < len(code) and code[k + 1].value == "("):
            return None, "", k + 1
        if k + 2 < len(code) and code[k + 1].value == "." and code[k + 2].kind == "word":
            if k + 4 < len(code) and code[k + 3].value == "." and code[k + 4].kind == "word":
                return uses.get(ident(code[k + 2].value)), ident(code[k + 4].value), k + 5
            return uses.get(ident(t.value)), ident(code[k + 2].value), k + 3
        col = ident(t.value)
        if len(order) == 1:
            return order[0], col, k + 1
        tname = schema.table_of(col, [u.table for u in order])
        return (next((u for u in order if u.table == tname), None) if tname else None), col, k + 1

    def clause_range(start_words: set[str], stop: set[str], begin: int = 0) -> list[tuple[int, int]]:
        spans, k = [], begin
        while k < len(code):
            if depth[k] == 0 and ups[k] in start_words:
                s = k + 1
                if ups[k] in ("ORDER", "GROUP") and s < len(code) and ups[s] == "BY":
                    s += 1
                e = s
                while e < len(code) and not (
                    depth[e] == 0 and (ups[e] in stop or ups[e] in JOIN_WORDS or ups[e] == "ON")
                ):
                    e += 1
                spans.append((s, e))
                k = e
                continue
            k += 1
        return spans

    def conjuncts(s: int, e: int) -> tuple[list[tuple[int, int]], bool]:
        parts, cur_s, k, has_or, between = [], s, s, False, False
        base = depth[s] if s < len(depth) else 0
        while k < e:
            if depth[k] == base and ups[k] == "BETWEEN":
                between = True
            elif depth[k] == base and ups[k] == "AND":
                if between:
                    between = False
                else:
                    parts.append((cur_s, k))
                    cur_s = k + 1
            elif depth[k] == base and ups[k] == "OR":
                has_or = True
            k += 1
        parts.append((cur_s, e))
        return parts, has_or

    def classify(s: int, e: int, join_ctx: bool) -> None:
        if s >= e:
            return
        if code[s].value == "(" and code[e - 1].value == ")":
            inner, has_or = conjuncts(s + 1, e - 1)
            if not has_or:
                for a, b in inner:
                    classify(a, b, join_ctx)
            return
        use, col, k = owner(s)
        if use is None or not col:
            if code[s].kind in ("number", "string", "param") and s + 2 < e and code[s + 1].value == "=":
                use, col, _ = owner(s + 2)
                if use is not None:
                    use.eq.append(col)
            return
        op = ups[k] if k < e else ""
        if op == "=":
            other, ocol, _ = owner(k + 1) if k + 1 < e else (None, "", 0)
            if other is not None and other is not use:
                use.join.append(col)
                other.join.append(ocol)
            elif other is None:
                use.eq.append(col)
        elif op == "IN":
            use.inlist.append(col)
        elif op == "IS" and k + 1 < e and ups[k + 1] == "NULL":
            use.eq.append(col)
        elif op in CMP_RANGE or op == "BETWEEN":
            use.range.append(col)
        elif op == "LIKE" and k + 1 < e and code[k + 1].kind == "string":
            if code[k + 1].value.startswith("'%"):
                use.notes.append(f"{col} LIKE '%...' cannot use a B-tree index")
            else:
                use.range.append(col)
        elif op in ("<>", "!=", "NOT"):
            use.notes.append(f"{col} {op} ... is not index-friendly (low selectivity)")

    for s, e in clause_range({"WHERE"}, STOP_FROM):
        parts, has_or = conjuncts(s, e)
        if has_or:
            order[0].notes.append("top-level OR in WHERE: index each OR branch separately or rewrite as UNION ALL")
            continue
        for a, b in parts:
            classify(a, b, False)
    for s, e in clause_range({"ON"}, STOP_FROM):
        parts, has_or = conjuncts(s, e)
        if not has_or:
            for a, b in parts:
                classify(a, b, True)
    for s, e in clause_range({"ORDER"}, STOP_FROM | {"LIMIT"}):
        k = s
        while k < e:
            use, col, nk = owner(k)
            if use is not None:
                direction = "DESC" if nk < e and ups[nk] == "DESC" else "ASC"
                use.sort.append((col, direction))
                for u in order:
                    if u is not use:
                        u.notes.append("ORDER BY uses another table; sort cannot come from this index")
            else:
                order[0].notes.append("ORDER BY expression cannot be served by a plain index")
            while nk < e and code[nk].value != ",":
                nk += 1
            k = nk + 1
    for s, e in clause_range({"GROUP"}, STOP_FROM):
        k = s
        while k < e:
            use, col, nk = owner(k)
            if use is not None:
                use.group.append(col)
            while nk < e and code[nk].value != ",":
                nk += 1
            k = nk + 1
    # selected columns (for covering)
    sel_end = next((k for k in range(len(code)) if depth[k] == 0 and ups[k] == "FROM"), len(code))
    if first in ("SELECT", "WITH"):
        k = 1
        while k < sel_end:
            if code[k].value == "*" and code[k - 1].value != "(":
                for u in order:
                    u.selected.add("*")
            use, col, nk = owner(k)
            if use is not None and col:
                use.selected.add(col)
            k = max(nk, k + 1)
    else:
        for u in order:
            u.selected.add("*")
    has_limit = "LIMIT" in ups or "FETCH" in ups
    for u in order:
        if has_limit:
            u.notes.append("_limit")
    return order


def _dedupe(cols: list[str]) -> list[str]:
    out = []
    for c in cols:
        if c not in out:
            out.append(c)
    return out


def build_suggestion(use: TableUse, dialect: str, max_covering: int, other_tables: int) -> Suggestion | None:
    eq = _dedupe(use.eq)
    join = [c for c in _dedupe(use.join) if c not in eq]
    if use.position == 0 and other_tables:
        join_first = []  # driving table: join columns are probed on the other side
    else:
        join_first = join
    inlist = [c for c in _dedupe(use.inlist) if c not in eq and c not in join_first]
    reasons = []
    cols = eq + join_first + inlist
    if eq:
        reasons.append("equality on " + ", ".join(eq) + " first (any order among equality columns)")
    if join_first:
        reasons.append("join column " + ", ".join(join_first) + " (looked up once per outer row)")
    if inlist:
        reasons.append("IN list on " + ", ".join(inlist) + " (acts like equality, but breaks sort order after it)")
    sort_cols = [c for c, _d in use.sort if c not in cols]
    sort_from_index = bool(use.sort) and not inlist and not any(n.startswith("ORDER BY uses") for n in use.notes)
    if sort_from_index and sort_cols:
        dirs = {d for _c, d in use.sort}
        mixed = len(dirs) > 1
        for c, d in use.sort:
            if c not in cols:
                cols.append(f"{c} DESC" if mixed and d == "DESC" else c)
        reasons.append(
            "then ORDER BY "
            + ", ".join(sort_cols)
            + (" (with LIMIT: avoids sorting all rows)" if "_limit" in use.notes else "")
            + ("; mixed ASC/DESC needs per-column direction" if mixed else "")
        )
    elif use.group and not use.sort:
        gcols = [c for c in _dedupe(use.group) if c not in cols]
        if gcols:
            cols += gcols
            reasons.append("then GROUP BY " + ", ".join(gcols) + " (rows arrive grouped; no hash/sort)")
    rng = [c for c in _dedupe(use.range) if c not in [x.split()[0] for x in cols]]
    if rng:
        cols.append(rng[0])
        reasons.append(
            f"range on {rng[0]} last (columns after a range cannot be used for seeking)"
            + (f"; other ranges ({', '.join(rng[1:])}) are filtered from the table" if len(rng) > 1 else "")
        )
    if not cols:
        return None
    plain = [c.split()[0] for c in cols]
    include = []
    if "*" not in use.selected:
        include = sorted(c for c in use.selected if c not in plain)
        if len(plain) + len(include) > max_covering:
            include = []
    name = re.sub(r"[^a-z0-9_]", "_", f"{use.table}_{'_'.join(plain)}_idx")[:63]
    col_sql = ", ".join(cols)
    if dialect == "mysql":
        sql = f"ALTER TABLE {use.table} ADD INDEX {name} ({col_sql}), ALGORITHM=INPLACE, LOCK=NONE;"
        cov = (
            f"ALTER TABLE {use.table} ADD INDEX {name} ({col_sql}, {', '.join(include)}), ALGORITHM=INPLACE, LOCK=NONE;"
            if include
            else ""
        )
    else:
        sql = f"CREATE INDEX CONCURRENTLY {name} ON {use.table} ({col_sql});"
        cov = (
            f"CREATE INDEX CONCURRENTLY {name} ON {use.table} ({col_sql}) INCLUDE ({', '.join(include)});"
            if include
            else ""
        )
    if include:
        reasons.append("covering variant adds " + ", ".join(include) + " so the table is not visited")
    return Suggestion(use.table, cols, include, reasons, sql, cov, "new", n_eq=len(eq) + len(join_first))


def served_by(sug: Suggestion, idx: Index, n_eq: int) -> bool:
    if idx.partial or idx.expression or idx.method not in ("btree", ""):
        return False
    want = [c.split()[0] for c in sug.columns]
    have = idx.columns
    if len(have) < len(want):
        return False
    if set(have[:n_eq]) != set(want[:n_eq]):
        return False
    return have[n_eq : len(want)] == want[n_eq:]


def is_prefix_of(idx: Index, sug: Suggestion) -> bool:
    want = [c.split()[0] for c in sug.columns]
    return (
        not idx.partial
        and not idx.expression
        and len(idx.columns) < len(want)
        and want[: len(idx.columns)] == idx.columns
    )


def redundant_indexes(schema: Schema) -> list[dict]:
    out = []
    by_table: dict[str, list[Index]] = {}
    for idx in schema.indexes:
        by_table.setdefault(idx.table, []).append(idx)
    for table, idxs in by_table.items():
        for a in idxs:
            if a.partial or a.expression or a.method != "btree" or not a.columns:
                continue
            others = [b for b in idxs if b is not a and not b.partial and not b.expression and b.method == "btree"]
            dup = next((b for b in others if b.columns == a.columns and b.unique == a.unique), None)
            if dup is not None:
                if a.name > dup.name:
                    out.append(
                        {
                            "table": table,
                            "index": a.name,
                            "kind": "duplicate",
                            "covered_by": dup.name,
                            "detail": f"{a.name} ({', '.join(a.columns)}) duplicates {dup.name}",
                        }
                    )
                    continue
            if a.unique:
                continue
            wider = next(
                (b for b in others if len(a.columns) < len(b.columns) and b.columns[: len(a.columns)] == a.columns),
                None,
            )
            if wider is not None:
                out.append(
                    {
                        "table": table,
                        "index": a.name,
                        "kind": "redundant",
                        "covered_by": wider.name,
                        "detail": f"{a.name} ({', '.join(a.columns)}) is a left prefix of "
                        f"{wider.name} ({', '.join(wider.columns)})",
                    }
                )
    return out


def advise(query_text: str, schema: Schema, dialect: str, max_covering: int, source: str) -> list[dict]:
    results = []
    for stmt in split_statements(tokenize(query_text)):
        if not stmt or stmt[0].up not in ("SELECT", "WITH", "UPDATE", "DELETE"):
            continue
        label = f"{source}:{stmt[0].line}"
        text = " ".join(t.value for t in stmt)
        entry = {"query": label, "sql": text[:200], "tables": []}
        for block in extract_blocks(stmt):
            uses = analyse_block(block, schema)
            for use in uses:
                sug = build_suggestion(use, dialect, max_covering, len(uses) - 1)
                notes = [n for n in use.notes if n != "_limit"]
                item = {"table": use.table, "alias": use.alias, "notes": _dedupe(notes), "suggestion": None}
                if sug:
                    for idx in schema.indexes:
                        if idx.table == use.table and served_by(sug, idx, sug.n_eq):
                            sug.status, sug.existing = "served", idx.name
                            break
                    else:
                        prefixes = [
                            idx
                            for idx in schema.indexes
                            if idx.table == use.table and is_prefix_of(idx, sug) and not idx.unique
                        ]
                        if prefixes:
                            best = max(prefixes, key=lambda x: len(x.columns))
                            sug.status, sug.existing = "extend", best.name
                    sug.queries.append(label)
                    item["suggestion"] = asdict(sug)
                entry["tables"].append(item)
        results.append(entry)
    return results


def consolidate(results: list[dict]) -> list[dict]:
    """Merge suggestions across queries; drop ones that are a left prefix of another."""
    sugs: list[dict] = []
    for r in results:
        for t in r["tables"]:
            s = t["suggestion"]
            if not s or s["status"] == "served":
                continue
            for other in sugs:
                if other["table"] == s["table"] and other["columns"] == s["columns"]:
                    other["queries"] += s["queries"]
                    break
            else:
                sugs.append(json.loads(json.dumps(s)))
    final = []
    for s in sugs:
        cols = [c.split()[0] for c in s["columns"]]
        wider = next(
            (
                o
                for o in sugs
                if o is not s
                and o["table"] == s["table"]
                and len(o["columns"]) > len(cols)
                and [c.split()[0] for c in o["columns"]][: len(cols)] == cols
            ),
            None,
        )
        if wider:
            wider["queries"] = _dedupe(wider["queries"] + s["queries"])
            continue
        final.append(s)
    return final


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _d, names in os.walk(p):
                files += [os.path.join(dirpath, n) for n in sorted(names) if n.endswith(".sql")]
        else:
            files.append(p)
    return files


def render(results: list[dict], final: list[dict], redundant: list[dict]) -> str:
    out = []
    for r in results:
        out.append(f"Query {r['query']}: {r['sql'][:100]}")
        for t in r["tables"]:
            s = t["suggestion"]
            if s is None:
                out.append(f"  {t['table']}: no index-friendly predicates")
            elif s["status"] == "served":
                out.append(f"  {t['table']}: already served by {s['existing']}")
            else:
                verb = f"extend {s['existing']} ->" if s["status"] == "extend" else "suggest"
                out.append(f"  {t['table']}: {verb} ({', '.join(s['columns'])})")
                for why in s["reasons"]:
                    out.append(f"      - {why}")
            for n in t["notes"]:
                out.append(f"      note: {n}")
    out.append("")
    if final:
        out.append(f"Proposed indexes ({len(final)}):")
        for s in final:
            if s["status"] == "extend":
                out.append(f"  -- replaces {s['existing']} (drop it after the new index is valid)")
            out.append(f"  {s['sql']}   -- serves {', '.join(s['queries'])}")
            if s["covering_sql"]:
                out.append(f"  -- covering variant: {s['covering_sql']}")
    else:
        out.append("Proposed indexes: none (existing indexes serve every query)")
    if redundant:
        out.append("Redundant indexes (cost on every write, check usage before dropping):")
        for r in redundant:
            out.append(f"  {r['kind']}: {r['detail']}")
    out.append("Verify every change with EXPLAIN before and after; build on a replica or staging copy first.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Suggest composite/covering indexes from SQL queries and an optional schema (read-only).",
        epilog="Exit codes: 0 = nothing to suggest, 1 = suggestions or redundant indexes, 2 = error.",
    )
    p.add_argument("queries", nargs="+", help=".sql files or directories with the queries, or - for stdin")
    p.add_argument("--schema", action="append", default=[], help="schema dump with CREATE TABLE / CREATE INDEX")
    p.add_argument("--dialect", choices=["postgres", "mysql"], default="postgres", help="DDL to print")
    p.add_argument("--max-covering", type=int, default=6, help="max columns in a covering variant (6)")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    schema = Schema()
    try:
        for s in args.schema:
            with open(s, encoding="utf-8", errors="replace") as fh:
                part = parse_schema(fh.read())
            for t, cols in part.columns.items():
                schema.columns.setdefault(t, []).extend(cols)
            schema.indexes += part.indexes
        results = []
        for path in collect(args.queries):
            if path == "-":
                text, name = sys.stdin.read(), "<stdin>"
            else:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text, name = fh.read(), path
            results += advise(text, schema, args.dialect, args.max_covering, name)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    final = consolidate(results)
    redundant = redundant_indexes(schema)
    if args.json:
        print(json.dumps({"queries": results, "proposed": final, "redundant": redundant}, indent=2))
    else:
        print(render(results, final, redundant))
    return 1 if final or redundant else 0


if __name__ == "__main__":
    sys.exit(main())
