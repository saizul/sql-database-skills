SET SESSION lock_wait_timeout = 5;
ALTER TABLE `posts` ADD COLUMN `slug` varchar(200) NULL, ALGORITHM=INSTANT;
ALTER TABLE `posts` ADD INDEX `idx_slug` (`slug`), ALGORITHM=INPLACE, LOCK=NONE;
CREATE TABLE `tags` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(64) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
ALTER TABLE `tags` ADD UNIQUE KEY `uq_name` (`name`);
