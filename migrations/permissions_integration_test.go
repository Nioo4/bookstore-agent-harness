package migrations_test

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"

	"github.com/go-sql-driver/mysql"
)

func TestBootstrapEscapesMySQLWildcardOnlyForParameterizedQueries(t *testing.T) {
	_, currentFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	source, err := os.ReadFile(filepath.Join(filepath.Dir(currentFile), "..", "deploy", "bootstrap.py"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(source)
	// asyncmy formats queries with Python's percent operator whenever arguments
	// are supplied. Parameterized CREATE/ALTER statements therefore need %% to
	// emit one literal MySQL wildcard, while statements without arguments must
	// use the single % that is sent to MySQL unchanged.
	if !strings.Contains(text, "@'%%' IDENTIFIED BY %s") {
		t.Fatal("parameterized CREATE/ALTER statements must escape the MySQL host wildcard as %%")
	}
	if !strings.Contains(text, "REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{username}'@'%'") {
		t.Fatal("non-parameterized statements must use the literal MySQL host wildcard %")
	}
	if !strings.Contains(text, "REVOKE ALL PRIVILEGES, GRANT OPTION") {
		t.Fatal("bootstrap must revoke accumulated grants before applying the v2 permission matrix")
	}
}

func TestStage10DatabaseAccountsEnforceServiceBoundaries(t *testing.T) {
	raw := os.Getenv("BOOKSTORE_PERMISSIONS_TEST_DSN")
	if raw == "" {
		t.Skip("BOOKSTORE_PERMISSIONS_TEST_DSN is required")
	}
	cfg, err := mysql.ParseDSN(raw)
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[A-Za-z0-9_]+$`).MatchString(cfg.DBName) {
		t.Fatalf("unsafe integration database name %q", cfg.DBName)
	}
	root, err := sql.Open("mysql", raw)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	for _, user := range []string{"stage9_agent", "stage10_worker"} {
		_, _ = root.Exec("DROP USER IF EXISTS '" + user + "'@'%'")
	}
	t.Cleanup(func() {
		_, _ = root.Exec("DROP USER IF EXISTS 'stage9_agent'@'%'")
		_, _ = root.Exec("DROP USER IF EXISTS 'stage10_worker'@'%'")
	})
	if _, err := root.Exec("CREATE USER 'stage9_agent'@'%' IDENTIFIED BY 'agent-test-only'"); err != nil {
		t.Fatal(err)
	}
	if _, err := root.Exec("CREATE USER 'stage10_worker'@'%' IDENTIFIED BY 'worker-test-only'"); err != nil {
		t.Fatal(err)
	}
	grants := []string{
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`user_memory` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`memory_outbox` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`agent_task` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,UPDATE ON `%s`.`notification` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`agent_run` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`agent_step` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT ON `%s`.`schema_migrations` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`agent_evidence` TO 'stage9_agent'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT ON `%s`.`agent_tool_fixture` TO 'stage9_agent'@'%%'", cfg.DBName),
	}
	for _, grant := range grants {
		if _, err := root.Exec(grant); err != nil {
			t.Fatal(err)
		}
	}
	workerGrants := []string{
		fmt.Sprintf("GRANT SELECT,UPDATE ON `%s`.`outbox_event` TO 'stage10_worker'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT ON `%s`.`inventory` TO 'stage10_worker'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`inventory_event_consumption` TO 'stage10_worker'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,UPDATE ON `%s`.`agent_task` TO 'stage10_worker'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,INSERT,UPDATE ON `%s`.`notification` TO 'stage10_worker'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,DELETE ON `%s`.`agent_run` TO 'stage10_worker'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,UPDATE ON `%s`.`memory_outbox` TO 'stage10_worker'@'%%'", cfg.DBName),
		fmt.Sprintf("GRANT SELECT,UPDATE ON `%s`.`user_memory` TO 'stage10_worker'@'%%'", cfg.DBName),
	}
	for _, grant := range workerGrants {
		if _, err := root.Exec(grant); err != nil {
			t.Fatal(err)
		}
	}

	agentCfg := *cfg
	agentCfg.User = "stage9_agent"
	agentCfg.Passwd = "agent-test-only"
	agent := openPermissionDB(t, agentCfg.FormatDSN())
	defer agent.Close()
	assertQueryAllowed(t, agent, "SELECT task_id FROM agent_task LIMIT 1")
	assertQueryAllowed(t, agent, "SELECT notification_id FROM notification LIMIT 1")
	assertQueryAllowed(t, agent, "SELECT memory_id FROM user_memory LIMIT 1")
	assertQueryAllowed(t, agent, "SELECT run_id FROM agent_run LIMIT 1")
	assertQueryAllowed(t, agent, "SELECT source_run_id FROM agent_tool_fixture LIMIT 1")
	assertQueryDenied(t, agent, "SELECT eval_run_id FROM evaluation_run LIMIT 1")
	assertQueryDenied(t, agent, "SELECT available_stock FROM inventory LIMIT 1")
	assertQueryDenied(t, agent, "SELECT event_id FROM outbox_event LIMIT 1")
	assertQueryDenied(t, agent, "SELECT password_hash FROM user_account LIMIT 1")
	assertExecDenied(t, agent, "DELETE FROM agent_tool_fixture WHERE 1=0")

	workerCfg := *cfg
	workerCfg.User = "stage10_worker"
	workerCfg.Passwd = "worker-test-only"
	worker := openPermissionDB(t, workerCfg.FormatDSN())
	defer worker.Close()
	assertQueryAllowed(t, worker, "SELECT event_id FROM outbox_event LIMIT 1")
	assertQueryAllowed(t, worker, "SELECT available_stock FROM inventory LIMIT 1")
	assertQueryAllowed(t, worker, "SELECT memory_id FROM memory_outbox LIMIT 1")
	assertQueryAllowed(t, worker, "SELECT memory_id FROM user_memory LIMIT 1")
	assertExecAllowed(t, worker, "DELETE FROM agent_run WHERE 1=0")
	assertQueryDenied(t, worker, "SELECT password_hash FROM user_account LIMIT 1")
	assertQueryDenied(t, worker, "SELECT isbn FROM book LIMIT 1")
	assertExecDenied(t, worker, "DELETE FROM user_memory WHERE 1=0")
	assertExecDenied(t, worker, "INSERT INTO memory_outbox (event_id) VALUES ('permission-test')")
	assertExecDenied(t, worker, "INSERT INTO user_memory (memory_id) VALUES ('permission-test')")
	assertExecDenied(t, worker, "DELETE FROM memory_outbox WHERE 1=0")
}

func openPermissionDB(t *testing.T, dsn string) *sql.DB {
	t.Helper()
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Ping(); err != nil {
		db.Close()
		t.Fatal(err)
	}
	return db
}

func assertQueryAllowed(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	rows, err := db.Query(query)
	if err != nil {
		t.Fatalf("allowed query failed: %v", err)
	}
	rows.Close()
}

func assertQueryDenied(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	rows, err := db.Query(query)
	if rows != nil {
		rows.Close()
	}
	if err == nil {
		t.Fatalf("forbidden query succeeded: %s", query)
	}
	if mysqlErr, ok := err.(*mysql.MySQLError); !ok || mysqlErr.Number != 1142 {
		t.Fatalf("forbidden query error = %v; want MySQL 1142", err)
	}
}

func assertExecDenied(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	if _, err := db.Exec(query); err == nil {
		t.Fatalf("forbidden mutation succeeded: %s", query)
	} else if mysqlErr, ok := err.(*mysql.MySQLError); !ok || mysqlErr.Number != 1142 {
		t.Fatalf("forbidden mutation error = %v; want MySQL 1142", err)
	}
}

func assertExecAllowed(t *testing.T, db *sql.DB, query string) {
	t.Helper()
	if _, err := db.Exec(query); err != nil {
		t.Fatalf("allowed mutation failed: %v", err)
	}
}
