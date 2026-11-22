import json
from types import SimpleNamespace

from conftest import FIXTURES, load_script, run_script

SKILL = "sql-query-review"
SCRIPT = "sql_lint.py"
mod = load_script(SKILL, SCRIPT)
F = FIXTURES / SKILL


def opts(**kw):
    base = dict(dialect="auto", ignore=set(), repeat_threshold=5, offset_threshold=1000, in_threshold=500)
    base.update(kw)
    return SimpleNamespace(**base)


def checks(sql, schema=None, **kw):
    findings, _ = mod.lint_text(sql, "x.sql", opts(**kw), schema or {})
    return [f.check for f in findings]


def test_bad_fixture_reports_every_check():
    findings, n = mod.lint_text((F / "bad.sql").read_text(), "bad.sql", opts(), {})
    found = {f.check for f in findings}
    for check in (
        "select_star",
        "function_on_column",
        "leading_wildcard",
        "or_different_columns",
        "or_same_column",
        "not_in_subquery",
        "null_comparison",
        "dml_without_where",
        "order_by_random",
        "large_offset",
        "implicit_cast",
        "comma_join",
        "insert_without_columns",
        "union_distinct",
        "optional_filter",
    ):
        assert check in found, check
    assert n == 19
    by_line = {(f.check, f.line) for f in findings}
    assert ("null_comparison", 16) in by_line
    assert ("dml_without_where", 18) in by_line


def test_good_fixture_is_clean():
    findings, n = mod.lint_text((F / "good.sql").read_text(), "good.sql", opts(), {})
    assert findings == [] and n == 14


def test_exists_select_star_and_count_star_are_fine():
    assert checks("SELECT count(*) FROM t WHERE EXISTS (SELECT * FROM u WHERE u.id = t.id)") == []


def test_strings_and_comments_do_not_trigger():
    sql = "SELECT a FROM t WHERE note = 'SELECT * FROM x; DELETE FROM y' -- = NULL\n"
    assert checks(sql) == []


def test_update_set_null_is_not_null_comparison():
    assert "null_comparison" not in checks("UPDATE t SET deleted_at = NULL WHERE id = 1")


def test_on_conflict_update_is_not_flagged():
    sql = "INSERT INTO t (a, b) VALUES (1, 2) ON CONFLICT (a) DO UPDATE SET b = EXCLUDED.b"
    assert checks(sql) == []


def test_ddl_is_skipped():
    assert checks("CREATE INDEX i ON t (lower(email)); ALTER TABLE t ADD COLUMN x int") == []


def test_schema_types_make_implicit_cast_exact():
    schema = mod.parse_schema_types("CREATE TABLE c (id bigint, account_ref varchar(20), qty integer);")
    assert schema["account_ref"] == "VARCHAR"
    found, _ = mod.lint_text("SELECT id FROM c WHERE account_ref = 123", "x", opts(), schema)
    assert [(f.check, f.severity) for f in found] == [("implicit_cast", "medium")]
    found, _ = mod.lint_text("SELECT id FROM c WHERE qty = '5'", "x", opts(), schema)
    assert [(f.check, f.severity) for f in found] == [("implicit_cast", "info")]


def test_inline_ignore():
    assert checks("-- sql-lint: ignore select_star\nSELECT * FROM t") == []
    assert checks("SELECT * FROM t WHERE a = NULL /* sql-lint: ignore */") == []
    assert checks("SELECT * FROM t", ignore={"select_star"}) == []


def test_mysql_limit_offset_and_hash_comment():
    assert "large_offset" in checks("SELECT a FROM t ORDER BY a LIMIT 20000, 20", dialect="mysql")
    assert checks("# just a comment\nSELECT a FROM t WHERE id = 1", dialect="mysql") == []


def test_postgres_operators_tokenize():
    assert checks("SELECT a FROM t WHERE data #>> '{a}' = 'x' AND tags @> ARRAY['b']", dialect="postgres") == []


def test_large_in_list():
    values = ", ".join(str(i) for i in range(600))
    assert "large_in_list" in checks(f"SELECT a FROM t WHERE id IN ({values})")


def test_fingerprint_normalises_literals():
    toks = [t for t in mod.tokenize("SELECT a FROM t WHERE id IN (1, 2, 3) AND s = 'x'") if t.kind != "comment"]
    assert mod.fingerprint(toks) == "SELECT a FROM t WHERE id IN (?+) AND s = ?"


def test_n_plus_one_from_log(tmp_path):
    log = tmp_path / "general.log"
    lines = [
        f"2026-11-20T10:00:0{i}.000000Z\t   12 Query\tSELECT * FROM comments WHERE post_id = {i}" for i in range(7)
    ]
    log.write_text("\n".join(lines) + "\n")
    r = run_script(SKILL, SCRIPT, str(log), "--log", "--json", "--ignore", "select_star")
    data = json.loads(r.stdout)
    assert r.returncode == 1
    assert [f["check"] for f in data["findings"]] == ["n_plus_one"]
    assert "post_id = ?" in data["findings"][0]["snippet"]


def test_cli(tmp_path):
    r = run_script(SKILL, SCRIPT, str(F / "bad.sql"), "--json")
    assert r.returncode == 1 and json.loads(r.stdout)["statements"] == 19
    assert run_script(SKILL, SCRIPT, str(F / "good.sql")).returncode == 0
    assert run_script(SKILL, SCRIPT, str(F)).returncode == 1  # directory
    assert run_script(SKILL, SCRIPT, str(F / "bad.sql"), "--fail-on", "high", "--min-severity", "high").returncode == 1
    assert run_script(SKILL, SCRIPT, "-", stdin="SELECT a FROM t WHERE b LIKE 'x%'").returncode == 0
    assert run_script(SKILL, SCRIPT, str(tmp_path / "missing.sql")).returncode == 2
    assert run_script(SKILL, SCRIPT, "--help").returncode == 0
