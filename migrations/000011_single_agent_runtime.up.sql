-- v2 runtime migration. 000001-000010 are immutable historical migrations.
-- The migration keeps the event identity and payload columns unchanged so a
-- worker can resume an event after a process crash without re-emitting a new
-- event id.

ALTER TABLE `outbox_event`
  DROP CHECK `ck_outbox_lease_consistency`,
  DROP CHECK `ck_outbox_status`;

ALTER TABLE `outbox_event`
  CHANGE COLUMN `published_at` `processed_at` datetime(3) NULL;

-- In v1, PUBLISHED means only that XADD succeeded; it does not prove that the
-- Redis consumer ACKed the entry. Requeue every such row for the direct v2
-- worker. inventory_event_consumption.event_id and the task/notification
-- unique keys make this replay idempotent, while mapping it to PROCESSED would
-- lose an entry that was still pending in the Redis consumer group.
UPDATE `outbox_event`
SET `status` = 'PENDING',
    `processed_at` = NULL,
    `available_at` = LEAST(`available_at`, CURRENT_TIMESTAMP(3)),
    `lease_owner` = NULL,
    `lease_expires_at` = NULL,
    `last_error` = NULL
WHERE `status` = 'PUBLISHED';

-- V1 had two producer spellings for the same inventory aggregate: ordinary
-- stock writes used INVENTORY while order deduction used book. V2 has one
-- event contract. Normalize historical rows before the direct worker starts;
-- aggregate_id remains the decimal book_id and the payload is unchanged.
UPDATE `outbox_event`
SET `aggregate_type` = 'INVENTORY'
WHERE `event_type` = 'inventory.stock_changed.v1';

ALTER TABLE `outbox_event`
  DROP INDEX `idx_outbox_claim`,
  ADD KEY `idx_outbox_claim_v2`
    (`status`, `available_at`, `lease_expires_at`, `created_at`, `event_id`),
  ADD CONSTRAINT `ck_outbox_status_v2`
    CHECK (`status` IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED')),
  ADD CONSTRAINT `ck_outbox_lease_consistency_v2` CHECK (
    (`status` = 'PENDING' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `processed_at` IS NULL)
    OR (`status` = 'PROCESSING' AND `lease_owner` IS NOT NULL AND `lease_expires_at` IS NOT NULL AND `processed_at` IS NULL)
    OR (`status` = 'PROCESSED' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `processed_at` IS NOT NULL)
    OR (`status` = 'FAILED' AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL AND `processed_at` IS NULL)
  );

ALTER TABLE `inventory_event_consumption`
  CHANGE COLUMN `stream_entry_id` `source_event_ref` varchar(128) NOT NULL;

ALTER TABLE `agent_task`
  DROP CHECK `ck_agent_task_checkpoint_status`,
  DROP INDEX `idx_agent_task_recovery`,
  DROP COLUMN `checkpoint_status`,
  ADD KEY `idx_agent_task_recovery_v2` (`status`, `updated_at`, `task_id`);
