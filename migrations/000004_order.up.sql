CREATE TABLE `pending_action`
(
  `action_id` char(36) NOT NULL,
  `confirmation_digest` binary(32) NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `conversation_id` varchar(64) NOT NULL,
  `items_hash` binary(32) NOT NULL,
  `items_json` json NOT NULL,
  `total_amount` bigint NOT NULL,
  `currency` char(3) NOT NULL,
  `idempotency_key` varchar(64) NOT NULL,
  `expires_at` datetime(3) NOT NULL,
  `used_at` datetime(3) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`action_id`),
  UNIQUE KEY `uk_pending_confirmation_digest` (`confirmation_digest`),
  UNIQUE KEY `uk_pending_user_idempotency` (`user_id`, `idempotency_key`),
  KEY `idx_pending_expiry` (`expires_at`),
  CONSTRAINT `fk_pending_action_user` FOREIGN KEY (`user_id`) REFERENCES `user_account` (`user_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_pending_total_nonnegative` CHECK (`total_amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `customer_order`
(
  `order_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `conversation_id` varchar(64) NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'PLACED',
  `total_amount` bigint NOT NULL,
  `currency` char(3) NOT NULL,
  `idempotency_key` varchar(64) NOT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`order_id`),
  UNIQUE KEY `uk_order_user_idempotency` (`user_id`, `idempotency_key`),
  KEY `idx_order_user_created` (`user_id`, `created_at`, `order_id`),
  CONSTRAINT `fk_customer_order_user` FOREIGN KEY (`user_id`) REFERENCES `user_account` (`user_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_order_status` CHECK (`status` IN ('PLACED', 'CANCELLED')),
  CONSTRAINT `ck_order_total_nonnegative` CHECK (`total_amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `order_item`
(
  `order_item_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id` bigint unsigned NOT NULL,
  `book_id` bigint unsigned NOT NULL,
  `isbn_snapshot` varchar(32) NOT NULL,
  `title_snapshot` varchar(255) NOT NULL,
  `unit_price` bigint NOT NULL,
  `quantity` bigint NOT NULL,
  `line_total` bigint NOT NULL,
  PRIMARY KEY (`order_item_id`),
  UNIQUE KEY `uk_order_item_book` (`order_id`, `book_id`),
  KEY `idx_order_item_book` (`book_id`),
  CONSTRAINT `fk_order_item_order` FOREIGN KEY (`order_id`) REFERENCES `customer_order` (`order_id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_order_item_book` FOREIGN KEY (`book_id`) REFERENCES `book` (`book_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_order_item_price_nonnegative` CHECK (`unit_price` >= 0),
  CONSTRAINT `ck_order_item_quantity_positive` CHECK (`quantity` > 0),
  CONSTRAINT `ck_order_item_total_nonnegative` CHECK (`line_total` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `outbox_event`
(
  `event_id` char(36) NOT NULL,
  `event_type` varchar(64) NOT NULL,
  `aggregate_type` varchar(32) NOT NULL,
  `aggregate_id` varchar(64) NOT NULL,
  `payload_json` json NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'PENDING',
  `available_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `published_at` datetime(3) NULL,
  `attempt_count` int unsigned NOT NULL DEFAULT 0,
  `last_error` varchar(1024) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`event_id`),
  KEY `idx_outbox_publish` (`status`, `available_at`, `created_at`),
  CONSTRAINT `ck_outbox_status` CHECK (`status` IN ('PENDING', 'PUBLISHED', 'FAILED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

