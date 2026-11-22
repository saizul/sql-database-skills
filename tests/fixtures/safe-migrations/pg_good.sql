-- Zero-downtime version (run outside a transaction)
SET lock_timeout = '5s';
SET statement_timeout = '15min';

CREATE TABLE shipments (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id bigint NOT NULL,
    carrier text NOT NULL
);
CREATE INDEX shipments_order_id_idx ON shipments (order_id);
ALTER TABLE shipments ADD CONSTRAINT shipments_order_fk FOREIGN KEY (order_id) REFERENCES orders (id);

CREATE INDEX CONCURRENTLY orders_status_idx ON orders (status);
ALTER TABLE orders ADD COLUMN region text;
ALTER TABLE orders ADD COLUMN created_on timestamptz DEFAULT now();
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_fk;
ALTER TABLE orders ADD CONSTRAINT orders_status_nn CHECK (status IS NOT NULL) NOT VALID;
CREATE UNIQUE INDEX CONCURRENTLY orders_public_id_key ON orders (public_id);
ALTER TABLE orders ADD CONSTRAINT orders_public_id_key UNIQUE USING INDEX orders_public_id_key;
DROP INDEX CONCURRENTLY orders_old_idx;
-- migration-lint: ignore drop_column (column unused since release 41)
ALTER TABLE orders DROP COLUMN legacy_flag;
