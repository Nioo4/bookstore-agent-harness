-- Writers must be drained. Do not rewrite any historical usage/cost/timestamps.
ALTER TABLE `agent_run`
  MODIFY COLUMN `prompt_tokens` bigint unsigned NULL,
  MODIFY COLUMN `completion_tokens` bigint unsigned NULL,
  ADD COLUMN `usage_status` varchar(24) NOT NULL DEFAULT 'LEGACY_UNKNOWN',
  ADD CONSTRAINT `ck_agent_run_usage` CHECK (
    `usage_status` = 'LEGACY_UNKNOWN'
    OR (`usage_status` = 'NO_CALLS' AND `prompt_tokens` IS NOT NULL AND `prompt_tokens` = 0
      AND `completion_tokens` IS NOT NULL AND `completion_tokens` = 0
      AND `cached_tokens` IS NOT NULL AND `cached_tokens` = 0
      AND `reasoning_tokens` IS NOT NULL AND `reasoning_tokens` = 0)
    OR (`usage_status` = 'COMPLETE' AND `prompt_tokens` IS NOT NULL AND `completion_tokens` IS NOT NULL)
    OR (`usage_status` = 'PARTIAL' AND (`prompt_tokens` IS NULL OR `completion_tokens` IS NULL))
    OR (`usage_status` = 'UNAVAILABLE' AND `prompt_tokens` IS NULL AND `completion_tokens` IS NULL)
  );

ALTER TABLE `agent_step`
  MODIFY COLUMN `prompt_tokens` bigint unsigned NULL,
  MODIFY COLUMN `completion_tokens` bigint unsigned NULL,
  ADD COLUMN `cached_tokens` bigint unsigned NULL,
  ADD COLUMN `reasoning_tokens` bigint unsigned NULL,
  ADD COLUMN `usage_status` varchar(24) NOT NULL DEFAULT 'LEGACY_UNKNOWN',
  ADD CONSTRAINT `ck_agent_step_usage` CHECK (
    `usage_status` = 'LEGACY_UNKNOWN'
    OR (`usage_status` = 'NO_CALLS' AND `prompt_tokens` IS NOT NULL AND `prompt_tokens` = 0
      AND `completion_tokens` IS NOT NULL AND `completion_tokens` = 0
      AND `cached_tokens` IS NOT NULL AND `cached_tokens` = 0
      AND `reasoning_tokens` IS NOT NULL AND `reasoning_tokens` = 0)
    OR (`usage_status` = 'COMPLETE' AND `prompt_tokens` IS NOT NULL AND `completion_tokens` IS NOT NULL)
    OR (`usage_status` = 'PARTIAL' AND ((`prompt_tokens` IS NULL AND `completion_tokens` IS NOT NULL)
      OR (`prompt_tokens` IS NOT NULL AND `completion_tokens` IS NULL)))
    OR (`usage_status` = 'UNAVAILABLE' AND `prompt_tokens` IS NULL AND `completion_tokens` IS NULL)
  ),
  ADD CONSTRAINT `ck_agent_step_model_usage` CHECK (
    `usage_status` = 'LEGACY_UNKNOWN'
    OR (LEFT(`node_id`, 6) = 'model:' AND CHAR_LENGTH(`node_id`) > 6 AND `step_type` = 'SYSTEM'
      AND `model_profile` IS NOT NULL AND CHAR_LENGTH(`model_profile`) > 0 AND `usage_status` <> 'NO_CALLS')
    OR (LEFT(`node_id`, 6) <> 'model:' AND `usage_status` = 'NO_CALLS')
  );
