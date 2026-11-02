package ordering

import (
	"encoding/json"
	"errors"
	"regexp"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

func newOrderStoreTest(t *testing.T) (*SQLStore, sqlmock.Sqlmock) {
	t.Helper()
	db, mock, err := sqlmock.New()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return NewSQLStore(sqlx.NewSqlConnFromDB(db)), mock
}

func TestSQLStorePrepareUsesRealtimeCatalogAndPersistsBoundPreview(t *testing.T) {
	store, mock := newOrderStoreTest(t)
	expires := time.Date(2026, 8, 25, 12, 10, 0, 0, time.UTC)
	mock.ExpectBegin()
	mock.ExpectQuery(`(?s)select b\.book_id.*from book b join inventory.*where b\.book_id = \? limit 1`).WithArgs(int64(7)).
		WillReturnRows(sqlmock.NewRows([]string{"book_id", "isbn", "title", "price", "currency", "status", "available_stock", "version"}).
			AddRow(7, "isbn-7", "Systems", 2500, "CNY", "ACTIVE", 5, 3))
	mock.ExpectExec(`(?s)insert into pending_action`).
		WithArgs(sqlmock.AnyArg(), sqlmock.AnyArg(), int64(11), "conv", sqlmock.AnyArg(), sqlmock.AnyArg(), int64(5000), "CNY", "idem", expires).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	preview, err := store.Prepare(t.Context(), 11, "conv", []ItemInput{{BookID: 7, Quantity: 2}}, "idem", make([]byte, 32), expires)
	if err != nil {
		t.Fatal(err)
	}
	if preview.TotalAmount != 5000 || preview.Currency != "CNY" || len(preview.Items) != 1 || preview.Items[0].LineTotal != 5000 {
		t.Fatalf("preview=%+v", preview)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func pendingFixture(t *testing.T, userID int64, conversationID, status string, decision, orderID, consumed any, expiresAt time.Time) *sqlmock.Rows {
	t.Helper()
	items := []Item{{BookID: 7, ISBN: "isbn-7", Title: "Systems", UnitPrice: 2500, Quantity: 2, LineTotal: 5000}}
	itemsJSON, err := json.Marshal(items)
	if err != nil {
		t.Fatal(err)
	}
	hash, err := pendingPayloadHash(items, 5000, "CNY")
	if err != nil {
		t.Fatal(err)
	}
	return sqlmock.NewRows([]string{"pending_action_id", "user_id", "conversation_id", "action_type", "payload_hash", "items_json", "quoted_amount", "currency", "status", "decision", "order_id", "expires_at", "consumed_at"}).
		AddRow("action-1", userID, conversationID, "CREATE_ORDER", hash, itemsJSON, 5000, "CNY", status, decision, orderID, expiresAt, consumed)
}

func expectPendingQuery(mock sqlmock.Sqlmock, digest []byte, rows *sqlmock.Rows) {
	mock.ExpectQuery(regexp.QuoteMeta(`select pending_action_id, user_id, conversation_id, action_type,
			payload_hash, items_json, quoted_amount, currency, status, decision, order_id, expires_at, consumed_at
			from pending_action where token_hash = ? for update`)).WithArgs(digest).WillReturnRows(rows)
}

func expectOutbox(mock sqlmock.Sqlmock, eventType, aggregateType, aggregateID string) {
	mock.ExpectExec(`(?s)insert into outbox_event.*values \(\?, \?, \?, \?, \?, 'PENDING'\)`).
		WithArgs(sqlmock.AnyArg(), eventType, aggregateType, aggregateID, sqlmock.AnyArg()).
		WillReturnResult(sqlmock.NewResult(0, 1))
}

func TestSQLStoreApproveAtomicallyWritesOrderInventoryOutboxAndDecision(t *testing.T) {
	store, mock := newOrderStoreTest(t)
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	digest := make([]byte, 32)
	mock.ExpectBegin()
	expectPendingQuery(mock, digest, pendingFixture(t, 11, "conv", "PENDING", nil, nil, nil, now.Add(10*time.Minute)))
	mock.ExpectQuery(`(?s)select b\.book_id.*where b\.book_id = \? for update`).WithArgs(int64(7)).
		WillReturnRows(sqlmock.NewRows([]string{"book_id", "isbn", "title", "price", "currency", "status", "available_stock", "version"}).AddRow(7, "isbn-7", "Systems", 2500, "CNY", "ACTIVE", 5, 3))
	mock.ExpectExec(`(?s)insert into customer_order.*values \(\?, \?, 'PLACED', \?, \?, \?\)`).
		WithArgs(int64(11), "conv", int64(5000), "CNY", "order-confirmation:action-1").WillReturnResult(sqlmock.NewResult(91, 1))
	mock.ExpectExec(`(?s)insert into order_item`).WithArgs(int64(91), int64(7), "isbn-7", "Systems", int64(2500), int64(2), int64(5000)).WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectExec(regexp.QuoteMeta(`update inventory
				set available_stock = available_stock - ?, version = version + 1
				where book_id = ? and available_stock >= ?`)).WithArgs(int64(2), int64(7), int64(2)).WillReturnResult(sqlmock.NewResult(0, 1))
	expectOutbox(mock, "inventory.stock_changed.v1", "INVENTORY", "7")
	expectOutbox(mock, "order.placed.v1", "order", "91")
	mock.ExpectExec(`(?s)update pending_action.*status = 'APPROVED'.*decision = 'APPROVE'.*order_id = \?.*consumed_at = \?`).
		WithArgs(int64(91), now, "action-1").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	result, err := store.Decide(t.Context(), 11, "conv", digest, "APPROVE", now)
	if err != nil || result.Order == nil || result.Order.OrderID != 91 || result.Status != "COMPLETED" || result.Replayed {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestSQLStoreApproveReplaysTheCommittedOrder(t *testing.T) {
	store, mock := newOrderStoreTest(t)
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	digest := make([]byte, 32)
	mock.ExpectBegin()
	expectPendingQuery(mock, digest, pendingFixture(t, 11, "conv", "APPROVED", "APPROVE", int64(91), now, now.Add(time.Minute)))
	mock.ExpectQuery(`(?s)select order_id, status, total_amount, currency, created_at.*order_id = \? and user_id = \?`).WithArgs(int64(91), int64(11)).
		WillReturnRows(sqlmock.NewRows([]string{"order_id", "status", "total_amount", "currency", "created_at"}).AddRow(91, "PLACED", 5000, "CNY", now))
	mock.ExpectQuery(`(?s)select book_id, isbn_snapshot.*where order_id = \?`).WithArgs(int64(91)).
		WillReturnRows(sqlmock.NewRows([]string{"book_id", "isbn_snapshot", "title_snapshot", "unit_price", "quantity", "line_total"}).AddRow(7, "isbn-7", "Systems", 2500, 2, 5000))
	mock.ExpectCommit()
	result, err := store.Decide(t.Context(), 11, "conv", digest, "APPROVE", now)
	if err != nil || !result.Replayed || result.Order == nil || result.Order.OrderID != 91 {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestSQLStoreRejectHasNoOrderOrInventorySideEffects(t *testing.T) {
	store, mock := newOrderStoreTest(t)
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	digest := make([]byte, 32)
	mock.ExpectBegin()
	expectPendingQuery(mock, digest, pendingFixture(t, 11, "conv", "PENDING", nil, nil, nil, now.Add(time.Minute)))
	mock.ExpectExec(`(?s)update pending_action set status = 'REJECTED'`).WithArgs(now, "action-1").WillReturnResult(sqlmock.NewResult(0, 1))
	expectOutbox(mock, "order.confirmation_rejected.v1", "pending_action", "action-1")
	mock.ExpectCommit()
	result, err := store.Decide(t.Context(), 11, "conv", digest, "REJECT", now)
	if err != nil || result.Order != nil || result.Status != "CANCELLED" || result.Replayed {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestSQLStoreSameDecisionReplaysAndOppositeDecisionConflicts(t *testing.T) {
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name, decision string
		want           error
	}{{"replay", "REJECT", nil}, {"conflict", "APPROVE", ErrConfirmationAlreadyDecided}} {
		t.Run(tc.name, func(t *testing.T) {
			store, mock := newOrderStoreTest(t)
			digest := make([]byte, 32)
			mock.ExpectBegin()
			expectPendingQuery(mock, digest, pendingFixture(t, 11, "conv", "REJECTED", "REJECT", nil, now, now.Add(time.Minute)))
			if tc.want == nil {
				mock.ExpectCommit()
			} else {
				mock.ExpectRollback()
			}
			result, err := store.Decide(t.Context(), 11, "conv", digest, tc.decision, now)
			if !errors.Is(err, tc.want) || (tc.want == nil && (result == nil || !result.Replayed)) {
				t.Fatalf("result=%+v err=%v", result, err)
			}
			if err := mock.ExpectationsWereMet(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestSQLStoreStalePriceBecomesTerminalWithoutOrder(t *testing.T) {
	store, mock := newOrderStoreTest(t)
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	digest := make([]byte, 32)
	mock.ExpectBegin()
	expectPendingQuery(mock, digest, pendingFixture(t, 11, "conv", "PENDING", nil, nil, nil, now.Add(time.Minute)))
	mock.ExpectQuery(`(?s)select b\.book_id.*where b\.book_id = \? for update`).WithArgs(int64(7)).
		WillReturnRows(sqlmock.NewRows([]string{"book_id", "isbn", "title", "price", "currency", "status", "available_stock", "version"}).AddRow(7, "isbn-7", "Systems", 2600, "CNY", "ACTIVE", 5, 3))
	mock.ExpectExec(`(?s)update pending_action set status = 'EXPIRED'`).WithArgs(now, "action-1").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	_, err := store.Decide(t.Context(), 11, "conv", digest, "APPROVE", now)
	if !errors.Is(err, ErrPriceChanged) {
		t.Fatalf("error=%v", err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestSQLStoreExpiryInactiveAndInsufficientBecomeTerminalWithoutOrder(t *testing.T) {
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name       string
		expiresAt  time.Time
		bookStatus string
		stock      int64
		queryBook  bool
		want       error
	}{
		{"expired", now.Add(-time.Second), "ACTIVE", 5, false, ErrConfirmationExpired},
		{"exact expiration", now, "ACTIVE", 5, false, ErrConfirmationExpired},
		{"inactive", now.Add(time.Minute), "INACTIVE", 5, true, ErrBookInactive},
		{"insufficient", now.Add(time.Minute), "ACTIVE", 1, true, ErrInventoryInsufficient},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, mock := newOrderStoreTest(t)
			digest := make([]byte, 32)
			mock.ExpectBegin()
			expectPendingQuery(mock, digest, pendingFixture(t, 11, "conv", "PENDING", nil, nil, nil, tc.expiresAt))
			if tc.queryBook {
				mock.ExpectQuery(`(?s)select b\.book_id.*where b\.book_id = \? for update`).WithArgs(int64(7)).
					WillReturnRows(sqlmock.NewRows([]string{"book_id", "isbn", "title", "price", "currency", "status", "available_stock", "version"}).
						AddRow(7, "isbn-7", "Systems", 2500, "CNY", tc.bookStatus, tc.stock, 3))
			}
			mock.ExpectExec(`(?s)update pending_action set status = 'EXPIRED'`).WithArgs(now, "action-1").WillReturnResult(sqlmock.NewResult(0, 1))
			mock.ExpectCommit()
			_, err := store.Decide(t.Context(), 11, "conv", digest, "APPROVE", now)
			if !errors.Is(err, tc.want) {
				t.Fatalf("error=%v want=%v", err, tc.want)
			}
			if err := mock.ExpectationsWereMet(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestSQLStoreHidesWrongUserOrConversation(t *testing.T) {
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		user         int64
		conversation string
	}{{11, "other"}, {12, "conv"}} {
		store, mock := newOrderStoreTest(t)
		digest := make([]byte, 32)
		mock.ExpectBegin()
		expectPendingQuery(mock, digest, pendingFixture(t, tc.user, tc.conversation, "PENDING", nil, nil, nil, now.Add(time.Minute)))
		mock.ExpectRollback()
		_, err := store.Decide(t.Context(), 11, "conv", digest, "APPROVE", now)
		if !errors.Is(err, ErrConfirmationNotFound) {
			t.Fatalf("user=%d conversation=%s error=%v", tc.user, tc.conversation, err)
		}
		if err := mock.ExpectationsWereMet(); err != nil {
			t.Fatal(err)
		}
	}
}
