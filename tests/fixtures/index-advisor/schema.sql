-- pg_dump --schema-only style (trimmed)
CREATE TABLE public.customers (
    id bigint NOT NULL,
    email text NOT NULL,
    country text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.orders (
    id bigint NOT NULL,
    customer_id bigint NOT NULL,
    status text NOT NULL,
    total numeric(12,2) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.order_items (
    id bigint NOT NULL,
    order_id bigint NOT NULL,
    product_id bigint NOT NULL,
    quantity integer NOT NULL
);

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_email_key UNIQUE (email);
ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_pkey PRIMARY KEY (id);

CREATE INDEX orders_customer_id_idx ON public.orders USING btree (customer_id);
CREATE INDEX orders_status_idx ON public.orders USING btree (status);
CREATE INDEX orders_status_created_idx ON public.orders USING btree (status, created_at);
CREATE INDEX orders_status_dup_idx ON public.orders USING btree (status);
CREATE INDEX customers_lower_email_idx ON public.customers USING btree (lower(email));
