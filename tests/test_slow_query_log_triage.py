import gzip
import json

import pytest

from conftest import FIXTURES, load_script, run_script

SKILL = "slow-query-log-triage"
SCRIPT = "slowlog_triage.py"
mod = load_script(SKILL, SCRIPT)
F = FIXTURES / SKILL


def test_fingerprint():
    a = mod.fingerprint("SELECT * FROM t WHERE id = 42 AND name = 'bob' AND x IN (1,2,3)")
    b = mod.fingerprint("select *  from t\nwhere id = 7 and name = 'al' and x in (9)")
    assert a == b == "SELECT * FROM t WHERE id = ? AND name = ? AND x IN (?+)"
    assert mod.fingerprint("INSERT INTO t VALUES (1, 'a'), (2, 'b')") == "INSERT INTO t VALUES (?+), ..."


def test_parse_mysql_slow_log():
    events = mod.parse_mysql((F / "mysql-slow.log").read_text())
    assert len(events) == 11
    first = events[0]
    assert first.ms == pytest.approx(2000.0) and first.rows_examined == 1000000 and first.db == "shop"
    assert first.user == "app" and first.time.startswith("2026-11-20T10:00:00")
    assert "SET timestamp" not in first.sql and first.sql.startswith("SELECT id, total FROM orders")
    groups = sorted(mod.group_events(events), key=lambda g: -g.total_ms)
    assert len(groups) == 4 and groups[0].count == 6


def test_mysql_hints():
    groups = mod.group_events(mod.parse_mysql((F / "mysql-slow.log").read_text()))
    mod.add_hints(groups, "mysql")
    by_fp = {g.fingerprint.split()[0] + str(g.count): g for g in groups}
    assert any("rows per row returned" in h for h in by_fp["SELECT6"].hints)
    assert any("lock wait" in h for h in by_fp["UPDATE3"].hints)


def test_parse_pg_log_multiline():
    events = mod.parse_pglog((F / "postgresql.log").read_text())
    assert len(events) == 5
    assert events[0].sql.endswith("ORDER BY o.created_at DESC") and events[0].db == "shop"
    groups = mod.group_events(events)
    assert {g.count for g in groups} == {4, 1}


def test_parse_pgss():
    groups = mod.parse_pgss((F / "pg_stat_statements.csv").read_text())
    mod.add_hints(groups, "pgss")
    top = max(groups, key=lambda g: g.total_ms)
    assert top.count == 1200 and top.total_ms == 96000.0
    assert any("temp blocks" in h for h in top.hints) and any("cache hit" in h for h in top.hints)
    with pytest.raises(ValueError):
        mod.parse_pgss("a,b\n1,2\n")


def test_chatty_hint():
    csv_text = "query,calls,total_exec_time\nSELECT 1 FROM t WHERE id = $1,50000,1000\nSELECT 2,1,9000\n"
    groups = mod.parse_pgss(csv_text)
    mod.add_hints(groups, "pgss")
    chatty = next(g for g in groups if g.count == 50000)
    assert any("chatty" in h for h in chatty.hints)


def test_detect():
    assert mod.detect((F / "mysql-slow.log").read_text()) == "mysql"
    assert mod.detect((F / "postgresql.log").read_text()) == "pglog"
    assert mod.detect((F / "pg_stat_statements.csv").read_text()) == "pgss"
    with pytest.raises(ValueError):
        mod.detect("nothing to see")


def test_p95():
    g = mod.Group("x", durations=[float(i) for i in range(1, 101)])
    assert g.p95() == 95.0


def test_cli(tmp_path):
    r = run_script(SKILL, SCRIPT, str(F / "mysql-slow.log"), "--json", "--top", "2")
    data = json.loads(r.stdout)
    assert r.returncode == 0 and data["format"] == "mysql" and data["executions"] == 11
    assert len(data["top"]) == 2 and data["top"][0]["count"] == 6
    assert "sample" not in data["top"][0]
    assert "person@example.com" not in r.stdout  # literals are normalised away
    r = run_script(SKILL, SCRIPT, str(F / "mysql-slow.log"), "--samples", "--json", "--sort", "count")
    assert "sample" in json.loads(r.stdout)["top"][0]
    r = run_script(
        SKILL,
        SCRIPT,
        str(F / "mysql-slow.log"),
        "--since",
        "2026-11-20T10:10",
        "--until",
        "2026-11-20T11:00",
        "--json",
    )
    assert json.loads(r.stdout)["executions"] == 4
    gz = tmp_path / "slow.log.gz"
    gz.write_bytes(gzip.compress((F / "mysql-slow.log").read_bytes()))
    assert run_script(SKILL, SCRIPT, str(gz)).returncode == 0
    assert run_script(SKILL, SCRIPT, str(F / "postgresql.log"), "--fail-over-ms", "5000").returncode == 1
    assert run_script(SKILL, SCRIPT, str(F / "pg_stat_statements.csv"), "--db", "shop").returncode == 0
    bad = tmp_path / "x.log"
    bad.write_text("hello\n")
    assert run_script(SKILL, SCRIPT, str(bad)).returncode == 2
    assert run_script(SKILL, SCRIPT, "--help").returncode == 0
