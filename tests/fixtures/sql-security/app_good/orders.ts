export async function find(req) {
  const rows = await pool.query('SELECT * FROM orders WHERE customer_id = $1', [req.query.id]);
  const users = await prisma.$queryRaw`SELECT * FROM users WHERE email = ${email}`;
  const more = await sql`SELECT id FROM posts WHERE author_id = ${id}`;
  console.log(`found ${rows.length} orders`);
  return rows;
}
