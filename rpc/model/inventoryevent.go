package model

import (
	"context"
	"encoding/json"
	"time"

	"github.com/google/uuid"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

const inventoryStockChangedEventType = "inventory.stock_changed.v1"

type inventorySnapshot struct {
	AvailableStock   int64 `db:"available_stock"`
	InventoryVersion int64 `db:"version"`
}

type inventoryStockChangedPayload struct {
	EventID          string `json:"event_id"`
	EventType        string `json:"event_type"`
	OccurredAt       string `json:"occurred_at"`
	BookID           int64  `json:"book_id"`
	AvailableStock   int64  `json:"available_stock"`
	InventoryVersion int64  `json:"inventory_version"`
}

func loadInventorySnapshot(ctx context.Context, session sqlx.Session, bookID int64) (inventorySnapshot, error) {
	var snapshot inventorySnapshot
	err := session.QueryRowCtx(ctx, &snapshot,
		"select available_stock, version from inventory where book_id = ?", bookID)
	return snapshot, err
}

func insertInventoryStockChangedEvent(
	ctx context.Context,
	session sqlx.Session,
	bookID int64,
	snapshot inventorySnapshot,
) error {
	eventID := uuid.NewString()
	payload, err := json.Marshal(inventoryStockChangedPayload{
		EventID:          eventID,
		EventType:        inventoryStockChangedEventType,
		OccurredAt:       time.Now().UTC().Format(time.RFC3339Nano),
		BookID:           bookID,
		AvailableStock:   snapshot.AvailableStock,
		InventoryVersion: snapshot.InventoryVersion,
	})
	if err != nil {
		return err
	}
	_, err = session.ExecCtx(ctx, `insert into outbox_event
		(event_id, event_type, aggregate_type, aggregate_id, payload_json, status, available_at)
		values (?, ?, 'INVENTORY', ?, ?, 'PENDING', CURRENT_TIMESTAMP(3))`,
		eventID, inventoryStockChangedEventType, bookID, payload)
	return err
}
