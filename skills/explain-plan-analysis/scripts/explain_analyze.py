#!/usr/bin/env python3
"""Analyse PostgreSQL and MySQL/MariaDB query plans and point at the expensive parts.

Accepted input (format is auto-detected, psql decorations such as "QUERY PLAN",
"-----", "(1 row)" and trailing "+" are stripped):

  * PostgreSQL  EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ...   (JSON)
  * PostgreSQL  EXPLAIN (ANALYZE, BUFFERS) ...                (text)
  * MySQL       EXPLAIN FORMAT=JSON ...  / MariaDB ANALYZE FORMAT=JSON ...
  * MySQL 8     EXPLAIN ANALYZE ... / EXPLAIN FORMAT=TREE ... (tree text)

Findings: sequential / full table scans with selective filters, scans repeated inside
nested loops, row misestimates, sorts and hashes spilling to disk, temporary tables and
filesorts, low "filtered" ratios, lossy bitmap scans, heap fetches on index-only scans,
rows thrown away by join filters, missing parallel workers and expensive JIT. Also
lists the nodes with the highest exclusive (self) time.

The script only reads the plan you give it; it never connects to a database.
Exit codes: 0 = no findings at or above --fail-on, 1 = findings, 2 = parse error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field

SEVERITIES = ["info", "low", "medium", "high"]


@dataclass
class Finding:
    check: str
    severity: str
    title: str
    node: str
    fix: str = ""


@dataclass
class Report:
    engine: str
    analyzed: bool
    execution_ms: float | None = None
    planning_ms: float | None = None
    findings: list[Finding] = field(default_factory=list)
    hotspots: list[dict] = field(default_factory=list)


class PlanError(Exception):
    pass


# --------------------------------------------------------------------------- input cleanup


def clean_psql(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s in ("QUERY PLAN", "EXPLAIN") or re.fullmatch(r"-{3,}\+?-*", s) or re.fullmatch(r"\(\d+ rows?\)", s):
            continue
        if line.endswith("+"):
            line = line[:-1].rstrip()
        lines.append(line)
    return "\n".join(lines)


def load_json(text: str):
    t = text.strip()
    if not t or t[0] not in "[{":
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # psql wraps JSON lines with a leading space and '+'; clean_psql already removed '+'
        try:
            return json.loads(re.sub(r"\n\s*", "\n", t))
        except json.JSONDecodeError as exc:
            raise PlanError(f"input looks like JSON but does not parse: {exc}") from exc


# --------------------------------------------------------------------------- PostgreSQL text -> JSON shape

NODE_RE = re.compile(
    r"^(?P<indent>\s*)(?:->\s+)?(?P<label>\S.*?)\s+"
    r"\(cost=(?P<sc>[\d.]+)\.\.(?P<tc>[\d.]+) rows=(?P<rows>[\d.]+) width=(?P<w>\d+)\)"
    r"(?:\s+\(actual(?: time=(?P<as>[\d.]+)\.\.(?P<at>[\d.]+))? rows=(?P<ar>[\d.]+) loops=(?P<loops>\d+)\)"
    r"|\s+\((?P<never>never executed)\))?"
)
DETAIL_RE = re.compile(r"^\s*(?P<key>[A-Z][A-Za-z ]+?):\s*(?P<val>.*)$")


def _num(v: str) -> float:
    return float(v) if "." in v else int(v)


def parse_pg_text(text: str) -> list:
    root = None
    stack: list[tuple[int, dict]] = []
    top: dict = {}
    current = None
    in_jit = False
    for line in text.splitlines():
        if not line.strip():
            continue
        m = NODE_RE.match(line)
        if m:
            in_jit = False
            label = m.group("label")
            node: dict = {"Plan Rows": _num(m.group("rows")), "Total Cost": float(m.group("tc"))}
            nm = re.match(r"^(?P<type>.+?)(?: using (?P<idx>\S+))?(?: on (?P<rel>\S+)(?: (?P<alias>\S+))?)?$", label)
            node["Node Type"] = nm.group("type") if nm else label
            if nm and nm.group("rel"):
                node["Relation Name"] = nm.group("rel")
            if nm and nm.group("idx"):
                node["Index Name"] = nm.group("idx")
            if m.group("ar") is not None:
                node["Actual Rows"] = _num(m.group("ar"))
                node["Actual Loops"] = int(m.group("loops"))
                if m.group("at") is not None:
                    node["Actual Total Time"] = float(m.group("at"))
            elif m.group("never"):
                node["Actual Rows"] = 0
                node["Actual Loops"] = 0
                node["Actual Total Time"] = 0.0
            indent = len(m.group("indent"))
            if root is None:
                root = node
                stack = [(-1, node)]
            else:
                while len(stack) > 1 and stack[-1][0] >= indent:
                    stack.pop()
                stack[-1][1].setdefault("Plans", []).append(node)
                stack.append((indent, node))
            current = node
            continue
        d = DETAIL_RE.match(line)
        if not d:
            continue
        key, val = d.group("key"), d.group("val")
        if key in ("Planning Time", "Planning time"):
            top["Planning Time"] = float(val.split()[0])
        elif key in ("Execution Time", "Execution time", "Total runtime"):
            top["Execution Time"] = float(val.split()[0])
        elif key == "JIT":
            in_jit = True
        elif key == "Timing" and in_jit:
            tm = re.search(r"Total ([\d.]+) ms", val)
            if tm:
                top["JIT"] = {"Timing": {"Total": float(tm.group(1))}}
        elif current is not None:
            _apply_detail(current, key, val)
    if root is None:
        raise PlanError("no plan nodes found (expected lines like 'Seq Scan on t  (cost=... rows=... width=...)')")
    return [{"Plan": root, **top}]


def _apply_detail(node: dict, key: str, val: str) -> None:
    ints = {
        "Rows Removed by Filter",
        "Rows Removed by Join Filter",
        "Rows Removed by Index Recheck",
        "Heap Fetches",
        "Workers Planned",
        "Workers Launched",
    }
    if key in ints:
        node[key] = int(float(val.split(maxsplit=1)[0]))
    elif key == "Sort Method":
        sm = re.match(r"(?P<m>[a-z\- ]+?)\s+(?P<space>Disk|Memory):\s*(?P<kb>\d+)kB", val)
        if sm:
            node["Sort Method"] = sm.group("m").strip()
            node["Sort Space Type"] = sm.group("space")
            node["Sort Space Used"] = int(sm.group("kb"))
        else:
            node["Sort Method"] = val.strip()
    elif key == "Buckets":
        bm = re.search(r"Batches: (\d+)(?: \(originally (\d+)\))?", val)
        if bm:
            node["Hash Batches"] = int(bm.group(1))
    elif key == "Heap Blocks":
        lm = re.search(r"lossy=(\d+)", val)
        if lm:
            node["Lossy Heap Blocks"] = int(lm.group(1))
    elif key == "Buffers":
        tw = re.search(r"temp(?: read=\d+)? written=(\d+)", val)
        if tw:
            node["Temp Written Blocks"] = int(tw.group(1))
        sr = re.search(r"shared(?: hit=\d+)? read=(\d+)", val)
        if sr:
            node["Shared Read Blocks"] = int(sr.group(1))
    elif key in ("Filter", "Index Cond", "Join Filter", "Hash Cond", "Sort Key", "Recheck Cond"):
        node[key] = val.strip()


# --------------------------------------------------------------------------- PostgreSQL analysis


def node_label(n: dict) -> str:
    s = n.get("Node Type", "?")
    if n.get("Index Name"):
        s += f" using {n['Index Name']}"
    if n.get("Relation Name"):
        s += f" on {n['Relation Name']}"
    return s


def analyse_pg(doc, opts) -> Report:
    if isinstance(doc, list):
        if not doc or not isinstance(doc[0], dict) or "Plan" not in doc[0]:
            raise PlanError("PostgreSQL JSON plan must be a list whose first item has a 'Plan' key")
        top = doc[0]
    elif isinstance(doc, dict) and "Plan" in doc:
        top = doc
    else:
        raise PlanError("unrecognised JSON plan")
    root = top["Plan"]
    analyzed = "Actual Rows" in root
    rep = Report("postgresql", analyzed, top.get("Execution Time"), top.get("Planning Time"))
    nodes: list[tuple[dict, float]] = []

    def walk(n: dict, parent: dict | None, ancestors: tuple[str, ...]) -> None:
        loops = n.get("Actual Loops", 1) or 0
        total = n.get("Actual Total Time", 0.0) * loops
        child_total = sum(c.get("Actual Total Time", 0.0) * (c.get("Actual Loops", 1) or 0) for c in n.get("Plans", []))
        nodes.append((n, max(total - child_total, 0.0)))
        check_pg_node(n, parent, rep, opts, analyzed)
        n["_ancestors"] = ancestors
        for c in n.get("Plans", []):
            walk(c, n, (*ancestors, n.get("Node Type", "")))

    walk(root, None, ())
    if analyzed:
        report_misestimates(nodes, rep, opts)
    if not analyzed:
        rep.findings.append(
            Finding(
                "no_analyze",
                "info",
                "plan has estimates only (no actual rows or timings)",
                node_label(root),
                "run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) on a copy or inside BEGIN ... ROLLBACK for writes",
            )
        )
    else:
        exec_ms = rep.execution_ms or root.get("Actual Total Time", 0.0)
        for n, self_ms in sorted(nodes, key=lambda x: -x[1])[: opts.top]:
            if self_ms <= 0:
                continue
            rep.hotspots.append(
                {
                    "node": node_label(n),
                    "self_ms": round(self_ms, 3),
                    "pct": round(100 * self_ms / exec_ms, 1) if exec_ms else None,
                    "rows": n.get("Actual Rows"),
                    "loops": n.get("Actual Loops"),
                }
            )
        jit = (top.get("JIT") or {}).get("Timing", {}).get("Total")
        if jit and exec_ms and jit > 50 and jit > 0.1 * exec_ms:
            rep.findings.append(
                Finding(
                    "jit_overhead",
                    "low",
                    f"JIT took {jit:.0f} ms of {exec_ms:.0f} ms",
                    "JIT",
                    "for short OLTP queries set jit = off (or raise jit_above_cost)",
                )
            )
        plan_ms = rep.planning_ms
        if plan_ms and plan_ms > 10 and plan_ms > exec_ms:
            rep.findings.append(
                Finding(
                    "planning_time",
                    "info",
                    f"planning ({plan_ms:.1f} ms) took longer than execution ({exec_ms:.1f} ms)",
                    "planner",
                    "use prepared statements; check for many partitions or joins (join_collapse_limit)",
                )
            )
    return rep


def misestimate_of(n: dict, opts) -> tuple[float, str] | None:
    actual, plan_rows = n.get("Actual Rows"), n.get("Plan Rows")
    if actual is None or plan_rows is None or not n.get("Actual Loops"):
        return None
    hi, lo = max(actual, plan_rows), max(min(actual, plan_rows), 1)
    if hi / lo >= opts.misestimate and hi >= 100:
        return hi / lo, ("under" if actual > plan_rows else "over")
    return None


def report_misestimates(nodes: list[tuple[dict, float]], rep: Report, opts) -> None:
    """Report the deepest misestimated node of each chain (parents inherit the error).

    Overestimates below a Limit (or semi/anti join) are expected: execution stops early.
    """
    for n, _self in nodes:
        mis = misestimate_of(n, opts)
        if mis is None:
            continue
        ratio, direction = mis
        if any(misestimate_of(c, opts) for c in n.get("Plans", [])):
            continue
        if direction == "over" and any(a in ("Limit", "Nested Loop", "Merge Join") for a in n["_ancestors"]):
            continue
        label = node_label(n)
        rep.findings.append(
            Finding(
                "misestimate",
                "high" if ratio >= 1000 else "medium",
                f"{label}: estimated {n['Plan Rows']:,} rows, actual {n['Actual Rows']:,} "
                f"({direction}estimate x{ratio:.0f})",
                label,
                "ANALYZE the table; raise the column statistics target; for correlated columns "
                "CREATE STATISTICS (dependencies, ndistinct)",
            )
        )
    for n, _self in nodes:
        n.pop("_ancestors", None)


def check_pg_node(n: dict, parent: dict | None, rep: Report, opts, analyzed: bool) -> None:
    label = node_label(n)
    ntype = n.get("Node Type", "")
    loops = n.get("Actual Loops", 1) or 1
    actual = n.get("Actual Rows")
    plan_rows = n.get("Plan Rows", 0)
    removed = n.get("Rows Removed by Filter", 0)
    add = rep.findings.append
    if ntype.endswith("Seq Scan"):
        scanned = ((actual or 0) + removed) * loops if analyzed else plan_rows
        if loops >= opts.loop_threshold and analyzed:
            add(
                Finding(
                    "seq_scan_in_loop",
                    "high",
                    f"{label} runs {loops} times (once per outer row)",
                    label,
                    "index the join/filter column of this table: "
                    + (n.get("Filter") or "see the join condition of the parent node"),
                )
            )
        elif scanned >= opts.rows_threshold:
            selective = analyzed and removed > 10 * max(actual or 0, 1)
            add(
                Finding(
                    "seq_scan",
                    "medium" if selective else "low",
                    f"{label} reads {int(scanned):,} rows"
                    + (f", filter keeps {int(actual or 0) * loops:,}" if analyzed and removed else ""),
                    label,
                    (f"selective filter - index candidate: {n.get('Filter')}" if selective else "")
                    or "fine for small tables or when most rows are needed; otherwise index the filter columns",
                )
            )
    if n.get("Sort Space Type") == "Disk" or "external" in str(n.get("Sort Method", "")):
        add(
            Finding(
                "sort_spill",
                "medium",
                f"{label} spilled to disk ({n.get('Sort Method', 'external')}, {n.get('Sort Space Used', '?')} kB)",
                label,
                "index the ORDER BY columns, add LIMIT, or raise work_mem for this query (SET LOCAL work_mem = '64MB')",
            )
        )
    if (n.get("Hash Batches") or 1) > 1:
        add(
            Finding(
                "hash_spill",
                "medium",
                f"{label} used {n['Hash Batches']} batches (hash table spilled to disk)",
                label,
                "raise work_mem / hash_mem_multiplier for this query or reduce the hashed input",
            )
        )
    if n.get("Lossy Heap Blocks"):
        add(
            Finding(
                "lossy_bitmap",
                "low",
                f"{label}: {n['Lossy Heap Blocks']} lossy heap blocks (rows rechecked)",
                label,
                "raise work_mem so the bitmap stays exact",
            )
        )
    if ntype == "Index Only Scan" and analyzed and (n.get("Heap Fetches") or 0) > max((actual or 0) * loops, 1) * 0.2:
        add(
            Finding(
                "heap_fetches",
                "low",
                f"{label}: {n['Heap Fetches']:,} heap fetches - the visibility map is stale",
                label,
                "VACUUM the table (check autovacuum settings for it)",
            )
        )
    jf = n.get("Rows Removed by Join Filter", 0)
    if analyzed and jf * loops >= opts.rows_threshold:
        add(
            Finding(
                "join_filter",
                "medium",
                f"{label} discarded {jf * loops:,} row combinations in its join filter",
                label,
                "turn the join filter into an indexable join condition: " + str(n.get("Join Filter", "")),
            )
        )
    wp, wl = n.get("Workers Planned"), n.get("Workers Launched")
    if wp and wl is not None and wl < wp:
        add(
            Finding(
                "workers_not_launched",
                "low",
                f"{label}: {wl} of {wp} parallel workers launched",
                label,
                "max_parallel_workers / max_worker_processes are exhausted at this time",
            )
        )
    if (n.get("Temp Written Blocks") or 0) > 0 and not ntype.startswith(("Sort", "Hash")):
        add(
            Finding(
                "temp_io",
                "low",
                f"{label} wrote {n['Temp Written Blocks']:,} temp blocks",
                label,
                "raise work_mem for this query or reduce intermediate result size",
            )
        )


# --------------------------------------------------------------------------- MySQL / MariaDB JSON


def analyse_mysql_json(doc: dict, opts) -> Report:
    rep = Report("mysql", False)
    tables: list[dict] = []

    def walk(obj, depth_in_loop: int, dependent: bool) -> None:
        if isinstance(obj, dict):
            if obj.get("using_filesort") is True or isinstance(obj.get("filesort"), dict):
                rows = _rows_under(obj)
                rep.findings.append(
                    Finding(
                        "filesort",
                        "medium" if rows >= opts.rows_threshold else "low",
                        f"filesort over ~{rows:,} rows",
                        "ORDER BY / GROUP BY",
                        "add an index whose column order matches the WHERE equality columns "
                        "followed by the ORDER BY columns",
                    )
                )
            if obj.get("using_temporary_table") is True or isinstance(obj.get("temporary_table"), dict):
                rep.findings.append(
                    Finding(
                        "temporary_table",
                        "medium",
                        "query builds a temporary table (GROUP BY / DISTINCT / UNION)",
                        "temporary table",
                        "index the GROUP BY columns, or avoid DISTINCT over joins",
                    )
                )
            if obj.get("dependent") is True:
                dependent = True
                rep.findings.append(
                    Finding(
                        "dependent_subquery",
                        "medium",
                        "dependent (correlated) subquery runs once per outer row",
                        f"select #{obj.get('query_block', {}).get('select_id', '?')}",
                        "rewrite as a JOIN or EXISTS, or make sure the correlated column is indexed",
                    )
                )
            for k, v in obj.items():
                if k == "table" and isinstance(v, dict) and "table_name" in v:
                    tables.append({"t": v, "pos": depth_in_loop, "dependent": dependent})
                    walk({kk: vv for kk, vv in v.items() if kk != "table_name"}, depth_in_loop, dependent)
                elif k == "nested_loop" and isinstance(v, list):
                    for pos, item in enumerate(v):
                        walk(item, pos, dependent)
                else:
                    walk(v, depth_in_loop, dependent)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth_in_loop, dependent)

    walk(doc, 0, False)
    for entry in tables:
        check_mysql_table(entry["t"], entry["pos"], rep, opts)
        if "r_rows" in entry["t"]:
            rep.analyzed = True
    return rep


def _rows_under(obj) -> int:
    best = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("rows_examined_per_scan", "rows") and isinstance(v, (int, float)):
                best = max(best, int(v))
            else:
                best = max(best, _rows_under(v))
    elif isinstance(obj, list):
        for v in obj:
            best = max(best, _rows_under(v))
    return best


def check_mysql_table(t: dict, pos: int, rep: Report, opts) -> None:
    name = t.get("table_name", "?")
    access = str(t.get("access_type", "")).upper()
    rows = t.get("rows_examined_per_scan", t.get("rows", 0)) or 0
    filtered = float(t.get("filtered", 100) or 100)
    label = f"table {name} (access_type={access.lower() or '?'})"
    join_buffer = t.get("using_join_buffer") or t.get("block-nl-join")
    add = rep.findings.append
    if access == "ALL" and rows >= opts.rows_threshold:
        inner = pos > 0
        add(
            Finding(
                "full_scan",
                "high" if inner and not join_buffer else "medium",
                f"full table scan of {name}: ~{int(rows):,} rows per scan"
                + (" (repeated for each row of the previous table)" if inner else "")
                + ("" if t.get("possible_keys") else "; no usable index (possible_keys is NULL)"),
                label,
                "index the columns in: " + str(t.get("attached_condition", "the WHERE / join condition")),
            )
        )
    elif access == "INDEX" and rows >= opts.rows_threshold and not t.get("using_index"):
        add(
            Finding(
                "full_index_scan",
                "low",
                f"full index scan of {name} via {t.get('key')} (~{int(rows):,} rows)",
                label,
                "the index is read end to end; usually for ORDER BY without a selective WHERE",
            )
        )
    if filtered < opts.filtered and rows >= opts.rows_threshold and access != "ALL":
        add(
            Finding(
                "low_filtered",
                "medium",
                f"{name}: only {filtered:g}% of ~{int(rows):,} examined rows survive the condition",
                label,
                "extend the index with the columns in: " + str(t.get("attached_condition", "?")),
            )
        )
    if "r_rows" in t and rows:
        actual = float(t["r_rows"])
        hi, lo = max(actual, rows), max(min(actual, rows), 1)
        if hi / lo >= opts.misestimate and hi >= 100:
            add(
                Finding(
                    "misestimate",
                    "medium",
                    f"{name}: estimated {int(rows):,} rows, actual {actual:,.0f}",
                    label,
                    "ANALYZE TABLE; consider histograms (ANALYZE TABLE t UPDATE HISTOGRAM ON col)",
                )
            )


# --------------------------------------------------------------------------- MySQL 8 tree (EXPLAIN ANALYZE)

TREE_RE = re.compile(
    r"^(?P<indent>\s*)->\s*(?P<label>.+?)\s*(?:\(cost=[\d.e+]+(?:\.\.[\d.e+]+)? rows=(?P<rows>[\d.e+]+)\))?"
    r"\s*(?:\(actual time=(?P<as>[\d.]+)\.\.(?P<at>[\d.]+) rows=(?P<ar>[\d.e+]+) loops=(?P<loops>\d+)\))?\s*$"
)


def analyse_mysql_tree(text: str, opts) -> Report:
    rep = Report("mysql", False)
    nodes = []
    for line in text.splitlines():
        m = TREE_RE.match(re.sub(r"^\s*EXPLAIN:\s*", "", line))
        if m:
            nodes.append(m)
    if not nodes:
        raise PlanError("no '-> ...' plan lines found")
    timed = []
    for m in nodes:
        label = m.group("label")
        est = float(m.group("rows")) if m.group("rows") else None
        actual = float(m.group("ar")) if m.group("ar") else None
        loops = int(m.group("loops")) if m.group("loops") else 1
        if actual is not None:
            rep.analyzed = True
            timed.append((label, float(m.group("at")) * loops, actual, loops))
        rows = (actual if actual is not None else est or 0) * loops
        if label.startswith("Table scan on"):
            if loops >= opts.loop_threshold and actual is not None:
                rep.findings.append(
                    Finding(
                        "full_scan_in_loop",
                        "high",
                        f"{label} runs {loops} times",
                        label,
                        "index the join column of this table",
                    )
                )
            elif rows >= opts.rows_threshold:
                rep.findings.append(
                    Finding(
                        "full_scan",
                        "medium",
                        f"{label}: ~{int(rows):,} rows",
                        label,
                        "index the filter columns",
                    )
                )
        if label.startswith("Sort") and "using index" not in label and rows >= opts.rows_threshold:
            rep.findings.append(
                Finding(
                    "filesort",
                    "medium",
                    f"{label} over ~{int(rows):,} rows",
                    label,
                    "index the ORDER BY columns",
                )
            )
        if "temporary" in label.lower():
            rep.findings.append(
                Finding("temporary_table", "medium", label, label, "index the GROUP BY / DISTINCT columns")
            )
        if est is not None and actual is not None:
            hi, lo = max(actual, est), max(min(actual, est), 1)
            if hi / lo >= opts.misestimate and hi >= 100:
                rep.findings.append(
                    Finding(
                        "misestimate",
                        "medium",
                        f"{label}: estimated {est:,.0f} rows, actual {actual:,.0f}",
                        label,
                        "ANALYZE TABLE; add histograms for skewed columns",
                    )
                )
    for label, total, actual, loops in sorted(timed, key=lambda x: -x[1])[: opts.top]:
        rep.hotspots.append({"node": label, "total_ms": round(total, 3), "rows": actual, "loops": loops})
    return rep


# --------------------------------------------------------------------------- driver


def analyse(text: str, opts) -> Report:
    text = clean_psql(text)
    doc = load_json(text)
    if doc is not None:
        if isinstance(doc, dict) and "query_block" in doc:
            return analyse_mysql_json(doc, opts)
        return analyse_pg(doc, opts)
    if re.search(r"^\s*->\s.*\(cost=[\d.e+]+(?:\.\.[\d.e+]+)? rows=[\d.e+]+\)\s*(?:\(actual|$)", text, re.M) and (
        "width=" not in text
    ):
        return analyse_mysql_tree(text, opts)
    if "(cost=" in text and "width=" in text:
        return analyse_pg(parse_pg_text(text), opts)
    raise PlanError("unrecognised plan format; see --help for supported EXPLAIN variants")


def render(rep: Report) -> str:
    out = []
    counts = Counter(f.severity for f in rep.findings)
    summary = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "no findings"
    timing = ""
    if rep.execution_ms is not None:
        timing = f", execution {rep.execution_ms:.1f} ms"
    if rep.planning_ms is not None:
        timing += f", planning {rep.planning_ms:.1f} ms"
    out.append(f"{rep.engine} plan ({'ANALYZE' if rep.analyzed else 'estimates only'}{timing}): {summary}")
    if rep.hotspots:
        out.append("Hotspots:")
        for h in rep.hotspots:
            ms = h.get("self_ms", h.get("total_ms"))
            pct = f" ({h['pct']}%)" if h.get("pct") is not None else ""
            out.append(f"  {ms:>10.2f} ms{pct}  {h['node']}  rows={h['rows']} loops={h['loops']}")
    for f in rep.findings:
        out.append(f"{f.severity.upper():<7} {f.check}: {f.title}")
        if f.fix:
            out.append(f"        fix: {f.fix}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Analyse PostgreSQL / MySQL EXPLAIN output (JSON, PG text, MySQL tree). Read-only.",
        epilog="Exit codes: 0 = clean, 1 = findings at or above --fail-on, 2 = parse error.",
    )
    p.add_argument("path", help="file with the EXPLAIN output, or - for stdin")
    p.add_argument("--rows-threshold", type=int, default=10000, help="rows before a scan is reported (10000)")
    p.add_argument("--loop-threshold", type=int, default=1000, help="loops before a repeated scan is high (1000)")
    p.add_argument("--misestimate", type=float, default=10.0, help="estimate/actual ratio to report (10)")
    p.add_argument("--filtered", type=float, default=10.0, help="MySQL: report filtered%% below this (10)")
    p.add_argument("--top", type=int, default=5, help="number of hotspot nodes to list (5)")
    p.add_argument("--fail-on", choices=SEVERITIES, default="medium")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    try:
        if args.path == "-":
            text = sys.stdin.read()
        else:
            with open(args.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        rep = analyse(text, args)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rep.findings.sort(key=lambda f: -SEVERITIES.index(f.severity))
    if args.json:
        print(json.dumps(asdict(rep), indent=2))
    else:
        print(render(rep))
    fail = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) >= fail for f in rep.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
