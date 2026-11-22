# Least-privilege role model

| Role | Purpose | Can log in | Privileges |
| --- | --- | --- | --- |
| `app_owner` | Owns schema objects; runs migrations | PostgreSQL: NOLOGIN (migrations `SET ROLE app_owner`) / MySQL: separate `migrator` account | DDL on the app schema only |
| `app` | Runtime application | yes, from app hosts only | SELECT, INSERT, UPDATE, DELETE on app tables; USAGE on sequences; EXECUTE on needed functions |
| `app_readonly` | Reporting, BI, support | yes | SELECT (ideally on a replica) |
| personal accounts | Humans | yes, with SSO/strong auth | membership in the roles above as needed, time-limited elevation |
| superuser / root | Break glass | localhost / bastion only | everything; never used by applications |

## PostgreSQL
```sql
CREATE ROLE app_owner NOLOGIN;
CREATE ROLE app LOGIN PASSWORD :'app_password';           -- psql variable, not in the file
CREATE ROLE app_readonly NOLOGIN;
CREATE ROLE reporting LOGIN IN ROLE app_readonly PASSWORD :'reporting_password';

REVOKE CREATE ON SCHEMA public FROM PUBLIC;                -- default before PostgreSQL 15
REVOKE ALL ON DATABASE shop FROM PUBLIC;
GRANT CONNECT ON DATABASE shop TO app, app_readonly;
CREATE SCHEMA app AUTHORIZATION app_owner;
GRANT USAGE ON SCHEMA app TO app, app_readonly;

-- existing objects
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO app;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO app_readonly;
-- future objects created by app_owner
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT USAGE, SELECT ON SEQUENCES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT ON TABLES TO app_readonly;

-- transfer ownership away from a login role
REASSIGN OWNED BY app TO app_owner;    -- per database; review first
```
Also: `password_encryption = 'scram-sha-256'`, `pg_hba.conf` with `hostssl ... scram-sha-256`
for specific networks, `ALTER ROLE app SET statement_timeout = '30s'`,
`ALTER ROLE app CONNECTION LIMIT 100`. `--no-role-passwords` dumps hide hashes, so the md5
check needs `SELECT rolname FROM pg_authid WHERE rolpassword LIKE 'md5%'` (superuser).

## MySQL / MariaDB
```sql
CREATE USER 'app'@'10.0.1.%' IDENTIFIED WITH caching_sha2_password BY '<from secret manager>'
  REQUIRE SSL;
GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO 'app'@'10.0.1.%';
CREATE USER 'migrator'@'10.0.1.5' IDENTIFIED BY '<secret>' REQUIRE SSL;
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES, CREATE VIEW, TRIGGER
  ON shop.* TO 'migrator'@'10.0.1.5';
CREATE USER 'reporting'@'10.0.2.%' IDENTIFIED BY '<secret>';
GRANT SELECT ON shop.* TO 'reporting'@'10.0.2.%';
-- MySQL 8 roles
CREATE ROLE app_rw; GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO app_rw;
GRANT app_rw TO 'app'@'10.0.1.%'; SET DEFAULT ROLE app_rw TO 'app'@'10.0.1.%';
-- clean up
DROP USER IF EXISTS ''@'localhost', ''@'%';   -- anonymous accounts (old installs)
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'app'@'%';
```
Never grant `FILE` (reads/writes server files), `SUPER`, `PROCESS` (sees other sessions'
queries), `SHUTDOWN`, `CREATE USER` or `*.*` privileges to application accounts.

## Rollout order (no downtime)
1. Create new roles/users and grants alongside the old ones
2. Switch the application to the new credentials (rolling deploy)
3. Watch errors (permission denied) for a day
4. Revoke the old privileges / drop the old user
