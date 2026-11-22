--
-- PostgreSQL database dump
--

-- Dumped from database version 16.4 (Ubuntu 16.4-1.pgdg24.04+1)
-- Dumped by pg_dump version 16.4 (Ubuntu 16.4-1.pgdg24.04+1)

SET statement_timeout = 0;
SET client_encoding = 'UTF8';

CREATE TABLE public.customers (
    id bigint NOT NULL,
    email text NOT NULL
);

ALTER TABLE public.customers OWNER TO app;

CREATE TABLE public.orders (
    id bigint NOT NULL,
    customer_id bigint NOT NULL
);

ALTER TABLE public.orders OWNER TO app;

CREATE TABLE public.audit_log (
    id bigint NOT NULL
);

COPY public.customers (id, email) FROM stdin;
1	user1@example.com
2	user2@example.com
3	user3@example.com
4	user4@example.com
5	user5@example.com
6	user6@example.com
7	user7@example.com
8	user8@example.com
9	user9@example.com
10	user10@example.com
11	user11@example.com
12	user12@example.com
13	user13@example.com
14	user14@example.com
15	user15@example.com
16	user16@example.com
17	user17@example.com
18	user18@example.com
19	user19@example.com
20	user20@example.com
21	user21@example.com
22	user22@example.com
23	user23@example.com
24	user24@example.com
25	user25@example.com
26	user26@example.com
27	user27@example.com
28	user28@example.com
29	user29@example.com
30	user30@example.com
31	user31@example.com
32	user32@example.com
33	user33@example.com
34	user34@example.com
35	user35@example.com
36	user36@example.com
37	user37@example.com
38	user38@example.com
39	user39@example.com
40	user40@example.com
41	user41@example.com
42	user42@example.com
43	user43@example.com
44	user44@example.com
45	user45@example.com
46	user46@example.com
47	user47@example.com
48	user48@example.com
49	user49@example.com
50	user50@example.com
\.

COPY public.orders (id, customer_id) FROM stdin;
1	2
2	3
3	4
4	5
5	6
6	7
7	8
8	9
9	10
10	11
11	12
12	13
13	14
14	15
15	16
16	17
17	18
18	19
19	20
20	21
21	22
22	23
23	24
24	25
25	26
26	27
27	28
28	29
29	30
30	31
31	32
32	33
33	34
34	35
35	36
36	37
37	38
38	39
39	40
40	41
41	42
42	43
43	44
44	45
45	46
46	47
47	48
48	49
49	50
50	1
51	2
52	3
53	4
54	5
55	6
56	7
57	8
58	9
59	10
60	11
61	12
62	13
63	14
64	15
65	16
66	17
67	18
68	19
69	20
70	21
71	22
72	23
73	24
74	25
75	26
76	27
77	28
78	29
79	30
80	31
81	32
82	33
83	34
84	35
85	36
86	37
87	38
88	39
89	40
90	41
91	42
92	43
93	44
94	45
95	46
96	47
97	48
98	49
99	50
100	1
101	2
102	3
103	4
104	5
105	6
106	7
107	8
108	9
109	10
110	11
111	12
112	13
113	14
114	15
115	16
116	17
117	18
118	19
119	20
120	21
\.

CREATE INDEX orders_customer_id_idx ON public.orders USING btree (customer_id);

--
-- PostgreSQL database dump complete
--

