package store

func Find(db *sql.DB, name string) {
	q := fmt.Sprintf("SELECT id FROM users WHERE name = '%s'", name)
	rows, _ := db.Query(q)
	_ = rows
}
