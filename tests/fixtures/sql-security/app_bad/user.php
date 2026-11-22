<?php
$id = $_GET['id'];
$result = mysqli_query($conn, "SELECT * FROM users WHERE id = $id");
$rows = $pdo->query("SELECT name FROM posts WHERE author = '" . $_POST['author'] . "'");
