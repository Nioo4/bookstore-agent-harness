package migrations_test

import (
	"bookstore/internal/migrationrunner"
	"context"
	"database/sql"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/go-sql-driver/mysql"
)

const mysqlIntegrationDSNEnv = "BOOKSTORE_TEST_MYSQL_DSN"

func openMigrationIntegrationDB(t *testing.T) (*sql.DB, *sql.Conn, string) {
	t.Helper()
	rawDSN := strings.TrimSpace(os.Getenv(mysqlIntegrationDSNEnv))
	if rawDSN == "" {
		t.Skipf("set %s to a dedicated MySQL 8 test database", mysqlIntegrationDSNEnv)
	}
	cfg, err := mysql.ParseDSN(rawDSN)
	if err != nil {
		t.Fatalf("parse integration DSN: %v", err)
	}
	if cfg.DBName == "" || !strings.Contains(strings.ToLower(cfg.DBName), "test") {
		t.Fatalf("%s database name must contain 'test'; got %q", mysqlIntegrationDSNEnv, cfg.DBName)
	}
	cfg.MultiStatements = true
	cfg.ParseTime = true
	db, err := sql.Open("mysql", cfg.FormatDSN())
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(1)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := db.Conn(ctx)
	if err != nil {
		db.Close()
		t.Fatalf("connect to integration MySQL: %v", err)
	}
	var lockAcquired int
	if err := conn.QueryRowContext(ctx, "SELECT GET_LOCK('bookstore_mysql_integration', 30)").Scan(&lockAcquired); err != nil {
		conn.Close()
		db.Close()
		t.Fatalf("acquire integration database lock: %v", err)
	}
	if lockAcquired != 1 {
		conn.Close()
		db.Close()
		t.Fatal("timed out acquiring integration database lock")
	}
	if err := resetMigrationSchema(ctx, conn); err != nil {
		_, _ = conn.ExecContext(ctx, "DO RELEASE_LOCK('bookstore_mysql_integration')")
		conn.Close()
		db.Close()
		t.Fatalf("reset integration schema: %v", err)
	}
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cleanupCancel()
		_ = resetMigrationSchema(cleanupCtx, conn)
		_, _ = conn.ExecContext(cleanupCtx, "DO RELEASE_LOCK('bookstore_mysql_integration')")
		_ = conn.Close()
		_ = db.Close()
	})
	return db, conn, cfg.FormatDSN()
}

func resetMigrationSchema(ctx context.Context, conn *sql.Conn) error {
	if _, err := conn.ExecContext(ctx, "SET FOREIGN_KEY_CHECKS = 0"); err != nil {
		return err
	}
	for _, table := range []string{
		"evaluation_case_result", "evaluation_run", "agent_tool_fixture", "agent_evidence", "agent_step", "agent_run",
		"notification", "agent_task", "inventory_event_consumption",
		"memory_outbox", "user_memory",
		"schema_migrations",
		"order_item", "pending_action", "customer_order", "outbox_event",
		"email_verification_token", "user_account",
		"book_category", "book_author", "inventory", "category", "author",
		"stock_deduction", "book",
	} {
		if _, err := conn.ExecContext(ctx, "DROP TABLE IF EXISTS `"+table+"`"); err != nil {
			return err
		}
	}
	_, err := conn.ExecContext(ctx, "SET FOREIGN_KEY_CHECKS = 1")
	return err
}

func execMigrationFile(ctx context.Context, conn *sql.Conn, name string) error {
	contents, err := os.ReadFile(name)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, string(contents))
	return err
}

func seedLegacyConfirmation(ctx context.Context, conn *sql.Conn, withOrder, consumed bool) error {
	if _, err := conn.ExecContext(ctx, `INSERT INTO user_account
		(user_id, email, display_name, password_hash, email_verified_at)
		VALUES (42, 'legacy@example.com', 'Legacy', 'argon2id-placeholder', NOW(3))`); err != nil {
		return err
	}
	if withOrder {
		if _, err := conn.ExecContext(ctx, `INSERT INTO customer_order
			(order_id, user_id, conversation_id, status, total_amount, currency, idempotency_key)
			VALUES (99, 42, 'legacy-conversation', 'PLACED', 8800, 'CNY', 'legacy-prepare-key')`); err != nil {
			return err
		}
	}
	var usedAt any
	if consumed {
		usedAt = time.Now().UTC()
	}
	_, err := conn.ExecContext(ctx, `INSERT INTO pending_action
		(action_id, confirmation_digest, user_id, conversation_id, items_hash, items_json,
		 total_amount, currency, idempotency_key, expires_at, used_at)
		VALUES ('00000000-0000-0000-0000-000000000042', UNHEX(REPEAT('ab', 32)), 42,
		 'legacy-conversation', UNHEX(REPEAT('cd', 32)), JSON_ARRAY(), 8800, 'CNY',
		 'legacy-prepare-key', NOW(3) + INTERVAL 10 MINUTE, ?)`, usedAt)
	return err
}

func prepareLegacyConfirmationSchema(t *testing.T, conn *sql.Conn) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	t.Cleanup(cancel)
	for _, migration := range []string{
		"000001_legacy_baseline.up.sql",
		"000002_catalog_v1.up.sql",
		"000003_auth.up.sql",
		"000004_order.up.sql",
	} {
		if err := execMigrationFile(ctx, conn, migration); err != nil {
			t.Fatalf("apply %s: %v", migration, err)
		}
	}
	return ctx
}

func TestMySQLConfirmationHardeningBackfillsConsumedLegacyActionAndEnforcesState(t *testing.T) {
	_, conn, _ := openMigrationIntegrationDB(t)
	ctx := prepareLegacyConfirmationSchema(t, conn)
	if err := seedLegacyConfirmation(ctx, conn, true, true); err != nil {
		t.Fatal(err)
	}
	assertAmountCheckRejectsNegative(t, ctx, conn, "total_amount")
	if err := execMigrationFile(ctx, conn, "000005_confirmation_decision.up.sql"); err != nil {
		t.Fatalf("apply v5: %v", err)
	}
	assertAmountCheckRejectsNegative(t, ctx, conn, "quoted_amount")
	if err := execMigrationFile(ctx, conn, "000006_confirmation_hardening.up.sql"); err != nil {
		t.Fatalf("apply v6: %v", err)
	}
	assertAmountCheckRejectsNegative(t, ctx, conn, "quoted_amount")

	var status, decision string
	var orderID uint64
	if err := conn.QueryRowContext(ctx, `SELECT status, decision, order_id FROM pending_action
		WHERE pending_action_id = '00000000-0000-0000-0000-000000000042'`).Scan(
		&status, &decision, &orderID,
	); err != nil {
		t.Fatal(err)
	}
	if status != "APPROVED" || decision != "APPROVE" || orderID != 99 {
		t.Fatalf("legacy action was not preserved as terminal: status=%s decision=%s order=%d", status, decision, orderID)
	}
	if _, err := conn.ExecContext(ctx, `UPDATE pending_action SET status='PENDING', decision=NULL, order_id=NULL
		WHERE pending_action_id='00000000-0000-0000-0000-000000000042'`); err == nil {
		t.Fatal("state consistency constraint accepted PENDING with consumed_at")
	}
	if err := execMigrationFile(ctx, conn, "000006_confirmation_hardening.down.sql"); err != nil {
		t.Fatalf("rollback hardening constraint: %v", err)
	}
	if err := execMigrationFile(ctx, conn, "000005_confirmation_decision.down.sql"); err != nil {
		t.Fatalf("rollback confirmation decision: %v", err)
	}
	assertAmountCheckRejectsNegative(t, ctx, conn, "total_amount")
	if err := execMigrationFile(ctx, conn, "000005_confirmation_decision.up.sql"); err != nil {
		t.Fatalf("reapply confirmation decision: %v", err)
	}
	assertAmountCheckRejectsNegative(t, ctx, conn, "quoted_amount")
	if err := execMigrationFile(ctx, conn, "000006_confirmation_hardening.up.sql"); err != nil {
		t.Fatalf("reapply hardening: %v", err)
	}
	assertLegacyApproved(t, ctx, conn)
}

func TestMySQLInventoryWatchMigrationDownThenUpIsReversible(t *testing.T) {
	_, conn, _ := openMigrationIntegrationDB(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	for _, migration := range []string{
		"000001_legacy_baseline.up.sql",
		"000002_catalog_v1.up.sql",
		"000003_auth.up.sql",
		"000004_order.up.sql",
		"000005_confirmation_decision.up.sql",
		"000006_confirmation_hardening.up.sql",
		"000007_agent_memory.up.sql",
		"000008_inventory_watch.up.sql",
	} {
		if err := execMigrationFile(ctx, conn, migration); err != nil {
			t.Fatalf("apply %s: %v", migration, err)
		}
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO outbox_event
		(event_id,event_type,aggregate_type,aggregate_id,payload_json,status,lease_owner,lease_expires_at)
		VALUES ('00000000-0000-0000-0000-000000000088','inventory.stock_changed.v1','INVENTORY','88',
		JSON_OBJECT('event_id','00000000-0000-0000-0000-000000000088'),'PROCESSING','rollback-test',
		CURRENT_TIMESTAMP(3)+INTERVAL 30 SECOND)`); err != nil {
		t.Fatal(err)
	}
	if err := execMigrationFile(ctx, conn, "000008_inventory_watch.down.sql"); err != nil {
		t.Fatal(err)
	}
	var tables, leaseColumns int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
		WHERE table_schema=DATABASE() AND table_name IN ('agent_task','notification','inventory_event_consumption')`).Scan(&tables); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.columns
		WHERE table_schema=DATABASE() AND table_name='outbox_event' AND column_name IN ('lease_owner','lease_expires_at')`).Scan(&leaseColumns); err != nil {
		t.Fatal(err)
	}
	if tables != 0 || leaseColumns != 0 {
		t.Fatalf("down left tables=%d lease_columns=%d", tables, leaseColumns)
	}
	var rolledBackStatus string
	if err := conn.QueryRowContext(ctx, `SELECT status FROM outbox_event
		WHERE event_id='00000000-0000-0000-0000-000000000088'`).Scan(&rolledBackStatus); err != nil {
		t.Fatal(err)
	}
	if rolledBackStatus != "PENDING" {
		t.Fatalf("processing lease rollback status = %s; want PENDING", rolledBackStatus)
	}
	if err := execMigrationFile(ctx, conn, "000008_inventory_watch.up.sql"); err != nil {
		t.Fatal(err)
	}
	assertInventoryWatchSchema(t, ctx, conn)
}

func TestMySQLAgentTraceEvalMigrationUpDownUpAndConstraints(t *testing.T) {
	_, conn, _ := openMigrationIntegrationDB(t)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	for _, migration := range []string{
		"000001_legacy_baseline.up.sql",
		"000002_catalog_v1.up.sql",
		"000003_auth.up.sql",
		"000004_order.up.sql",
		"000005_confirmation_decision.up.sql",
		"000006_confirmation_hardening.up.sql",
		"000007_agent_memory.up.sql",
		"000008_inventory_watch.up.sql",
		"000009_agent_trace_eval.up.sql",
	} {
		if err := execMigrationFile(ctx, conn, migration); err != nil {
			t.Fatalf("apply %s: %v", migration, err)
		}
	}
	assertAgentTraceEvalSchema(t, ctx, conn)
	if _, err := conn.ExecContext(ctx, `INSERT INTO user_account
		(user_id,email,display_name,password_hash,email_verified_at)
		VALUES (77,'trace@example.test','Trace User','argon2id-placeholder',NOW(3))`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO agent_run
		(run_id,user_id,conversation_id,run_kind,workflow_type,status,trace_status,replay_mode,
		 replayable,input_json,model_profile,model_manifest_hash,prompt_set_version,prompt_manifest_hash,
		 tool_registry_version,harness_version,cost_status,retention_expires_at,started_at,completed_at)
		VALUES ('00000000-0000-0000-0000-000000000901',77,'trace-conversation','LIVE','CHAT',
		'COMPLETED','COMPLETE','NONE',TRUE,JSON_OBJECT('content','safe'),'default',UNHEX(REPEAT('11',32)),
		'v1',UNHEX(REPEAT('22',32)),'tools-v1','harness-v1','UNAVAILABLE',NOW(3)+INTERVAL 30 DAY,NOW(3),NOW(3))`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO agent_step
		(step_id,run_id,sequence_no,node_id,step_type,status,input_json,output_json,evidence_ids_json,
		 cost_status,started_at,completed_at,duration_ms)
		VALUES ('00000000-0000-0000-0000-000000000902','00000000-0000-0000-0000-000000000901',
		1,'catalog','TOOL','COMPLETED',JSON_OBJECT(),JSON_OBJECT(),JSON_ARRAY('book:1'),
		'UNAVAILABLE',NOW(3),NOW(3),0)`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO agent_evidence
		(run_id,evidence_id,source_step_id,evidence_type,source_ref,payload_json,content_hash)
		VALUES ('00000000-0000-0000-0000-000000000901','book:1',
		'00000000-0000-0000-0000-000000000902','BOOK','catalog:1',JSON_OBJECT('book_id',1),
		UNHEX(REPEAT('33',32)))`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO agent_run
		(run_id,source_run_id,user_id,conversation_id,run_kind,workflow_type,status,trace_status,replay_mode,
		 replayable,input_json,model_profile,model_manifest_hash,prompt_set_version,prompt_manifest_hash,
		 tool_registry_version,harness_version,cost_status,request_idempotency_key,request_hash,
		 retention_expires_at,completed_at)
		VALUES ('00000000-0000-0000-0000-000000000903','00000000-0000-0000-0000-000000000901',
		77,'trace-conversation','REPLAY','CHAT','FAILED','COMPLETE','FIXED_TOOL_RESULTS',FALSE,JSON_OBJECT(),
		'default',UNHEX(REPEAT('11',32)),'v1',UNHEX(REPEAT('22',32)),'tools-v1','harness-v1',
		'UNAVAILABLE','trace-replay-key-0001',UNHEX(REPEAT('44',32)),NOW(3)+INTERVAL 30 DAY,NOW(3))`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO agent_run
		(run_id,user_id,conversation_id,run_kind,workflow_type,status,trace_status,replay_mode,replayable,
		 input_json,model_profile,model_manifest_hash,prompt_set_version,prompt_manifest_hash,
		 tool_registry_version,harness_version,cost_status,retention_expires_at,completed_at)
		VALUES ('00000000-0000-0000-0000-000000000904',NULL,NULL,'LIVE','CHAT','COMPLETED','COMPLETE',
		'NONE',FALSE,JSON_OBJECT(),'default',UNHEX(REPEAT('11',32)),'v1',UNHEX(REPEAT('22',32)),
		'tools-v1','harness-v1','UNAVAILABLE',NOW(3)+INTERVAL 30 DAY,NOW(3))`); err == nil {
		t.Fatal("trace identity constraint accepted LIVE without a user or conversation")
	}
	if err := execMigrationFile(ctx, conn, "000009_agent_trace_eval.down.sql"); err != nil {
		t.Fatalf("rollback v9: %v", err)
	}
	var count int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
		WHERE table_schema=DATABASE() AND table_name IN
		('agent_run','agent_step','agent_evidence','agent_tool_fixture','evaluation_run','evaluation_case_result')`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("v9 down left %d trace/eval tables", count)
	}
	if err := execMigrationFile(ctx, conn, "000009_agent_trace_eval.up.sql"); err != nil {
		t.Fatalf("reapply v9: %v", err)
	}
	assertAgentTraceEvalSchema(t, ctx, conn)
}

func TestMySQLConfirmationHardeningFailsWhenConsumedLegacyActionHasNoOrder(t *testing.T) {
	_, conn, _ := openMigrationIntegrationDB(t)
	ctx := prepareLegacyConfirmationSchema(t, conn)
	if err := seedLegacyConfirmation(ctx, conn, false, true); err != nil {
		t.Fatal(err)
	}
	if err := execMigrationFile(ctx, conn, "000005_confirmation_decision.up.sql"); err != nil {
		t.Fatal(err)
	}
	if err := execMigrationFile(ctx, conn, "000006_confirmation_hardening.up.sql"); err == nil {
		t.Fatal("hardening migration accepted a consumed legacy action without an order")
	} else if !strings.Contains(strings.ToLower(err.Error()), "check constraint") {
		t.Fatalf("hardening failed for an unexpected reason: %v", err)
	}

	var status string
	if err := conn.QueryRowContext(ctx, `SELECT status FROM pending_action
		WHERE pending_action_id='00000000-0000-0000-0000-000000000042'`).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "PENDING" {
		t.Fatalf("failed migration changed unmatched legacy action to %s", status)
	}
}

func TestMySQLConfirmationHardeningKeepsUnconsumedLegacyActionPending(t *testing.T) {
	_, conn, dsn := openMigrationIntegrationDB(t)
	ctx := prepareLegacyConfirmationSchema(t, conn)
	if err := seedLegacyConfirmation(ctx, conn, false, false); err != nil {
		t.Fatal(err)
	}
	setMigrationVersion(t, ctx, conn, 4, false)
	if err := migrationrunner.Run(ctx, migrationRunnerConfig(dsn)); err != nil {
		t.Fatalf("migrate clean v4 with unconsumed action: %v", err)
	}
	assertMigrationVersion(t, ctx, conn, 11, false)
	assertMemorySchema(t, ctx, conn)
	assertSingleAgentRuntimeSchema(t, ctx, conn)
	assertAgentTraceEvalSchema(t, ctx, conn)
	var status string
	var decision sql.NullString
	var orderID sql.NullInt64
	var consumedAt sql.NullTime
	if err := conn.QueryRowContext(ctx, `SELECT status, decision, order_id, consumed_at FROM pending_action
		WHERE pending_action_id='00000000-0000-0000-0000-000000000042'`).Scan(
		&status, &decision, &orderID, &consumedAt,
	); err != nil {
		t.Fatal(err)
	}
	if status != "PENDING" || decision.Valid || orderID.Valid || consumedAt.Valid {
		t.Fatalf("unconsumed action changed terminal fields: status=%s decision=%v order=%v consumed=%v",
			status, decision, orderID, consumedAt)
	}
}

func TestMySQLMigrationRunnerUpgradesConsumedCleanV4ToLatest(t *testing.T) {
	_, conn, dsn := openMigrationIntegrationDB(t)
	ctx := prepareLegacyConfirmationSchema(t, conn)
	if err := seedLegacyConfirmation(ctx, conn, true, true); err != nil {
		t.Fatal(err)
	}
	setMigrationVersion(t, ctx, conn, 4, false)
	if err := migrationrunner.Run(ctx, migrationRunnerConfig(dsn)); err != nil {
		t.Fatalf("migrate clean v4 with consumed action: %v", err)
	}
	assertMigrationVersion(t, ctx, conn, 11, false)
	assertLegacyApproved(t, ctx, conn)
	assertMemorySchema(t, ctx, conn)
	assertSingleAgentRuntimeSchema(t, ctx, conn)
	assertAgentTraceEvalSchema(t, ctx, conn)
}

func TestMySQLMigrationRunnerUpgradesEmptyDatabaseToLatest(t *testing.T) {
	_, conn, dsn := openMigrationIntegrationDB(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := migrationrunner.Run(ctx, migrationRunnerConfig(dsn)); err != nil {
		t.Fatalf("migrate empty database: %v", err)
	}
	assertMigrationVersion(t, ctx, conn, 11, false)
	assertAmountCheckRejectsNegativeAfterSeed(t, ctx, conn, "quoted_amount")
	assertMemorySchema(t, ctx, conn)
	assertSingleAgentRuntimeSchema(t, ctx, conn)
	assertAgentTraceEvalSchema(t, ctx, conn)
}

func TestMySQLSingleAgentRuntimeRequeuesV1PublishedEventsAndRoundTrips(t *testing.T) {
	_, conn, _ := openMigrationIntegrationDB(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	for _, migration := range []string{
		"000001_legacy_baseline.up.sql",
		"000002_catalog_v1.up.sql",
		"000003_auth.up.sql",
		"000004_order.up.sql",
		"000005_confirmation_decision.up.sql",
		"000006_confirmation_hardening.up.sql",
		"000007_agent_memory.up.sql",
		"000008_inventory_watch.up.sql",
		"000009_agent_trace_eval.up.sql",
		"000010_trace_usage_semantics.up.sql",
	} {
		if err := execMigrationFile(ctx, conn, migration); err != nil {
			t.Fatalf("apply %s: %v", migration, err)
		}
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO book(book_id,isbn,title,price,stock_count)
		VALUES(711,'v2-migration-711','V2 migration replay',1000,1)`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO inventory(book_id,available_stock,version)
		VALUES(711,1,2)`); err != nil {
		t.Fatal(err)
	}
	const completedEvent = "00000000-0000-0000-0000-000000000711"
	const pendingEvent = "00000000-0000-0000-0000-000000000712"
	for _, event := range []struct {
		id            string
		version       int
		aggregateType string
	}{
		{completedEvent, 1, "INVENTORY"},
		{pendingEvent, 2, "book"},
	} {
		if _, err := conn.ExecContext(ctx, `INSERT INTO outbox_event
			(event_id,event_type,aggregate_type,aggregate_id,payload_json,status,published_at,attempt_count)
			VALUES(?,'inventory.stock_changed.v1',?,'711',
			JSON_OBJECT('event_id',?,'event_type','inventory.stock_changed.v1','book_id',711,
			'available_stock',1,'inventory_version',?),'PUBLISHED',UTC_TIMESTAMP(3),1)`,
			event.id, event.aggregateType, event.id, event.version); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO inventory_event_consumption
		(event_id,stream_entry_id,event_type,payload_hash,book_id,inventory_version,status,attempt_count,completed_at)
		SELECT event_id,'v1-stream-711',event_type,UNHEX(SHA2(CAST(payload_json AS CHAR),256)),711,1,
		'COMPLETED',1,UTC_TIMESTAMP(3) FROM outbox_event WHERE event_id=?`, completedEvent); err != nil {
		t.Fatal(err)
	}

	if err := execMigrationFile(ctx, conn, "000011_single_agent_runtime.up.sql"); err != nil {
		t.Fatalf("apply v11: %v", err)
	}
	assertSingleAgentRuntimeSchema(t, ctx, conn)
	rows, err := conn.QueryContext(ctx, `SELECT event_id,status,aggregate_type,processed_at,lease_owner,lease_expires_at
		FROM outbox_event WHERE event_id IN (?,?) ORDER BY event_id`, completedEvent, pendingEvent)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	seen := 0
	for rows.Next() {
		var eventID, status, aggregateType string
		var processedAt sql.NullTime
		var leaseOwner sql.NullString
		var leaseExpires sql.NullTime
		if err := rows.Scan(&eventID, &status, &aggregateType, &processedAt, &leaseOwner, &leaseExpires); err != nil {
			t.Fatal(err)
		}
		if status != "PENDING" || processedAt.Valid || leaseOwner.Valid || leaseExpires.Valid {
			t.Fatalf("v1 event %s migrated to status=%s processed=%v owner=%v expiry=%v; want retryable PENDING",
				eventID, status, processedAt, leaseOwner, leaseExpires)
		}
		if aggregateType != "INVENTORY" {
			t.Fatalf("v1 event %s aggregate_type=%s; want canonical INVENTORY", eventID, aggregateType)
		}
		seen++
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if seen != 2 {
		t.Fatalf("migrated event rows = %d; want 2", seen)
	}

	if _, err := conn.ExecContext(ctx, `UPDATE outbox_event SET status='PROCESSED',processed_at=UTC_TIMESTAMP(3)
		WHERE event_id=?`, completedEvent); err != nil {
		t.Fatal(err)
	}
	if err := execMigrationFile(ctx, conn, "000011_single_agent_runtime.down.sql"); err != nil {
		t.Fatalf("rollback v11: %v", err)
	}
	var downStatus string
	if err := conn.QueryRowContext(ctx, `SELECT status FROM outbox_event WHERE event_id=?`, completedEvent).Scan(&downStatus); err != nil {
		t.Fatal(err)
	}
	if downStatus != "PUBLISHED" {
		t.Fatalf("v11 rollback status = %s; want terminal v1 PUBLISHED", downStatus)
	}
	if err := execMigrationFile(ctx, conn, "000011_single_agent_runtime.up.sql"); err != nil {
		t.Fatalf("reapply v11: %v", err)
	}
	var upStatus string
	if err := conn.QueryRowContext(ctx, `SELECT status FROM outbox_event WHERE event_id=?`, completedEvent).Scan(&upStatus); err != nil {
		t.Fatal(err)
	}
	if upStatus != "PENDING" {
		t.Fatalf("v11 reapply status = %s; want safe replay PENDING", upStatus)
	}
}

func TestMySQLCleanV5PreflightThenRunnerAppliesRemainingMigrations(t *testing.T) {
	_, conn, dsn := openMigrationIntegrationDB(t)
	ctx := prepareLegacyConfirmationSchema(t, conn)
	if err := execMigrationFile(ctx, conn, "000005_confirmation_decision.up.sql"); err != nil {
		t.Fatal(err)
	}
	setMigrationVersion(t, ctx, conn, 5, false)
	if err := migrationrunner.Run(ctx, migrationRunnerConfig(dsn)); err != nil {
		t.Fatalf("migrate clean v5: %v", err)
	}
	assertMigrationVersion(t, ctx, conn, 11, false)
	var constraintCount int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.table_constraints
		WHERE constraint_schema=DATABASE() AND table_name='pending_action'
		AND constraint_name='ck_pending_state_consistency'`).Scan(&constraintCount); err != nil {
		t.Fatal(err)
	}
	if constraintCount != 1 {
		t.Fatalf("v6 state constraint count = %d; want 1", constraintCount)
	}
	assertMemorySchema(t, ctx, conn)
	assertSingleAgentRuntimeSchema(t, ctx, conn)
	assertAgentTraceEvalSchema(t, ctx, conn)
}

func TestMySQLDirtyV5RunnerFailsClosedWithoutChangingVersionOrSchema(t *testing.T) {
	_, conn, dsn := openMigrationIntegrationDB(t)
	ctx := prepareLegacyConfirmationSchema(t, conn)
	if err := execMigrationFile(ctx, conn, "000005_confirmation_decision.up.sql"); err != nil {
		t.Fatal(err)
	}
	setMigrationVersion(t, ctx, conn, 5, true)
	err := migrationrunner.Run(ctx, migrationRunnerConfig(dsn))
	if err == nil {
		t.Fatal("dirty v5 migration unexpectedly continued")
	}
	cfg, _ := mysql.ParseDSN(dsn)
	if !strings.Contains(err.Error(), migrationrunner.RecoveryDocument) ||
		strings.Contains(err.Error(), dsn) || (cfg != nil && cfg.Passwd != "" && strings.Contains(err.Error(), cfg.Passwd)) {
		t.Fatalf("dirty v5 error is missing safe guidance or leaked credentials: %v", err)
	}
	assertMigrationVersion(t, ctx, conn, 5, true)
	var constraintCount int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.table_constraints
		WHERE constraint_schema=DATABASE() AND table_name='pending_action'
		AND constraint_name='ck_pending_state_consistency'`).Scan(&constraintCount); err != nil {
		t.Fatal(err)
	}
	if constraintCount != 0 {
		t.Fatal("dirty v5 runner skipped the version gate and applied v6")
	}
}

func TestMySQLConfirmationV5PreflightRejectsSchemaDrift(t *testing.T) {
	tests := []struct {
		name   string
		mutate string
	}{
		{name: "missing column", mutate: `ALTER TABLE pending_action DROP CHECK ck_pending_action_type, DROP COLUMN action_type`},
		{name: "residual old column", mutate: `ALTER TABLE pending_action ADD COLUMN total_amount bigint NULL`},
		{name: "missing token index", mutate: `ALTER TABLE pending_action DROP INDEX uk_pending_token_hash`},
		{name: "missing idempotency index", mutate: `ALTER TABLE pending_action
			ADD KEY idx_pending_user_for_test (user_id), DROP INDEX uk_pending_user_prepare_idempotency`},
		{name: "missing foreign key", mutate: `ALTER TABLE pending_action DROP FOREIGN KEY fk_pending_order`},
		{name: "missing status check", mutate: `ALTER TABLE pending_action DROP CHECK ck_pending_status`},
		{name: "wrong amount check", mutate: `ALTER TABLE pending_action DROP CHECK ck_pending_total_nonnegative,
			ADD COLUMN legacy_amount bigint NOT NULL DEFAULT 0,
			ADD CONSTRAINT ck_pending_total_nonnegative CHECK (legacy_amount >= 0)`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, conn, _ := openMigrationIntegrationDB(t)
			ctx := prepareLegacyConfirmationSchema(t, conn)
			if err := execMigrationFile(ctx, conn, "000005_confirmation_decision.up.sql"); err != nil {
				t.Fatal(err)
			}
			setMigrationVersion(t, ctx, conn, 5, false)
			if _, err := conn.ExecContext(ctx, test.mutate); err != nil {
				t.Fatalf("create schema drift: %v", err)
			}
			if err := execMigrationFile(ctx, conn, "verify_confirmation_v5.sql"); err == nil {
				t.Fatal("v5 preflight accepted invalid schema")
			}
		})
	}
}

func assertLegacyApproved(t *testing.T, ctx context.Context, conn *sql.Conn) {
	t.Helper()
	var status, decision string
	var orderID uint64
	if err := conn.QueryRowContext(ctx, `SELECT status, decision, order_id FROM pending_action
		WHERE pending_action_id = '00000000-0000-0000-0000-000000000042'`).Scan(
		&status, &decision, &orderID,
	); err != nil {
		t.Fatal(err)
	}
	if status != "APPROVED" || decision != "APPROVE" || orderID != 99 {
		t.Fatalf("legacy action was not preserved as terminal: status=%s decision=%s order=%d", status, decision, orderID)
	}
}

func assertAmountCheckRejectsNegative(t *testing.T, ctx context.Context, conn *sql.Conn, column string) {
	t.Helper()
	query := "UPDATE pending_action SET `" + column + "`=-1 WHERE "
	if column == "total_amount" {
		query += "action_id='00000000-0000-0000-0000-000000000042'"
	} else {
		query += "pending_action_id='00000000-0000-0000-0000-000000000042'"
	}
	if _, err := conn.ExecContext(ctx, query); err == nil {
		t.Fatalf("%s amount check accepted a negative value", column)
	}
}

func assertAmountCheckRejectsNegativeAfterSeed(t *testing.T, ctx context.Context, conn *sql.Conn, column string) {
	t.Helper()
	if _, err := conn.ExecContext(ctx, `INSERT INTO user_account
		(user_id, email, display_name, password_hash, email_verified_at)
		VALUES (42, 'constraint@example.test', 'Constraint', 'argon2id-placeholder', NOW(3))`); err != nil {
		t.Fatal(err)
	}
	_, err := conn.ExecContext(ctx, `INSERT INTO pending_action
		(pending_action_id, token_hash, user_id, conversation_id, action_type, payload_hash, items_json,
		 quoted_amount, currency, status, prepare_idempotency_key, expires_at)
		VALUES ('00000000-0000-0000-0000-000000000043', UNHEX(REPEAT('ef', 32)), 42,
		 'constraint-conversation', 'CREATE_ORDER', UNHEX(REPEAT('12', 32)), JSON_ARRAY(),
		 -1, 'CNY', 'PENDING', 'constraint-prepare-key', NOW(3) + INTERVAL 10 MINUTE)`)
	if err == nil {
		t.Fatalf("%s amount check accepted a negative insert", column)
	}
}

func setMigrationVersion(t *testing.T, ctx context.Context, conn *sql.Conn, version uint, dirty bool) {
	t.Helper()
	if _, err := conn.ExecContext(ctx, `CREATE TABLE schema_migrations
		(version bigint NOT NULL PRIMARY KEY, dirty boolean NOT NULL)`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, `INSERT INTO schema_migrations(version, dirty) VALUES (?, ?)`, version, dirty); err != nil {
		t.Fatal(err)
	}
}

func assertMigrationVersion(t *testing.T, ctx context.Context, conn *sql.Conn, version uint, dirty bool) {
	t.Helper()
	var gotVersion uint
	var gotDirty bool
	if err := conn.QueryRowContext(ctx, `SELECT version, dirty FROM schema_migrations`).Scan(&gotVersion, &gotDirty); err != nil {
		t.Fatal(err)
	}
	if gotVersion != version || gotDirty != dirty {
		t.Fatalf("migration state = v%d dirty=%v; want v%d dirty=%v", gotVersion, gotDirty, version, dirty)
	}
}

func assertMemorySchema(t *testing.T, ctx context.Context, conn *sql.Conn) {
	t.Helper()
	for _, table := range []string{"user_memory", "memory_outbox"} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
			WHERE table_schema=DATABASE() AND table_name=?`, table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("memory migration table %s count = %d; want 1", table, count)
		}
	}
}

func assertInventoryWatchSchema(t *testing.T, ctx context.Context, conn *sql.Conn) {
	t.Helper()
	for _, table := range []string{"inventory_event_consumption", "agent_task", "notification"} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
			WHERE table_schema=DATABASE() AND table_name=?`, table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("inventory watch migration table %s count = %d; want 1", table, count)
		}
	}
	for _, constraint := range []string{"ck_outbox_status", "ck_outbox_lease_consistency"} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.table_constraints
			WHERE constraint_schema=DATABASE() AND table_name='outbox_event' AND constraint_name=?`,
			constraint).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("outbox constraint %s count = %d; want 1", constraint, count)
		}
	}
}

func assertSingleAgentRuntimeSchema(t *testing.T, ctx context.Context, conn *sql.Conn) {
	t.Helper()
	for _, table := range []string{"inventory_event_consumption", "agent_task", "notification"} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
			WHERE table_schema=DATABASE() AND table_name=?`, table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("single-agent table %s count = %d; want 1", table, count)
		}
	}
	for _, constraint := range []string{"ck_outbox_status_v2", "ck_outbox_lease_consistency_v2"} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.table_constraints
			WHERE constraint_schema=DATABASE() AND table_name='outbox_event' AND constraint_name=?`,
			constraint).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("single-agent outbox constraint %s count = %d; want 1", constraint, count)
		}
	}
	for table, column := range map[string]string{
		"outbox_event":                "processed_at",
		"inventory_event_consumption": "source_event_ref",
	} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.columns
			WHERE table_schema=DATABASE() AND table_name=? AND column_name=?`, table, column).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("single-agent column %s.%s count = %d; want 1", table, column, count)
		}
	}
	var legacyColumns int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.columns
		WHERE table_schema=DATABASE() AND (
			(table_name='outbox_event' AND column_name='published_at') OR
			(table_name='inventory_event_consumption' AND column_name='stream_entry_id') OR
			(table_name='agent_task' AND column_name='checkpoint_status'))`).Scan(&legacyColumns); err != nil {
		t.Fatal(err)
	}
	if legacyColumns != 0 {
		t.Fatalf("single-agent runtime retained %d legacy columns; want 0", legacyColumns)
	}
}

func assertAgentTraceEvalSchema(t *testing.T, ctx context.Context, conn *sql.Conn) {
	t.Helper()
	for _, table := range []string{
		"agent_run", "agent_step", "agent_evidence", "agent_tool_fixture",
		"evaluation_run", "evaluation_case_result",
	} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
			WHERE table_schema=DATABASE() AND table_name=?`, table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("trace/eval migration table %s count = %d; want 1", table, count)
		}
	}
	for table, constraint := range map[string]string{
		"agent_run":              "ck_agent_run_kind_consistency",
		"agent_step":             "fk_agent_step_parent",
		"agent_evidence":         "fk_agent_evidence_step",
		"agent_tool_fixture":     "uk_agent_tool_fixture_match",
		"evaluation_run":         "ck_evaluation_run_judge_mode",
		"evaluation_case_result": "fk_evaluation_case_agent_run",
	} {
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.table_constraints
			WHERE constraint_schema=DATABASE() AND table_name=? AND constraint_name=?`,
			table, constraint).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("trace/eval constraint %s.%s count = %d; want 1", table, constraint, count)
		}
	}
}

func migrationRunnerConfig(dsn string) migrationrunner.Config {
	return migrationrunner.Config{
		DataSource:        dsn,
		MigrationsPath:    ".",
		ConfirmationV5SQL: "verify_confirmation_v5.sql",
	}
}
