#!/usr/bin/env python3
"""Summarise slow queries from MySQL/MariaDB slow logs, PostgreSQL logs or pg_stat_statements.

Inputs (auto-detected, or force with --format):

  mysql   slow query log (slow_query_log_file); .gz files are read transparently
  pglog   PostgreSQL server log with log_min_duration_statement lines
          ("duration: 1234.5 ms  statement: ..."); multi-line statements are joined
  pgss    CSV export of pg_stat_statements, e.g.
          \\copy (SELECT * FROM pg_stat_statements) TO 'pgss.csv' CSV HEADER

Queries are grouped by fingerprint (literals -> ?, IN lists collapsed, whitespace
normalised) and ranked by total time. Each group gets hints: rows examined per row
sent (missing index), lock time share, temp spills and cache misses (pgss), and
very frequent cheap queries (N+1 / missing cache).

The script only reads files; query text in the output is normalised, so literal
values (emails, tokens) from the log are not printed unless --samples is given.
Exit codes: 0 = ok, 1 = a group's mean time exceeds --fail-over-ms, 2 = error.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import re
import sys
from dataclasses import dataclass, field

KEYWORDS = set(
    """
    SELECT FROM WHERE AND OR NOT IN IS NULL JOIN LEFT RIGHT INNER OUTER ON GROUP BY
    ORDER LIMIT OFFSET HAVING INSERT INTO VALUES UPDATE SET DELETE AS DISTINCT UNION ALL
    EXISTS BETWEEN LIKE ILIKE CASE WHEN THEN ELSE END ASC DESC WITH RETURNING FOR COUNT
    SUM MIN MAX AVG CROSS USING NULLS FIRST LAST TRUE FALSE REPLACE ANY
    """.split()
)
TOKEN_RE = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))"
    r"|(?P<string>[EeNn]?'(?:[^'\\]|''|\\.)*')"
    r"|(?P<qident>`(?:[^`]|``)*`|\"(?:[^\"]|\"\")*\")"
    r"|(?P<number>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|0x[0-9a-fA-F]+)"
    r"|(?P<param>\$\d+|\?|%s|:[A-Za-z_]\w*)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<op>.)",
    re.S,
)


def fingerprint(sql: str) -> str:
    parts = []
    for m in TOKEN_RE.finditer(sql):
        kind, v = m.lastgroup, m.group(0)
        if kind in ("ws", "comment"):
            continue
        if kind in ("string", "number", "param"):
            parts.append("?")
        elif kind == "word":
            parts.append(v.upper() if v.upper() in KEYWORDS else v.lower())
        elif kind == "qident":
            parts.append(v[1:-1].lower())
        else:
            parts.append(v)
    s = " ".join(parts)
    s = re.sub(r"\(\s*\?(?:\s*,\s*\?)*\s*\)", "(?+)", s)
    s = re.sub(r"(\(\?\+\))(?:\s*,\s*\(\?\+\))+", r"\1, ...", s)
    s = re.sub(r"\s*([(),.;])\s*", lambda m: {"(": " (", ")": ") ", ",": ", ", ".": ".", ";": ""}[m.group(1)], s)
    s = re.sub(r"\s+([,)])", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Event:
    sql: str
    ms: float
    lock_ms: float = 0.0
    rows_sent: int = 0
    rows_examined: int = 0
    db: str = ""
    user: str = ""
    time: str = ""


@dataclass
class Group:
    fingerprint: str
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    lock_ms: float = 0.0
    rows_sent: int = 0
    rows_examined: int = 0
    durations: list[float] = field(default_factory=list)
    dbs: set[str] = field(default_factory=set)
    users: set[str] = field(default_factory=set)
    sample: str = ""
    # pg_stat_statements extras
    shared_hit: int = 0
    shared_read: int = 0
    temp_written: int = 0
    hints: list[str] = field(default_factory=list)

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def p95(self) -> float | None:
        if not self.durations:
            return None
        d = sorted(self.durations)
        return d[min(len(d) - 1, max(0, math.ceil(0.95 * len(d)) - 1))]


# --------------------------------------------------------------------------- parsers

MYSQL_STATS = re.compile(
    r"#\s*Query_time:\s*([\d.]+)\s+Lock_time:\s*([\d.]+)\s+Rows_sent:\s*(\d+)\s+Rows_examined:\s*(\d+)"
)


def parse_mysql(text: str) -> list[Event]:
    events: list[Event] = []
    cur: Event | None = None
    lines: list[str] = []
    db = ""
    user = ""
    when = ""

    def flush() -> None:
        nonlocal cur, lines
        if cur is not None:
            stmts = [ln for ln in lines if not re.match(r"^(SET timestamp=\d+|use \S+);?\s*$", ln.strip(), re.I)]
            cur.sql = "\n".join(stmts).strip()
            if cur.sql:
                events.append(cur)
        cur, lines = None, []

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("# Time:"):
            flush()
            when = line.split(":", 1)[1].strip()
            continue
        if line.startswith("# User@Host:"):
            flush()
            m = re.match(r"# User@Host:\s*(\S+?)\[", line)
            user = m.group(1) if m else ""
            continue
        m = MYSQL_STATS.search(line)
        if m:
            flush()
            cur = Event(
                "",
                float(m.group(1)) * 1000,
                float(m.group(2)) * 1000,
                int(m.group(3)),
                int(m.group(4)),
                db,
                user,
                when,
            )
            continue
        sm = re.search(r"Schema:\s*(\S+)", line)
        if line.startswith("#"):
            if sm:
                db = sm.group(1)
            continue
        um = re.match(r"^use (\S+?);?\s*$", line.strip(), re.I)
        if um:
            db = um.group(1).strip("`")
            if cur is not None:
                cur.db = db
            continue
        if re.match(r"^(/\S+mysqld|Tcp port:|Time\s+Id\s+Command)", line):
            continue
        if cur is not None:
            lines.append(line)
    flush()
    return events


PG_ENTRY = re.compile(r"duration:\s*([\d.]+)\s*ms\s+(?:statement|execute [^:]*|parse [^:]*|bind [^:]*):\s?(.*)$")
PG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d")
PG_USERDB = re.compile(r"(\w+)@(\w+)")


def parse_pglog(text: str) -> list[Event]:
    events: list[Event] = []
    cur: Event | None = None
    for line in text.splitlines():
        m = PG_ENTRY.search(line)
        if m:
            if cur is not None:
                events.append(cur)
            prefix = line[: m.start()]
            ud = PG_USERDB.search(prefix)
            ts = PG_PREFIX.match(line)
            cur = Event(
                m.group(2),
                float(m.group(1)),
                user=ud.group(1) if ud else "",
                db=ud.group(2) if ud else "",
                time=ts.group(0) if ts else "",
            )
            continue
        if PG_PREFIX.match(line):
            if cur is not None:
                events.append(cur)
                cur = None
            continue
        if cur is not None and (line.startswith(("\t", " "))):
            cur.sql += "\n" + line.strip()
    if cur is not None:
        events.append(cur)
    return [e for e in events if e.sql.strip()]


def parse_pgss(text: str) -> list[Group]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "query" not in reader.fieldnames or "calls" not in reader.fieldnames:
        raise ValueError("pg_stat_statements CSV needs at least 'query' and 'calls' columns")
    total_col = "total_exec_time" if "total_exec_time" in reader.fieldnames else "total_time"
    max_col = "max_exec_time" if "max_exec_time" in reader.fieldnames else "max_time"
    groups: dict[str, Group] = {}

    def num(row: dict, key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except ValueError:
            return 0.0

    for row in reader:
        fp = fingerprint(row["query"] or "")
        g = groups.setdefault(fp, Group(fp))
        g.count += int(num(row, "calls"))
        g.total_ms += num(row, total_col)
        g.max_ms = max(g.max_ms, num(row, max_col))
        g.rows_sent += int(num(row, "rows"))
        g.shared_hit += int(num(row, "shared_blks_hit"))
        g.shared_read += int(num(row, "shared_blks_read"))
        g.temp_written += int(num(row, "temp_blks_written"))
        if row.get("datname"):
            g.dbs.add(row["datname"])
        if row.get("usename") or row.get("rolname"):
            g.users.add(row.get("usename") or row.get("rolname") or "")
        g.sample = g.sample or (row["query"] or "")[:500]
    return list(groups.values())


def group_events(events: list[Event]) -> list[Group]:
    groups: dict[str, Group] = {}
    for e in events:
        fp = fingerprint(e.sql)
        g = groups.setdefault(fp, Group(fp))
        g.count += 1
        g.total_ms += e.ms
        g.max_ms = max(g.max_ms, e.ms)
        g.lock_ms += e.lock_ms
        g.rows_sent += e.rows_sent
        g.rows_examined += e.rows_examined
        g.durations.append(e.ms)
        if e.db:
            g.dbs.add(e.db)
        if e.user:
            g.users.add(e.user)
        if len(e.sql) > len(g.sample):
            g.sample = e.sql[:500]
    return list(groups.values())


def add_hints(groups: list[Group], fmt: str) -> None:
    total = sum(g.total_ms for g in groups) or 1
    for g in groups:
        if g.rows_examined and g.rows_examined >= 10000 and g.rows_examined / max(g.rows_sent, 1) >= 100:
            ratio = g.rows_examined / max(g.rows_sent, 1)
            g.hints.append(
                f"examines {ratio:,.0f} rows per row returned: missing or unused index (run EXPLAIN, see index-advisor)"
            )
        if g.lock_ms and g.lock_ms > 0.3 * g.total_ms:
            g.hints.append(f"{100 * g.lock_ms / g.total_ms:.0f}% of the time is lock wait: look for long transactions")
        if g.temp_written:
            g.hints.append(f"wrote {g.temp_written:,} temp blocks: sorts/hashes spill (work_mem or an index)")
        reads = g.shared_hit + g.shared_read
        if reads >= 10000 and g.shared_read / reads > 0.1:
            g.hints.append(f"cache hit ratio {100 * g.shared_hit / reads:.1f}%: I/O bound")
        if g.count >= 1000 and g.mean_ms < 5 and g.total_ms / total > 0.05:
            g.hints.append(
                f"{g.count:,} calls at {g.mean_ms:.2f} ms: chatty - batch (N+1) or cache instead of tuning the query"
            )
        if fmt != "pgss" and g.count >= 5:
            p95 = g.p95()
            if p95 and g.mean_ms and g.max_ms > 10 * max(g.mean_ms, 0.001) and g.max_ms > 1000:
                g.hints.append(
                    f"max {g.max_ms:,.0f} ms vs mean {g.mean_ms:,.0f} ms: occasional stalls (locks, I/O, plan flips)"
                )


def detect(text: str) -> str:
    head = text[:4096]
    first = head.splitlines()[0] if head else ""
    if "query" in first and "calls" in first and "," in first:
        return "pgss"
    if "# Query_time:" in head or "# User@Host:" in head:
        return "mysql"
    if re.search(r"duration:\s*[\d.]+\s*ms", head):
        return "pglog"
    if "# Query_time:" in text:
        return "mysql"
    if re.search(r"duration:\s*[\d.]+\s*ms\s+(statement|execute)", text):
        return "pglog"
    raise ValueError("unrecognised input; use --format mysql|pglog|pgss")


def read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Summarise MySQL slow logs, PostgreSQL duration logs or pg_stat_statements CSV (read-only).",
        epilog="Exit codes: 0 = ok, 1 = a query group's mean exceeds --fail-over-ms, 2 = error.",
    )
    p.add_argument("paths", nargs="+", help="log or CSV files (.gz ok), or - for stdin")
    p.add_argument("--format", choices=["auto", "mysql", "pglog", "pgss"], default="auto")
    p.add_argument("--top", type=int, default=10, help="groups to show (10)")
    p.add_argument("--sort", choices=["total", "mean", "count", "max", "examined"], default="total")
    p.add_argument("--since", help="only events at or after this time prefix, e.g. 2026-11-20T10:00 (logs only)")
    p.add_argument("--until", help="only events before this time prefix (logs only)")
    p.add_argument("--db", help="only this database")
    p.add_argument("--samples", action="store_true", help="also print one raw sample query per group")
    p.add_argument("--fail-over-ms", type=float, help="exit 1 if any shown group's mean time exceeds this")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    try:
        texts = [read(path) for path in args.paths]
        fmt = args.format if args.format != "auto" else detect(texts[0])
        if fmt == "pgss":
            groups = []
            for t in texts:
                groups += parse_pgss(t)
        else:
            events: list[Event] = []
            for t in texts:
                events += parse_mysql(t) if fmt == "mysql" else parse_pglog(t)

            def norm(ts: str) -> str:
                return ts.replace(" ", "T")

            if args.since:
                events = [e for e in events if not e.time or norm(e.time) >= norm(args.since)]
            if args.until:
                events = [e for e in events if not e.time or norm(e.time) < norm(args.until)]
            if args.db:
                events = [e for e in events if e.db == args.db]
            groups = group_events(events)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.db and fmt == "pgss":
        groups = [g for g in groups if not g.dbs or args.db in g.dbs]
    add_hints(groups, fmt)
    key = {
        "total": lambda g: g.total_ms,
        "mean": lambda g: g.mean_ms,
        "count": lambda g: g.count,
        "max": lambda g: g.max_ms,
        "examined": lambda g: g.rows_examined,
    }[args.sort]
    groups.sort(key=key, reverse=True)
    total = sum(g.total_ms for g in groups)
    shown = groups[: args.top]
    rows = []
    for rank, g in enumerate(shown, 1):
        p95 = g.p95()
        row = {
            "rank": rank,
            "fingerprint": g.fingerprint,
            "count": g.count,
            "total_ms": round(g.total_ms, 3),
            "pct_total": round(100 * g.total_ms / total, 1) if total else 0.0,
            "mean_ms": round(g.mean_ms, 3),
            "p95_ms": round(p95, 3) if p95 is not None else None,
            "max_ms": round(g.max_ms, 3),
            "rows_sent": g.rows_sent,
            "rows_examined": g.rows_examined,
            "databases": sorted(g.dbs),
            "users": sorted(g.users),
            "hints": g.hints,
        }
        if args.samples:
            row["sample"] = g.sample
        rows.append(row)
    if args.json:
        print(
            json.dumps(
                {
                    "format": fmt,
                    "groups": len(groups),
                    "executions": sum(g.count for g in groups),
                    "total_ms": round(total, 3),
                    "top": rows,
                },
                indent=2,
            )
        )
    else:
        print(
            f"{fmt}: {sum(g.count for g in groups):,} executions, {len(groups)} distinct queries, "
            f"{total / 1000:,.1f} s total"
        )
        for r in rows:
            p95 = f" p95 {r['p95_ms']:,.0f} ms" if r["p95_ms"] is not None else ""
            print(
                f"\n#{r['rank']}  {r['pct_total']}% of time  {r['count']:,} calls  "
                f"total {r['total_ms'] / 1000:,.2f} s  "
                f"mean {r['mean_ms']:,.1f} ms{p95}  max {r['max_ms']:,.0f} ms"
            )
            if r["rows_examined"]:
                print(f"    rows sent {r['rows_sent']:,}  examined {r['rows_examined']:,}")
            elif r["rows_sent"]:
                print(f"    rows {r['rows_sent']:,}")
            if r["databases"]:
                print(f"    db: {', '.join(r['databases'])}  user: {', '.join(r['users'])}")
            print(f"    {r['fingerprint'][:300]}")
            for h in r["hints"]:
                print(f"    hint: {h}")
            if args.samples:
                print(f"    sample: {r['sample'][:300]}")
    if args.fail_over_ms is not None and any(r["mean_ms"] > args.fail_over_ms for r in rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
