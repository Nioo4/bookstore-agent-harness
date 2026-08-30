ALTER TABLE `pending_action`
  DROP CHECK `ck_pending_total_nonnegative`;

ALTER TABLE `pending_action`
  DROP FOREIGN KEY `fk_pending_order`,
  DROP CHECK `ck_pending_decision`,
  DROP CHECK `ck_pending_status`,
  DROP CHECK `ck_pending_action_type`,
  DROP INDEX `idx_pending_order`,
  DROP INDEX `uk_pending_user_prepare_idempotency`,
  DROP INDEX `uk_pending_token_hash`,
  DROP COLUMN `order_id`,
  DROP COLUMN `decision`,
  DROP COLUMN `status`,
  DROP COLUMN `action_type`,
  CHANGE COLUMN `consumed_at` `used_at` datetime(3) NULL,
  CHANGE COLUMN `prepare_idempotency_key` `idempotency_key` varchar(64) NOT NULL,
  CHANGE COLUMN `quoted_amount` `total_amount` bigint NOT NULL,
  CHANGE COLUMN `payload_hash` `items_hash` binary(32) NOT NULL,
  CHANGE COLUMN `token_hash` `confirmation_digest` binary(32) NOT NULL,
  CHANGE COLUMN `pending_action_id` `action_id` char(36) NOT NULL,
  ADD UNIQUE KEY `uk_pending_confirmation_digest` (`confirmation_digest`),
  ADD UNIQUE KEY `uk_pending_user_idempotency` (`user_id`, `idempotency_key`);

ALTER TABLE `pending_action`
  ADD CONSTRAINT `ck_pending_total_nonnegative` CHECK (`total_amount` >= 0);
