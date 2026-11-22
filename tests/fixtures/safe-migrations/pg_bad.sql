-- 20261101_orders_changes.sql (PostgreSQL)
BEGIN;
CREATE INDEX orders_status_idx ON orders (status);
CREATE INDEX CONCURRENTLY orders_created_idx ON orders (created_at);
ALTER TABLE orders ADD COLUMN public_id uuid DEFAULT gen_random_uuid();
ALTER TABLE orders ADD COLUMN region text NOT NULL;
ALTER TABLE orders ALTER COLUMN total TYPE numeric(14,2);
ALTER TABLE orders ALTER COLUMN status SET NOT NULL;
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE orders ADD CONSTRAINT orders_total_positive CHECK (total >= 0);
ALTER TABLE orders ADD CONSTRAINT orders_public_id_key UNIQUE (public_id);
ALTER TABLE orders RENAME COLUMN total TO amount;
ALTER TABLE orders DROP COLUMN legacy_flag;
UPDATE orders SET region = 'eu';
DROP INDEX orders_old_idx;
DROP TABLE order_archive;
VACUUM FULL orders;
COMMIT;
