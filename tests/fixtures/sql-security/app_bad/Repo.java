class Repo {
  ResultSet find(String email) throws Exception {
    return stmt.executeQuery("SELECT * FROM users WHERE email = '" + email + "'");
  }
}
