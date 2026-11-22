#!/usr/bin/env python3
"""Check that a database dump is complete and restorable, and verify restore drills.

Inspect a dump (read-only, streams the file; .gz is fine):

  * pg_dump plain SQL (-Fp), pg_dump custom/tar archives (detected, use --toc),
    `pg_restore --list` output (--toc or auto-detected), mysqldump / mariadb-dump
  * completeness: end-of-dump marker, truncated gzip streams, tiny files, file age
  * versions: "Dumped from / by" (pg_dump older than the server is an error),
    MySQL server version
  * contents: tables, tables with and without data, approximate row counts per table
  * restore traps: DEFINER clauses, GTID_PURGED, OWNER TO statements
  * --expected counts.tsv: compare row counts in the dump with counts from production

Verify a restore drill: compare table row counts from the source with counts from the
restored copy (both TSV "table<TAB>rows", produced by the queries in
references/verify-queries.md):

  dump_inspect.py --compare-counts source.tsv restored.tsv

Exit codes: 0 = no findings at or above --fail-on, 1 = findings, 2 = error.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field

SEVERITIES = ["info", "low", "medium", "high"]


@dataclass
class Finding:
    check: str
    severity: str
    title: str
    fix: str = ""


@dataclass
class Report:
    path: str
    format: str = "unknown"
    complete: bool | None = None
    size_bytes: int = 0
    age_hours: float | None = None
    source_version: str = ""
    dump_version: str = ""
    tables: dict[str, int | None] = field(default_factory=dict)
    objects: dict[str, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, severity: str, title: str, fix: str = "") -> None:
        self.findings.append(Finding(check, severity, title, fix))


def open_text(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def sniff(path: str) -> bytes:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head[:2] == b"\x1f\x8b":
        try:
            with gzip.open(path, "rb") as fh:
                return fh.read(8)
        except (OSError, EOFError):
            return b""
    return head


def short(name: str) -> str:
    name = name.strip('`"')
    return name.split(".")[-1].strip('`"')


def inspect_lines(lines, rep: Report) -> None:
    tables = rep.tables
    objects: Counter = Counter()
    in_copy: str | None = None
    copy_rows = 0
    last = ""
    definers = gtid = owners = 0
    toc = False
    mysql_comments = False
    pg_end = False
    for raw in lines:
        line = raw.rstrip("\n")
        if line.strip():
            last = line
            if line.startswith("-- PostgreSQL database dump complete"):
                pg_end = True
            elif not line.startswith("--"):
                pg_end = False
        if in_copy is not None:
            if line == "\\.":
                tables[in_copy] = (tables.get(in_copy) or 0) + copy_rows
                in_copy = None
            else:
                copy_rows += 1
            continue
        if rep.format == "unknown":
            if line.startswith("-- PostgreSQL database dump"):
                rep.format = "pg_dump plain"
            elif re.match(r"^-- (MySQL|MariaDB) dump", line):
                rep.format = "mysqldump"
                mysql_comments = True
            elif re.match(r"^/\*!40\d{3} SET ", line):
                rep.format = "mysqldump"
            elif line.startswith("; Archive created at") or re.match(r"^\d+; \d+ \d+ ", line):
                rep.format = "pg_restore --list"
                toc = True
        m = re.match(r"^-- Dumped from database version (\S+)", line) or re.match(
            r"^;\s+Dumped from database version: (\S+)", line
        )
        if m:
            rep.source_version = m.group(1)
        m = re.match(r"^-- Dumped by pg_dump version (\S+)", line) or re.match(
            r"^;\s+Dumped by pg_dump version: (\S+)", line
        )
        if m:
            rep.dump_version = m.group(1)
        m = re.match(r"^-- Server version\s+(\S+)", line)
        if m:
            rep.source_version = m.group(1)
        m = re.match(r"^-- (?:MySQL|MariaDB) dump \S+\s+Distrib (\S+)", line)
        if m:
            rep.dump_version = m.group(1).rstrip(",")
        if toc or rep.format == "pg_restore --list":
            tm = re.match(
                r"^\d+; \d+ \d+ (TABLE DATA|TABLE|INDEX|CONSTRAINT|FK CONSTRAINT|SEQUENCE|VIEW|FUNCTION) (\S+) (\S+)",
                line,
            )
            if tm:
                kind, _schema, name = tm.groups()
                objects[kind] += 1
                if kind == "TABLE":
                    tables.setdefault(name, None)
                elif kind == "TABLE DATA":
                    tables[name] = -1  # has data, row count unknown in a TOC
            continue
        cm = re.match(r"^COPY (\S+) \(.*\) FROM stdin;", line)
        if cm:
            in_copy = short(cm.group(1))
            tables.setdefault(in_copy, None)
            copy_rows = 0
            continue
        tm = re.match(r"^CREATE TABLE (?:IF NOT EXISTS )?(\S+)", line)
        if tm:
            tables.setdefault(short(tm.group(1)), None)
            objects["TABLE"] += 1
            continue
        im = re.match(r"^INSERT INTO (\S+)", line)
        if im:
            name = short(im.group(1))
            rows = line.count("),(") + 1
            tables[name] = (tables.get(name) or 0) + rows
            continue
        if re.match(r"^CREATE (UNIQUE )?INDEX", line):
            objects["INDEX"] += 1
        if "DEFINER=" in line:
            definers += 1
        if "GTID_PURGED" in line:
            gtid += 1
        if re.search(r"\bOWNER TO\b", line):
            owners += 1
    rep.objects = dict(objects)
    if rep.format == "pg_dump plain":
        rep.complete = pg_end
        if in_copy is not None:
            rep.complete = False
    elif rep.format == "mysqldump":
        rep.complete = bool(re.match(r"^-- Dump completed", last)) if mysql_comments else None
    elif rep.format == "pg_restore --list":
        rep.complete = True if objects else None
    if definers:
        rep.add(
            "definer_clauses",
            "info",
            f"{definers} DEFINER= clauses (views/triggers/routines)",
            "restoring as another user needs SET_USER_ID/SUPER or the same account; mariadb-dump has "
            "--skip-definer, otherwise strip with sed before restore",
        )
    if gtid:
        rep.add(
            "gtid_purged",
            "info",
            "dump sets GTID_PURGED",
            "restoring into a server that already has GTIDs fails; dump with --set-gtid-purged=OFF for copies",
        )
    if owners:
        rep.add(
            "owner_statements",
            "info",
            f"{owners} OWNER TO statements",
            "if the target lacks these roles restore with pg_restore --no-owner --role=<app_role>",
        )


def major(v: str) -> int | None:
    m = re.match(r"(\d+)", v)
    return int(m.group(1)) if m else None


def inspect(path: str, opts) -> Report:
    rep = Report(path)
    st = os.stat(path)
    rep.size_bytes = st.st_size
    rep.age_hours = round((time.time() - st.st_mtime) / 3600, 2)
    head = sniff(path)
    if head.startswith(b"PGDMP"):
        rep.format = "pg_dump custom"
        rep.add(
            "archive_format",
            "info",
            "pg_dump custom-format archive: contents are compressed and binary",
            f"run: pg_restore --list {os.path.basename(path)} > toc.txt  and inspect toc.txt with this script",
        )
    elif head.startswith(b"ustar") or (len(head) == 0 and path.endswith(".tar")):
        rep.format = "pg_dump tar"
    else:
        try:
            with open_text(path) as fh:
                inspect_lines(fh, rep)
        except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
            rep.complete = False
            rep.add(
                "truncated_gzip",
                "high",
                f"gzip stream is truncated or corrupt ({exc})",
                "the backup job was killed or the disk filled; re-run it and check its exit status",
            )
    if rep.size_bytes < opts.min_bytes:
        rep.add(
            "tiny_dump",
            "high",
            f"dump is only {rep.size_bytes} bytes",
            "the dump command probably failed (authentication, permissions); check the job log",
        )
    if rep.complete is False and not any(f.check == "truncated_gzip" for f in rep.findings):
        rep.add(
            "incomplete_dump",
            "high",
            "end-of-dump marker missing: the dump is truncated",
            "re-run the dump; make the backup script fail on errors (set -o pipefail) and alert on it",
        )
    elif rep.complete is None and rep.format == "mysqldump":
        rep.add(
            "completion_unknown",
            "low",
            "cannot confirm completion (dumped with --skip-comments?)",
            "keep comments on, or check the dump command's exit status",
        )
    if opts.max_age_hours is not None and rep.age_hours is not None and rep.age_hours > opts.max_age_hours:
        rep.add(
            "stale_backup",
            "high",
            f"newest dump is {rep.age_hours:.1f} h old (limit {opts.max_age_hours} h)",
            "the scheduled backup is not running; check the timer/cron and its logs",
        )
    if rep.source_version and rep.dump_version and rep.format.startswith("pg"):
        s, d = major(rep.source_version), major(rep.dump_version)
        if s and d and d < s:
            rep.add(
                "pg_dump_older_than_server",
                "high",
                f"pg_dump {rep.dump_version} is older than the server ({rep.source_version})",
                "use the pg_dump from the same or a newer major version as the server",
            )
    empty = sorted(t for t, n in rep.tables.items() if not n)
    if rep.tables and rep.format in ("pg_dump plain", "mysqldump", "pg_restore --list") and empty:
        rep.add(
            "tables_without_data",
            "info",
            f"{len(empty)} of {len(rep.tables)} tables have no data in the dump: {', '.join(empty[:10])}"
            + (" ..." if len(empty) > 10 else ""),
            "fine for empty tables; otherwise check --exclude-table-data / --no-data / --where options",
        )
    if rep.format == "pg_dump plain" and not rep.tables:
        rep.add(
            "no_tables",
            "medium",
            "dump contains no tables",
            "check --schema / --table filters and the database name",
        )
    if opts.expected:
        known = {k: v or 0 for k, v in rep.tables.items() if v != -1}
        compare_counts(read_counts(opts.expected), known, rep, opts.tolerance, "dump")
    return rep


def read_counts(path: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.lower().startswith(("table", "relname", "table_name")):
                continue
            parts = re.split(r"\t|,|\s*\|\s*|\s{2,}", line)
            if len(parts) >= 2 and re.fullmatch(r"-?\d+", parts[-1].strip()):
                counts[short(parts[0].strip())] = int(parts[-1].strip())
    if not counts:
        raise ValueError(f"{path}: no 'table<TAB>rows' lines found")
    return counts


def compare_counts(source: dict[str, int], target: dict[str, int], rep: Report, tolerance: float, what: str) -> None:
    missing = sorted(set(source) - set(target))
    if missing:
        rep.add(
            "missing_tables",
            "high",
            f"{len(missing)} tables missing in the {what}: {', '.join(missing[:10])}",
            "check the dump/restore options and the restore log for errors",
        )
    for t in sorted(set(source) & set(target)):
        s, d = source[t], target[t]
        if s == d:
            continue
        diff = abs(s - d) / max(s, 1)
        if diff > tolerance:
            rep.add(
                "row_count_mismatch",
                "high" if d < s else "medium",
                f"{t}: source {s:,} rows, {what} {d:,} ({'-' if d < s else '+'}{diff:.1%})",
                "counts from a busy table differ slightly; large gaps mean failed COPY/INSERT batches - "
                "check the restore log",
            )


def render(rep: Report) -> str:
    counts = Counter(f.severity for f in rep.findings)
    summary = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "no findings"
    if rep.format == "row-count comparison":
        out = [f"{rep.path}: {rep.format}"]
    else:
        out = [f"{rep.path}: {rep.format}, {rep.size_bytes:,} bytes, {rep.age_hours} h old"]
    if rep.complete is not None:
        out.append(f"  complete: {'yes' if rep.complete else 'NO'}")
    if rep.source_version or rep.dump_version:
        out.append(f"  source version {rep.source_version or '?'}, dump tool {rep.dump_version or '?'}")
    if rep.tables:
        with_data = sum(1 for v in rep.tables.values() if v)
        out.append(f"  tables: {len(rep.tables)} ({with_data} with data)")
        for t, n in sorted(rep.tables.items(), key=lambda x: -(x[1] or 0))[:15]:
            shown = "-" if n is None else ("data" if n == -1 else f"~{n:,}")
            out.append(f"    {t:<40} {shown}")
    out.append(f"  result: {summary}")
    for f in rep.findings:
        out.append(f"{f.severity.upper():<7} {f.check}: {f.title}")
        if f.fix:
            out.append(f"        fix: {f.fix}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Check database dumps for completeness and verify restore drills (read-only).",
        epilog="Exit codes: 0 = clean, 1 = findings at or above --fail-on, 2 = error.",
    )
    p.add_argument("dumps", nargs="*", help="dump files (.sql, .sql.gz, .dump) or pg_restore --list output")
    p.add_argument("--expected", help="TSV 'table<TAB>rows' from the source; compare with rows in the dump")
    p.add_argument("--compare-counts", nargs=2, metavar=("SOURCE_TSV", "RESTORED_TSV"), help="verify a restore")
    p.add_argument("--tolerance", type=float, default=0.0, help="allowed relative row difference (0.01 = 1%%)")
    p.add_argument("--max-age-hours", type=float, help="report dumps older than this")
    p.add_argument("--min-bytes", type=int, default=1024, help="report dumps smaller than this (1024)")
    p.add_argument("--fail-on", choices=SEVERITIES, default="medium")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    if not args.dumps and not args.compare_counts:
        p.error("give a dump file or --compare-counts SOURCE RESTORED")
    reports: list[Report] = []
    try:
        if args.compare_counts:
            rep = Report(f"{args.compare_counts[0]} vs {args.compare_counts[1]}", format="row-count comparison")
            src, dst = (read_counts(x) for x in args.compare_counts)
            rep.tables = dict(dst)
            compare_counts(src, dst, rep, args.tolerance, "restore")
            reports.append(rep)
        for d in args.dumps:
            reports.append(inspect(d, args))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for r in reports:
        r.findings.sort(key=lambda f: -SEVERITIES.index(f.severity))
    if args.json:
        print(json.dumps([asdict(r) for r in reports], indent=2))
    else:
        print("\n\n".join(render(r) for r in reports))
    fail = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) >= fail for r in reports for f in r.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
