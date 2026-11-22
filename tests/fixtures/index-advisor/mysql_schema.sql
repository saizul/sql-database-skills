-- mysqldump --no-data style
CREATE TABLE `posts` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `author_id` bigint unsigned NOT NULL,
  `status` varchar(20) NOT NULL,
  `published_at` datetime DEFAULT NULL,
  `title` varchar(200) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_author` (`author_id`),
  KEY `idx_author_status` (`author_id`,`status`),
  UNIQUE KEY `uq_title` (`title`(100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
