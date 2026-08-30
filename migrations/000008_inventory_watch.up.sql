ALTER TABLE `outbox_event`
  DROP CHECK `ck_outbox_status`,
  DROP INDEX `idx_outbox_publish`,
  ADD COLUMN `lease_owner` varchar(128) NULL AFTER `status`,
  ADD COLUMN `lease_expires_at` datetime(3) NULL AFTER `lease_owner`;

UPDATE `outbox_event`
SET `published_at` = COALESCE(`published_at`, `created_at`)
WHERE `status` = 'PUBLISHED';

UPDATE `outbox_event`
SET `published_at` = NULL
WHERE `status` IN ('PENDING', 'FAILED');

ALTER TABLE `outbox_event`
  ADD KEY `idx_outbox_claim`
    (`status`, `available_at`, `lease_expires_at`, `created_at`, `event_id`),
  ADD CONSTRAINT `ck_outbox_status`
    CHECK (`status` IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'FAILED')),
  ADD CONSTRAINT `ck_outbox_lease_consistency` CHECK (
    (`status` = 'PENDING' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `published_at` IS NULL)
    OR (`status` = 'PROCESSING' AND `lease_owner` IS NOT NULL AND `lease_expires_at` IS NOT NULL AND `published_at` IS NULL)
    OR (`status` = 'PUBLISHED' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `published_at` IS NOT NULL)
    OR (`status` = 'FAILED' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `published_at` IS NULL)
  );

CREATE TABLE `inventory_event_consumption`
(
  `event_id` char(36) NOT NULL,
  `stream_entry_id` varchar(32) NOT NULL,
  `event_type` varchar(64) NOT NULL,
  `payload_hash` binary(32) NOT NULL,
  `book_id` bigint unsigned NOT NULL,
  `inventory_version` bigint unsigned NOT NULL,
  `status` varchar(16) NOT NULL,
  `attempt_count` int unsigned NOT NULL DEFAULT 0,
  `last_error` varchar(1024) NULL,
  `received_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `completed_at` datetime(3) NULL,
  PRIMARY KEY (`event_id`),
  UNIQUE KEY `uk_inventory_consumption_book_version` (`book_id`, `inventory_version`),
  KEY `idx_inventory_consumption_status_received` (`status`, `received_at`, `event_id`),
  CONSTRAINT `fk_inventory_consumption_book` FOREIGN KEY (`book_id`)
    REFERENCES `book` (`book_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_inventory_consumption_event_type`
    CHECK (`event_type` = 'inventory.stock_changed.v1'),
  CONSTRAINT `ck_inventory_consumption_status`
    CHECK (`status` IN ('PROCESSING', 'COMPLETED', 'DEAD_LETTER')),
  CONSTRAINT `ck_inventory_consumption_state_consistency` CHECK (
    (`status` = 'PROCESSING' AND `completed_at` IS NULL)
    OR (`status` IN ('COMPLETED', 'DEAD_LETTER') AND `completed_at` IS NOT NULL)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `agent_task`
(
  `task_id` char(36) NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `conversation_id` varchar(128) NOT NULL,
  `run_id` varchar(128) NOT NULL,
  `task_type` varchar(32) NOT NULL,
  `status` varchar(16) NOT NULL,
  `book_id` bigint unsigned NOT NULL,
  `book_title_snapshot` varchar(255) NOT NULL,
  `desired_quantity` int unsigned NOT NULL,
  `last_seen_inventory_version` bigint unsigned NOT NULL,
  `last_seen_available_stock` bigint NOT NULL,
  `trigger_event_id` char(36) NULL,
  `create_idempotency_key` varchar(128) NOT NULL,
  `request_hash` binary(32) NOT NULL,
  `checkpoint_status` varchar(16) NOT NULL,
  `active_slot` tinyint unsigned NULL,
  `expires_at` datetime(3) NOT NULL,
  `triggered_at` datetime(3) NULL,
  `completed_at` datetime(3) NULL,
  `expired_at` datetime(3) NULL,
  `cancelled_at` datetime(3) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`task_id`),
  UNIQUE KEY `uk_agent_task_run` (`run_id`),
  UNIQUE KEY `uk_agent_task_user_idempotency` (`user_id`, `create_idempotency_key`),
  UNIQUE KEY `uk_agent_task_active_watch` (`user_id`, `task_type`, `book_id`, `active_slot`),
  KEY `idx_agent_task_user_created` (`user_id`, `created_at`, `task_id`),
  KEY `idx_agent_task_recovery` (`status`, `checkpoint_status`, `updated_at`, `task_id`),
  KEY `idx_agent_task_expiry` (`status`, `expires_at`, `task_id`),
  KEY `idx_agent_task_trigger_event` (`trigger_event_id`),
  CONSTRAINT `fk_agent_task_user` FOREIGN KEY (`user_id`)
    REFERENCES `user_account` (`user_id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_agent_task_book` FOREIGN KEY (`book_id`)
    REFERENCES `book` (`book_id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_agent_task_trigger_event` FOREIGN KEY (`trigger_event_id`)
    REFERENCES `inventory_event_consumption` (`event_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_agent_task_type` CHECK (`task_type` = 'INVENTORY_WATCH'),
  CONSTRAINT `ck_agent_task_status`
    CHECK (`status` IN ('ACTIVE', 'TRIGGERED', 'COMPLETED', 'EXPIRED', 'CANCELLED')),
  CONSTRAINT `ck_agent_task_quantity` CHECK (`desired_quantity` BETWEEN 1 AND 99),
  CONSTRAINT `ck_agent_task_available_stock` CHECK (`last_seen_available_stock` >= 0),
  CONSTRAINT `ck_agent_task_expiry_after_create` CHECK (`expires_at` > `created_at`),
  CONSTRAINT `ck_agent_task_checkpoint_status`
    CHECK (`checkpoint_status` IN ('PENDING', 'READY', 'FAILED')),
  CONSTRAINT `ck_agent_task_active_slot` CHECK (`active_slot` IS NULL OR `active_slot` = 1),
  CONSTRAINT `ck_agent_task_state_consistency` CHECK (
    (`status` = 'ACTIVE' AND `active_slot` = 1 AND `trigger_event_id` IS NULL
      AND `triggered_at` IS NULL AND `completed_at` IS NULL AND `expired_at` IS NULL AND `cancelled_at` IS NULL)
    OR (`status` = 'TRIGGERED' AND `active_slot` = 1 AND `trigger_event_id` IS NOT NULL
      AND `triggered_at` IS NOT NULL AND `completed_at` IS NULL AND `expired_at` IS NULL AND `cancelled_at` IS NULL)
    OR (`status` = 'COMPLETED' AND `active_slot` IS NULL AND `trigger_event_id` IS NOT NULL
      AND `triggered_at` IS NOT NULL AND `completed_at` IS NOT NULL AND `expired_at` IS NULL AND `cancelled_at` IS NULL)
    OR (`status` = 'EXPIRED' AND `active_slot` IS NULL AND `trigger_event_id` IS NULL
      AND `triggered_at` IS NULL AND `completed_at` IS NULL AND `expired_at` IS NOT NULL AND `cancelled_at` IS NULL)
    OR (`status` = 'CANCELLED' AND `active_slot` IS NULL AND `trigger_event_id` IS NULL
      AND `triggered_at` IS NULL AND `completed_at` IS NULL AND `expired_at` IS NULL AND `cancelled_at` IS NOT NULL)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `notification`
(
  `notification_id` char(36) NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `task_id` char(36) NOT NULL,
  `notification_type` varchar(32) NOT NULL,
  `status` varchar(16) NOT NULL,
  `title` varchar(255) NOT NULL,
  `body` varchar(1024) NOT NULL,
  `payload_json` json NOT NULL,
  `read_at` datetime(3) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`notification_id`),
  UNIQUE KEY `uk_notification_task_type` (`task_id`, `notification_type`),
  KEY `idx_notification_user_created` (`user_id`, `created_at`, `notification_id`),
  KEY `idx_notification_user_status_created` (`user_id`, `status`, `created_at`, `notification_id`),
  CONSTRAINT `fk_notification_user` FOREIGN KEY (`user_id`)
    REFERENCES `user_account` (`user_id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_notification_task` FOREIGN KEY (`task_id`)
    REFERENCES `agent_task` (`task_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_notification_type` CHECK (`notification_type` = 'INVENTORY_AVAILABLE'),
  CONSTRAINT `ck_notification_status` CHECK (`status` IN ('UNREAD', 'READ')),
  CONSTRAINT `ck_notification_state_consistency` CHECK (
    (`status` = 'UNREAD' AND `read_at` IS NULL)
    OR (`status` = 'READ' AND `read_at` IS NOT NULL)
  ),
  CONSTRAINT `ck_notification_payload_shape` CHECK (
    JSON_TYPE(`payload_json`) = 'OBJECT'
    AND JSON_LENGTH(`payload_json`) = 6
    AND JSON_CONTAINS_PATH(`payload_json`, 'all',
      '$.book_id', '$.book_title_snapshot', '$.desired_quantity',
      '$.available_stock', '$.inventory_version', '$.triggered_at')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
