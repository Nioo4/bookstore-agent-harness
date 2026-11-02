-- Roll back only the v2 schema shape. Historical rows and event payloads are
-- preserved; events that were terminal in v2 become terminal in v1.

ALTER TABLE `agent_task`
  DROP INDEX `idx_agent_task_recovery_v2`,
  ADD COLUMN `checkpoint_status` varchar(16) NOT NULL DEFAULT 'READY' AFTER `request_hash`,
  ADD KEY `idx_agent_task_recovery` (`status`, `checkpoint_status`, `updated_at`, `task_id`),
  ADD CONSTRAINT `ck_agent_task_checkpoint_status`
    CHECK (`checkpoint_status` IN ('PENDING', 'READY', 'FAILED'));

ALTER TABLE `inventory_event_consumption`
  -- Keep the wider v2 width during rollback so a direct-worker event
  -- reference is never truncated. The old publisher accepts this wider
  -- compatible column and only writes short Redis entry ids.
  CHANGE COLUMN `source_event_ref` `stream_entry_id` varchar(128) NOT NULL;

ALTER TABLE `outbox_event`
  DROP CHECK `ck_outbox_lease_consistency_v2`,
  DROP CHECK `ck_outbox_status_v2`;

-- A v2 PROCESSING row has not completed delivery. Release its lease before
-- restoring the v1 state constraint; it will be retried by the old publisher.
UPDATE `outbox_event`
SET `status` = 'PENDING',
    `lease_owner` = NULL,
    `lease_expires_at` = NULL,
    `processed_at` = NULL,
    `available_at` = LEAST(`available_at`, CURRENT_TIMESTAMP(3)),
    `last_error` = COALESCE(`last_error`, 'v2 outbox lease released by migration rollback')
WHERE `status` = 'PROCESSING';

UPDATE `outbox_event`
SET `status` = 'PUBLISHED',
    `processed_at` = COALESCE(`processed_at`, CURRENT_TIMESTAMP(3))
WHERE `status` = 'PROCESSED';

ALTER TABLE `outbox_event`
  CHANGE COLUMN `processed_at` `published_at` datetime(3) NULL,
  DROP INDEX `idx_outbox_claim_v2`,
  ADD KEY `idx_outbox_claim` (`status`, `available_at`, `lease_expires_at`, `created_at`, `event_id`),
  ADD CONSTRAINT `ck_outbox_status`
    CHECK (`status` IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'FAILED')),
  ADD CONSTRAINT `ck_outbox_lease_consistency` CHECK (
    (`status` = 'PENDING' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `published_at` IS NULL)
    OR (`status` = 'PROCESSING' AND `lease_owner` IS NOT NULL AND `lease_expires_at` IS NOT NULL AND `published_at` IS NULL)
    OR (`status` = 'PUBLISHED' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `published_at` IS NOT NULL)
    OR (`status` = 'FAILED' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `published_at` IS NULL)
  );
