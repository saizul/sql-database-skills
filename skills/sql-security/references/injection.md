# Parameterised queries by language

The rule: **values** are always bind parameters. **Identifiers** (table, column, sort
direction) cannot be parameters - map user input through an allow-list. Escaping
functions (`mysqli_real_escape_string`, manual quote doubling) are not a fix.

## Python
```python
# DB-API (psycopg, mysqlclient, PyMySQL): %s placeholders, values as a separate argument
cur.execute("SELECT id FROM users WHERE email = %s AND status = %s", (email, status))
# sqlite3: ?
cur.execute("SELECT id FROM users WHERE email = ?", (email,))
# psycopg 3 identifiers
from psycopg import sql

cur.execute(sql.SQL("SELECT {} FROM users").format(sql.Identifier(column)))
# SQLAlchemy
conn.execute(text("SELECT id FROM users WHERE email = :email"), {"email": email})
# Django
User.objects.raw("SELECT * FROM auth_user WHERE email = %s", [email])
```
Never: `f"... {x}"`, `"..." % x`, `"...".format(x)`, `"..." + x`.

## JavaScript / TypeScript
```js
await pool.query('SELECT * FROM orders WHERE customer_id = $1', [id]);          // node-postgres
await conn.execute('SELECT * FROM orders WHERE customer_id = ?', [id]);         // mysql2
await sql`SELECT * FROM orders WHERE customer_id = ${id}`;                       // postgres.js / slonik (tagged)
await prisma.$queryRaw`SELECT * FROM orders WHERE customer_id = ${id}`;          // Prisma (tagged, safe)
await knex('orders').where({ customer_id: id });  knex.raw('... = ?', [id]);
await sequelize.query('... = :id', { replacements: { id } });
```
Unsafe: `` pool.query(`... ${id}`) `` (untagged template), `$queryRawUnsafe(str)`,
`sequelize.literal(userInput)`.

## PHP
```php
$stmt = $pdo->prepare('SELECT * FROM users WHERE email = :email');
$stmt->execute(['email' => $email]);
$stmt = $mysqli->prepare('SELECT * FROM users WHERE id = ?'); $stmt->bind_param('i', $id);
// Laravel
DB::select('SELECT * FROM users WHERE email = ?', [$email]);
User::whereRaw('lower(email) = ?', [strtolower($email)]);
```
Set `PDO::ATTR_EMULATE_PREPARES => false` for native prepares.

## Ruby on Rails
```ruby
User.where(email: email)
User.where("created_at > ?", since)
User.find_by_sql(["SELECT * FROM users WHERE email = ?", email])
Post.order(ALLOWED_SORT.fetch(params[:sort], "created_at"))   # allow-list for ORDER BY
```
Unsafe: `where("email = '#{email}'")`, `order(params[:sort])`, `pluck(params[:col])`.

## Go
```go
row := db.QueryRowContext(ctx, "SELECT id FROM users WHERE email = $1", email) // pgx / lib/pq
row := db.QueryRowContext(ctx, "SELECT id FROM users WHERE email = ?", email)  // go-sql-driver/mysql
```
`fmt.Sprintf` is only acceptable for allow-listed identifiers; quote them with
`pgx.Identifier{name}.Sanitize()`.

## Java / Kotlin
```java
PreparedStatement ps = conn.prepareStatement("SELECT id FROM users WHERE email = ?");
ps.setString(1, email);
// JPA
em.createQuery("SELECT u FROM User u WHERE u.email = :email").setParameter("email", email);
// Spring JdbcTemplate
jdbc.query("SELECT id FROM users WHERE email = ?", mapper, email);
```

## C#
```csharp
using var cmd = new NpgsqlCommand("SELECT id FROM users WHERE email = @email", conn);
cmd.Parameters.AddWithValue("email", email);
// Dapper
conn.Query<User>("SELECT * FROM users WHERE email = @Email", new { Email = email });
// EF Core: FromSql / FromSqlInterpolated are parameterised; FromSqlRaw with $"..." is not
```

## Dynamic identifiers (sorting, filtering columns)
```python
SORTS = {"newest": "created_at DESC", "price": "price ASC"}
order_by = SORTS.get(request.GET.get("sort"), "created_at DESC")
cur.execute(f"SELECT id FROM products ORDER BY {order_by} LIMIT %s", (limit,))  # sqli-scan: ignore (allow-list)
```

## Defence in depth
- The app's database role cannot DROP, ALTER, read other schemas or files (least-privilege.md)
- Errors are not returned to clients verbatim (stack traces reveal SQL)
- Stored procedures that build dynamic SQL use `format('%I', ...)` / `quote_ident` (PostgreSQL)
  or prepared statements (MySQL `PREPARE ... USING`)
- WAF rules are a supplement, not a fix
