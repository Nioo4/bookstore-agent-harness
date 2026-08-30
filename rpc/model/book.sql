CREATE TABLE `book`
(
  `book_id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT 'internal book id',
  `isbn` varchar(32) NOT NULL COMMENT 'ISBN-10/ISBN-13 or legacy compatibility id',
  `title` varchar(255) NOT NULL COMMENT 'book title',
  `author` varchar(255) NOT NULL DEFAULT '' COMMENT 'book author',
  `price` bigint NOT NULL COMMENT 'book price in the smallest currency unit',
  `stock_count` bigint NOT NULL DEFAULT 0 COMMENT 'available stock',
  PRIMARY KEY (`book_id`),
  UNIQUE KEY `uk_book_isbn` (`isbn`),
  KEY `idx_book_title` (`title`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `stock_deduction`
(
  `idempotency_key` varchar(64) NOT NULL COMMENT 'client supplied unique request key',
  `book_id` bigint unsigned NOT NULL,
  `quantity` bigint NOT NULL,
  `outcome` varchar(16) NOT NULL COMMENT 'PROCESSING/SUCCEEDED/INSUFFICIENT/NOT_FOUND',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idempotency_key`),
  KEY `idx_stock_deduction_book_id` (`book_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
