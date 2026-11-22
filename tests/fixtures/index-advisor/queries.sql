-- 1: customer order history page
SELECT id, total, created_at FROM orders
WHERE customer_id = $1 AND status = 'paid'
ORDER BY created_at DESC LIMIT 20;

-- 2: login by email (served by the unique constraint)
SELECT id FROM customers WHERE email = $1;

-- 3: order lines for an order
SELECT oi.product_id, oi.quantity
FROM orders o JOIN order_items oi ON oi.order_id = o.id
WHERE o.id = $1;

-- 4: dashboard report
SELECT c.country, count(*) FROM customers c
WHERE c.created_at >= '2026-01-01' AND c.created_at < '2026-02-01'
GROUP BY c.country;

-- 5: pending orders queue (served by orders_status_created_idx)
SELECT id FROM orders WHERE status = 'pending' ORDER BY created_at LIMIT 100;

-- 6: OR across columns
SELECT id FROM orders WHERE status = 'new' OR customer_id = 5;

-- 7: subquery
SELECT id, email FROM customers
WHERE id IN (SELECT customer_id FROM orders WHERE total > 1000 AND status = 'paid');
