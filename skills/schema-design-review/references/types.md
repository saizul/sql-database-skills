# Type choices

| Data | PostgreSQL | MySQL / MariaDB | Avoid |
| --- | --- | --- | --- |
| Surrogate id | `bigint GENERATED ALWAYS AS IDENTITY` | `BIGINT UNSIGNED AUTO_INCREMENT` | `int`/`serial` on growing tables |
| Public / distributed id | `uuid` (v7 for locality; `gen_random_uuid()` is v4) | `BINARY(16)` + `UUID_TO_BIN(uuid, 1)` | `CHAR(36)` as clustered PK in InnoDB |
| Money | `numeric(12,2)` + `currency char(3)` | `DECIMAL(12,2)` | `float`, `double`, `real`, `money` type |
| Point in time | `timestamptz` | `DATETIME(6)` stored in UTC (or `TIMESTAMP` before 2038) | `timestamp` without zone in PostgreSQL |
| Calendar date | `date` | `DATE` | strings |
| Duration | `interval` | `INT` seconds | strings |
| Short text | `text` (+ `CHECK (length(x) <= n)`) | `VARCHAR(n)` sized to the data | `VARCHAR(255)` by reflex |
| Case-insensitive text | `citext` or `lower()` expression index | `_ci` collation (default) | - |
| Boolean | `boolean` | `BOOLEAN` (`TINYINT(1)`) | `CHAR(1)` 'Y'/'N' |
| Status | `text` + CHECK, or enum | `ENUM(...)` or `VARCHAR` + CHECK (8.0.16+) | free text |
| JSON document | `jsonb` | `JSON` | `text` with JSON in it |
| IP address | `inet` / `cidr` | `VARBINARY(16)` + `INET6_ATON` | `varchar(15)` |
| Binary | `bytea` (or object storage + key) | `VARBINARY` / `BLOB` | large blobs in hot tables |

Integer limits: `integer`/`INT` 2,147,483,647 (4,294,967,295 unsigned); `bigint` 9.2e18.
Changing an id from int to bigint later rewrites the table and every referencing table.
