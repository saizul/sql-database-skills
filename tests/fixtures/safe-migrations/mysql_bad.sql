-- 2026_11_01_000000_alter_posts.sql (MySQL 8)
ALTER TABLE `posts` MODIFY COLUMN `title` varchar(500) NOT NULL;
ALTER TABLE `posts` ADD COLUMN `slug` varchar(200) NULL AFTER `title`;
ALTER TABLE `posts` ADD INDEX `idx_slug` (`slug`);
ALTER TABLE `posts` ADD FULLTEXT INDEX `ft_body` (`body`);
ALTER TABLE `posts` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE `posts` DROP PRIMARY KEY, ADD PRIMARY KEY (`id`, `author_id`);
ALTER TABLE `posts` CHANGE `body` `content` mediumtext;
RENAME TABLE `posts` TO `articles`;
OPTIMIZE TABLE `comments`;
TRUNCATE TABLE `sessions`;
