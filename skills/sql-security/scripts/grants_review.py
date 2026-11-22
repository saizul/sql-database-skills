#!/usr/bin/env python3
"""Review database privileges for least privilege (MySQL/MariaDB and PostgreSQL).

Input: text containing GRANT / CREATE USER / CREATE ROLE / ALTER ROLE statements, e.g.

  MySQL:      SHOW GRANTS FOR 'app'@'%';  (one or more outputs pasted into a file)
              mysqldump --system=users  /  pt-show-grants
  PostgreSQL: pg_dumpall --roles-only   plus   pg_dump --schema-only
              (GRANT, ALTER ... OWNER TO, ENABLE ROW LEVEL SECURITY, CREATE POLICY)

MySQL checks: ALL PRIVILEGES ON *.*, global admin privileges (SUPER, FILE, PROCESS,
SHUTDOWN, RELOAD, CREATE USER, REPLICATION), WITH GRANT OPTION, accounts on host '%',
empty passwords, application accounts with DDL rights on their schema.

PostgreSQL checks: SUPERUSER / BYPASSRLS / CREATEROLE roles, grants TO PUBLIC (CREATE on
schema public), GRANT ALL on tables, login roles that own tables (owners bypass RLS and
can DROP), RLS enabled without policies, RLS not FORCEd for owners, md5 password hashes.

Password hashes and secrets are never printed. The script only reads the file.
Exit codes: 0 = no findings at or above --fail-on, 1 = findings, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

SEVERITIES = ["info", "low", "medium", "high"]
ADMIN_PRIVS = {
    "SUPER": "high",
    "FILE": "high",
    "SHUTDOWN": "high",
    "CREATE USER": "high",
    "PROCESS": "medium",
    "RELOAD": "medium",
    "REPLICATION SLAVE": "medium",
    "REPLICATION CLIENT": "low",
    "SYSTEM_VARIABLES_ADMIN": "high",
    "CONNECTION_ADMIN": "medium",
}
DDL_PRIVS = {
    "CREATE",
    "DROP",
    "ALTER",
    "INDEX",
    "REFERENCES",
    "TRIGGER",
    "CREATE VIEW",
    "CREATE ROUTINE",
    "ALTER ROUTINE",
    "EVENT",
}
ADMIN_ACCOUNTS = re.compile(
    r"^(root|admin|mysql\.\w+|debian-sys-maint|mariadb\.sys|rdsadmin|postgres|azure_superuser|rds\w*)$"
)


@dataclass
class Finding:
    check: str
    severity: str
    subject: str
    title: str
    fix: str


def statements(text: str) -> list[str]:
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", text, flags=re.S)
    out, cur, quote = [], [], ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    result = []
    for s in out:
        # SHOW GRANTS output has one GRANT per line without semicolons
        for part in re.split(r"\n(?=\s*(?:GRANT|REVOKE|CREATE|ALTER|\|)\s)", s):
            part = " ".join(part.split()).strip(" |")
            if part:
                result.append(part)
    return result


def account(s: str) -> str:
    m = re.search(r"\bTO\s+((?:'[^']*'|`[^`]*`|\"[^\"]*\"|[\w$.-]+)(?:\s*@\s*(?:'[^']*'|`[^`]*`|[\w%.:-]+))?)", s, re.I)
    if not m:
        return "?"
    return re.sub(r"\s+", "", m.group(1))


def user_host(acct: str) -> tuple[str, str]:
    m = re.match(r"^['`\"]?([^'`\"@]*)['`\"]?(?:@['`\"]?([^'`\"]*)['`\"]?)?$", acct)
    if not m:
        return acct, ""
    return m.group(1), m.group(2) or ""


def review_mysql(stmts: list[str]) -> list[Finding]:
    out: list[Finding] = []
    hosts_seen: set[str] = set()
    for s in stmts:
        up = s.upper()
        if up.startswith("CREATE USER") or up.startswith("ALTER USER"):
            m = re.search(r"USER\s+(?:IF NOT EXISTS\s+)?((?:'[^']*'|`[^`]*`|\S+)@(?:'[^']*'|`[^`]*`|\S+))", s, re.I)
            acct = re.sub(r"\s+", "", m.group(1)) if m else "?"
            if re.search(r"IDENTIFIED\s+(?:WITH\s+\S+\s+)?BY\s+''", s, re.I) or (
                up.startswith("CREATE USER") and "IDENTIFIED" not in up
            ):
                out.append(
                    Finding(
                        "mysql_empty_password",
                        "high",
                        acct,
                        f"{acct} has no password",
                        "ALTER USER ... IDENTIFIED BY a generated secret (store it in your secret manager)",
                    )
                )
            if re.search(r"IDENTIFIED WITH\s+'?mysql_native_password", s, re.I):
                out.append(
                    Finding(
                        "mysql_native_password",
                        "low",
                        acct,
                        f"{acct} uses mysql_native_password (deprecated in MySQL 8.0, removed in 9)",
                        "ALTER USER ... IDENTIFIED WITH caching_sha2_password BY ...",
                    )
                )
            continue
        if not up.startswith("GRANT"):
            continue
        m = re.match(r"GRANT\s+(.+?)\s+ON\s+(?:(?:TABLE|FUNCTION|PROCEDURE)\s+)?(\S+)\s+TO\s", s, re.I)
        if not m:
            continue  # role grants: GRANT 'role' TO 'user'
        privs = [p.strip().upper() for p in re.split(r",(?![^(]*\))", m.group(1))]
        privs = [re.sub(r"\s*\(.*\)$", "", p) for p in privs]
        target = m.group(2).replace("`", "")
        acct = account(s)
        user, host = user_host(acct)
        admin = bool(ADMIN_ACCOUNTS.match(user))
        if host == "%" and acct not in hosts_seen:
            hosts_seen.add(acct)
            out.append(
                Finding(
                    "mysql_any_host",
                    "high" if admin else "medium",
                    acct,
                    f"{acct} can connect from any host",
                    "restrict the host to the app servers ('app'@'10.0.0.%') and firewall port 3306",
                )
            )
        all_privs = any(p in ("ALL", "ALL PRIVILEGES") for p in privs)
        if target == "*.*":
            if all_privs and not admin:
                out.append(
                    Finding(
                        "mysql_global_all",
                        "high",
                        acct,
                        f"{acct} has ALL PRIVILEGES ON *.*",
                        "grant only the needed privileges on the application schema: "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.* TO ...",
                    )
                )
            elif not admin:
                for p in privs:
                    if p in ADMIN_PRIVS:
                        out.append(
                            Finding(
                                "mysql_admin_privilege",
                                ADMIN_PRIVS[p],
                                acct,
                                f"{acct} has global {p}",
                                "revoke it from application accounts; use a separate admin account",
                            )
                        )
        elif not admin:
            ddl = sorted(p for p in privs if p in DDL_PRIVS)
            if all_privs or ddl:
                out.append(
                    Finding(
                        "mysql_app_ddl",
                        "low",
                        acct,
                        f"{acct} has {'ALL' if all_privs else ', '.join(ddl)} on {target} (can DROP/ALTER tables)",
                        "use a separate migration account; the runtime account needs SELECT, INSERT, UPDATE, DELETE",
                    )
                )
        if "WITH GRANT OPTION" in up and not admin:
            out.append(
                Finding(
                    "mysql_grant_option",
                    "medium",
                    acct,
                    f"{acct} has WITH GRANT OPTION on {target}",
                    "remove GRANT OPTION from application accounts",
                )
            )
    return out


def review_pg(stmts: list[str]) -> list[Finding]:
    out: list[Finding] = []
    roles: dict[str, set[str]] = defaultdict(set)
    owners: dict[str, str] = {}
    rls: set[str] = set()
    forced: set[str] = set()
    policies: set[str] = set()
    for s in stmts:
        up = s.upper()
        m = re.match(r"(?:CREATE|ALTER) ROLE\s+(\"[^\"]+\"|\S+)(?:\s+WITH)?(.*)$", s, re.I)
        if m and not re.match(r"ALTER ROLE \S+ (SET|RESET|RENAME)", s, re.I):
            name = m.group(1).strip('"')
            attrs = set(
                re.findall(
                    r"\b(NO)?(SUPERUSER|CREATEROLE|CREATEDB|LOGIN|BYPASSRLS|REPLICATION)\b",
                    m.group(2).upper(),
                )
            )
            for neg, a in attrs:
                if neg:
                    roles[name].discard(a)
                else:
                    roles[name].add(a)
            if re.search(r"PASSWORD\s+'md5", s, re.I):
                roles[name].add("MD5")
            continue
        m = re.match(r"CREATE USER\s+(\"[^\"]+\"|\S+)(.*)$", s, re.I)
        if m:
            name = m.group(1).strip('"')
            roles[name].add("LOGIN")
            for a in re.findall(r"\b(SUPERUSER|CREATEROLE|CREATEDB|BYPASSRLS)\b", m.group(2).upper()):
                roles[name].add(a)
            continue
        m = re.match(
            r"ALTER (?:TABLE|VIEW|SEQUENCE|FUNCTION|SCHEMA|MATERIALIZED VIEW)\s+(?:ONLY\s+)?(\S+).*?\sOWNER TO\s+(\S+)",
            s,
            re.I,
        )
        if m:
            owners[m.group(1).split(".")[-1].strip('"')] = m.group(2).strip('"')
            continue
        m = re.match(r"ALTER TABLE\s+(?:ONLY\s+)?(\S+)\s+(ENABLE|FORCE) ROW LEVEL SECURITY", s, re.I)
        if m:
            t = m.group(1).split(".")[-1].strip('"')
            (rls if m.group(2).upper() == "ENABLE" else forced).add(t)
            continue
        m = re.match(r"CREATE POLICY\s+\S+\s+ON\s+(\S+)", s, re.I)
        if m:
            policies.add(m.group(1).split(".")[-1].strip('"'))
            continue
        m = re.match(r"GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+(.+?)(\s+WITH GRANT OPTION)?$", s, re.I)
        if m:
            privs, obj, grantees = m.group(1).upper(), m.group(2), m.group(3)
            for g in [x.strip().strip('"') for x in grantees.split(",")]:
                if g.upper() == "PUBLIC":
                    sev = "high" if ("CREATE" in privs and "SCHEMA" in obj.upper()) or "ALL" in privs else "medium"
                    if privs == "USAGE" and obj.upper().startswith("SCHEMA PUBLIC"):
                        continue
                    out.append(
                        Finding(
                            "pg_grant_public",
                            sev,
                            "PUBLIC",
                            f"GRANT {privs} ON {obj} TO PUBLIC (every role, including future ones)",
                            "REVOKE ... FROM PUBLIC and grant to a named group role",
                        )
                    )
                elif "ALL" in privs.split() or privs.startswith("ALL"):
                    out.append(
                        Finding(
                            "pg_grant_all",
                            "low",
                            g,
                            f"{g} has ALL on {obj} (includes TRUNCATE, TRIGGER, REFERENCES)",
                            "grant SELECT, INSERT, UPDATE, DELETE (and USAGE on sequences) only",
                        )
                    )
                if m.group(4):
                    out.append(
                        Finding(
                            "pg_grant_option",
                            "medium",
                            g,
                            f"{g} can re-grant privileges on {obj}",
                            "drop WITH GRANT OPTION",
                        )
                    )
            continue
        if up.startswith("GRANT ") and " TO " in up:
            # role membership: GRANT pg_read_server_files TO app
            m = re.match(r"GRANT\s+(\S+)\s+TO\s+(\S+)", s, re.I)
            if m and m.group(1).lower() in (
                "pg_read_server_files",
                "pg_write_server_files",
                "pg_execute_server_program",
            ):
                out.append(
                    Finding(
                        "pg_server_file_role",
                        "high",
                        m.group(2).strip('"'),
                        f"{m.group(2)} is a member of {m.group(1)} (file system / program access)",
                        "revoke it; these roles are superuser-equivalent in practice",
                    )
                )
    for name, attrs in sorted(roles.items()):
        builtin = bool(ADMIN_ACCOUNTS.match(name))
        if "SUPERUSER" in attrs and not builtin:
            out.append(
                Finding(
                    "pg_superuser",
                    "high",
                    name,
                    f"role {name} is SUPERUSER",
                    "ALTER ROLE ... NOSUPERUSER; applications never need superuser",
                )
            )
        if "BYPASSRLS" in attrs and not builtin:
            out.append(
                Finding("pg_bypassrls", "medium", name, f"role {name} has BYPASSRLS", "ALTER ROLE ... NOBYPASSRLS")
            )
        if "CREATEROLE" in attrs and not builtin and "LOGIN" in attrs:
            out.append(
                Finding(
                    "pg_createrole",
                    "medium",
                    name,
                    f"login role {name} has CREATEROLE",
                    "keep role management in a separate admin role",
                )
            )
        if "MD5" in attrs:
            out.append(
                Finding(
                    "pg_md5_password",
                    "low",
                    name,
                    f"role {name} has an md5 password hash",
                    "set password_encryption = 'scram-sha-256', reset the password, use scram-sha-256 in pg_hba.conf",
                )
            )
    by_owner: dict[str, list[str]] = defaultdict(list)
    for obj, owner in owners.items():
        by_owner[owner].append(obj)
    for owner, objs in sorted(by_owner.items()):
        if "LOGIN" in roles.get(owner, set()) and not ADMIN_ACCOUNTS.match(owner):
            out.append(
                Finding(
                    "pg_login_owner",
                    "medium",
                    owner,
                    f"login role {owner} owns {len(objs)} objects ({', '.join(sorted(objs)[:5])}"
                    + (" ..." if len(objs) > 5 else "")
                    + "): it can DROP/ALTER them and bypasses RLS",
                    "make a NOLOGIN owner role for migrations; the app logs in as a role with only DML grants",
                )
            )
    for t in sorted(rls):
        if t not in policies:
            out.append(
                Finding(
                    "pg_rls_no_policy",
                    "medium",
                    t,
                    f"RLS is enabled on {t} but no policy exists (non-owners see no rows)",
                    "CREATE POLICY ... USING (tenant_id = current_setting('app.tenant_id')::bigint)",
                )
            )
        owner = owners.get(t)
        if t not in forced and owner and "LOGIN" in roles.get(owner, set()):
            out.append(
                Finding(
                    "pg_rls_not_forced",
                    "medium",
                    t,
                    f"{t} has RLS but its owner {owner} can log in and bypasses it",
                    f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY, or connect as a non-owner role",
                )
            )
    return out


def detect(text: str) -> str:
    if re.search(r"`|'@'|@'%'|\bON\s+\*\.\*|IDENTIFIED (WITH|BY)", text, re.I):
        return "mysql"
    return "postgres"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Review MySQL / PostgreSQL grants and roles for least privilege (read-only).",
        epilog="Exit codes: 0 = clean, 1 = findings at or above --fail-on, 2 = error.",
    )
    p.add_argument("path", help="file with SHOW GRANTS output / pg_dumpall --roles-only / pg_dump DDL, or -")
    p.add_argument("--dialect", choices=["auto", "mysql", "postgres"], default="auto")
    p.add_argument("--fail-on", choices=SEVERITIES, default="medium")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    try:
        if args.path == "-":
            text = sys.stdin.read()
        else:
            with open(args.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    dialect = detect(text) if args.dialect == "auto" else args.dialect
    stmts = statements(text)
    if not any(re.match(r"(GRANT|CREATE|ALTER)\b", s, re.I) for s in stmts):
        print("error: no GRANT / CREATE / ALTER statements found", file=sys.stderr)
        return 2
    findings = review_mysql(stmts) if dialect == "mysql" else review_pg(stmts)
    findings.sort(key=lambda f: (-SEVERITIES.index(f.severity), f.subject))
    counts = Counter(f.severity for f in findings)
    if args.json:
        print(json.dumps({"dialect": dialect, "findings": [asdict(f) for f in findings]}, indent=2))
    else:
        summary = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "no findings"
        print(f"grants review ({dialect}): {summary}")
        for f in findings:
            print(f"{f.severity.upper():<7} {f.check}  {f.title}")
            print(f"        fix: {f.fix}")
    fail = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) >= fail for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
