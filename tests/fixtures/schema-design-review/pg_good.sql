CREATE TABLE public.customers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email text NOT NULL UNIQUE,
    first_name text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.orders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES public.customers (id),
    total numeric(12,2) NOT NULL CHECK (total >= 0),
    placed_at timestamptz NOT NULL
);
CREATE INDEX orders_customer_id_idx ON public.orders USING btree (customer_id, placed_at);

CREATE TABLE public.customer_phones (
    customer_id bigint NOT NULL REFERENCES public.customers (id),
    phone text NOT NULL,
    PRIMARY KEY (customer_id, phone)
);
