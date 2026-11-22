-- Index-friendly versions of the same queries
SELECT id, total, status FROM orders WHERE customer_id = 42;

SELECT id, total FROM orders
WHERE created_at >= '2026-01-15' AND created_at < '2026-01-16';

SELECT id FROM users WHERE email = 'a@example.com';

SELECT id FROM products WHERE name LIKE 'phone%';

SELECT id FROM orders WHERE status IN ('new', 'paid', 'shipped');

SELECT c.id FROM customers c
WHERE NOT EXISTS (SELECT * FROM orders o WHERE o.customer_id = c.id);

SELECT id FROM orders WHERE shipped_at IS NULL;

UPDATE orders SET status = 'archived' WHERE created_at < '2024-01-01';

SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id
WHERE o.created_at < $1 ORDER BY o.created_at DESC, o.id DESC LIMIT 20;

INSERT INTO audit_log (user_id, action, created_at) VALUES (1, 'login', now())
ON CONFLICT (user_id, action) DO UPDATE SET created_at = EXCLUDED.created_at;

SELECT id FROM a UNION ALL SELECT id FROM b;

SELECT count(*) FROM orders WHERE data #> '{a,b}' = '"x"';

SELECT id FROM t WHERE body = $$it's ; fine$$;

-- sql-lint: ignore select_star
SELECT * FROM tiny_lookup_table;
