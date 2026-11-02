package migrations_test

import (
	"crypto/sha256"
	"fmt"
	"os"
	"strings"
	"testing"
)

func TestCatalogMigrationContainsRequiredSchemaAndBackfill(t *testing.T) {
	contents, err := os.ReadFile("000002_catalog_v1.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	required := []string{
		"create table `author`",
		"create table `book_author`",
		"create table `category`",
		"create table `book_category`",
		"create table `inventory`",
		"add fulltext key `ft_book_search`",
		"check (`price` >= 0)",
		"check (`page_count` is null or `page_count` > 0)",
		"'draft', 'active', 'inactive'",
		"insert into `author`",
		"insert ignore into `book_author`",
		"insert into `inventory`",
	}
	for _, fragment := range required {
		if !strings.Contains(sql, fragment) {
			t.Errorf("catalog migration missing %q", fragment)
		}
	}
}

func TestEveryMigrationHasUpAndDownPair(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	files := make(map[string]bool)
	for _, entry := range entries {
		files[entry.Name()] = true
	}
	for name := range files {
		if !strings.HasSuffix(name, ".up.sql") {
			continue
		}
		down := strings.TrimSuffix(name, ".up.sql") + ".down.sql"
		if !files[down] {
			t.Errorf("%s has no matching %s", name, down)
		}
	}
}

func TestAuthMigrationStoresOnlyVerificationDigest(t *testing.T) {
	contents, err := os.ReadFile("000003_auth.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"create table `user_account`",
		"unique key `uk_user_account_email`",
		"create table `email_verification_token`",
		"`token_digest` binary(32)",
		"`expires_at` datetime(3)",
		"`used_at` datetime(3)",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("auth migration missing %q", fragment)
		}
	}
	if strings.Contains(sql, "raw_token") {
		t.Error("auth schema must never persist raw verification tokens")
	}
}

func TestOrderMigrationDefinesConfirmationIdempotencyAndOutboxBoundaries(t *testing.T) {
	contents, err := os.ReadFile("000004_order.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"create table `pending_action`", "`confirmation_digest` binary(32)",
		"unique key `uk_pending_user_idempotency`", "create table `customer_order`",
		"unique key `uk_order_user_idempotency`", "create table `order_item`",
		"create table `outbox_event`", "'pending', 'published', 'failed'",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("order migration missing %q", fragment)
		}
	}
	if strings.Contains(sql, "confirmation_token`") {
		t.Error("raw confirmation tokens must not be persisted")
	}
}

func TestConfirmationDecisionMigrationDefinesHashedBoundStateMachine(t *testing.T) {
	contents, err := os.ReadFile("000005_confirmation_decision.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"`pending_action_id` char(36)", "`token_hash` binary(32)", "`conversation_id`",
		"`action_type` varchar(32)", "`payload_hash` binary(32)", "`quoted_amount` bigint",
		"`status` varchar(16)", "`decision` varchar(16)", "`order_id` bigint unsigned",
		"`consumed_at` datetime(3)", "'pending', 'approved', 'rejected', 'expired'",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("confirmation decision migration missing %q", fragment)
		}
	}
	if strings.Contains(sql, "raw_token") || strings.Contains(sql, "confirmation_token") {
		t.Error("confirmation decision schema must never persist raw confirmation tokens")
	}
	dropCheck := strings.Index(sql, "drop check `ck_pending_total_nonnegative`")
	renameAmount := strings.Index(sql, "change column `total_amount` `quoted_amount`")
	restoreCheck := strings.LastIndex(sql, "constraint `ck_pending_total_nonnegative` check (`quoted_amount` >= 0)")
	if dropCheck < 0 || renameAmount < 0 || restoreCheck < 0 || !(dropCheck < renameAmount && renameAmount < restoreCheck) {
		t.Errorf("confirmation decision up migration must drop, rename, then restore the amount check")
	}

	downContents, err := os.ReadFile("000005_confirmation_decision.down.sql")
	if err != nil {
		t.Fatal(err)
	}
	downSQL := strings.ToLower(string(downContents))
	downDropCheck := strings.Index(downSQL, "drop check `ck_pending_total_nonnegative`")
	downRenameAmount := strings.Index(downSQL, "change column `quoted_amount` `total_amount`")
	downRestoreCheck := strings.LastIndex(downSQL, "constraint `ck_pending_total_nonnegative` check (`total_amount` >= 0)")
	if downDropCheck < 0 || downRenameAmount < 0 || downRestoreCheck < 0 ||
		!(downDropCheck < downRenameAmount && downRenameAmount < downRestoreCheck) {
		t.Errorf("confirmation decision down migration must drop, rename, then restore the amount check")
	}
}

func TestConfirmationHardeningMigrationBackfillsLegacyTerminalStateAndAddsCompositeCheck(t *testing.T) {
	contents, err := os.ReadFile("000006_confirmation_hardening.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"`pending`.`consumed_at` is not null",
		"`orders`.`user_id` = `pending`.`user_id`",
		"`orders`.`idempotency_key` = `pending`.`prepare_idempotency_key`",
		"`pending`.`status` = 'approved'",
		"`pending`.`decision` = 'approve'",
		"constraint `ck_pending_state_consistency` check",
		"`status` = 'pending' and `decision` is null and `order_id` is null and `consumed_at` is null",
		"`status` = 'rejected' and `decision` = 'reject' and `order_id` is null and `consumed_at` is not null",
		"`status` = 'expired' and `decision` is null and `order_id` is null and `consumed_at` is not null",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("confirmation hardening migration missing %q", fragment)
		}
	}
	down, err := os.ReadFile("000006_confirmation_hardening.down.sql")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.ToLower(string(down)), "drop check `ck_pending_state_consistency`") {
		t.Error("confirmation hardening down migration must remove the composite state check")
	}
}

func TestConfirmationV5VerificationIsReadOnlyAndChecksRequiredSchema(t *testing.T) {
	contents, err := os.ReadFile("verify_confirmation_v5.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"create temporary table", "`schema_migrations`", "`version` = 5", "`dirty` = 0",
		"'pending_action_id'", "'token_hash'", "'quoted_amount'", "'prepare_idempotency_key'",
		"'fk_pending_order'", "'uk_pending_token_hash'", "'uk_pending_user_prepare_idempotency'",
		"'ck_pending_action_type'", "'ck_pending_status'", "'ck_pending_decision'",
		"'ck_pending_total_nonnegative'", "like '%quoted_amount%'", "not like '%total_amount%'",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("confirmation v5 verification missing %q", fragment)
		}
	}
	for _, forbidden := range []string{
		"alter table `pending_action`", "update `pending_action`", "delete from `pending_action`",
		"drop table `pending_action`", "truncate table", "force 4",
	} {
		if strings.Contains(sql, forbidden) {
			t.Errorf("confirmation v5 verification must not contain %q", forbidden)
		}
	}
}

func TestConfirmationMigrationBaselineIsImmutable(t *testing.T) {
	contents, err := os.ReadFile("BASELINE.sha256")
	if err != nil {
		t.Fatal(err)
	}
	entries := 0
	seen := make(map[string]bool)
	for _, line := range strings.Split(strings.TrimSpace(string(contents)), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			t.Fatalf("invalid baseline entry %q", line)
		}
		expected, name := fields[0], fields[1]
		legacy := strings.HasPrefix(name, "00000") && len(name) > 5 && name[5] >= '1' && name[5] <= '9'
		if (!legacy && !strings.HasPrefix(name, "000010_")) || seen[name] ||
			(!strings.HasSuffix(name, ".up.sql") && !strings.HasSuffix(name, ".down.sql")) {
			t.Fatalf("baseline contains an out-of-scope migration %q", name)
		}
		migration, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		actual := fmt.Sprintf("%x", sha256.Sum256(migration))
		if actual != expected {
			t.Errorf("published migration %s changed: got %s, want %s; add 000011+ instead", name, actual, expected)
		}
		entries++
		seen[name] = true
	}
	if entries != 20 {
		t.Fatalf("baseline entries = %d; want 20 Up/Down files for 000001 through 000010", entries)
	}
}

func TestAgentTraceEvalMigrationDefinesRunsStepsFixturesAndEvaluation(t *testing.T) {
	contents, err := os.ReadFile("000009_agent_trace_eval.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"create table `agent_run`", "create table `agent_step`", "create table `agent_evidence`",
		"create table `agent_tool_fixture`", "create table `evaluation_run`",
		"create table `evaluation_case_result`", "constraint `fk_agent_run_source`",
		"unique key `uk_agent_step_run_sequence` (`run_id`, `sequence_no`)",
		"constraint `fk_agent_step_parent`", "constraint `fk_agent_evidence_step`",
		"unique key `uk_agent_tool_fixture_match`", "constraint `fk_agent_tool_fixture_step`",
		"'live', 'replay', 'evaluation'", "'fixed_tool_results'",
		"'plan', 'agent', 'tool', 'verification', 'repair', 'system'",
		"'deterministic', 'deterministic_with_judge'", "'passed', 'failed', 'error', 'skipped'",
		"constraint `ck_agent_run_cost`", "constraint `ck_agent_run_lease`",
		"constraint `ck_evaluation_run_judge_mode`", "on delete set null",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("agent trace/eval migration missing %q", fragment)
		}
	}
	down, err := os.ReadFile("000009_agent_trace_eval.down.sql")
	if err != nil {
		t.Fatal(err)
	}
	downSQL := strings.ToLower(string(down))
	if strings.Index(downSQL, "drop table if exists `evaluation_case_result`") >
		strings.Index(downSQL, "drop table if exists `evaluation_run`") {
		t.Error("trace/eval down migration must drop evaluation cases before evaluation runs")
	}
	if strings.Index(downSQL, "drop table if exists `agent_tool_fixture`") >
		strings.Index(downSQL, "drop table if exists `agent_step`") {
		t.Error("trace/eval down migration must drop fixtures before steps")
	}
}

func TestAgentMemoryMigrationDefinesOwnershipVersioningAndOutbox(t *testing.T) {
	contents, err := os.ReadFile("000007_agent_memory.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"create table `user_memory`", "`memory_id` char(36)", "`user_id` bigint unsigned",
		"`memory_type` varchar(32)", "`memory_key` varchar(191)", "`value_json` json",
		"`write_idempotency_key` varchar(128)", "`active_slot` tinyint unsigned",
		"unique key `uk_user_memory_active` (`user_id`, `memory_key`, `active_slot`)",
		"unique key `uk_user_memory_write_idempotency` (`user_id`, `write_idempotency_key`)",
		"constraint `fk_user_memory_user`", "on delete cascade",
		"'preference', 'negative_preference', 'episode'", "'explicit', 'inferred'",
		"`confidence` between 0 and 1", "'active', 'superseded', 'deleted'",
		"create table `memory_outbox`", "'memory.index_requested.v1', 'memory.delete_requested.v1'",
		"unique key `uk_memory_outbox_type_memory` (`event_type`, `memory_id`)",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("agent memory migration missing %q", fragment)
		}
	}
	down, err := os.ReadFile("000007_agent_memory.down.sql")
	if err != nil {
		t.Fatal(err)
	}
	downSQL := strings.ToLower(string(down))
	if strings.Index(downSQL, "drop table if exists `memory_outbox`") > strings.Index(downSQL, "drop table if exists `user_memory`") {
		t.Error("memory down migration must drop outbox before its parent memory table")
	}
}

func TestInventoryWatchMigrationDefinesTasksNotificationsConsumptionAndOutboxLeases(t *testing.T) {
	contents, err := os.ReadFile("000008_inventory_watch.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"create table `inventory_event_consumption`", "create table `agent_task`", "create table `notification`",
		"unique key `uk_inventory_consumption_book_version` (`book_id`, `inventory_version`)",
		"unique key `uk_agent_task_user_idempotency` (`user_id`, `create_idempotency_key`)",
		"unique key `uk_agent_task_active_watch` (`user_id`, `task_type`, `book_id`, `active_slot`)",
		"unique key `uk_notification_task_type` (`task_id`, `notification_type`)",
		"constraint `ck_agent_task_state_consistency`", "constraint `ck_notification_payload_shape`",
		"add column `lease_owner`", "add column `lease_expires_at`",
		"'pending', 'processing', 'published', 'failed'", "constraint `ck_outbox_lease_consistency`",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("inventory watch migration missing %q", fragment)
		}
	}
	down, err := os.ReadFile("000008_inventory_watch.down.sql")
	if err != nil {
		t.Fatal(err)
	}
	downSQL := strings.ToLower(string(down))
	for _, fragment := range []string{
		"drop table if exists `notification`", "drop table if exists `agent_task`",
		"drop table if exists `inventory_event_consumption`", "drop column `lease_owner`",
		"'pending', 'published', 'failed'",
	} {
		if !strings.Contains(downSQL, fragment) {
			t.Errorf("inventory watch down migration missing %q", fragment)
		}
	}
}

func TestSingleAgentRuntimeMigrationPreservesV1AndDefinesDirectOutboxContract(t *testing.T) {
	contents, err := os.ReadFile("000011_single_agent_runtime.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(contents))
	for _, fragment := range []string{
		"change column `published_at` `processed_at`",
		"set `status` = 'pending'",
		"where `status` = 'published'",
		"set `aggregate_type` = 'inventory'",
		"where `event_type` = 'inventory.stock_changed.v1'",
		"'pending', 'processing', 'processed', 'failed'",
		"constraint `ck_outbox_lease_consistency_v2`",
		"change column `stream_entry_id` `source_event_ref`",
		"drop column `checkpoint_status`",
		"idx_agent_task_recovery_v2",
	} {
		if !strings.Contains(sql, fragment) {
			t.Errorf("single-agent migration missing %q", fragment)
		}
	}

	down, err := os.ReadFile("000011_single_agent_runtime.down.sql")
	if err != nil {
		t.Fatal(err)
	}
	downSQL := strings.ToLower(string(down))
	for _, fragment := range []string{
		"change column `processed_at` `published_at`",
		"set `status` = 'published'",
		"change column `source_event_ref` `stream_entry_id`",
		"add column `checkpoint_status`",
		"constraint `ck_outbox_lease_consistency`",
	} {
		if !strings.Contains(downSQL, fragment) {
			t.Errorf("single-agent migration down missing %q", fragment)
		}
	}
}
