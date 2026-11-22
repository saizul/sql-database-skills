import json
import os
import time
from types import SimpleNamespace

from conftest import FIXTURES, load_script, run_script

SKILL = "backup-restore-drills"
SCRIPT = "dump_inspect.py"
mod = load_script(SKILL, SCRIPT)
F = FIXTURES / SKILL


def opts(**kw):
    base = dict(min_bytes=100, max_age_hours=None, expected=None, tolerance=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


def inspect(name, **kw):
    return mod.inspect(str(F / name), opts(**kw))


def checks(rep):
    return sorted(f.check for f in rep.findings)


def test_pg_plain_good():
    rep = inspect("pg_good.sql")
    assert rep.format == "pg_dump plain" and rep.complete is True
    assert rep.tables == {"customers": 50, "orders": 120, "audit_log": None}
    assert rep.source_version == "16.4" and rep.dump_version == "16.4"
    assert checks(rep) == ["owner_statements", "tables_without_data"]


def test_pg_truncated():
    rep = inspect("pg_truncated.sql")
    assert rep.complete is False and "incomplete_dump" in checks(rep)


def test_pg_dump_older_than_server():
    assert "pg_dump_older_than_server" in checks(inspect("pg_old_client.sql"))


def test_mysqldump_gz():
    rep = inspect("mysql_good.sql.gz")
    assert rep.format == "mysqldump" and rep.complete is True
    assert rep.tables == {"posts": 4, "tags": None}
    assert {"definer_clauses", "gtid_purged"} <= set(checks(rep))


def test_truncated_gzip():
    rep = inspect("mysql_truncated.sql.gz")
    assert rep.complete is False and "truncated_gzip" in checks(rep)
    assert "incomplete_dump" not in checks(rep)


def test_toc_listing():
    rep = inspect("pg_restore_list.txt")
    assert rep.format == "pg_restore --list" and rep.complete
    assert rep.tables == {"customers": -1, "orders": -1, "audit_log": None}
    assert rep.objects["INDEX"] == 1 and rep.objects["FK CONSTRAINT"] == 1


def test_custom_archive_detected():
    rep = inspect("pg_custom.dump")
    assert rep.format == "pg_dump custom" and checks(rep) == ["archive_format"]


def test_tiny_and_stale(tmp_path):
    p = tmp_path / "x.sql"
    p.write_text("-- PostgreSQL database dump\n")
    old = time.time() - 50 * 3600
    os.utime(p, (old, old))
    rep = mod.inspect(str(p), opts(min_bytes=1024, max_age_hours=26))
    assert {"tiny_dump", "stale_backup", "incomplete_dump", "no_tables"} <= set(checks(rep))


def test_expected_counts(tmp_path):
    exp = tmp_path / "expected.tsv"
    exp.write_text("table\trows\npublic.customers\t50\norders\t200\nmissing_table\t3\n")
    rep = inspect("pg_good.sql", expected=str(exp), tolerance=0.01)
    found = {(f.check, f.severity) for f in rep.findings}
    assert ("row_count_mismatch", "high") in found and ("missing_tables", "high") in found


def test_read_counts_formats(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text(" relname | n \n customers | 5\norders,7\nitems  9\n")
    assert mod.read_counts(str(p)) == {"customers": 5, "orders": 7, "items": 9}


def test_cli(tmp_path):
    r = run_script(
        SKILL,
        SCRIPT,
        "--compare-counts",
        str(F / "source_counts.tsv"),
        str(F / "restored_counts.tsv"),
        "--json",
    )
    data = json.loads(r.stdout)
    assert r.returncode == 1
    assert {f["check"] for f in data[0]["findings"]} == {"missing_tables", "row_count_mismatch"}
    same = run_script(SKILL, SCRIPT, "--compare-counts", str(F / "source_counts.tsv"), str(F / "source_counts.tsv"))
    assert same.returncode == 0
    assert run_script(SKILL, SCRIPT, str(F / "pg_good.sql"), "--min-bytes", "100").returncode == 0
    assert run_script(SKILL, SCRIPT, str(F / "pg_truncated.sql")).returncode == 1
    assert run_script(SKILL, SCRIPT, str(F / "mysql_truncated.sql.gz"), "--json").returncode == 1
    assert run_script(SKILL, SCRIPT).returncode == 2
    assert run_script(SKILL, SCRIPT, str(tmp_path / "missing.sql")).returncode == 2
    assert run_script(SKILL, SCRIPT, "--help").returncode == 0
