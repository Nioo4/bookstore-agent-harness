-- This read-only guard MUST run before either ALTER. Invalid JSON deliberately
-- aborts with a fixed diagnostic; no stored procedure/DDL is needed for the guard.
-- Keep writers drained; on refusal retain v10 and forward-fix (see recovery guide).
SELECT IF(
  EXISTS(SELECT 1 FROM `agent_run` WHERE `usage_status` <> 'LEGACY_UNKNOWN'
    OR `prompt_tokens` IS NULL OR `completion_tokens` IS NULL)
  OR EXISTS(SELECT 1 FROM `agent_step` WHERE `usage_status` <> 'LEGACY_UNKNOWN'
    OR `prompt_tokens` IS NULL OR `completion_tokens` IS NULL
    OR `cached_tokens` IS NOT NULL OR `reasoning_tokens` IS NOT NULL),
  JSON_EXTRACT('DQ011_DOWN_REFUSED_NEW_USAGE_DATA_KEEP_V10', '$'), 0
);

ALTER TABLE `agent_step`
  DROP CHECK `ck_agent_step_model_usage`,
  DROP CHECK `ck_agent_step_usage`,
  DROP COLUMN `usage_status`,
  DROP COLUMN `cached_tokens`,
  DROP COLUMN `reasoning_tokens`,
  MODIFY COLUMN `prompt_tokens` bigint unsigned NOT NULL DEFAULT 0,
  MODIFY COLUMN `completion_tokens` bigint unsigned NOT NULL DEFAULT 0;

ALTER TABLE `agent_run`
  DROP CHECK `ck_agent_run_usage`,
  DROP COLUMN `usage_status`,
  MODIFY COLUMN `prompt_tokens` bigint unsigned NOT NULL DEFAULT 0,
  MODIFY COLUMN `completion_tokens` bigint unsigned NOT NULL DEFAULT 0;
