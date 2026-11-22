# Before / after rewrites

## Date functions -> ranges
```sql
-- before (no index use)
WHERE DATE(created_at) = '2026-01-15'
WHERE YEAR(created_at) = 2026
WHERE created_at::date = CURRENT_DATE
-- after
WHERE created_at >= '2026-01-15' AND created_at < '2026-01-16'
WHERE created_at >= '2026-01-01' AND created_at < '2027-01-01'
WHERE created_at >= CURRENT_DATE AND created_at < CURRENT_DATE + 1
```

## Case-insensitive lookups
```sql
-- PostgreSQL: expression index, query must match the expression exactly
CREATE INDEX CONCURRENTLY users_lower_email_idx ON users (lower(email));
SELECT id FROM users WHERE lower(email) = lower($1);
-- or citext: ALTER TABLE users ALTER COLUMN email TYPE citext;  (rewrite! see safe-migrations)
-- MySQL: the default *_ci collations are already case-insensitive
SELECT id FROM users WHERE email = ?;
```

## OR across columns -> UNION ALL
```sql
-- before
SELECT id FROM orders WHERE customer_id = 7 OR assigned_to = 7;
-- after (each branch uses its own index; second branch excludes rows already returned)
SELECT id FROM orders WHERE customer_id = 7
UNION ALL
SELECT id FROM orders WHERE assigned_to = 7 AND customer_id IS DISTINCT FROM 7;
```
MySQL: replace `IS DISTINCT FROM 7` with `NOT (customer_id <=> 7)`.

## NOT IN -> NOT EXISTS
```sql
-- before: returns nothing if any orders.customer_id is NULL
SELECT id FROM customers WHERE id NOT IN (SELECT customer_id FROM orders);
-- after
SELECT c.id FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id);
```

## OFFSET -> keyset pagination
```sql
-- before
SELECT id, created_at FROM orders ORDER BY created_at DESC LIMIT 20 OFFSET 40000;
-- after: pass the last row of the previous page
SELECT id, created_at FROM orders
WHERE (created_at, id) < ($1, $2)
ORDER BY created_at DESC, id DESC
LIMIT 20;
-- index: (created_at, id)
```
MySQL 8 supports row constructors too, but older versions optimise
`created_at < ? OR (created_at = ? AND id < ?)` better.

## Random rows
```sql
-- PostgreSQL, approximate sample
SELECT id FROM products TABLESAMPLE SYSTEM (1) LIMIT 5;
-- exact and fast with dense ids: pick ids in the app, then
SELECT id FROM products WHERE id = ANY($1);
```

## Optional filters
```python
# build the predicate list in code instead of (col = %s OR %s IS NULL)
clauses, params = [], []
if status:
    clauses.append("status = %s")
    params.append(status)
if customer_id:
    clauses.append("customer_id = %s")
    params.append(customer_id)
sql = "SELECT id FROM orders" + (" WHERE " + " AND ".join(clauses) if clauses else "")
```

## N+1 -> one query
```sql
-- before: once per post
SELECT * FROM comments WHERE post_id = ?;
-- after
SELECT post_id, id, body FROM comments WHERE post_id = ANY($1);   -- PostgreSQL
SELECT post_id, id, body FROM comments WHERE post_id IN (?, ?, ?); -- MySQL
```
ORMs: Django `prefetch_related("comments")`, Rails `includes(:comments)`,
Laravel `with('comments')`, Prisma `include: { comments: true }`, SQLAlchemy `selectinload`.

## Existence
```sql
-- before
SELECT count(*) FROM orders WHERE customer_id = $1;   -- app checks > 0
-- after
SELECT EXISTS (SELECT 1 FROM orders WHERE customer_id = $1);
```
