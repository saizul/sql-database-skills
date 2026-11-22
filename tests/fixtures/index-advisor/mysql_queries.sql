SELECT id, title FROM posts WHERE author_id = ? AND status = 'published' ORDER BY published_at DESC LIMIT 10;
SELECT id FROM posts WHERE author_id = ? AND status = ?;
