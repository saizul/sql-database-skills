import json

from conftest import FIXTURES, load_script, run_script

SKILL = "schema-design-review"
SCRIPT = "schema_review.py"
mod = load_script(SKILL, SCRIPT)
F = FIXTURES / SKILL


def review(name, dialect):
    return mod.review(mod.parse((F / name).read_text()), dialect)


def found(findings):
    return {(f.check, f.table) for f in findings}


def test_parse_pg_dump():
    tables = mod.parse((F / "pg_bad.sql").read_text())
    assert set(tables) == {"customers", "orders", "event_log", "user"}
    assert tables["orders"].pk == ["id"]
    assert tables["orders"].fks == [(["customer_id"], "customers", ["id"], 34)]
    assert tables["customers"].columns["created_at"].type == "timestamp without time zone"
    assert tables["orders"].columns["customer_id"].nullable is False


def test_pg_bad():
    f = found(review("pg_bad.sql", "postgres"))
    for item in (
        ("no_primary_key", "event_log"),
        ("float_money", "orders"),
        ("list_in_column", "orders"),
        ("fk_without_index", "orders"),
        ("fk_type_mismatch", "orders"),
        ("int_primary_key", "customers"),
        ("quoted_identifier", "customers"),
        ("repeating_group", "customers"),
        ("timestamp_without_tz", "customers"),
        ("reserved_word", "user"),
        ("serial_column", "user"),
        ("all_nullable", "customers"),
    ):
        assert item in f, item


def test_pg_good_is_clean():
    assert review("pg_good.sql", "postgres") == []


def test_mysql_bad():
    f = found(review("mysql_bad.sql", "mysql"))
    for item in (
        ("no_primary_key", "order_lines"),
        ("mysql_myisam", "products"),
        ("mysql_utf8mb3", "products"),
        ("float_money", "products"),
        ("fk_type_mismatch", "order_lines"),
        ("list_in_column", "products"),
        ("enum_column", "order_lines"),
    ):
        assert item in f, item
    # MySQL creates FK indexes itself: never report fk_without_index
    assert not any(c == "fk_without_index" for c, _ in f)


def test_mixed_naming_and_varchar_255():
    sql = (
        "CREATE TABLE a (id bigint PRIMARY KEY, firstName varchar(255) NOT NULL, lastName varchar(255) NOT NULL,"
        " city varchar(255) NOT NULL, zip_code varchar(255) NOT NULL, country_code varchar(255) NOT NULL);"
    )
    checks = {f.check for f in mod.review(mod.parse(sql), "postgres")}
    assert {"mixed_naming", "varchar_255"} <= checks


def test_alter_table_add_column_and_index():
    sql = (
        "CREATE TABLE p (id bigint PRIMARY KEY, owner_id bigint NOT NULL REFERENCES u (id));"
        "CREATE TABLE u (id bigint PRIMARY KEY);"
        "ALTER TABLE p ADD COLUMN price real;"
        "CREATE INDEX p_owner ON p (owner_id);"
    )
    checks = {f.check for f in mod.review(mod.parse(sql), "postgres")}
    assert "float_money" in checks and "fk_without_index" not in checks


def test_cli(tmp_path):
    r = run_script(SKILL, SCRIPT, str(F / "pg_bad.sql"), "--json")
    data = json.loads(r.stdout)
    assert r.returncode == 1 and data["dialect"] == "postgres" and data["tables"] == 4
    r = run_script(SKILL, SCRIPT, str(F / "mysql_bad.sql"), "--json")
    assert json.loads(r.stdout)["dialect"] == "mysql"
    assert run_script(SKILL, SCRIPT, str(F / "pg_good.sql")).returncode == 0
    r = run_script(SKILL, SCRIPT, str(F / "pg_bad.sql"), "--min-severity", "high", "--json")
    assert {f["severity"] for f in json.loads(r.stdout)["findings"]} == {"high"}
    empty = tmp_path / "e.sql"
    empty.write_text("SELECT 1;")
    assert run_script(SKILL, SCRIPT, str(empty)).returncode == 2
    assert run_script(SKILL, SCRIPT, str(tmp_path / "missing.sql")).returncode == 2
    assert run_script(SKILL, SCRIPT, "--help").returncode == 0
