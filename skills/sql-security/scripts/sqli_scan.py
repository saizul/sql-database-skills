#!/usr/bin/env python3
"""Find SQL built with string interpolation or concatenation in application code.

Languages: Python, JavaScript/TypeScript, PHP, Ruby, Go, Java/Kotlin, C#.

Reported patterns (a SQL keyword inside the string is required):
  * Python f-strings, % formatting, .format() and + concatenation; Django .raw()/.extra()/
    RawSQL, SQLAlchemy text() with interpolation
  * JS/TS template literals with ${...} passed to query/execute/raw, Prisma
    $queryRawUnsafe / $executeRawUnsafe, + concatenation (tagged templates such as
    sql`...` or Prisma $queryRaw`...` are parameterised and NOT reported)
  * PHP "...$var..." / "...{$var}..." and . concatenation, superglobals ($_GET, $_POST)
  * Ruby "...#{x}..." in where / find_by_sql / execute / order, and interpolated SQL
    fragments passed to ORM methods (where, order, whereRaw, orderByRaw, ...) in any language
  * Go fmt.Sprintf("SELECT ... %s/%v") and + concatenation
  * Java / Kotlin / C# + concatenation, String.format, C# $"...{x}..."

Severity: high when the line also reaches an execution sink or request input, medium
otherwise. Interpolated identifiers (ORDER BY / table names) get an allow-list hint.
Suppress a line with a trailing comment containing "sqli-scan: ignore".

Line-based heuristics: review every finding; strings split over many lines can be
missed. The script only reads files. Exit codes: 0 = no findings at or above
--fail-on, 1 = findings, 2 = error.
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
EXT_LANG = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".ts": "js",
    ".tsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".php": "php",
    ".rb": "ruby",
    ".go": "go",
    ".java": "java",
    ".kt": "java",
    ".cs": "csharp",
}
SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "venv",
    ".venv",
    "dist",
    "build",
    "__pycache__",
    ".tox",
    "target",
}
SQL_RE = re.compile(
    r"\b(SELECT\b[^\n]*?\bFROM\b|INSERT\s+INTO\b|UPDATE\s+[\w.`\"\[\]]+\s+SET\b|DELETE\s+FROM\b|"
    r"\bWHERE\b\s+[\w.`\"]+\s*(=|<|>|LIKE|IN\b)|ORDER\s+BY\b|CREATE\s+TABLE\b|DROP\s+TABLE\b)",
    re.I,
)
SINK_RE = re.compile(
    r"\b(execute|executemany|executescript|query|exec|raw|extra|RawSQL|text|find_by_sql|"
    r"select_all|select_rows|executeQuery|executeUpdate|prepareStatement|createStatement|"
    r"createQuery|createNativeQuery|ExecuteReader|ExecuteNonQuery|ExecuteScalar|SqlCommand|"
    r"QueryRow|QueryContext|ExecContext|mysqli_query|mysql_query|pg_query|\$queryRawUnsafe|"
    r"\$executeRawUnsafe|sequelize\.query|knex\.raw|where|order|having|joins|pluck)\s*\(",
    re.I,
)
INPUT_RE = re.compile(
    r"(\$_(GET|POST|REQUEST|COOKIE|SERVER)|\breq\.(query|body|params)|\brequest\.(GET|POST|args|form|json|query_params)"
    r"|\bparams\[|\bparams\.|\bargs\.|\binput\(|\bctx\.(query|params|request)|r\.URL\.Query|FormValue\()"
)
IDENT_RE = re.compile(
    r"(ORDER\s+BY|GROUP\s+BY|FROM|JOIN|INTO|UPDATE|TABLE)\s*[\"'`]?\s*(\{|\$\{|#\{|%s|\"\s*\+|'\s*\+|\$\w)",
    re.I,
)


ORM_FRAGMENT_RE = re.compile(
    r"\.(where|order|reorder|having|joins|group|pluck|select|from|find_by_sql|extra|whereRaw|orderByRaw|"
    r"havingRaw|selectRaw)\(\s*(\[?\s*[fF]?\"|\[?\s*[fF]?'|%Q?\{|`)"
)


@dataclass
class Finding:
    check: str
    severity: str
    file: str
    line: int
    title: str
    code: str
    fix: str


def strings_with_sql(line: str) -> bool:
    return bool(SQL_RE.search(line))


PATTERNS: dict[str, list[tuple[str, re.Pattern, str]]] = {
    "python": [
        ("fstring", re.compile(r"\b[rR]?[fF][rR]?(\"\"\"|\"|'''|')(?=[^\n]*\{)"), "f-string"),
        ("percent_format", re.compile(r"(\"|')\s*%\s*[\(\w]"), "% formatting"),
        ("format_call", re.compile(r"(\"|')\s*\.format\("), ".format()"),
        (
            "concat",
            re.compile(r"(\"|')\s*\+\s*[A-Za-z_(]|[A-Za-z_)\]]\s*\+\s*(\"|')"),
            "string concatenation",
        ),
    ],
    "js": [
        ("unsafe_raw", re.compile(r"\$(queryRawUnsafe|executeRawUnsafe)\s*\("), "Prisma *Unsafe raw query"),
        ("template", re.compile(r"(?<![\w$.])`[^`]*\$\{"), "template literal"),
        (
            "concat",
            re.compile(r"(\"|')\s*\+\s*[A-Za-z_$(]|[A-Za-z_$)\]]\s*\+\s*(\"|')"),
            "string concatenation",
        ),
    ],
    "php": [
        (
            "interpolation",
            re.compile(r"\"[^\"]*(\$[A-Za-z_]|\{\$)"),
            "variable interpolation in a double-quoted string",
        ),
        ("concat", re.compile(r"(\"|')\s*\.\s*\$|\$[\w\]\[\'\"]+\s*\.\s*(\"|')"), "string concatenation"),
    ],
    "ruby": [
        ("interpolation", re.compile(r"\"[^\"]*#\{|%Q?\{[^}]*#\{"), "#{} interpolation"),
        ("concat", re.compile(r"(\"|')\s*\+\s*[A-Za-z_@(]"), "string concatenation"),
    ],
    "go": [
        ("sprintf", re.compile(r"fmt\.Sprintf\(\s*(`|\")[^`\"]*%[sv]"), "fmt.Sprintf"),
        (
            "concat",
            re.compile(r"(\"|`)\s*\+\s*[A-Za-z_(]|[A-Za-z_)\]]\s*\+\s*(\"|`)"),
            "string concatenation",
        ),
    ],
    "java": [
        ("concat", re.compile(r"\"\s*\+\s*[A-Za-z_(]|[A-Za-z_)\]]\s*\+\s*\""), "string concatenation"),
        ("format", re.compile(r"String\.format\(\s*\""), "String.format"),
        ("template", re.compile(r"\"[^\"]*\$\{?[A-Za-z_]"), "string template"),
    ],
    "csharp": [
        ("interpolation", re.compile(r"\$@?\"[^\"]*\{"), "interpolated string"),
        ("concat", re.compile(r"\"\s*\+\s*[A-Za-z_(]|[A-Za-z_)\]]\s*\+\s*\""), "string concatenation"),
        ("format", re.compile(r"String\.Format\(\s*\""), "String.Format"),
    ],
}
FIXES = {
    "python": 'pass values as parameters: cur.execute("... WHERE id = %s", (user_id,)) '
    '(sqlite3: ?, SQLAlchemy: text("... :id").bindparams(id=...))',
    "js": "use placeholders: db.query('... WHERE id = $1', [id]) / mysql2 '?' / a tagged template "
    "sql`... ${id}` / Prisma $queryRaw`...`",
    "php": "use PDO/mysqli prepared statements: $stmt = $pdo->prepare('... WHERE id = ?'); $stmt->execute([$id]);",
    "ruby": 'use bind params: where("name = ?", name) / where(name: name); sanitize_sql_array for raw SQL',
    "go": 'use placeholders: db.QueryContext(ctx, "... WHERE id = $1", id) (MySQL driver: ?)',
    "java": "use PreparedStatement with ? placeholders (JPA: setParameter)",
    "csharp": 'use SqlCommand parameters (cmd.Parameters.AddWithValue("@id", id)) or Dapper/EF parameters',
}


def scan_file(path: str, lang: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for n, line in enumerate(lines, 1):
        if "sqli-scan: ignore" in line or len(line) > 2000:
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*", "/*", "--")) and lang != "php":
            continue
        orm_fragment = bool(ORM_FRAGMENT_RE.search(line))
        if not orm_fragment and not strings_with_sql(line):
            continue
        window = " ".join(lines[n - 1 : n + 1])
        for check, pat, label in PATTERNS[lang]:
            if not pat.search(line):
                continue
            if lang == "js" and check == "template" and re.search(r"(\bsql|\$queryRaw|\$executeRaw|SQL)\s*`", line):
                continue
            if check == "concat" and not re.search(r"[A-Za-z_$@)\]]", re.sub(r"(\"[^\"]*\"|'[^']*')", "", line)):
                continue
            sink = bool(SINK_RE.search(window)) or check == "unsafe_raw"
            user = bool(INPUT_RE.search(line))
            severity = "high" if sink or user else "medium"
            title = f"SQL built with {label}" + (" from request input" if user else "")
            fix = FIXES[lang]
            if IDENT_RE.search(line):
                fix += (
                    "; identifiers (column/table names, ASC/DESC) cannot be parameters: map them through an allow-list"
                )
            out.append(Finding(f"sqli_{check}", severity, path, n, title, stripped[:200], fix))
            break
    return out


def collect(paths: list[str]) -> list[tuple[str, str]]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for name in sorted(names):
                    lang = EXT_LANG.get(os.path.splitext(name)[1])
                    if lang:
                        files.append((os.path.join(dirpath, name), lang))
        elif os.path.isfile(p):
            lang = EXT_LANG.get(os.path.splitext(p)[1])
            if lang:
                files.append((p, lang))
        else:
            raise FileNotFoundError(p)
    return files


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Find SQL injection risks (interpolated / concatenated SQL) in application code (read-only).",
        epilog="Exit codes: 0 = clean, 1 = findings at or above --fail-on, 2 = error.",
    )
    p.add_argument("paths", nargs="+", help="source files or directories")
    p.add_argument("--fail-on", choices=SEVERITIES, default="medium")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    try:
        files = collect(args.paths)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    findings: list[Finding] = []
    for path, lang in files:
        findings += scan_file(path, lang)
    findings.sort(key=lambda f: (-SEVERITIES.index(f.severity), f.file, f.line))
    counts = Counter(f.severity for f in findings)
    if args.json:
        print(json.dumps({"files": len(files), "findings": [asdict(f) for f in findings]}, indent=2))
    else:
        summary = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "no findings"
        print(f"sqli-scan: {summary} ({len(files)} files)")
        for f in findings:
            print(f"{f.severity.upper():<7} {f.file}:{f.line}  {f.title}")
            print(f"        {f.code}")
            print(f"        fix: {f.fix}")
    fail = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) >= fail for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
