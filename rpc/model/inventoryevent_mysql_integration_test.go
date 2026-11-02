package model

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"testing"

	"github.com/alicebob/miniredis/v2"
	_ "github.com/go-sql-driver/mysql"
	"github.com/zeromicro/go-zero/core/stores/cache"
	"github.com/zeromicro/go-zero/core/stores/redis"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

func TestInventoryMutationsWriteAtomicRealMySQLOutbox(t *testing.T) {
	dsn := os.Getenv("BOOKSTORE_MODEL_TEST_MYSQL_DSN")
	if dsn == "" {
		t.Skip("BOOKSTORE_MODEL_TEST_MYSQL_DSN is required")
	}
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	mini := miniredis.RunT(t)
	model := NewBookModel(sqlx.NewSqlConnFromDB(db), cache.CacheConf{{
		RedisConf: redis.RedisConf{Host: mini.Addr(), Type: redis.NodeType, NonBlock: true}, Weight: 100,
	}})
	isbn := "stage9-inventory-event-501"
	cleanupIntegrationBook(t, db, isbn)
	defer cleanupIntegrationBook(t, db, isbn)

	result, err := model.Insert(Book{ISBN: isbn, Title: "库存事件", Author: "Tester", Price: 5000, StockCount: 5})
	if err != nil {
		t.Fatal(err)
	}
	bookID, _ := result.LastInsertId()
	assertIntegrationEvent(t, db, bookID, 5, 1, 1)

	if err := model.Update(Book{BookID: bookID, ISBN: isbn, Title: "库存事件", Author: "Tester", Price: 5100, StockCount: 7}); err != nil {
		t.Fatal(err)
	}
	assertIntegrationEvent(t, db, bookID, 7, 2, 2)

	replayed, err := model.DeductStockIdempotent(context.Background(), bookID, 2, "stage9-real-deduction")
	if err != nil || replayed {
		t.Fatalf("deduction replayed=%v err=%v", replayed, err)
	}
	assertIntegrationEvent(t, db, bookID, 5, 3, 3)

	if _, err := model.DeductStockIdempotent(context.Background(), bookID, 99, "stage9-real-insufficient"); !errors.Is(err, ErrInsufficientStock) {
		t.Fatalf("insufficient deduction error = %v", err)
	}
	assertIntegrationEvent(t, db, bookID, 5, 3, 3)

	if _, err := db.Exec(`CREATE TRIGGER reject_stage9_inventory_outbox BEFORE INSERT ON outbox_event
		FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='injected outbox failure'`); err != nil {
		t.Fatal(err)
	}
	defer db.Exec("DROP TRIGGER IF EXISTS reject_stage9_inventory_outbox")
	err = model.Update(Book{BookID: bookID, ISBN: isbn, Title: "应回滚", Author: "Tester", Price: 9999, StockCount: 12})
	if err == nil {
		t.Fatal("update succeeded despite injected outbox failure")
	}
	var price, stock, version int64
	if err := db.QueryRow(`SELECT b.price,i.available_stock,i.version FROM book b
		JOIN inventory i ON i.book_id=b.book_id WHERE b.book_id=?`, bookID).Scan(&price, &stock, &version); err != nil {
		t.Fatal(err)
	}
	if price != 5100 || stock != 5 || version != 3 {
		t.Fatalf("rollback state price=%d stock=%d version=%d", price, stock, version)
	}
	assertIntegrationEvent(t, db, bookID, 5, 3, 3)
}

func assertIntegrationEvent(t *testing.T, db *sql.DB, bookID, stock, version, count int64) {
	t.Helper()
	var gotCount int64
	if err := db.QueryRow("SELECT COUNT(*) FROM outbox_event WHERE event_type=? AND aggregate_id=?", inventoryStockChangedEventType, bookID).
		Scan(&gotCount); err != nil {
		t.Fatal(err)
	}
	if gotCount != count {
		t.Fatalf("outbox count=%d; want %d", gotCount, count)
	}
	var eventID string
	var payloadBytes []byte
	if err := db.QueryRow(`SELECT event_id,payload_json FROM outbox_event
		WHERE event_type=? AND aggregate_id=? ORDER BY created_at DESC,event_id DESC LIMIT 1`, inventoryStockChangedEventType, bookID).
		Scan(&eventID, &payloadBytes); err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := json.Unmarshal(payloadBytes, &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload) != 6 || payload["event_id"] != eventID || payload["event_type"] != inventoryStockChangedEventType ||
		int64(payload["book_id"].(float64)) != bookID || int64(payload["available_stock"].(float64)) != stock ||
		int64(payload["inventory_version"].(float64)) != version {
		t.Fatalf("unexpected inventory event payload: %#v", payload)
	}
}

func cleanupIntegrationBook(t *testing.T, db *sql.DB, isbn string) {
	t.Helper()
	_, _ = db.Exec("DROP TRIGGER IF EXISTS reject_stage9_inventory_outbox")
	var bookID int64
	err := db.QueryRow("SELECT book_id FROM book WHERE isbn=?", isbn).Scan(&bookID)
	if err != nil {
		return
	}
	_, _ = db.Exec("DELETE FROM outbox_event WHERE aggregate_id=?", bookID)
	_, _ = db.Exec("DELETE FROM stock_deduction WHERE book_id=?", bookID)
	_, _ = db.Exec("DELETE FROM book_author WHERE book_id=?", bookID)
	_, _ = db.Exec("DELETE FROM inventory WHERE book_id=?", bookID)
	_, _ = db.Exec("DELETE FROM book WHERE book_id=?", bookID)
}
