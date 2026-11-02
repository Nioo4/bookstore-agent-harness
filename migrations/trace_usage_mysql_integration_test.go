package migrations_test

import (
	"context"
	"database/sql"
	"os"
	"strings"
	"testing"
	"time"
)

// Reuses the existing dedicated-instance migration driver. This test is only
// executed after the acceptance runner verifies the target's isolation.
func TestMySQLTraceUsageV10PreservesHistoryAndRefusesLossyDown(t *testing.T) {
	for _, newTable := range []string{"agent_run", "agent_step"} {
		t.Run(newTable, func(t *testing.T) {
			_, conn, _ := openMigrationIntegrationDB(t)
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			entries, err := os.ReadDir(".")
			if err != nil {
				t.Fatal(err)
			}
			for _, entry := range entries {
				name := entry.Name()
				if strings.HasPrefix(name, "00000") && strings.HasSuffix(name, ".up.sql") {
					if err := execMigrationFile(ctx, conn, name); err != nil {
						t.Fatal(err)
					}
				}
			}
			for _, statement := range []string{
				`INSERT INTO agent_run (run_id,run_kind,workflow_type,status,trace_status,input_json,
				model_profile,model_manifest_hash,prompt_set_version,prompt_manifest_hash,tool_registry_version,harness_version,
				prompt_tokens,completion_tokens,known_cost_microusd,cost_status,pricing_version,retention_expires_at,completed_at)
				VALUES ('usage-old','EVALUATION','EVALUATION_CASE','COMPLETED','COMPLETE',JSON_OBJECT(),
				'default',UNHEX(REPEAT('aa',32)),'v1',UNHEX(REPEAT('bb',32)),'tools-v1','old',10,0,25,'CALCULATED','pricing-v1',
				NOW(3)+INTERVAL 1 DAY,NOW(3))`,
				`INSERT INTO agent_step (step_id,run_id,sequence_no,node_id,step_type,status,evidence_ids_json,
				prompt_tokens,completion_tokens,cost_status,started_at,completed_at,duration_ms)
				VALUES ('usage-step','usage-old',1,'ordinary','SYSTEM','COMPLETED',JSON_ARRAY(),7,0,'UNAVAILABLE',NOW(3),NOW(3),0)`,
			} {
				if _, err := conn.ExecContext(ctx, statement); err != nil {
					t.Fatal(err)
				}
			}
			before := usageLegacyDigest(t, ctx, conn)
			if err := execMigrationFile(ctx, conn, "000010_trace_usage_semantics.up.sql"); err != nil {
				t.Fatal(err)
			}
			if after := usageLegacyDigest(t, ctx, conn); after != before {
				t.Fatal("Up changed legacy physical data")
			}
			var status string
			if err := conn.QueryRowContext(ctx, "SELECT usage_status FROM agent_run WHERE run_id='usage-old'").Scan(&status); err != nil || status != "LEGACY_UNKNOWN" {
				t.Fatalf("legacy classification: %q, %v", status, err)
			}
			if err := execMigrationFile(ctx, conn, "000010_trace_usage_semantics.down.sql"); err != nil {
				t.Fatal(err)
			}
			if after := usageLegacyDigest(t, ctx, conn); after != before {
				t.Fatal("pure legacy Down changed data")
			}
			if err := execMigrationFile(ctx, conn, "000010_trace_usage_semantics.up.sql"); err != nil {
				t.Fatal(err)
			}
			// Contradictory and SQL-UNKNOWN combinations must not evade CHECK.
			for _, statement := range []string{
				"UPDATE agent_run SET usage_status='COMPLETE',prompt_tokens=NULL WHERE run_id='usage-old'",
				"UPDATE agent_run SET usage_status='NO_CALLS',prompt_tokens=0,completion_tokens=0,cached_tokens=NULL,reasoning_tokens=0 WHERE run_id='usage-old'",
				"UPDATE agent_step SET usage_status='COMPLETE' WHERE step_id='usage-step'",
				"UPDATE agent_step SET usage_status='PARTIAL',prompt_tokens=NULL,completion_tokens=NULL,node_id='model:test',model_profile='default' WHERE step_id='usage-step'",
			} {
				if _, err := conn.ExecContext(ctx, statement); err == nil {
					t.Fatalf("constraint accepted invalid usage: %s", statement)
				}
			}
			statement := "UPDATE agent_run SET usage_status='COMPLETE' WHERE run_id='usage-old'"
			if newTable == "agent_step" {
				statement = "UPDATE agent_step SET usage_status='UNAVAILABLE',prompt_tokens=NULL,completion_tokens=NULL,node_id='model:test',model_profile='default' WHERE step_id='usage-step'"
			}
			if _, err := conn.ExecContext(ctx, statement); err != nil {
				t.Fatal(err)
			}
			if err := execMigrationFile(ctx, conn, "000010_trace_usage_semantics.down.sql"); err == nil {
				t.Fatal("lossy Down was not rejected")
			}
			var columns int
			if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE()
			AND table_name IN ('agent_run','agent_step') AND column_name='usage_status'`).Scan(&columns); err != nil || columns != 2 {
				t.Fatalf("Down performed DDL before refusal: %d, %v", columns, err)
			}
		})
	}
}

func usageLegacyDigest(t *testing.T, ctx context.Context, conn *sql.Conn) string {
	t.Helper()
	var digest string
	err := conn.QueryRowContext(ctx, `SELECT SHA2(CONCAT_WS('|',r.prompt_tokens,r.completion_tokens,r.known_cost_microusd,
	 r.cost_status,r.pricing_version,r.status,r.created_at,r.updated_at,r.completed_at,
	 s.prompt_tokens,s.completion_tokens,s.cost_status,s.status,s.created_at,s.completed_at),256)
	 FROM agent_run r JOIN agent_step s ON s.run_id=r.run_id WHERE r.run_id='usage-old'`).Scan(&digest)
	if err != nil {
		t.Fatal(err)
	}
	return digest
}
