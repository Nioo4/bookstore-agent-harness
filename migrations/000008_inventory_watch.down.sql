DROP TABLE IF EXISTS `notification`;
DROP TABLE IF EXISTS `agent_task`;
DROP TABLE IF EXISTS `inventory_event_consumption`;

UPDATE `outbox_event`
SET `status` = 'PENDING',
    `lease_owner` = NULL,
    `lease_expires_at` = NULL,
    `published_at` = NULL,
    `available_at` = LEAST(`available_at`, CURRENT_TIMESTAMP(3)),
    `last_error` = 'publisher lease released by migration rollback'
WHERE `status` = 'PROCESSING';

ALTER TABLE `outbox_event`
  DROP CHECK `ck_outbox_lease_consistency`,
  DROP CHECK `ck_outbox_status`,
  DROP INDEX `idx_outbox_claim`,
  DROP COLUMN `lease_expires_at`,
  DROP COLUMN `lease_owner`,
  ADD KEY `idx_outbox_publish` (`status`, `available_at`, `created_at`),
  ADD CONSTRAINT `ck_outbox_status`
    CHECK (`status` IN ('PENDING', 'PUBLISHED', 'FAILED'));
