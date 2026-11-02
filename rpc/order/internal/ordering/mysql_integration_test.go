package ordering

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

const orderMySQLIntegrationDSNEnv = "BOOKSTORE_TEST_MYSQL_DSN"

func TestMySQLConcurrentConfirmationCreatesOneOrderAndDeductsStockOnce(t *testing.T) {
	rawDSN := strings.TrimSpace(os.Getenv(orderMySQLIntegrationDSNEnv))
	if rawDSN == "" {
		t.Skipf("set %s to a dedicated MySQL 8 test database", orderMySQLIntegrationDSNEnv)
	}
	cfg, err := mysql.ParseDSN(rawDSN)
	if err != nil {
		t.Fatalf("parse integration DSN: %v", err)
	}
	if cfg.DBName == "" || !strings.Contains(strings.ToLower(cfg.DBName), "test") {
		t.Fatalf("%s database name must contain 'test'; got %q", orderMySQLIntegrationDSNEnv, cfg.DBName)
	}
	cfg.MultiStatements = true
	cfg.ParseTime = true
	integrationDSN := cfg.FormatDSN()

	admin := openOrderIntegrationDB(t, integrationDSN, 2)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	lockConnection, err := admin.Conn(ctx)
	if err != nil {
		admin.Close()
		t.Fatalf("open integration lock connection: %v", err)
	}
	var lockAcquired int
	if err := lockConnection.QueryRowContext(ctx, "SELECT GET_LOCK('bookstore_mysql_integration', 30)").Scan(&lockAcquired); err != nil {
		lockConnection.Close()
		admin.Close()
		t.Fatalf("acquire integration database lock: %v", err)
	}
	if lockAcquired != 1 {
		lockConnection.Close()
		admin.Close()
		t.Fatal("timed out acquiring integration database lock")
	}
	if err := resetOrderIntegrationSchema(ctx, admin); err != nil {
		_, _ = lockConnection.ExecContext(ctx, "DO RELEASE_LOCK('bookstore_mysql_integration')")
		lockConnection.Close()
		admin.Close()
		t.Fatalf("reset integration schema: %v", err)
	}
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cleanupCancel()
		_ = resetOrderIntegrationSchema(cleanupCtx, admin)
		_, _ = lockConnection.ExecContext(cleanupCtx, "DO RELEASE_LOCK('bookstore_mysql_integration')")
		_ = lockConnection.Close()
		_ = admin.Close()
	})

	for _, name := range []string{
		"000001_legacy_baseline.up.sql",
		"000002_catalog_v1.up.sql",
		"000003_auth.up.sql",
		"000004_order.up.sql",
		"000005_confirmation_decision.up.sql",
		"000006_confirmation_hardening.up.sql",
	} {
		contents, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "migrations", name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		if _, err := admin.ExecContext(ctx, string(contents)); err != nil {
			t.Fatalf("apply %s: %v", name, err)
		}
	}
	if _, err := admin.ExecContext(ctx, `INSERT INTO user_account
		(user_id, email, display_name, password_hash, email_verified_at)
		VALUES (42, 'concurrent@example.test', 'Concurrent', 'argon2id-placeholder', NOW(3))`); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.ExecContext(ctx, `INSERT INTO book
		(book_id, isbn, title, author, price, stock_count, currency, status)
		VALUES (1, '9780000000001', 'Concurrency', 'Tester', 2500, 10, 'CNY', 'ACTIVE')`); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.ExecContext(ctx, `INSERT INTO inventory
		(book_id, available_stock, reserved_stock, version) VALUES (1, 10, 0, 1)`); err != nil {
		t.Fatal(err)
	}

	firstDB := openOrderIntegrationDB(t, integrationDSN, 1)
	secondDB := openOrderIntegrationDB(t, integrationDSN, 1)
	t.Cleanup(func() {
		_ = secondDB.Close()
		_ = firstDB.Close()
	})
	if err := firstDB.PingContext(ctx); err != nil {
		t.Fatalf("open first confirmation connection: %v", err)
	}
	if err := secondDB.PingContext(ctx); err != nil {
		t.Fatalf("open second confirmation connection: %v", err)
	}
	firstStore := NewSQLStore(sqlx.NewSqlConnFromDB(firstDB))
	secondStore := NewSQLStore(sqlx.NewSqlConnFromDB(secondDB))

	digest := sha256.Sum256([]byte("integration-confirmation-token"))
	if _, err := firstStore.Prepare(ctx, 42, "concurrent-conversation", []ItemInput{{BookID: 1, Quantity: 2}},
		"concurrent-prepare-key", digest[:], time.Now().UTC().Add(10*time.Minute)); err != nil {
		t.Fatalf("prepare confirmation: %v", err)
	}

	stores := []*SQLStore{firstStore, secondStore}
	results := make([]*ConfirmationResult, len(stores))
	errs := make([]error, len(stores))
	start := make(chan struct{})
	var workers sync.WaitGroup
	workers.Add(len(stores))
	for index, store := range stores {
		go func(index int, store *SQLStore) {
			defer workers.Done()
			<-start
			results[index], errs[index] = store.Decide(ctx, 42, "concurrent-conversation", digest[:], "APPROVE", time.Now().UTC())
		}(index, store)
	}
	close(start)
	workers.Wait()

	replayed := 0
	var orderID int64
	for index, result := range results {
		if errs[index] != nil {
			t.Fatalf("confirmation %d failed: %v", index+1, errs[index])
		}
		if result == nil || result.Order == nil || result.Order.OrderID == 0 {
			t.Fatalf("confirmation %d returned no order: %#v", index+1, result)
		}
		if orderID == 0 {
			orderID = result.Order.OrderID
		} else if result.Order.OrderID != orderID {
			t.Fatalf("concurrent confirmations returned different orders: %d and %d", orderID, result.Order.OrderID)
		}
		if result.Replayed {
			replayed++
		}
	}
	if replayed != 1 {
		t.Fatalf("replayed responses = %d; want exactly 1", replayed)
	}

	var orders, orderItems int
	var stock int64
	var status, decision string
	var persistedOrderID int64
	if err := admin.QueryRowContext(ctx, `SELECT COUNT(*) FROM customer_order
		WHERE user_id=42 AND idempotency_key LIKE 'order-confirmation:%'`).Scan(&orders); err != nil {
		t.Fatal(err)
	}
	if err := admin.QueryRowContext(ctx, `SELECT COUNT(*) FROM order_item WHERE order_id=?`, orderID).Scan(&orderItems); err != nil {
		t.Fatal(err)
	}
	if err := admin.QueryRowContext(ctx, `SELECT available_stock FROM inventory WHERE book_id=1`).Scan(&stock); err != nil {
		t.Fatal(err)
	}
	if err := admin.QueryRowContext(ctx, `SELECT status, decision, order_id FROM pending_action
		WHERE token_hash=?`, digest[:]).Scan(&status, &decision, &persistedOrderID); err != nil {
		t.Fatal(err)
	}
	if orders != 1 || orderItems != 1 || stock != 8 || status != "APPROVED" || decision != "APPROVE" || persistedOrderID != orderID {
		t.Fatalf("unexpected terminal state: orders=%d items=%d stock=%d status=%s decision=%s persisted_order=%d response_order=%d",
			orders, orderItems, stock, status, decision, persistedOrderID, orderID)
	}
}

func openOrderIntegrationDB(t *testing.T, dsn string, maxOpenConnections int) *sql.DB {
	t.Helper()
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(maxOpenConnections)
	db.SetMaxIdleConns(maxOpenConnections)
	return db
}

func resetOrderIntegrationSchema(ctx context.Context, db *sql.DB) error {
	if _, err := db.ExecContext(ctx, "SET FOREIGN_KEY_CHECKS = 0"); err != nil {
		return err
	}
	for _, table := range []string{
		"notification", "agent_task", "inventory_event_consumption",
		"memory_outbox", "user_memory",
		"order_item", "pending_action", "customer_order", "outbox_event",
		"email_verification_token", "user_account",
		"book_category", "book_author", "inventory", "category", "author",
		"stock_deduction", "book",
	} {
		if _, err := db.ExecContext(ctx, "DROP TABLE IF EXISTS `"+table+"`"); err != nil {
			return err
		}
	}
	_, err := db.ExecContext(ctx, "SET FOREIGN_KEY_CHECKS = 1")
	return err
}
