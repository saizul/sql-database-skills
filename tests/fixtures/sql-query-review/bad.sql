-- Queries collected from an order service (anonymised)
SELECT * FROM orders WHERE customer_id = 42;

SELECT id, total FROM orders WHERE DATE(created_at) = '2026-01-15';

SELECT id FROM users WHERE LOWER(email) = LOWER('a@example.com');

SELECT id FROM products WHERE name LIKE '%phone%';

SELECT id FROM orders WHERE status = 'paid' OR customer_id = 7;

SELECT id FROM orders WHERE status = 'new' OR status = 'paid' OR status = 'shipped';

SELECT id FROM customers WHERE id NOT IN (SELECT customer_id FROM orders);

SELECT id FROM orders WHERE shipped_at = NULL;

UPDATE orders SET status = 'archived';

DELETE FROM sessions;

SELECT id FROM products ORDER BY RAND() LIMIT 5;

SELECT id, total FROM orders ORDER BY created_at DESC LIMIT 20 OFFSET 50000;

SELECT id FROM customers WHERE phone = 5551234;

SELECT o.id, c.name FROM orders o, customers c WHERE o.customer_id = c.id;

INSERT INTO audit_log VALUES (1, 'login', now());

SELECT id FROM a UNION SELECT id FROM b;

SELECT id FROM orders WHERE created_at::date = '2026-01-15';

SELECT id FROM orders WHERE (customer_id = $1 OR $1 IS NULL);

SELECT id FROM orders WHERE total * 2 > 100;
