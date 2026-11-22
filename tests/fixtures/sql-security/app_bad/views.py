from django.db import connection


def search(request):
    name = request.GET["name"]
    with connection.cursor() as cur:
        cur.execute(f"SELECT id, email FROM users WHERE name = '{name}'")
        cur.execute("SELECT id FROM orders WHERE status = '%s'" % request.GET["status"])
    query = "SELECT * FROM products WHERE sku = '" + sku + "'"
    sql = "DELETE FROM carts WHERE id = {}".format(cart_id)
    order = request.GET.get("sort")
    rows = Product.objects.raw(f"SELECT * FROM products ORDER BY {order}")
    return rows
