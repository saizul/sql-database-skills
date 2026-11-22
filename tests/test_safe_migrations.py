import json

from conftest import FIXTURES, load_script, run_script

SKILL = "safe-migrations"
SCRIPT = "migration_lint.py"
mod = load_script(SKILL, SCRIPT)
F = FIXTURES / SKILL


def lint(sql, dialect="postgres", transaction="auto", ignore=()):
    return mod.MigrationLinter("m.sql", dialect, transaction, set(ignore)).lint(sql)


def checks(sql, **kw):
    return sorted(f.check for f in lint(sql, **kw))


def test_pg_bad_fixture():
    found = {f.check for f in lint((F / "pg_bad.sql").read_text())}
    expected = {
        "create_index_not_concurrently",
        "concurrently_in_transaction",
        "add_column_volatile_default",
        "add_column_not_null_no_default",
        "alter_column_type",
        "set_not_null",
        "add_fk_without_not_valid",
        "add_check_without_not_valid",
        "add_unique_constraint",
        "rename_column",
        "drop_column",
        "backfill_in_migration",
        "drop_index_not_concurrently",
        "drop_table",
        "vacuum_full",
        "missing_lock_timeout",
    }
    assert expected <= found


def test_pg_good_fixture_is_clean():
    assert lint((F / "pg_good.sql").read_text()) == []


def test_mysql_fixtures():
    found = {f.check for f in lint((F / "mysql_bad.sql").read_text(), dialect="mysql")}
    for check in (
        "mysql_modify_column",
        "mysql_column_position",
        "mysql_no_algorithm",
        "mysql_fulltext_index",
        "mysql_convert_charset",
        "mysql_primary_key_change",
        "rename_column",
        "rename_table",
        "optimize_table",
        "truncate",
        "missing_lock_timeout",
    ):
        assert check in found, check
    assert lint((F / "mysql_good.sql").read_text(), dialect="mysql") == []


def test_constant_and_stable_defaults_are_fine_on_pg11_plus():
    sql = "SET lock_timeout = '3s'; ALTER TABLE t ADD COLUMN c int NOT NULL DEFAULT 0; ALTER TABLE t ADD COLUMN d timestamptz DEFAULT now();"
    assert checks(sql) == []


def test_new_tables_are_exempt():
    sql = "CREATE TABLE n (id bigint); CREATE INDEX n_idx ON n (id); ALTER TABLE n ADD COLUMN x int NOT NULL;"
    assert checks(sql) == []


def test_framework_transaction_flag():
    sql = "SET lock_timeout = '3s'; CREATE INDEX CONCURRENTLY i ON t (a);"
    assert checks(sql) == []
    assert checks(sql, transaction="yes") == ["concurrently_in_transaction"]


def test_dialect_detection():
    assert mod.detect_dialect("ALTER TABLE `t` ADD COLUMN x int") == "mysql"
    assert mod.detect_dialect("ALTER TABLE t ADD COLUMN x int") == "postgres"


def test_varchar_type_change_and_lines():
    found = lint("SET lock_timeout='1s';\n\nALTER TABLE users ALTER COLUMN name SET DATA TYPE text;")
    assert [(f.check, f.line) for f in found] == [("alter_column_type", 3)]


def test_inline_and_cli_ignore():
    sql = "SET lock_timeout='1s'; -- migration-lint: ignore drop_column\nALTER TABLE t DROP COLUMN a;"
    assert checks(sql) == []
    assert checks("SET lock_timeout='1s'; ALTER TABLE t DROP COLUMN a;", ignore=["drop_column"]) == []
    assert checks("SET lock_timeout='1s'; ALTER TABLE t DROP COLUMN a /* migration-lint: ignore */;") == []


def test_reindex_refresh_lock_cluster():
    sql = "REINDEX TABLE t; REFRESH MATERIALIZED VIEW mv; LOCK TABLE t; CLUSTER t USING i;"
    assert checks(sql) == ["cluster", "lock_table", "refresh_matview", "reindex_not_concurrently"]
    assert checks("REINDEX TABLE CONCURRENTLY t; REFRESH MATERIALIZED VIEW CONCURRENTLY mv;") == []


def test_delete_with_where_is_medium():
    found = lint("DELETE FROM sessions WHERE expires_at < now();")
    assert [(f.check, f.severity) for f in found] == [("backfill_in_migration", "medium")]


def test_cli(tmp_path):
    r = run_script(SKILL, SCRIPT, str(F / "pg_bad.sql"), "--json")
    assert r.returncode == 1 and json.loads(r.stdout)["files"] == 1
    assert run_script(SKILL, SCRIPT, str(F / "pg_good.sql")).returncode == 0
    assert run_script(SKILL, SCRIPT, str(F / "mysql_good.sql")).returncode == 0  # auto dialect
    r = run_script(SKILL, SCRIPT, str(F), "--json")
    assert json.loads(r.stdout)["files"] == 4
    low_only = tmp_path / "m.sql"
    low_only.write_text("SET SESSION lock_wait_timeout = 5; ALTER TABLE `t` ADD INDEX i (a);")
    assert run_script(SKILL, SCRIPT, str(low_only)).returncode == 0
    assert run_script(SKILL, SCRIPT, str(low_only), "--fail-on", "low").returncode == 1
    assert run_script(SKILL, SCRIPT, str(tmp_path / "missing.sql")).returncode == 2
    assert run_script(SKILL, SCRIPT, "--help").returncode == 0
