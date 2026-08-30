ALTER TABLE `pending_action`
  DROP CHECK `ck_pending_total_nonnegative`;

ALTER TABLE `pending_action`
  CHANGE COLUMN `action_id` `pending_action_id` char(36) NOT NULL,
  CHANGE COLUMN `confirmation_digest` `token_hash` binary(32) NOT NULL,
  CHANGE COLUMN `items_hash` `payload_hash` binary(32) NOT NULL,
  CHANGE COLUMN `total_amount` `quoted_amount` bigint NOT NULL,
  CHANGE COLUMN `idempotency_key` `prepare_idempotency_key` varchar(64) NOT NULL,
  CHANGE COLUMN `used_at` `consumed_at` datetime(3) NULL,
  ADD COLUMN `action_type` varchar(32) NOT NULL DEFAULT 'CREATE_ORDER' AFTER `conversation_id`,
  ADD COLUMN `status` varchar(16) NOT NULL DEFAULT 'PENDING' AFTER `currency`,
  ADD COLUMN `decision` varchar(16) NULL AFTER `status`,
  ADD COLUMN `order_id` bigint unsigned NULL AFTER `decision`,
  DROP INDEX `uk_pending_confirmation_digest`,
  DROP INDEX `uk_pending_user_idempotency`,
  ADD UNIQUE KEY `uk_pending_token_hash` (`token_hash`),
  ADD UNIQUE KEY `uk_pending_user_prepare_idempotency` (`user_id`, `prepare_idempotency_key`),
  ADD KEY `idx_pending_order` (`order_id`),
  ADD CONSTRAINT `fk_pending_order` FOREIGN KEY (`order_id`) REFERENCES `customer_order` (`order_id`) ON DELETE RESTRICT,
  ADD CONSTRAINT `ck_pending_action_type` CHECK (`action_type` = 'CREATE_ORDER'),
  ADD CONSTRAINT `ck_pending_status` CHECK (`status` IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
  ADD CONSTRAINT `ck_pending_decision` CHECK (`decision` IS NULL OR `decision` IN ('APPROVE', 'REJECT'));

ALTER TABLE `pending_action`
  ADD CONSTRAINT `ck_pending_total_nonnegative` CHECK (`quoted_amount` >= 0);
