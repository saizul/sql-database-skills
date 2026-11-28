import json

from conftest import FIXTURES, load_script, run_script

SKILL = "sql-security"
SCAN = "sqli_scan.py"
GRANTS = "grants_review.py"
scan = load_script(SKILL, SCAN)
grants = load_script(SKILL, GRANTS)
F = FIXTURES / SKILL


def scan_text(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    lang = scan.EXT_LANG[p.suffix]
    return scan.scan_file(str(p), lang)


# --------------------------------------------------------------------------- sqli_scan.py


def test_bad_app_findings():
    findings = []
    for path, lang in scan.collect([str(F / "app_bad")]):
        findings += scan.scan_file(path, lang)
    by_file = {}
    for f in findings:
        by_file.setdefault(f.file.split("/")[-1], []).append(f)
    assert set(by_file) == {
        "views.py",
        "orders.ts",
        "user.php",
        "posts_controller.rb",
        "store.go",
        "Repo.java",
    }
    assert len(by_file["views.py"]) == 5
    sev = {(f.file.split("/")[-1], f.line): f.severity for f in findings}
    assert sev[("views.py", 7)] == "high" and sev[("views.py", 10)] == "medium"
    assert sev[("orders.ts", 3)] == "high"
    order_by = next(f for f in findings if f.file.endswith("views.py") and f.line == 12)
    assert "allow-list" in order_by.fix


def test_good_app_is_clean():
    findings = []
    for path, lang in scan.collect([str(F / "app_good")]):
        findings += scan.scan_file(path, lang)
    assert findings == []


def test_tagged_templates_are_safe(tmp_path):
    assert scan_text(tmp_path, "a.ts", "await sql`SELECT * FROM t WHERE id = ${id}`;\n") == []
    assert scan_text(tmp_path, "b.ts", "await prisma.$queryRaw`SELECT * FROM t WHERE id = ${id}`;\n") == []
    assert scan_text(tmp_path, "c.ts", "db.query(`SELECT * FROM t WHERE id = ${id}`);\n")[0].severity == "high"


def test_non_sql_strings_are_ignored(tmp_path):
    src = 'print(f"Selected {n} items from the list")\nlog = "updated " + name\n'
    assert scan_text(tmp_path, "a.py", src) == []


def test_csharp_and_comments(tmp_path):
    src = (
        'var cmd = new SqlCommand($"SELECT * FROM users WHERE id = {id}", conn);\n// "SELECT * FROM x WHERE a = " + b\n'
    )
    found = scan_text(tmp_path, "A.cs", src)
    assert [(f.line, f.severity) for f in found] == [(1, "high")]


def test_skip_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("q.query(`SELECT * FROM t WHERE a = ${b}`)\n")
    assert scan.collect([str(tmp_path)]) == []


def test_scan_cli(tmp_path):
    r = run_script(SKILL, SCAN, str(F / "app_bad"), "--json")
    data = json.loads(r.stdout)
    assert r.returncode == 1 and data["files"] == 6 and len(data["findings"]) >= 12
    assert run_script(SKILL, SCAN, str(F / "app_good")).returncode == 0
    assert run_script(SKILL, SCAN, str(F / "app_bad"), "--fail-on", "high").returncode == 1
    assert run_script(SKILL, SCAN, str(tmp_path / "missing")).returncode == 2
    assert run_script(SKILL, SCAN, "--help").returncode == 0


# --------------------------------------------------------------------------- grants_review.py


def review(name):
    text = (F / name).read_text()
    stmts = grants.statements(text)
    return grants.review_mysql(stmts) if grants.detect(text) == "mysql" else grants.review_pg(stmts)


def found(findings):
    return {(f.check, f.severity) for f in findings}


def test_mysql_grants():
    f = found(review("mysql_grants.txt"))
    for item in (
        ("mysql_empty_password", "high"),
        ("mysql_admin_privilege", "high"),
        ("mysql_global_all", "high"),
        ("mysql_any_host", "medium"),
        ("mysql_grant_option", "medium"),
        ("mysql_native_password", "low"),
        ("mysql_app_ddl", "low"),
    ):
        assert item in f, item
    subjects = {x.subject for x in review("mysql_grants.txt")}
    assert not any("root" in s for s in subjects)  # admin accounts are expected to be powerful


def test_mysql_grants_good():
    assert review("mysql_grants_good.txt") == []


def test_pg_roles():
    f = found(review("pg_roles_bad.sql"))
    for item in (
        ("pg_grant_public", "high"),
        ("pg_superuser", "high"),
        ("pg_server_file_role", "high"),
        ("pg_login_owner", "medium"),
        ("pg_bypassrls", "medium"),
        ("pg_createrole", "medium"),
        ("pg_rls_not_forced", "medium"),
        ("pg_rls_no_policy", "medium"),
        ("pg_grant_option", "medium"),
        ("pg_md5_password", "low"),
        ("pg_grant_all", "low"),
    ):
        assert item in f, item
    assert not any(x.subject == "postgres" for x in review("pg_roles_bad.sql"))


def test_pg_roles_good():
    assert review("pg_roles_good.sql") == []


def test_no_secrets_in_output():
    r = run_script(SKILL, GRANTS, str(F / "mysql_grants.txt"))
    assert "redacted" not in r.stdout
    r = run_script(SKILL, GRANTS, str(F / "pg_roles_bad.sql"), "--json")
    assert "md500000" not in r.stdout


def test_grants_cli(tmp_path):
    r = run_script(SKILL, GRANTS, str(F / "mysql_grants.txt"), "--json")
    assert r.returncode == 1 and json.loads(r.stdout)["dialect"] == "mysql"
    assert run_script(SKILL, GRANTS, str(F / "pg_roles_good.sql")).returncode == 0
    assert run_script(SKILL, GRANTS, str(F / "mysql_grants_good.txt"), "--dialect", "mysql").returncode == 0
    empty = tmp_path / "e.txt"
    empty.write_text("hello\n")
    assert run_script(SKILL, GRANTS, str(empty)).returncode == 2
    assert run_script(SKILL, GRANTS, str(tmp_path / "missing.txt")).returncode == 2
    assert run_script(SKILL, GRANTS, "--help").returncode == 0


def test_baseline_hides_known_findings_only(tmp_path):
    app = tmp_path / "app.py"
    app.write_text('cur.execute(f"SELECT * FROM users WHERE id = {uid}")\n')
    base = tmp_path / "sqli-baseline.json"
    r = run_script(SKILL, SCAN, str(app), "--write-baseline", str(base))
    assert r.returncode == 0 and "wrote 1 finding" in r.stdout
    data = json.loads(base.read_text())
    assert data["version"] == 1 and len(data["findings"]) == 1
    assert run_script(SKILL, SCAN, str(app), "--baseline", str(base)).returncode == 0
    # shifting the line and adding a new injection: only the new one is reported
    app.write_text(
        "# moved down\n\n"
        'cur.execute(f"SELECT * FROM users WHERE id = {uid}")\n'
        'cur.execute(f"DELETE FROM orders WHERE id = {oid}")\n'
    )
    r = run_script(SKILL, SCAN, str(app), "--baseline", str(base), "--json")
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["baseline_suppressed"] == 1
    assert [f["line"] for f in out["findings"]] == [4]


def test_baseline_counts_duplicate_lines(tmp_path):
    line = 'cur.execute(f"SELECT * FROM t WHERE a = {a}")\n'
    p = tmp_path / "dup.py"
    p.write_text(line)
    findings = scan.scan_file(str(p), "python")
    base = scan.Counter(scan.fingerprint(f) for f in findings)
    p.write_text(line * 2)
    kept, hidden = scan.apply_baseline(scan.scan_file(str(p), "python"), base)
    assert hidden == 1 and len(kept) == 1 and kept[0].line == 2


def test_bad_baseline_file(tmp_path):
    bad = tmp_path / "b.json"
    bad.write_text("[1, 2]")
    assert run_script(SKILL, SCAN, str(F / "app_good"), "--baseline", str(bad)).returncode == 2
    assert run_script(SKILL, SCAN, str(F / "app_good"), "--baseline", str(tmp_path / "missing.json")).returncode == 2
