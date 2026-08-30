ALTER TABLE `book`
  ADD COLUMN `subtitle` varchar(255) NULL AFTER `title`,
  ADD COLUMN `description` text NULL AFTER `subtitle`,
  ADD COLUMN `publisher` varchar(255) NULL AFTER `description`,
  ADD COLUMN `publication_date` date NULL AFTER `publisher`,
  ADD COLUMN `language_code` varchar(10) NOT NULL DEFAULT 'zh-CN' AFTER `publication_date`,
  ADD COLUMN `page_count` int unsigned NULL AFTER `language_code`,
  ADD COLUMN `cover_url` varchar(1024) NULL AFTER `page_count`,
  ADD COLUMN `currency` char(3) NOT NULL DEFAULT 'CNY' AFTER `price`,
  ADD COLUMN `status` varchar(16) NOT NULL DEFAULT 'ACTIVE' AFTER `currency`,
  ADD COLUMN `version` bigint unsigned NOT NULL DEFAULT 1 AFTER `status`,
  ADD COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) AFTER `version`,
  ADD COLUMN `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3) AFTER `created_at`,
  ADD CONSTRAINT `ck_book_price_nonnegative` CHECK (`price` >= 0),
  ADD CONSTRAINT `ck_book_page_count_positive` CHECK (`page_count` IS NULL OR `page_count` > 0),
  ADD CONSTRAINT `ck_book_status` CHECK (`status` IN ('DRAFT', 'ACTIVE', 'INACTIVE')),
  ADD FULLTEXT KEY `ft_book_search` (`title`, `subtitle`, `description`),
  ADD KEY `idx_book_status_date_id` (`status`, `publication_date`, `book_id`),
  ADD KEY `idx_book_language_price_id` (`language_code`, `price`, `book_id`);

CREATE TABLE `author`
(
  `author_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `bio` text NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`author_id`),
  UNIQUE KEY `uk_author_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `book_author`
(
  `book_id` bigint unsigned NOT NULL,
  `author_id` bigint unsigned NOT NULL,
  `role` varchar(32) NOT NULL DEFAULT 'AUTHOR',
  `sort_order` int unsigned NOT NULL DEFAULT 0,
  PRIMARY KEY (`book_id`, `author_id`, `role`),
  KEY `idx_book_author_author_book` (`author_id`, `book_id`),
  CONSTRAINT `fk_book_author_book` FOREIGN KEY (`book_id`) REFERENCES `book` (`book_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_book_author_author` FOREIGN KEY (`author_id`) REFERENCES `author` (`author_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `category`
(
  `category_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `parent_id` bigint unsigned NULL,
  `name` varchar(255) NOT NULL,
  `slug` varchar(255) NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'ACTIVE',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`category_id`),
  UNIQUE KEY `uk_category_slug` (`slug`),
  KEY `idx_category_parent` (`parent_id`),
  CONSTRAINT `fk_category_parent` FOREIGN KEY (`parent_id`) REFERENCES `category` (`category_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_category_status` CHECK (`status` IN ('ACTIVE', 'INACTIVE'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `book_category`
(
  `book_id` bigint unsigned NOT NULL,
  `category_id` bigint unsigned NOT NULL,
  PRIMARY KEY (`book_id`, `category_id`),
  KEY `idx_book_category_category_book` (`category_id`, `book_id`),
  CONSTRAINT `fk_book_category_book` FOREIGN KEY (`book_id`) REFERENCES `book` (`book_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_book_category_category` FOREIGN KEY (`category_id`) REFERENCES `category` (`category_id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `inventory`
(
  `book_id` bigint unsigned NOT NULL,
  `available_stock` bigint NOT NULL DEFAULT 0,
  `reserved_stock` bigint NOT NULL DEFAULT 0,
  `version` bigint unsigned NOT NULL DEFAULT 1,
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`book_id`),
  CONSTRAINT `fk_inventory_book` FOREIGN KEY (`book_id`) REFERENCES `book` (`book_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_inventory_available_nonnegative` CHECK (`available_stock` >= 0),
  CONSTRAINT `ck_inventory_reserved_nonnegative` CHECK (`reserved_stock` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `author` (`name`)
SELECT DISTINCT TRIM(`author`)
FROM `book`
WHERE TRIM(`author`) <> ''
ON DUPLICATE KEY UPDATE `name` = VALUES(`name`);

INSERT IGNORE INTO `book_author` (`book_id`, `author_id`, `role`, `sort_order`)
SELECT b.`book_id`, a.`author_id`, 'AUTHOR', 0
FROM `book` b
JOIN `author` a ON a.`name` = TRIM(b.`author`)
WHERE TRIM(b.`author`) <> '';

INSERT INTO `inventory` (`book_id`, `available_stock`, `reserved_stock`, `version`)
SELECT `book_id`, `stock_count`, 0, 1
FROM `book`
ON DUPLICATE KEY UPDATE
  `available_stock` = VALUES(`available_stock`),
  `reserved_stock` = VALUES(`reserved_stock`);

