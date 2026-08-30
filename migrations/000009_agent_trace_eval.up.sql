CREATE TABLE `agent_run`
(
  `run_id` char(36) NOT NULL,
  `source_run_id` char(36) NULL,
  `user_id` bigint unsigned NULL,
  `conversation_id` varchar(128) NULL,
  `run_kind` varchar(16) NOT NULL,
  `workflow_type` varchar(32) NOT NULL,
  `status` varchar(24) NOT NULL,
  `trace_status` varchar(16) NOT NULL,
  `replay_mode` varchar(32) NOT NULL DEFAULT 'NONE',
  `replayable` boolean NOT NULL DEFAULT FALSE,
  `replay_block_reason` varchar(64) NULL,
  `input_json` json NOT NULL,
  `dag_json` json NULL,
  `final_answer` text NULL,
  `model_profile` varchar(64) NOT NULL,
  `model_manifest_hash` binary(32) NOT NULL,
  `prompt_set_version` varchar(64) NOT NULL,
  `prompt_manifest_hash` binary(32) NOT NULL,
  `tool_registry_version` varchar(64) NOT NULL,
  `harness_version` varchar(64) NOT NULL,
  `prompt_tokens` bigint unsigned NOT NULL DEFAULT 0,
  `completion_tokens` bigint unsigned NOT NULL DEFAULT 0,
  `cached_tokens` bigint unsigned NULL,
  `reasoning_tokens` bigint unsigned NULL,
  `known_cost_microusd` bigint unsigned NULL,
  `cost_currency` char(3) NOT NULL DEFAULT 'USD',
  `cost_status` varchar(16) NOT NULL,
  `pricing_version` varchar(64) NULL,
  `wall_duration_ms` bigint unsigned NULL,
  `active_duration_ms` bigint unsigned NULL,
  `retry_count` int unsigned NOT NULL DEFAULT 0,
  `unused_fixture_count` int unsigned NULL,
  `error_code` varchar(64) NULL,
  `error_message` varchar(2048) NULL,
  `request_idempotency_key` varchar(128) NULL,
  `request_hash` binary(32) NULL,
  `lease_owner` varchar(128) NULL,
  `lease_expires_at` datetime(3) NULL,
  `retention_expires_at` datetime(3) NOT NULL,
  `deleted_at` datetime(3) NULL,
  `started_at` datetime(3) NULL,
  `completed_at` datetime(3) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`run_id`),
  UNIQUE KEY `uk_agent_run_user_idempotency` (`user_id`, `request_idempotency_key`),
  KEY `idx_agent_run_user_created` (`user_id`, `created_at`, `run_id`),
  KEY `idx_agent_run_conversation_created` (`conversation_id`, `created_at`, `run_id`),
  KEY `idx_agent_run_source_created` (`source_run_id`, `created_at`, `run_id`),
  KEY `idx_agent_run_status_lease` (`status`, `lease_expires_at`, `run_id`),
  KEY `idx_agent_run_retention_deleted` (`retention_expires_at`, `deleted_at`, `run_id`),
  CONSTRAINT `fk_agent_run_user` FOREIGN KEY (`user_id`)
    REFERENCES `user_account` (`user_id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_agent_run_source` FOREIGN KEY (`source_run_id`)
    REFERENCES `agent_run` (`run_id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_agent_run_kind` CHECK (`run_kind` IN ('LIVE', 'REPLAY', 'EVALUATION')),
  CONSTRAINT `ck_agent_run_workflow` CHECK (`workflow_type` IN ('CHAT', 'INVENTORY_WATCH', 'EVALUATION_CASE')),
  CONSTRAINT `ck_agent_run_status` CHECK (`status` IN
    ('QUEUED', 'RUNNING', 'WAITING_CONFIRMATION', 'WAITING_EVENT', 'COMPLETED', 'FAILED', 'CANCELLED')),
  CONSTRAINT `ck_agent_run_trace_status` CHECK (`trace_status` IN ('RECORDING', 'COMPLETE', 'INCOMPLETE')),
  CONSTRAINT `ck_agent_run_replay_mode` CHECK (`replay_mode` IN ('NONE', 'FIXED_TOOL_RESULTS')),
  CONSTRAINT `ck_agent_run_identity` CHECK (
    (`run_kind` IN ('LIVE', 'REPLAY') AND `user_id` IS NOT NULL AND `conversation_id` IS NOT NULL)
    OR (`run_kind` = 'EVALUATION' AND `user_id` IS NULL AND `conversation_id` IS NULL)
  ),
  CONSTRAINT `ck_agent_run_kind_consistency` CHECK (
    (`run_kind` = 'LIVE' AND `source_run_id` IS NULL AND `replay_mode` = 'NONE'
      AND `workflow_type` IN ('CHAT', 'INVENTORY_WATCH'))
    OR (`run_kind` = 'REPLAY' AND `source_run_id` IS NOT NULL
      AND `replay_mode` = 'FIXED_TOOL_RESULTS' AND `workflow_type` = 'CHAT')
    OR (`run_kind` = 'EVALUATION' AND `source_run_id` IS NULL
      AND `replay_mode` = 'NONE' AND `workflow_type` = 'EVALUATION_CASE')
  ),
  CONSTRAINT `ck_agent_run_replay_request` CHECK (
    (`run_kind` = 'REPLAY' AND `request_idempotency_key` IS NOT NULL AND `request_hash` IS NOT NULL)
    OR (`run_kind` <> 'REPLAY' AND `request_idempotency_key` IS NULL AND `request_hash` IS NULL)
  ),
  CONSTRAINT `ck_agent_run_replayability` CHECK (
    (`replayable` = TRUE AND `replay_block_reason` IS NULL)
    OR (`replayable` = FALSE)
  ),
  CONSTRAINT `ck_agent_run_lease` CHECK (
    (`status` IN ('QUEUED', 'RUNNING')
      AND ((`lease_owner` IS NULL AND `lease_expires_at` IS NULL)
        OR (`lease_owner` IS NOT NULL AND `lease_expires_at` IS NOT NULL)))
    OR (`status` NOT IN ('QUEUED', 'RUNNING') AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL)
  ),
  CONSTRAINT `ck_agent_run_completion` CHECK (
    (`status` IN ('COMPLETED', 'FAILED', 'CANCELLED') AND `completed_at` IS NOT NULL)
    OR (`status` NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') AND `completed_at` IS NULL)
  ),
  CONSTRAINT `ck_agent_run_cost` CHECK (
    (`cost_status` = 'CALCULATED' AND `known_cost_microusd` IS NOT NULL AND `pricing_version` IS NOT NULL)
    OR (`cost_status` = 'PARTIAL' AND `known_cost_microusd` IS NOT NULL)
    OR (`cost_status` = 'UNAVAILABLE' AND `known_cost_microusd` IS NULL)
  ),
  CONSTRAINT `ck_agent_run_currency` CHECK (`cost_currency` = 'USD'),
  CONSTRAINT `ck_agent_run_retention` CHECK (`retention_expires_at` > `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `agent_step`
(
  `step_id` char(36) NOT NULL,
  `run_id` char(36) NOT NULL,
  `parent_step_id` char(36) NULL,
  `sequence_no` int unsigned NOT NULL,
  `node_id` varchar(128) NOT NULL,
  `step_type` varchar(24) NOT NULL,
  `agent_name` varchar(32) NULL,
  `tool_name` varchar(64) NULL,
  `status` varchar(16) NOT NULL,
  `attempt_no` int unsigned NOT NULL DEFAULT 1,
  `input_json` json NULL,
  `output_json` json NULL,
  `evidence_ids_json` json NOT NULL,
  `model_profile` varchar(64) NULL,
  `prompt_version` varchar(64) NULL,
  `tool_version` varchar(64) NULL,
  `prompt_tokens` bigint unsigned NOT NULL DEFAULT 0,
  `completion_tokens` bigint unsigned NOT NULL DEFAULT 0,
  `known_cost_microusd` bigint unsigned NULL,
  `cost_status` varchar(16) NOT NULL,
  `error_code` varchar(64) NULL,
  `error_message` varchar(2048) NULL,
  `started_at` datetime(3) NOT NULL,
  `completed_at` datetime(3) NULL,
  `duration_ms` bigint unsigned NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`step_id`),
  UNIQUE KEY `uk_agent_step_run_sequence` (`run_id`, `sequence_no`),
  UNIQUE KEY `uk_agent_step_run_step` (`run_id`, `step_id`),
  KEY `idx_agent_step_run_started` (`run_id`, `started_at`, `step_id`),
  KEY `idx_agent_step_run_node` (`run_id`, `node_id`, `sequence_no`),
  KEY `idx_agent_step_run_type` (`run_id`, `step_type`, `sequence_no`),
  CONSTRAINT `fk_agent_step_run` FOREIGN KEY (`run_id`)
    REFERENCES `agent_run` (`run_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_agent_step_parent` FOREIGN KEY (`run_id`, `parent_step_id`)
    REFERENCES `agent_step` (`run_id`, `step_id`) ON DELETE CASCADE,
  CONSTRAINT `ck_agent_step_type` CHECK (`step_type` IN ('PLAN', 'AGENT', 'TOOL', 'VERIFICATION', 'REPAIR', 'SYSTEM')),
  CONSTRAINT `ck_agent_step_status` CHECK (`status` IN ('RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED')),
  CONSTRAINT `ck_agent_step_attempt` CHECK (`attempt_no` >= 1),
  CONSTRAINT `ck_agent_step_evidence_ids` CHECK (JSON_TYPE(`evidence_ids_json`) = 'ARRAY'),
  CONSTRAINT `ck_agent_step_completion` CHECK (
    (`status` = 'RUNNING' AND `completed_at` IS NULL AND `duration_ms` IS NULL)
    OR (`status` IN ('COMPLETED', 'FAILED', 'SKIPPED') AND `completed_at` IS NOT NULL AND `duration_ms` IS NOT NULL)
  ),
  CONSTRAINT `ck_agent_step_cost` CHECK (
    (`cost_status` IN ('CALCULATED', 'PARTIAL') AND `known_cost_microusd` IS NOT NULL)
    OR (`cost_status` = 'UNAVAILABLE' AND `known_cost_microusd` IS NULL)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `agent_evidence`
(
  `run_id` char(36) NOT NULL,
  `evidence_id` varchar(160) NOT NULL,
  `source_step_id` char(36) NOT NULL,
  `evidence_type` varchar(32) NOT NULL,
  `source_ref` varchar(512) NOT NULL,
  `payload_json` json NOT NULL,
  `content_hash` binary(32) NOT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`run_id`, `evidence_id`),
  KEY `idx_agent_evidence_source_step` (`run_id`, `source_step_id`),
  CONSTRAINT `fk_agent_evidence_run` FOREIGN KEY (`run_id`)
    REFERENCES `agent_run` (`run_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_agent_evidence_step` FOREIGN KEY (`run_id`, `source_step_id`)
    REFERENCES `agent_step` (`run_id`, `step_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `agent_tool_fixture`
(
  `fixture_id` char(36) NOT NULL,
  `source_run_id` char(36) NOT NULL,
  `source_step_id` char(36) NOT NULL,
  `tool_name` varchar(64) NOT NULL,
  `risk_level` varchar(24) NOT NULL,
  `canonical_args_hash` binary(32) NOT NULL,
  `occurrence_index` int unsigned NOT NULL,
  `request_json` json NOT NULL,
  `response_json` json NULL,
  `outcome` varchar(24) NOT NULL,
  `error_code` varchar(64) NULL,
  `schema_version` varchar(64) NOT NULL,
  `replayable` boolean NOT NULL,
  `blocked_reason` varchar(64) NULL,
  `content_hash` binary(32) NOT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`fixture_id`),
  UNIQUE KEY `uk_agent_tool_fixture_match`
    (`source_run_id`, `tool_name`, `canonical_args_hash`, `occurrence_index`),
  KEY `idx_agent_tool_fixture_source_step` (`source_run_id`, `source_step_id`),
  CONSTRAINT `fk_agent_tool_fixture_run` FOREIGN KEY (`source_run_id`)
    REFERENCES `agent_run` (`run_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_agent_tool_fixture_step` FOREIGN KEY (`source_run_id`, `source_step_id`)
    REFERENCES `agent_step` (`run_id`, `step_id`) ON DELETE CASCADE,
  CONSTRAINT `ck_agent_tool_fixture_risk` CHECK (`risk_level` IN ('READ', 'PREPARE', 'COMMIT')),
  CONSTRAINT `ck_agent_tool_fixture_outcome` CHECK (`outcome` IN ('SUCCESS', 'BUSINESS_ERROR', 'TECHNICAL_ERROR')),
  CONSTRAINT `ck_agent_tool_fixture_replayability` CHECK (
    (`replayable` = TRUE AND `blocked_reason` IS NULL)
    OR (`replayable` = FALSE)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `evaluation_run`
(
  `eval_run_id` char(36) NOT NULL,
  `idempotency_key` varchar(128) NOT NULL,
  `request_hash` binary(32) NOT NULL,
  `suite_version` varchar(64) NOT NULL,
  `dataset_hash` binary(32) NOT NULL,
  `runner_version` varchar(64) NOT NULL,
  `mode` varchar(24) NOT NULL,
  `status` varchar(16) NOT NULL,
  `model_profile` varchar(64) NOT NULL,
  `prompt_set_version` varchar(64) NOT NULL,
  `judge_model_profile` varchar(64) NULL,
  `judge_prompt_version` varchar(64) NULL,
  `judge_status` varchar(16) NOT NULL,
  `case_count` int unsigned NOT NULL,
  `passed_count` int unsigned NOT NULL DEFAULT 0,
  `failed_count` int unsigned NOT NULL DEFAULT 0,
  `error_count` int unsigned NOT NULL DEFAULT 0,
  `metrics_json` json NULL,
  `deterministic_gate_passed` boolean NULL,
  `known_cost_microusd` bigint unsigned NULL,
  `cost_status` varchar(16) NOT NULL,
  `lease_owner` varchar(128) NULL,
  `lease_expires_at` datetime(3) NULL,
  `retention_expires_at` datetime(3) NOT NULL,
  `started_at` datetime(3) NULL,
  `completed_at` datetime(3) NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`eval_run_id`),
  UNIQUE KEY `uk_evaluation_run_idempotency` (`idempotency_key`),
  KEY `idx_evaluation_run_status_lease` (`status`, `lease_expires_at`, `eval_run_id`),
  KEY `idx_evaluation_run_retention` (`retention_expires_at`, `eval_run_id`),
  CONSTRAINT `ck_evaluation_run_mode` CHECK (`mode` IN ('DETERMINISTIC', 'DETERMINISTIC_WITH_JUDGE')),
  CONSTRAINT `ck_evaluation_run_status` CHECK (`status` IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
  CONSTRAINT `ck_evaluation_run_judge_status` CHECK (`judge_status` IN ('DISABLED', 'PENDING', 'COMPLETED', 'INCOMPLETE')),
  CONSTRAINT `ck_evaluation_run_counts` CHECK (
    `passed_count` + `failed_count` + `error_count` <= `case_count`
  ),
  CONSTRAINT `ck_evaluation_run_judge_mode` CHECK (
    (`mode` = 'DETERMINISTIC' AND `judge_status` = 'DISABLED'
      AND `judge_model_profile` IS NULL AND `judge_prompt_version` IS NULL)
    OR (`mode` = 'DETERMINISTIC_WITH_JUDGE' AND `judge_status` <> 'DISABLED'
      AND `judge_model_profile` IS NOT NULL AND `judge_prompt_version` IS NOT NULL)
  ),
  CONSTRAINT `ck_evaluation_run_lease` CHECK (
    (`status` IN ('QUEUED', 'RUNNING')
      AND ((`lease_owner` IS NULL AND `lease_expires_at` IS NULL)
        OR (`lease_owner` IS NOT NULL AND `lease_expires_at` IS NOT NULL)))
    OR (`status` NOT IN ('QUEUED', 'RUNNING') AND `lease_owner` IS NULL AND `lease_expires_at` IS NULL)
  ),
  CONSTRAINT `ck_evaluation_run_completion` CHECK (
    (`status` IN ('COMPLETED', 'FAILED', 'CANCELLED') AND `completed_at` IS NOT NULL)
    OR (`status` IN ('QUEUED', 'RUNNING') AND `completed_at` IS NULL)
  ),
  CONSTRAINT `ck_evaluation_run_cost` CHECK (
    (`cost_status` IN ('CALCULATED', 'PARTIAL') AND `known_cost_microusd` IS NOT NULL)
    OR (`cost_status` = 'UNAVAILABLE' AND `known_cost_microusd` IS NULL)
  ),
  CONSTRAINT `ck_evaluation_run_retention` CHECK (`retention_expires_at` > `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `evaluation_case_result`
(
  `eval_run_id` char(36) NOT NULL,
  `case_id` varchar(128) NOT NULL,
  `agent_run_id` char(36) NULL,
  `category` varchar(32) NOT NULL,
  `status` varchar(16) NOT NULL,
  `case_snapshot_json` json NOT NULL,
  `fixture_manifest_hash` binary(32) NOT NULL,
  `deterministic_metrics_json` json NOT NULL,
  `failure_codes_json` json NOT NULL,
  `judge_scores_json` json NULL,
  `judge_reason` varchar(2048) NULL,
  `duration_ms` bigint unsigned NOT NULL,
  `known_cost_microusd` bigint unsigned NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`eval_run_id`, `case_id`),
  KEY `idx_evaluation_case_agent_run` (`agent_run_id`),
  CONSTRAINT `fk_evaluation_case_eval_run` FOREIGN KEY (`eval_run_id`)
    REFERENCES `evaluation_run` (`eval_run_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_evaluation_case_agent_run` FOREIGN KEY (`agent_run_id`)
    REFERENCES `agent_run` (`run_id`) ON DELETE SET NULL,
  CONSTRAINT `ck_evaluation_case_status` CHECK (`status` IN ('PASSED', 'FAILED', 'ERROR', 'SKIPPED')),
  CONSTRAINT `ck_evaluation_case_failures` CHECK (JSON_TYPE(`failure_codes_json`) = 'ARRAY')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
