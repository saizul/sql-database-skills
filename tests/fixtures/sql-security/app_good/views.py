from django.db import connection

ALLOWED_SORT = {"name": "name", "price": "price"}


def search(request):
    with connection.cursor() as cur:
        cur.execute("SELECT id, email FROM users WHERE name = %s", [request.GET["name"]])
    order = ALLOWED_SORT.get(request.GET.get("sort"), "name")
    rows = Product.objects.raw("SELECT * FROM products ORDER BY " + order)  # sqli-scan: ignore (allow-listed)
    log.info(f"loaded {len(rows)} rows")
    return rows
