package ordering

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/google/uuid"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

type Store interface {
	Prepare(ctx context.Context, userID int64, conversationID string, items []ItemInput, idempotencyKey string, digest []byte, expiresAt time.Time) (*Preview, error)
	Decide(ctx context.Context, userID int64, conversationID string, digest []byte, decision string, now time.Time) (*ConfirmationResult, error)
	Get(ctx context.Context, userID, orderID int64) (*Order, error)
	List(ctx context.Context, userID int64, beforeOrderID int64, limit int) ([]Order, error)
}

type SQLStore struct{ conn sqlx.SqlConn }

func NewSQLStore(conn sqlx.SqlConn) *SQLStore { return &SQLStore{conn: conn} }

type catalogRow struct {
	BookID         int64  `db:"book_id"`
	ISBN           string `db:"isbn"`
	Title          string `db:"title"`
	Price          int64  `db:"price"`
	Currency       string `db:"currency"`
	Status         string `db:"status"`
	AvailableStock int64  `db:"available_stock"`
	Version        uint64 `db:"version"`
}

func (s *SQLStore) Prepare(ctx context.Context, userID int64, conversationID string, inputs []ItemInput, idempotencyKey string, digest []byte, expiresAt time.Time) (*Preview, error) {
	preview := &Preview{Items: make([]Item, 0, len(inputs)), ExpiresAt: expiresAt}
	err := s.conn.TransactCtx(ctx, func(ctx context.Context, session sqlx.Session) error {
		for _, input := range inputs {
			var book catalogRow
			err := session.QueryRowCtx(ctx, &book, `select b.book_id, b.isbn, b.title, b.price, b.currency,
				b.status, i.available_stock, i.version
				from book b join inventory i on i.book_id = b.book_id where b.book_id = ? limit 1`, input.BookID)
			if errors.Is(err, sqlx.ErrNotFound) {
				return ErrBookNotFound
			}
			if err != nil {
				return err
			}
			if book.Status != "ACTIVE" {
				return ErrBookInactive
			}
			if book.AvailableStock < input.Quantity {
				return ErrInventoryInsufficient
			}
			if preview.Currency == "" {
				preview.Currency = book.Currency
			} else if preview.Currency != book.Currency {
				return fmt.Errorf("%w: mixed order currencies are not supported", ErrInvalidOrderRequest)
			}
			lineTotal, err := checkedMultiply(book.Price, input.Quantity)
			if err != nil {
				return err
			}
			preview.TotalAmount, err = checkedAdd(preview.TotalAmount, lineTotal)
			if err != nil {
				return err
			}
			preview.Items = append(preview.Items, Item{BookID: book.BookID, ISBN: book.ISBN, Title: book.Title,
				UnitPrice: book.Price, Quantity: input.Quantity, LineTotal: lineTotal})
		}
		itemsJSON, err := json.Marshal(preview.Items)
		if err != nil {
			return err
		}
		payloadHash, err := pendingPayloadHash(preview.Items, preview.TotalAmount, preview.Currency)
		if err != nil {
			return err
		}
		actionID := uuid.NewString()
		_, err = session.ExecCtx(ctx, `insert into pending_action
			(pending_action_id, token_hash, user_id, conversation_id, action_type, payload_hash, items_json,
			 quoted_amount, currency, status, prepare_idempotency_key, expires_at)
			values (?, ?, ?, ?, 'CREATE_ORDER', ?, ?, ?, ?, 'PENDING', ?, ?)`, actionID, digest, userID, conversationID,
			payloadHash, itemsJSON, preview.TotalAmount, preview.Currency, idempotencyKey, expiresAt)
		if err != nil {
			var mysqlErr *mysql.MySQLError
			if errors.As(err, &mysqlErr) && mysqlErr.Number == 1062 {
				return ErrIdempotencyConflict
			}
		}
		return err
	})
	return preview, err
}

type pendingAction struct {
	PendingActionID string         `db:"pending_action_id"`
	UserID          int64          `db:"user_id"`
	ConversationID  string         `db:"conversation_id"`
	ActionType      string         `db:"action_type"`
	PayloadHash     []byte         `db:"payload_hash"`
	ItemsJSON       []byte         `db:"items_json"`
	QuotedAmount    int64          `db:"quoted_amount"`
	Currency        string         `db:"currency"`
	Status          string         `db:"status"`
	Decision        sql.NullString `db:"decision"`
	OrderID         sql.NullInt64  `db:"order_id"`
	ExpiresAt       time.Time      `db:"expires_at"`
	ConsumedAt      sql.NullTime   `db:"consumed_at"`
}

func (s *SQLStore) Decide(ctx context.Context, userID int64, conversationID string, digest []byte, decision string, now time.Time) (*ConfirmationResult, error) {
	var decided *ConfirmationResult
	var terminalErr error
	err := s.conn.TransactCtx(ctx, func(ctx context.Context, session sqlx.Session) error {
		var pending pendingAction
		err := session.QueryRowCtx(ctx, &pending, `select pending_action_id, user_id, conversation_id, action_type,
			payload_hash, items_json, quoted_amount, currency, status, decision, order_id, expires_at, consumed_at
			from pending_action where token_hash = ? for update`, digest)
		if errors.Is(err, sqlx.ErrNotFound) {
			return ErrConfirmationNotFound
		}
		if err != nil {
			return err
		}
		if pending.UserID != userID || pending.ConversationID != conversationID {
			return ErrConfirmationNotFound
		}
		if pending.ActionType != "CREATE_ORDER" {
			return ErrInvalidOrderRequest
		}
		if pending.Status == "APPROVED" || pending.Status == "REJECTED" {
			if !pending.Decision.Valid || pending.Decision.String != decision {
				return ErrConfirmationAlreadyDecided
			}
			decided = &ConfirmationResult{ConfirmationID: pending.PendingActionID, Decision: decision,
				Status: confirmationStatus(decision), Replayed: true}
			if decision == "APPROVE" {
				if !pending.OrderID.Valid {
					return ErrConfirmationAlreadyDecided
				}
				order, err := loadOrderWithSession(ctx, session, userID, pending.OrderID.Int64, "")
				if err != nil {
					return err
				}
				order.Replayed = true
				decided.Order = order
			}
			return nil
		}
		if pending.Status == "EXPIRED" {
			return ErrConfirmationExpired
		}
		if !pending.ExpiresAt.After(now) {
			if err := expirePending(ctx, session, pending.PendingActionID, now); err != nil {
				return err
			}
			terminalErr = ErrConfirmationExpired
			return nil
		}
		if decision == "REJECT" {
			result, err := session.ExecCtx(ctx, `update pending_action set status = 'REJECTED', decision = 'REJECT', consumed_at = ?
				where pending_action_id = ? and status = 'PENDING'`, now, pending.PendingActionID)
			if err != nil {
				return err
			}
			affected, err := result.RowsAffected()
			if err != nil || affected != 1 {
				return ErrConfirmationAlreadyDecided
			}
			payload, _ := json.Marshal(map[string]any{"confirmation_id": pending.PendingActionID,
				"conversation_id": pending.ConversationID, "decision": "REJECT", "occurred_at": now})
			if err := insertOutbox(ctx, session, "order.confirmation_rejected.v1", "pending_action", pending.PendingActionID, payload); err != nil {
				return err
			}
			decided = &ConfirmationResult{ConfirmationID: pending.PendingActionID, Decision: decision, Status: "CANCELLED"}
			return nil
		}
		var previewItems []Item
		if err := json.Unmarshal(pending.ItemsJSON, &previewItems); err != nil {
			return fmt.Errorf("decode pending order items: %w", err)
		}
		hash, err := pendingPayloadHash(previewItems, pending.QuotedAmount, pending.Currency)
		if err != nil {
			return err
		}
		if !equalBytes(hash, pending.PayloadHash) {
			terminalErr = ErrIdempotencyConflict
			return expirePending(ctx, session, pending.PendingActionID, now)
		}
		sort.Slice(previewItems, func(i, j int) bool { return previewItems[i].BookID < previewItems[j].BookID })

		currentRows := make(map[int64]catalogRow, len(previewItems))
		for _, item := range previewItems {
			var current catalogRow
			err := session.QueryRowCtx(ctx, &current, `select b.book_id, b.isbn, b.title, b.price, b.currency,
				b.status, i.available_stock, i.version
				from book b join inventory i on i.book_id = b.book_id
				where b.book_id = ? for update`, item.BookID)
			if errors.Is(err, sqlx.ErrNotFound) {
				terminalErr = ErrBookInactive
				break
			}
			if err != nil {
				return err
			}
			if current.Status != "ACTIVE" {
				terminalErr = ErrBookInactive
				break
			}
			if current.Price != item.UnitPrice || current.Currency != pending.Currency {
				terminalErr = ErrPriceChanged
				break
			}
			if current.AvailableStock < item.Quantity {
				terminalErr = ErrInventoryInsufficient
				break
			}
			currentRows[item.BookID] = current
		}
		if terminalErr != nil {
			return expirePending(ctx, session, pending.PendingActionID, now)
		}

		idempotencyKey := "order-confirmation:" + pending.PendingActionID
		result, err := session.ExecCtx(ctx, `insert into customer_order
			(user_id, conversation_id, status, total_amount, currency, idempotency_key)
			values (?, ?, 'PLACED', ?, ?, ?)`, userID, pending.ConversationID,
			pending.QuotedAmount, pending.Currency, idempotencyKey)
		if err != nil {
			return err
		}
		orderID, err := result.LastInsertId()
		if err != nil {
			return err
		}
		for _, item := range previewItems {
			if _, err := session.ExecCtx(ctx, `insert into order_item
				(order_id, book_id, isbn_snapshot, title_snapshot, unit_price, quantity, line_total)
				values (?, ?, ?, ?, ?, ?, ?)`, orderID, item.BookID, item.ISBN, item.Title,
				item.UnitPrice, item.Quantity, item.LineTotal); err != nil {
				return err
			}
			result, err := session.ExecCtx(ctx, `update inventory
				set available_stock = available_stock - ?, version = version + 1
				where book_id = ? and available_stock >= ?`, item.Quantity, item.BookID, item.Quantity)
			if err != nil {
				return err
			}
			affected, err := result.RowsAffected()
			if err != nil || affected != 1 {
				return ErrInventoryInsufficient
			}
			current := currentRows[item.BookID]
			payload, _ := json.Marshal(map[string]any{
				"event_id": "", "event_type": "inventory.stock_changed.v1", "occurred_at": now,
				"book_id": item.BookID, "available_stock": current.AvailableStock - item.Quantity,
				"inventory_version": current.Version + 1,
			})
			if err := insertOutbox(ctx, session, "inventory.stock_changed.v1", "INVENTORY", fmt.Sprint(item.BookID), payload); err != nil {
				return err
			}
		}
		orderPayload, _ := json.Marshal(map[string]any{"order_id": orderID, "user_id": userID, "status": "PLACED", "occurred_at": now})
		if err := insertOutbox(ctx, session, "order.placed.v1", "order", fmt.Sprint(orderID), orderPayload); err != nil {
			return err
		}
		result, err = session.ExecCtx(ctx, `update pending_action
			set status = 'APPROVED', decision = 'APPROVE', order_id = ?, consumed_at = ?
			where pending_action_id = ? and status = 'PENDING'`, orderID, now, pending.PendingActionID)
		if err != nil {
			return err
		}
		affected, err := result.RowsAffected()
		if err != nil || affected != 1 {
			return ErrConfirmationAlreadyUsed
		}
		order := &Order{OrderID: orderID, Status: "PLACED", Items: previewItems,
			TotalAmount: pending.QuotedAmount, Currency: pending.Currency, CreatedAt: now}
		decided = &ConfirmationResult{ConfirmationID: pending.PendingActionID, Decision: decision,
			Status: "COMPLETED", Order: order}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if terminalErr != nil {
		return nil, terminalErr
	}
	return decided, nil
}

func expirePending(ctx context.Context, session sqlx.Session, pendingActionID string, now time.Time) error {
	_, err := session.ExecCtx(ctx, `update pending_action set status = 'EXPIRED', consumed_at = ?
		where pending_action_id = ? and status = 'PENDING'`, now, pendingActionID)
	return err
}

func confirmationStatus(decision string) string {
	if decision == "APPROVE" {
		return "COMPLETED"
	}
	return "CANCELLED"
}

func pendingPayloadHash(items []Item, quotedAmount int64, currency string) ([]byte, error) {
	canonical, err := json.Marshal(struct {
		ActionType   string `json:"action_type"`
		Items        []Item `json:"items"`
		QuotedAmount int64  `json:"quoted_amount"`
		Currency     string `json:"currency"`
	}{ActionType: "CREATE_ORDER", Items: items, QuotedAmount: quotedAmount, Currency: currency})
	if err != nil {
		return nil, err
	}
	hash := sha256.Sum256(canonical)
	return hash[:], nil
}

func insertOutbox(ctx context.Context, session sqlx.Session, eventType, aggregateType, aggregateID string, payload []byte) error {
	eventID := uuid.NewString()
	var body map[string]any
	if json.Unmarshal(payload, &body) == nil {
		body["event_id"] = eventID
		payload, _ = json.Marshal(body)
	}
	_, err := session.ExecCtx(ctx, `insert into outbox_event
		(event_id, event_type, aggregate_type, aggregate_id, payload_json, status)
		values (?, ?, ?, ?, ?, 'PENDING')`, eventID, eventType, aggregateType, aggregateID, payload)
	return err
}

func (s *SQLStore) Get(ctx context.Context, userID, orderID int64) (*Order, error) {
	return loadOrder(ctx, s.conn, userID, orderID, "")
}

func (s *SQLStore) List(ctx context.Context, userID int64, beforeOrderID int64, limit int) ([]Order, error) {
	query := `select order_id, status, total_amount, currency, created_at
		from customer_order where user_id = ?`
	args := []any{userID}
	if beforeOrderID > 0 {
		query += " and order_id < ?"
		args = append(args, beforeOrderID)
	}
	query += " order by order_id desc limit ?"
	args = append(args, limit)
	var headers []struct {
		OrderID     int64     `db:"order_id"`
		Status      string    `db:"status"`
		TotalAmount int64     `db:"total_amount"`
		Currency    string    `db:"currency"`
		CreatedAt   time.Time `db:"created_at"`
	}
	if err := s.conn.QueryRowsCtx(ctx, &headers, query, args...); err != nil {
		return nil, err
	}
	orders := make([]Order, 0, len(headers))
	for _, header := range headers {
		order, err := s.Get(ctx, userID, header.OrderID)
		if err != nil {
			return nil, err
		}
		orders = append(orders, *order)
	}
	return orders, nil
}

type rowQuerier interface {
	QueryRowCtx(context.Context, any, string, ...any) error
	QueryRowsCtx(context.Context, any, string, ...any) error
}

func loadOrder(ctx context.Context, query rowQuerier, userID, orderID int64, idempotencyKey string) (*Order, error) {
	var header struct {
		OrderID     int64     `db:"order_id"`
		Status      string    `db:"status"`
		TotalAmount int64     `db:"total_amount"`
		Currency    string    `db:"currency"`
		CreatedAt   time.Time `db:"created_at"`
	}
	var err error
	if orderID > 0 {
		err = query.QueryRowCtx(ctx, &header, `select order_id, status, total_amount, currency, created_at
			from customer_order where order_id = ? and user_id = ? limit 1`, orderID, userID)
	} else {
		err = query.QueryRowCtx(ctx, &header, `select order_id, status, total_amount, currency, created_at
			from customer_order where user_id = ? and idempotency_key = ? limit 1`, userID, idempotencyKey)
	}
	if errors.Is(err, sqlx.ErrNotFound) {
		return nil, ErrOrderNotFound
	}
	if err != nil {
		return nil, err
	}
	order := &Order{OrderID: header.OrderID, Status: header.Status, TotalAmount: header.TotalAmount,
		Currency: header.Currency, CreatedAt: header.CreatedAt}
	if err := query.QueryRowsCtx(ctx, &order.Items, `select book_id, isbn_snapshot, title_snapshot,
		unit_price, quantity, line_total from order_item where order_id = ? order by order_item_id`, order.OrderID); err != nil {
		return nil, err
	}
	return order, nil
}

func loadOrderWithSession(ctx context.Context, session sqlx.Session, userID, orderID int64, idempotencyKey string) (*Order, error) {
	return loadOrder(ctx, session, userID, orderID, idempotencyKey)
}

func equalBytes(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	var diff byte
	for i := range a {
		diff |= a[i] ^ b[i]
	}
	return diff == 0
}
