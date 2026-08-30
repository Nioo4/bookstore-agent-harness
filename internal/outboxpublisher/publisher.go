package outboxpublisher

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log"
	"math"
	"time"

	redis "github.com/redis/go-redis/v9"
)

const InventoryEventType = "inventory.stock_changed.v1"

type Config struct {
	OwnerID      string
	Stream       string
	PollInterval time.Duration
	BatchSize    int
	LeaseTTL     time.Duration
	RetryBase    time.Duration
	RetryMax     time.Duration
	AlertAfter   uint
	Retention    time.Duration
}

type Event struct {
	EventID      string
	EventType    string
	PayloadJSON  []byte
	AttemptCount uint
}

type Store struct {
	DB *sql.DB
}

type Stream interface {
	Add(ctx context.Context, stream string, values map[string]any) (string, error)
}

type RedisStream struct {
	Client *redis.Client
}

func (s RedisStream) Add(ctx context.Context, stream string, values map[string]any) (string, error) {
	return s.Client.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: values}).Result()
}

type Publisher struct {
	Store  *Store
	Stream Stream
	Config Config
	Logger *log.Logger
}

func (p *Publisher) Run(ctx context.Context) error {
	if err := p.validate(); err != nil {
		return err
	}
	ticker := time.NewTicker(p.Config.PollInterval)
	defer ticker.Stop()
	cleanupTicker := time.NewTicker(time.Hour)
	defer cleanupTicker.Stop()
	for {
		if err := p.PublishBatch(ctx); err != nil && !errors.Is(err, context.Canceled) {
			p.logger().Print("outbox publish batch failed")
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		case <-cleanupTicker.C:
			if _, err := p.Store.DeletePublishedBefore(ctx, time.Now().UTC().Add(-p.Config.Retention), 1000); err != nil {
				p.logger().Print("outbox retention cleanup failed")
			}
		}
	}
}

func (p *Publisher) PublishBatch(ctx context.Context) error {
	if err := p.validate(); err != nil {
		return err
	}
	events, err := p.Store.Claim(ctx, p.Config.OwnerID, p.Config.BatchSize, p.Config.LeaseTTL)
	if err != nil {
		return err
	}
	var firstErr error
	for _, event := range events {
		_, publishErr := p.Stream.Add(ctx, p.Config.Stream, map[string]any{
			"event_id":     event.EventID,
			"event_type":   event.EventType,
			"payload_json": string(event.PayloadJSON),
		})
		if publishErr == nil {
			publishErr = p.Store.MarkPublished(ctx, event.EventID, p.Config.OwnerID)
		}
		if publishErr == nil {
			continue
		}
		if markErr := p.Store.MarkPendingAfterFailure(ctx, event.EventID, p.Config.OwnerID,
			event.AttemptCount, p.Config.RetryBase, p.Config.RetryMax); markErr != nil {
			publishErr = fmt.Errorf("publish failed and lease release failed: %w", markErr)
		}
		if event.AttemptCount >= p.Config.AlertAfter {
			p.logger().Printf("ALERT inventory outbox event %s failed after %d attempts", event.EventID, event.AttemptCount)
		}
		if firstErr == nil {
			firstErr = publishErr
		}
	}
	return firstErr
}

func (p *Publisher) ReplayInventory(ctx context.Context, from, to time.Time, dryRun bool) (int, error) {
	if err := validateReplayWindow(from, to); err != nil {
		return 0, err
	}
	events, err := p.Store.PublishedInventoryEvents(ctx, from, to)
	if err != nil {
		return 0, err
	}
	if dryRun {
		return len(events), nil
	}
	for _, event := range events {
		if _, err := p.Stream.Add(ctx, p.Config.Stream, map[string]any{
			"event_id":     event.EventID,
			"event_type":   event.EventType,
			"payload_json": string(event.PayloadJSON),
		}); err != nil {
			return 0, err
		}
	}
	return len(events), nil
}

func validateReplayWindow(from, to time.Time) error {
	_, fromOffset := from.Zone()
	_, toOffset := to.Zone()
	if fromOffset != 0 || toOffset != 0 {
		return errors.New("replay times must use UTC")
	}
	if !from.Before(to) {
		return errors.New("replay from must be before to")
	}
	if to.Sub(from) > 30*24*time.Hour {
		return errors.New("replay window must not exceed 30 days")
	}
	return nil
}

func (p *Publisher) validate() error {
	switch {
	case p.Store == nil || p.Store.DB == nil:
		return errors.New("outbox store is required")
	case p.Stream == nil:
		return errors.New("inventory stream is required")
	case p.Config.OwnerID == "":
		return errors.New("publisher owner id is required")
	case p.Config.Stream == "":
		return errors.New("inventory stream name is required")
	case p.Config.PollInterval <= 0, p.Config.LeaseTTL <= 0, p.Config.RetryBase <= 0, p.Config.RetryMax <= 0,
		p.Config.Retention <= 0:
		return errors.New("publisher durations must be positive")
	case p.Config.BatchSize < 1 || p.Config.BatchSize > 50:
		return errors.New("publisher batch size must be between 1 and 50")
	case p.Config.AlertAfter == 0:
		return errors.New("publisher alert threshold must be positive")
	}
	return nil
}

func (p *Publisher) logger() *log.Logger {
	if p.Logger != nil {
		return p.Logger
	}
	return log.Default()
}

func (s *Store) Claim(ctx context.Context, owner string, limit int, leaseTTL time.Duration) ([]Event, error) {
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()
	rows, err := tx.QueryContext(ctx, `select event_id, event_type, payload_json, attempt_count
		from outbox_event
		where event_type = ? and (
			(status = 'PENDING' and available_at <= CURRENT_TIMESTAMP(3))
			or (status = 'PROCESSING' and lease_expires_at <= CURRENT_TIMESTAMP(3))
		)
		order by available_at, created_at, event_id
		limit ? for update skip locked`, InventoryEventType, limit)
	if err != nil {
		return nil, err
	}
	events := make([]Event, 0, limit)
	for rows.Next() {
		var event Event
		if err := rows.Scan(&event.EventID, &event.EventType, &event.PayloadJSON, &event.AttemptCount); err != nil {
			_ = rows.Close()
			return nil, err
		}
		event.AttemptCount++
		events = append(events, event)
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	leaseMicros := leaseTTL.Microseconds()
	for _, event := range events {
		result, err := tx.ExecContext(ctx, `update outbox_event
			set status = 'PROCESSING', lease_owner = ?,
				lease_expires_at = TIMESTAMPADD(MICROSECOND, ?, CURRENT_TIMESTAMP(3)),
				attempt_count = attempt_count + 1, last_error = NULL
			where event_id = ? and event_type = ? and (
				(status = 'PENDING' and available_at <= CURRENT_TIMESTAMP(3))
				or (status = 'PROCESSING' and lease_expires_at <= CURRENT_TIMESTAMP(3))
			)`, owner, leaseMicros, event.EventID, InventoryEventType)
		if err != nil {
			return nil, err
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return nil, err
		}
		if affected != 1 {
			return nil, fmt.Errorf("outbox event %s lost its claim", event.EventID)
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return events, nil
}

func (s *Store) MarkPublished(ctx context.Context, eventID, owner string) error {
	result, err := s.DB.ExecContext(ctx, `update outbox_event
		set status = 'PUBLISHED', published_at = CURRENT_TIMESTAMP(3),
			lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
		where event_id = ? and status = 'PROCESSING' and lease_owner = ?`, eventID, owner)
	return requireOneRow(result, err, eventID)
}

func (s *Store) MarkPendingAfterFailure(
	ctx context.Context,
	eventID, owner string,
	attempt uint,
	retryBase, retryMax time.Duration,
) error {
	delay := retryDelay(attempt, retryBase, retryMax)
	result, err := s.DB.ExecContext(ctx, `update outbox_event
		set status = 'PENDING', available_at = TIMESTAMPADD(MICROSECOND, ?, CURRENT_TIMESTAMP(3)),
			lease_owner = NULL, lease_expires_at = NULL, published_at = NULL,
			last_error = 'redis publish failed'
		where event_id = ? and status = 'PROCESSING' and lease_owner = ?`, delay.Microseconds(), eventID, owner)
	return requireOneRow(result, err, eventID)
}

func (s *Store) PublishedInventoryEvents(ctx context.Context, from, to time.Time) ([]Event, error) {
	rows, err := s.DB.QueryContext(ctx, `select event_id, event_type, payload_json, attempt_count
		from outbox_event
		where event_type = ? and status = 'PUBLISHED'
			and created_at >= ? and created_at < ?
		order by created_at, event_id`, InventoryEventType, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []Event
	for rows.Next() {
		var event Event
		if err := rows.Scan(&event.EventID, &event.EventType, &event.PayloadJSON, &event.AttemptCount); err != nil {
			return nil, err
		}
		events = append(events, event)
	}
	return events, rows.Err()
}

func (s *Store) DeletePublishedBefore(ctx context.Context, cutoff time.Time, limit int) (int64, error) {
	if limit < 1 || limit > 1000 {
		return 0, errors.New("outbox cleanup limit must be between 1 and 1000")
	}
	result, err := s.DB.ExecContext(ctx, `delete from outbox_event
		where status = 'PUBLISHED' and published_at < ?
		order by published_at,event_id limit ?`, cutoff, limit)
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}

func retryDelay(attempt uint, base, maximum time.Duration) time.Duration {
	if attempt == 0 {
		attempt = 1
	}
	exponent := attempt - 1
	if exponent > 62 {
		exponent = 62
	}
	multiplier := math.Pow(2, float64(exponent))
	if multiplier >= float64(maximum)/float64(base) {
		return maximum
	}
	delay := time.Duration(float64(base) * multiplier)
	if delay > maximum {
		return maximum
	}
	return delay
}

func requireOneRow(result sql.Result, err error, eventID string) error {
	if err != nil {
		return err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if affected != 1 {
		return fmt.Errorf("outbox event %s lease is no longer owned", eventID)
	}
	return nil
}
