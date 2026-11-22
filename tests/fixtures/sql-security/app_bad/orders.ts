export async function find(req, res) {
  const rows = await pool.query(`SELECT * FROM orders WHERE customer_id = ${req.query.id}`);
  const more = await prisma.$queryRawUnsafe("SELECT * FROM users WHERE email = '" + email + "'");
  const sql = "UPDATE users SET name = '" + name + "' WHERE id = " + id;
  return rows;
}
