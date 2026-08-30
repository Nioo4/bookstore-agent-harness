CREATE TABLE `user_memory`
(
  `memory_id` char(36) NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `memory_type` varchar(32) NOT NULL,
  `memory_key` varchar(191) NOT NULL,
  `value_json` json NOT NULL,
  `source` varchar(16) NOT NULL,
  `confidence` decimal(5,4) NOT NULL,
  `evidence_excerpt` varchar(512) NULL,
  `source_conversation_id` varchar(128) NULL,
  `source_message_id` varchar(128) NULL,
  `write_idempotency_key` varchar(128) NOT NULL,
  `status` varchar(16) NOT NULL,
  `active_slot` tinyint unsigned NULL,
  `superseded_by_memory_id` char(36) NULL,
  `vector_status` varchar(24) NOT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `deleted_at` datetime(3) NULL,
  PRIMARY KEY (`memory_id`),
  UNIQUE KEY `uk_user_memory_active` (`user_id`, `memory_key`, `active_slot`),
  UNIQUE KEY `uk_user_memory_write_idempotency` (`user_id`, `write_idempotency_key`),
  KEY `idx_user_memory_active_type_updated` (`user_id`, `status`, `memory_type`, `updated_at`, `memory_id`),
  KEY `idx_user_memory_vector_status` (`vector_status`, `updated_at`, `memory_id`),
  KEY `idx_user_memory_superseded_by` (`superseded_by_memory_id`),
  CONSTRAINT `fk_user_memory_user` FOREIGN KEY (`user_id`)
    REFERENCES `user_account` (`user_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_user_memory_superseded_by` FOREIGN KEY (`superseded_by_memory_id`)
    REFERENCES `user_memory` (`memory_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_user_memory_type` CHECK (`memory_type` IN ('PREFERENCE', 'NEGATIVE_PREFERENCE', 'EPISODE')),
  CONSTRAINT `ck_user_memory_source` CHECK (`source` IN ('EXPLICIT', 'INFERRED')),
  CONSTRAINT `ck_user_memory_confidence` CHECK (`confidence` BETWEEN 0 AND 1),
  CONSTRAINT `ck_user_memory_status` CHECK (`status` IN ('ACTIVE', 'SUPERSEDED', 'DELETED')),
  CONSTRAINT `ck_user_memory_active_slot` CHECK (`active_slot` IS NULL OR `active_slot` = 1),
  CONSTRAINT `ck_user_memory_status_consistency` CHECK (
    (`status` = 'ACTIVE' AND `active_slot` = 1 AND `deleted_at` IS NULL)
    OR (`status` = 'SUPERSEDED' AND `active_slot` IS NULL AND `deleted_at` IS NULL)
    OR (`status` = 'DELETED' AND `active_slot` IS NULL AND `deleted_at` IS NOT NULL)
  ),
  CONSTRAINT `ck_user_memory_vector_status` CHECK (
    (`memory_type` IN ('PREFERENCE', 'NEGATIVE_PREFERENCE') AND `vector_status` = 'NONE')
    OR (`memory_type` = 'EPISODE' AND `vector_status` IN ('PENDING', 'INDEXED', 'DELETE_PENDING', 'DELETED', 'FAILED'))
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `memory_outbox`
(
  `event_id` char(36) NOT NULL,
  `event_type` varchar(64) NOT NULL,
  `memory_id` char(36) NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `payload_json` json NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'PENDING',
  `available_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `published_at` datetime(3) NULL,
  `attempt_count` int unsigned NOT NULL DEFAULT 0,
  `last_error` varchar(1024) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`event_id`),
  UNIQUE KEY `uk_memory_outbox_type_memory` (`event_type`, `memory_id`),
  KEY `idx_memory_outbox_dispatch` (`status`, `available_at`, `created_at`),
  KEY `idx_memory_outbox_user` (`user_id`, `created_at`),
  CONSTRAINT `fk_memory_outbox_memory` FOREIGN KEY (`memory_id`)
    REFERENCES `user_memory` (`memory_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_memory_outbox_user` FOREIGN KEY (`user_id`)
    REFERENCES `user_account` (`user_id`) ON DELETE CASCADE,
  CONSTRAINT `ck_memory_outbox_event_type` CHECK (`event_type` IN ('memory.index_requested.v1', 'memory.delete_requested.v1')),
  CONSTRAINT `ck_memory_outbox_status` CHECK (`status` IN ('PENDING', 'PUBLISHED', 'FAILED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
