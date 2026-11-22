import json
from types import SimpleNamespace

import pytest

from conftest import FIXTURES, load_script, run_script

SKILL = "explain-plan-analysis"
SCRIPT = "explain_analyze.py"
mod = load_script(SKILL, SCRIPT)
F = FIXTURES / SKILL


def opts(**kw):
    base = dict(rows_threshold=10000, loop_threshold=1000, misestimate=10.0, filtered=10.0, top=5)
    base.update(kw)
    return SimpleNamespace(**base)


def analyse(name, **kw):
    return mod.analyse((F / name).read_text(), opts(**kw))


def checks(rep):
    return sorted(f.check for f in rep.findings)


def test_pg_json_bad():
    rep = analyse("pg_bad.json")
    assert rep.engine == "postgresql" and rep.analyzed and rep.execution_ms == 870.3
    assert checks(rep) == sorted(["sort_spill", "seq_scan", "seq_scan", "misestimate", "hash_spill", "jit_overhead"])
    # misestimate is reported once, at the deepest node (the scan), not on Hash Join / Sort
    mis = [f for f in rep.findings if f.check == "misestimate"]
    assert len(mis) == 1 and "Seq Scan on orders" in mis[0].node
    seq = {f.node: f.severity for f in rep.findings if f.check == "seq_scan"}
    assert seq == {"Seq Scan on orders": "medium", "Seq Scan on customers": "low"}
    assert rep.hotspots[0]["node"] == "Seq Scan on orders" and rep.hotspots[0]["pct"] == 59.8


def test_pg_json_good_under_limit_has_no_misestimate():
    rep = analyse("pg_good.json")
    assert rep.findings == []


def test_pg_text_nested_loop():
    rep = analyse("pg_text.txt")
    assert rep.execution_ms == pytest.approx(2212.88)
    assert "seq_scan_in_loop" in checks(rep)
    assert rep.hotspots[0]["node"] == "Seq Scan on orders" and rep.hotspots[0]["loops"] == 4800


def test_pg_text_spill_and_lossy():
    rep = analyse("pg_text_spill.txt")
    assert checks(rep) == ["lossy_bitmap", "sort_spill"]


def test_pg_estimate_only():
    rep = analyse("pg_estimate_only.txt")
    assert not rep.analyzed
    assert checks(rep) == ["no_analyze", "seq_scan"]


def test_pg_text_parser_details():
    doc = mod.parse_pg_text(
        "Hash Join  (cost=1.00..2.00 rows=10 width=4) (actual time=0.1..0.2 rows=10 loops=1)\n"
        "  Hash Cond: (a.id = b.id)\n"
        "  ->  Seq Scan on a  (cost=0.00..1.00 rows=10 width=4) (actual time=0.01..0.02 rows=10 loops=1)\n"
        "  ->  Hash  (cost=1.00..1.00 rows=10 width=4) (actual time=0.05..0.05 rows=10 loops=1)\n"
        "        Buckets: 1024  Batches: 2  Memory Usage: 9kB\n"
        "        ->  Seq Scan on b  (cost=0.00..1.00 rows=10 width=4) (never executed)\n"
    )
    root = doc[0]["Plan"]
    assert root["Node Type"] == "Hash Join" and root["Hash Cond"] == "(a.id = b.id)"
    assert [p["Node Type"] for p in root["Plans"]] == ["Seq Scan", "Hash"]
    assert root["Plans"][1]["Hash Batches"] == 2
    assert root["Plans"][1]["Plans"][0]["Actual Loops"] == 0


def test_psql_wrapped_json():
    raw = (F / "pg_good.json").read_text()
    wrapped = "QUERY PLAN\n-----------\n" + "\n".join(" " + ln + " +" for ln in raw.splitlines()) + "\n(1 row)\n"
    rep = mod.analyse(wrapped, opts())
    assert rep.engine == "postgresql" and rep.analyzed


def test_mysql_json():
    rep = analyse("mysql_bad.json")
    assert rep.engine == "mysql"
    sev = {(f.check, f.severity) for f in rep.findings}
    assert ("full_scan", "high") in sev  # inner table of the nested loop
    assert ("filesort", "medium") in sev and ("temporary_table", "medium") in sev
    assert analyse("mysql_good.json").findings == []


def test_mysql_low_filtered_and_dependent_subquery():
    doc = {
        "query_block": {
            "select_id": 1,
            "table": {
                "table_name": "t",
                "access_type": "range",
                "key": "idx_a",
                "rows_examined_per_scan": 50000,
                "filtered": "2.00",
                "attached_condition": "(b = 1)",
            },
            "select_list_subqueries": [{"dependent": True, "query_block": {"select_id": 2}}],
        }
    }
    rep = mod.analyse(json.dumps(doc), opts())
    assert checks(rep) == ["dependent_subquery", "low_filtered"]


def test_mariadb_analyze_misestimate():
    doc = {"query_block": {"table": {"table_name": "t", "access_type": "ref", "rows": 10, "r_rows": 50000}}}
    rep = mod.analyse(json.dumps(doc), opts())
    assert rep.analyzed and checks(rep) == ["misestimate"]


def test_mysql_tree():
    rep = analyse("mysql_tree.txt")
    assert rep.engine == "mysql" and rep.analyzed
    assert "full_scan" in checks(rep)
    assert rep.hotspots[0]["node"].startswith("Sort")


def test_unrecognised():
    with pytest.raises(mod.PlanError):
        mod.analyse("hello world", opts())
    with pytest.raises(mod.PlanError):
        mod.analyse("{not json", opts())


def test_cli(tmp_path):
    r = run_script(SKILL, SCRIPT, str(F / "pg_bad.json"), "--json")
    assert r.returncode == 1 and json.loads(r.stdout)["engine"] == "postgresql"
    assert run_script(SKILL, SCRIPT, str(F / "pg_good.json")).returncode == 0
    assert run_script(SKILL, SCRIPT, str(F / "pg_estimate_only.txt")).returncode == 0
    assert run_script(SKILL, SCRIPT, "-", stdin=(F / "mysql_bad.json").read_text()).returncode == 1
    bad = tmp_path / "x.txt"
    bad.write_text("nothing here")
    assert run_script(SKILL, SCRIPT, str(bad)).returncode == 2
    assert run_script(SKILL, SCRIPT, "--help").returncode == 0
