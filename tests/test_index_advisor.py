import json

from conftest import FIXTURES, load_script, run_script

SKILL = "index-advisor"
SCRIPT = "index_advisor.py"
mod = load_script(SKILL, SCRIPT)
F = FIXTURES / SKILL


def schema(name="schema.sql"):
    return mod.parse_schema((F / name).read_text())


def advise(sql, sch=None, dialect="postgres"):
    return mod.advise(sql, sch or mod.Schema(), dialect, 6, "q.sql")


def first_suggestion(sql, sch=None, dialect="postgres"):
    return advise(sql, sch, dialect)[0]["tables"][0]["suggestion"]


def test_parse_pg_dump_schema():
    sch = schema()
    names = {i.name: i for i in sch.indexes}
    assert names["orders_pkey"].primary and names["orders_pkey"].columns == ["id"]
    assert names["customers_email_key"].unique
    assert names["orders_status_created_idx"].columns == ["status", "created_at"]
    assert names["customers_lower_email_idx"].expression
    assert "customer_id" in sch.columns["orders"]


def test_parse_mysql_schema():
    sch = schema("mysql_schema.sql")
    names = {i.name: i for i in sch.indexes}
    assert names["posts_pkey"].columns == ["id"]
    assert names["idx_author_status"].columns == ["author_id", "status"]
    assert names["uq_title"].unique and names["uq_title"].columns == ["title"]


def test_esr_order_and_covering():
    s = first_suggestion(
        "SELECT id, total FROM orders WHERE created_at > $1 AND customer_id = $2 AND status = 'paid' "
        "ORDER BY placed_at DESC LIMIT 10"
    )
    assert s["columns"] == ["customer_id", "status", "placed_at", "created_at"]
    assert s["include"] == ["id", "total"]
    assert s["sql"].startswith("CREATE INDEX CONCURRENTLY orders_customer_id_status_placed_at_created_at_idx")
    assert "INCLUDE (id, total)" in s["covering_sql"]


def test_mixed_sort_directions_keep_desc():
    s = first_suggestion("SELECT id FROM t WHERE a = 1 ORDER BY b ASC, c DESC")
    assert s["columns"] == ["a", "b", "c DESC"]


def test_in_list_breaks_sort():
    s = first_suggestion("SELECT id FROM t WHERE status IN ('a', 'b') ORDER BY created_at")
    assert s["columns"] == ["status"]


def test_between_is_one_range():
    s = first_suggestion("SELECT id FROM t WHERE tenant_id = 3 AND created_at BETWEEN $1 AND $2")
    assert s["columns"] == ["tenant_id", "created_at"]


def test_join_inner_table_gets_join_column():
    res = advise("SELECT o.id FROM orders o JOIN order_items oi ON oi.order_id = o.id WHERE o.customer_id = 5")
    tables = {t["table"]: t["suggestion"] for t in res[0]["tables"]}
    assert tables["orders"]["columns"] == ["customer_id"]
    assert tables["order_items"]["columns"] == ["order_id"]


def test_or_and_leading_wildcard_notes():
    res = advise("SELECT id FROM t WHERE a = 1 OR b = 2")
    assert res[0]["tables"][0]["suggestion"] is None
    assert any("OR" in n for n in res[0]["tables"][0]["notes"])
    res = advise("SELECT id FROM t WHERE name LIKE '%x'")
    assert any("LIKE" in n for n in res[0]["tables"][0]["notes"])


def test_subquery_is_analysed_separately():
    res = advise("SELECT id FROM customers WHERE id IN (SELECT customer_id FROM orders WHERE status = 'paid')")
    tables = [t["table"] for t in res[0]["tables"]]
    assert tables == ["customers", "orders"]


def test_existing_indexes_serve_or_extend():
    sch = schema()
    assert first_suggestion("SELECT id FROM customers WHERE email = $1", sch)["status"] == "served"
    s = first_suggestion("SELECT id FROM orders WHERE status = 'x' ORDER BY created_at LIMIT 5", sch)
    assert s["status"] == "served" and s["existing"] == "orders_status_created_idx"
    s = first_suggestion("SELECT id FROM orders WHERE customer_id = $1 AND status = 'paid'", sch)
    assert s["status"] == "extend" and s["existing"] == "orders_customer_id_idx"
    # expression indexes never count as serving a plain column
    s = first_suggestion("SELECT id FROM customers WHERE country = 'NL'", sch)
    assert s["status"] == "new"


def test_mysql_extend_longest_prefix_and_ddl():
    sch = schema("mysql_schema.sql")
    s = first_suggestion(
        "SELECT id FROM posts WHERE author_id = ? AND status = 'x' ORDER BY published_at DESC LIMIT 5",
        sch,
        "mysql",
    )
    assert s["status"] == "extend" and s["existing"] == "idx_author_status"
    assert s["sql"].startswith("ALTER TABLE posts ADD INDEX") and "LOCK=NONE" in s["sql"]


def test_redundant_and_duplicate_indexes():
    red = mod.redundant_indexes(schema())
    kinds = {(r["kind"], r["index"]) for r in red}
    assert ("duplicate", "orders_status_idx") in kinds
    assert ("redundant", "orders_status_dup_idx") in kinds
    # unique and primary indexes are never reported as redundant
    assert not any(r["index"].endswith("_pkey") or r["index"].endswith("_key") for r in red)


def test_consolidate_merges_prefixes():
    res = advise(
        "SELECT id FROM t WHERE a = 1;\nSELECT id FROM t WHERE a = 1 AND b = 2;\nSELECT id FROM t WHERE a = 3;"
    )
    final = mod.consolidate(res)
    assert len(final) == 1 and final[0]["columns"] == ["a", "b"]
    assert len(final[0]["queries"]) == 3


def test_cli(tmp_path):
    r = run_script(SKILL, SCRIPT, str(F / "queries.sql"), "--schema", str(F / "schema.sql"), "--json")
    data = json.loads(r.stdout)
    assert r.returncode == 1
    assert len(data["queries"]) == 7 and data["proposed"] and data["redundant"]
    txt = run_script(SKILL, SCRIPT, str(F / "queries.sql"), "--schema", str(F / "schema.sql")).stdout
    assert "Proposed indexes" in txt and "CREATE INDEX CONCURRENTLY order_items_order_id_idx" in txt
    served = tmp_path / "q.sql"
    served.write_text("SELECT id FROM customers WHERE email = $1;")
    clean = tmp_path / "s.sql"
    clean.write_text("CREATE TABLE customers (id bigint PRIMARY KEY, email text UNIQUE);")
    assert run_script(SKILL, SCRIPT, str(served), "--schema", str(clean)).returncode == 0
    assert run_script(SKILL, SCRIPT, str(tmp_path / "missing.sql")).returncode == 2
    assert run_script(SKILL, SCRIPT, "--help").returncode == 0
