package model

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/zeromicro/go-zero/core/logx"
	"github.com/zeromicro/go-zero/core/metric"
	"github.com/zeromicro/go-zero/core/stores/cache"
	"github.com/zeromicro/go-zero/core/stores/sqlc"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
	"github.com/zeromicro/go-zero/core/syncx"
)

var (
	bookRows = `b.book_id AS book_id, b.isbn AS isbn, b.title AS title,
		COALESCE(NULLIF(GROUP_CONCAT(DISTINCT a.name ORDER BY ba.sort_order, a.author_id SEPARATOR ', '), ''), b.author) AS author,
		b.price AS price, COALESCE(i.available_stock, b.stock_count) AS stock_count`

	cacheBookLookupPrefix = "cache#Book#lookup#"

	cacheFallbackRequests = metric.NewCounterVec(&metric.CounterVecOpts{
		Namespace: "bookstore",
		Subsystem: "check_cache_fallback",
		Name:      "requests_total",
		Help:      "Number of Redis cache fallback requests by result.",
		Labels:    []string{"result"},
	})
	cacheFallbackLoads = metric.NewCounterVec(&metric.CounterVecOpts{
		Namespace: "bookstore",
		Subsystem: "check_cache_fallback",
		Name:      "mysql_loads_total",
		Help:      "Number of actual MySQL fallback loads by result.",
		Labels:    []string{"result"},
	})
	cacheFallbackFlights = metric.NewCounterVec(&metric.CounterVecOpts{
		Namespace: "bookstore",
		Subsystem: "check_cache_fallback",
		Name:      "singleflight_total",
		Help:      "Number of fallback calls that led or shared a SingleFlight request.",
		Labels:    []string{"role"},
	})
	cacheFallbackInflight = metric.NewGaugeVec(&metric.GaugeVecOpts{
		Namespace: "bookstore",
		Subsystem: "check_cache_fallback",
		Name:      "inflight",
		Help:      "Current number of MySQL cache fallback loads.",
		Labels:    []string{},
	})
)

const (
	defaultCacheFallbackTimeout       = 250 * time.Millisecond
	defaultCacheFallbackMaxConcurrent = 32
)

type (
	BookModel interface {
		Insert(data Book) (sql.Result, error)
		FindOne(identifier string) (*Book, error)
		FindOneCtx(ctx context.Context, identifier string) (*Book, error)
		Update(data Book) error
		Delete(identifier string) error
		DeductStock(bookID, quantity int64) error
		DeductStockIdempotent(ctx context.Context, bookID, quantity int64, idempotencyKey string) (bool, error)
	}

	defaultBookModel struct {
		sqlc.CachedConn
		conn          sqlx.SqlConn
		table         string
		cacheTimeout  time.Duration
		fallbackSlots chan struct{}
		fallbackGroup syncx.SingleFlight
	}

	CacheFallbackConfig struct {
		CacheTimeout  time.Duration
		MaxConcurrent int
	}

	Book struct {
		BookID     int64  `db:"book_id" json:"book_id"`
		ISBN       string `db:"isbn" json:"isbn"`
		Title      string `db:"title" json:"title"`
		Author     string `db:"author" json:"author"`
		Price      int64  `db:"price" json:"price"`
		StockCount int64  `db:"stock_count" json:"stock_count"`
	}

	databaseQueryError struct {
		err error
	}
)

func (e *databaseQueryError) Error() string {
	return e.err.Error()
}

func (e *databaseQueryError) Unwrap() error {
	return e.err
}

func NewBookModel(conn sqlx.SqlConn, c cache.CacheConf) BookModel {
	return NewBookModelWithCacheFallback(conn, c, CacheFallbackConfig{
		CacheTimeout:  defaultCacheFallbackTimeout,
		MaxConcurrent: defaultCacheFallbackMaxConcurrent,
	})
}

func NewBookModelWithCacheFallback(conn sqlx.SqlConn, c cache.CacheConf, fallback CacheFallbackConfig) BookModel {
	if fallback.CacheTimeout <= 0 {
		panic("cache fallback timeout must be positive")
	}
	if fallback.MaxConcurrent <= 0 {
		panic("cache fallback max concurrency must be positive")
	}
	return &defaultBookModel{
		CachedConn:    sqlc.NewConn(conn, c),
		conn:          conn,
		table:         "book",
		cacheTimeout:  fallback.CacheTimeout,
		fallbackSlots: make(chan struct{}, fallback.MaxConcurrent),
		fallbackGroup: syncx.NewSingleFlight(),
	}
}

func (m *defaultBookModel) Insert(data Book) (sql.Result, error) {
	var result sql.Result
	err := m.conn.TransactCtx(context.Background(), func(ctx context.Context, session sqlx.Session) error {
		var err error
		result, err = session.ExecCtx(ctx, `insert into book
			(isbn, title, price, language_code, currency, status)
			values (?, ?, ?, 'zh-CN', 'CNY', 'ACTIVE')`, data.ISBN, data.Title, data.Price)
		if err != nil {
			return err
		}
		bookID, err := result.LastInsertId()
		if err != nil {
			return err
		}
		if _, err = session.ExecCtx(ctx, `insert into inventory
			(book_id, available_stock, reserved_stock, version)
			values (?, ?, 0, 1)`, bookID, data.StockCount); err != nil {
			return err
		}
		if err = insertInventoryStockChangedEvent(ctx, session, bookID, inventorySnapshot{
			AvailableStock:   data.StockCount,
			InventoryVersion: 1,
		}); err != nil {
			return err
		}
		return replacePrimaryAuthor(ctx, session, bookID, data.Author)
	})
	if err != nil {
		return nil, err
	}
	if err := m.DelCache(cacheKeysForIdentifiers(data.ISBN)...); err != nil {
		logx.Errorf("failed to invalidate cache after book insert: %v", err)
	}
	return result, nil
}

func (m *defaultBookModel) FindOne(identifier string) (*Book, error) {
	return m.FindOneCtx(context.Background(), identifier)
}

func (m *defaultBookModel) FindOneCtx(ctx context.Context, identifier string) (*Book, error) {
	identifier = strings.TrimSpace(identifier)
	bookKey := cacheKeyForIdentifier(identifier)
	var resp Book
	cacheCtx, cancel := context.WithTimeout(ctx, m.cacheTimeout)
	err := m.QueryRowCtx(cacheCtx, &resp, bookKey, func(queryCtx context.Context, conn sqlx.SqlConn, v interface{}) error {
		err := m.queryByIdentifierCtx(queryCtx, conn, v, identifier)
		if err != nil && !errors.Is(err, sqlc.ErrNotFound) {
			return &databaseQueryError{err: err}
		}
		return err
	})
	cancel()
	switch err {
	case nil:
		return &resp, nil
	case sqlc.ErrNotFound:
		return nil, ErrNotFound
	}

	// A databaseQueryError proves the cache callback reached MySQL, so this is
	// a database failure and must not be retried as a cache outage. Any other
	// error occurred before the callback (for example Redis connection loss),
	// and is safe to bypass with one direct MySQL read.
	var queryErr *databaseQueryError
	if errors.As(err, &queryErr) {
		return nil, queryErr.err
	}

	logx.Errorf("book cache unavailable for key %q, falling back to MySQL: %v", bookKey, err)
	return m.findOneWithFallback(ctx, bookKey, identifier)
}

func (m *defaultBookModel) findOneWithFallback(ctx context.Context, bookKey, identifier string) (*Book, error) {
	value, fresh, err := m.fallbackGroup.DoEx(bookKey, func() (any, error) {
		select {
		case m.fallbackSlots <- struct{}{}:
			cacheFallbackInflight.Inc()
			defer func() {
				cacheFallbackInflight.Dec()
				<-m.fallbackSlots
			}()
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
			return nil, ErrCacheFallbackBusy
		}

		book, loadErr := m.findOneNoCacheCtx(ctx, identifier)
		cacheFallbackLoads.Inc(cacheFallbackResult(loadErr))
		return book, loadErr
	})
	if fresh {
		cacheFallbackFlights.Inc("leader")
	} else {
		cacheFallbackFlights.Inc("shared")
	}
	cacheFallbackRequests.Inc(cacheFallbackResult(err))
	if err != nil {
		return nil, err
	}
	return value.(*Book), nil
}

func cacheFallbackResult(err error) string {
	switch {
	case err == nil:
		return "success"
	case errors.Is(err, ErrNotFound):
		return "not_found"
	case errors.Is(err, ErrCacheFallbackBusy):
		return "overloaded"
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	default:
		return "error"
	}
}

func (m *defaultBookModel) Update(data Book) error {
	old, err := m.findOneNoCache(strconv.FormatInt(data.BookID, 10))
	if err != nil {
		return err
	}

	err = m.conn.TransactCtx(context.Background(), func(ctx context.Context, session sqlx.Session) error {
		result, err := session.ExecCtx(ctx, `update book
			set isbn = ?, title = ?, price = ?, version = version + 1
			where book_id = ? and status = 'ACTIVE'`, data.ISBN, data.Title, data.Price, data.BookID)
		if err != nil {
			return err
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if affected != 1 {
			return ErrNotFound
		}
		inventoryResult, err := session.ExecCtx(ctx, `update inventory
			set available_stock = ?, version = version + 1
			where book_id = ?`, data.StockCount, data.BookID)
		if err != nil {
			return err
		}
		inventoryAffected, err := inventoryResult.RowsAffected()
		if err != nil {
			return err
		}
		if inventoryAffected != 1 {
			return fmt.Errorf("inventory row missing for book %d", data.BookID)
		}
		snapshot, err := loadInventorySnapshot(ctx, session, data.BookID)
		if err != nil {
			return err
		}
		if err := insertInventoryStockChangedEvent(ctx, session, data.BookID, snapshot); err != nil {
			return err
		}
		return replacePrimaryAuthor(ctx, session, data.BookID, data.Author)
	})
	if err != nil {
		return err
	}
	keys := append(cacheKeysForBook(*old), cacheKeysForBook(data)...)
	if err := m.DelCache(uniqueStrings(keys)...); err != nil {
		logx.Errorf("failed to invalidate cache after book update: %v", err)
	}
	return nil
}

func (m *defaultBookModel) Delete(identifier string) error {
	book, err := m.findOneNoCache(identifier)
	if err != nil {
		return err
	}

	result, err := m.conn.ExecCtx(context.Background(), `update book
		set status = 'INACTIVE', version = version + 1
		where book_id = ? and status = 'ACTIVE'`, book.BookID)
	if err != nil {
		return err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if affected != 1 {
		return ErrNotFound
	}
	keys := append(cacheKeysForBook(*book), cacheKeyForIdentifier(identifier))
	if err := m.DelCache(uniqueStrings(keys)...); err != nil {
		logx.Errorf("failed to invalidate cache after book deactivation: %v", err)
	}
	return nil
}

// DeductStock atomically decrements stock when enough inventory is available.
// The conditional UPDATE is the correctness boundary: concurrent requests can
// never make stock_count negative.
func (m *defaultBookModel) DeductStock(bookID, quantity int64) error {
	if quantity <= 0 {
		return fmt.Errorf("quantity must be positive")
	}

	book, err := m.findOneNoCache(strconv.FormatInt(bookID, 10))
	if err != nil {
		return err
	}

	var affected int64
	err = m.conn.TransactCtx(context.Background(), func(ctx context.Context, session sqlx.Session) error {
		query := `update inventory i
			join book b on b.book_id = i.book_id
			set i.available_stock = i.available_stock - ?, i.version = i.version + 1
			where i.book_id = ? and b.status = 'ACTIVE' and i.available_stock >= ?`
		result, err := session.ExecCtx(ctx, query, quantity, bookID, quantity)
		if err != nil {
			return err
		}
		affected, err = result.RowsAffected()
		if err != nil || affected != 1 {
			return err
		}
		snapshot, err := loadInventorySnapshot(ctx, session, bookID)
		if err != nil {
			return err
		}
		return insertInventoryStockChangedEvent(ctx, session, bookID, snapshot)
	})
	if err != nil {
		return err
	}
	if affected == 1 {
		if err := m.DelCache(cacheKeysForBook(*book)...); err != nil {
			logx.Errorf("failed to invalidate cache after stock deduction: %v", err)
		}
		return nil
	}

	// Recheck only to distinguish a concurrent delete from insufficient stock.
	if _, err := m.findOneNoCache(strconv.FormatInt(bookID, 10)); err != nil {
		return err
	}
	return ErrInsufficientStock
}

const (
	stockDeductionProcessing   = "PROCESSING"
	stockDeductionSucceeded    = "SUCCEEDED"
	stockDeductionInsufficient = "INSUFFICIENT"
	stockDeductionNotFound     = "NOT_FOUND"
)

var errDuplicateIdempotencyKey = errors.New("duplicate idempotency key")

type stockDeductionRecord struct {
	BookID   int64  `db:"book_id"`
	Quantity int64  `db:"quantity"`
	Outcome  string `db:"outcome"`
}

// DeductStockIdempotent stores the request result and stock change in the same
// MySQL transaction. A duplicate key returns the committed original outcome.
func (m *defaultBookModel) DeductStockIdempotent(ctx context.Context, bookID, quantity int64, idempotencyKey string) (bool, error) {
	idempotencyKey = strings.TrimSpace(idempotencyKey)
	if idempotencyKey == "" {
		return false, fmt.Errorf("idempotency key is required")
	}
	if quantity <= 0 {
		return false, fmt.Errorf("quantity must be positive")
	}

	outcome := stockDeductionProcessing
	err := m.conn.TransactCtx(ctx, func(ctx context.Context, session sqlx.Session) error {
		insertRequest := `insert into stock_deduction
			(idempotency_key, book_id, quantity, outcome)
			values (?, ?, ?, ?)`
		if _, err := session.ExecCtx(ctx, insertRequest, idempotencyKey, bookID, quantity, stockDeductionProcessing); err != nil {
			var mysqlErr *mysql.MySQLError
			if errors.As(err, &mysqlErr) && mysqlErr.Number == 1062 {
				return errDuplicateIdempotencyKey
			}
			return err
		}

		updateStock := `update inventory i
			join book b on b.book_id = i.book_id
			set i.available_stock = i.available_stock - ?, i.version = i.version + 1
			where i.book_id = ? and b.status = 'ACTIVE' and i.available_stock >= ?`
		result, err := session.ExecCtx(ctx, updateStock, quantity, bookID, quantity)
		if err != nil {
			return err
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return err
		}

		if affected == 1 {
			snapshot, err := loadInventorySnapshot(ctx, session, bookID)
			if err != nil {
				return err
			}
			if err := insertInventoryStockChangedEvent(ctx, session, bookID, snapshot); err != nil {
				return err
			}
			outcome = stockDeductionSucceeded
		} else {
			var count int64
			if err := session.QueryRowCtx(ctx, &count, "select count(1) from book where book_id = ? and status = 'ACTIVE'", bookID); err != nil {
				return err
			}
			if count == 0 {
				outcome = stockDeductionNotFound
			} else {
				outcome = stockDeductionInsufficient
			}
		}

		_, err = session.ExecCtx(ctx,
			"update stock_deduction set outcome = ? where idempotency_key = ?",
			outcome, idempotencyKey)
		return err
	})

	if errors.Is(err, errDuplicateIdempotencyKey) {
		record, loadErr := m.findStockDeduction(ctx, idempotencyKey)
		if loadErr != nil {
			return false, loadErr
		}
		if record.BookID != bookID || record.Quantity != quantity {
			return true, ErrIdempotencyConflict
		}
		return true, stockDeductionOutcomeError(record.Outcome)
	}
	if err != nil {
		return false, err
	}

	if outcome == stockDeductionSucceeded {
		keys := []string{cacheKeyForIdentifier(strconv.FormatInt(bookID, 10))}
		if book, loadErr := m.findOneNoCache(strconv.FormatInt(bookID, 10)); loadErr == nil {
			keys = append(keys, cacheKeysForBook(*book)...)
		}
		if cacheErr := m.DelCacheCtx(ctx, uniqueStrings(keys)...); cacheErr != nil {
			logx.WithContext(ctx).Errorf("failed to invalidate book cache after stock deduction: %v", cacheErr)
		}
	}
	return false, stockDeductionOutcomeError(outcome)
}

func (m *defaultBookModel) findStockDeduction(ctx context.Context, idempotencyKey string) (*stockDeductionRecord, error) {
	var record stockDeductionRecord
	err := m.conn.QueryRowCtx(ctx, &record,
		"select book_id, quantity, outcome from stock_deduction where idempotency_key = ? limit 1",
		idempotencyKey)
	if err != nil {
		return nil, err
	}
	return &record, nil
}

func stockDeductionOutcomeError(outcome string) error {
	switch outcome {
	case stockDeductionSucceeded:
		return nil
	case stockDeductionInsufficient:
		return ErrInsufficientStock
	case stockDeductionNotFound:
		return ErrNotFound
	default:
		return fmt.Errorf("invalid stock deduction outcome: %s", outcome)
	}
}

func (m *defaultBookModel) formatPrimary(primary interface{}) string {
	return cacheKeyForIdentifier(fmt.Sprint(primary))
}

func (m *defaultBookModel) findOneNoCache(identifier string) (*Book, error) {
	return m.findOneNoCacheCtx(context.Background(), identifier)
}

func (m *defaultBookModel) findOneNoCacheCtx(ctx context.Context, identifier string) (*Book, error) {
	var resp Book
	query, args := m.identifierQuery(strings.TrimSpace(identifier))
	if err := m.QueryRowNoCacheCtx(ctx, &resp, query, args...); err != nil {
		if err == sqlc.ErrNotFound {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &resp, nil
}

func (m *defaultBookModel) queryByIdentifier(conn sqlx.SqlConn, v interface{}, identifier string) error {
	return m.queryByIdentifierCtx(context.Background(), conn, v, identifier)
}

func (m *defaultBookModel) queryByIdentifierCtx(ctx context.Context, conn sqlx.SqlConn, v interface{}, identifier string) error {
	query, args := m.identifierQuery(identifier)
	return conn.QueryRowCtx(ctx, v, query, args...)
}

func (m *defaultBookModel) identifierQuery(identifier string) (string, []interface{}) {
	bookID, _ := strconv.ParseInt(identifier, 10, 64)
	query := fmt.Sprintf(`select %s from book b
		left join inventory i on i.book_id = b.book_id
		left join book_author ba on ba.book_id = b.book_id
		left join author a on a.author_id = ba.author_id
		where (b.book_id = ? or b.isbn = ?) and b.status = 'ACTIVE'
		group by b.book_id, b.isbn, b.title, b.author, b.price, i.available_stock, b.stock_count
		limit 1`, bookRows)
	return query, []interface{}{bookID, identifier}
}

func cacheKeyForIdentifier(identifier string) string {
	identifier = strings.TrimSpace(identifier)
	if bookID, err := strconv.ParseInt(identifier, 10, 64); err == nil && bookID > 0 {
		return fmt.Sprintf("%sid#%d", cacheBookLookupPrefix, bookID)
	}
	return fmt.Sprintf("%sisbn#%s", cacheBookLookupPrefix, strings.ToLower(identifier))
}

func cacheKeysForBook(book Book) []string {
	keys := cacheKeysForIdentifiers(strconv.FormatInt(book.BookID, 10), book.ISBN)
	keys = append(keys,
		catalogCacheKey(book.BookID, strconv.FormatInt(book.BookID, 10)),
		catalogCacheKey(0, book.ISBN),
	)
	return uniqueStrings(keys)
}

func cacheKeysForIdentifiers(identifiers ...string) []string {
	keys := make([]string, 0, len(identifiers))
	for _, identifier := range identifiers {
		identifier = strings.TrimSpace(identifier)
		if identifier != "" && identifier != "0" {
			keys = append(keys, cacheKeyForIdentifier(identifier))
		}
	}
	return uniqueStrings(keys)
}

func replacePrimaryAuthor(ctx context.Context, session sqlx.Session, bookID int64, author string) error {
	author = strings.TrimSpace(author)
	if _, err := session.ExecCtx(ctx, "delete from book_author where book_id = ? and role = 'AUTHOR'", bookID); err != nil {
		return err
	}
	if author == "" {
		return nil
	}
	result, err := session.ExecCtx(ctx, `insert into author (name)
		values (?)
		on duplicate key update author_id = LAST_INSERT_ID(author_id), name = VALUES(name)`, author)
	if err != nil {
		return err
	}
	authorID, err := result.LastInsertId()
	if err != nil {
		return err
	}
	_, err = session.ExecCtx(ctx, `insert into book_author (book_id, author_id, role, sort_order)
		values (?, ?, 'AUTHOR', 0)`, bookID, authorID)
	return err
}

func uniqueStrings(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}
