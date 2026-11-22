-- pg_dump --schema-only (trimmed, anonymised)
CREATE TABLE public.customers (
    id integer NOT NULL,
    email character varying(255),
    "firstName" character varying(255),
    phone1 character varying(255),
    phone2 character varying(255),
    created_at timestamp without time zone
);

CREATE TABLE public.orders (
    id bigint NOT NULL,
    customer_id bigint NOT NULL,
    total double precision NOT NULL,
    tag_ids text,
    placed_at timestamp with time zone NOT NULL
);

CREATE TABLE public.event_log (
    payload jsonb,
    received_at timestamptz
);

CREATE TABLE public."user" (
    id serial PRIMARY KEY,
    name text NOT NULL
);

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);
