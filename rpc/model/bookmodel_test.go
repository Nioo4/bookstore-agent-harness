package model

import (
	"context"
	"database/sql/driver"
	"errors"
	"fmt"
	"regexp"
	"sync"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/alicebob/miniredis/v2"
	"github.com/go-sql-driver/mysql"
	"github.com/zeromicro/go-zero/core/stores/cache"
	"github.com/zeromicro/go-zero/core/stores/redis"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

var bookColumns = []string{"book_id", "isbn", "title", "author", "price", "stock_count"}

func expectInventoryOutbox(mock sqlmock.Sqlmock, bookID int64) {
	mock.ExpectExec(regexp.QuoteMeta(`insert into outbox_event
		(event_id, event_type, aggregate_type, aggregate_id, payload_json, status, available_at)
		values (?, ?, 'INVENTORY', ?, ?, 'PENDING', CURRENT_TIMESTAMP(3))`)).
		WithArgs(sqlmock.AnyArg(), inventoryStockChangedEventType, bookID, sqlmock.AnyArg()).
		WillReturnResult(sqlmock.NewResult(0, 1))
}

func newTestBookModel(t *testing.T) (*defaultBookModel, sqlmock.Sqlmock) {
	m, mock, _ := newTestBookModelWithRedis(t)
	return m, mock
}

func newTestBookModelWithRedis(t *testing.T) (*defaultBookModel, sqlmock.Sqlmock, *miniredis.Miniredis) {
	return newTestBookModelWithFallback(t, CacheFallbackConfig{
		CacheTimeout:  defaultCacheFallbackTimeout,
		MaxConcurrent: defaultCacheFallbackMaxConcurrent,
	})
}

func newTestBookModelWithFallback(t *testing.T, fallback CacheFallbackConfig) (*defaultBookModel, sqlmock.Sqlmock, *miniredis.Miniredis) {
	t.Helper()

	db, mock, err := sqlmock.New()
	if err != nil {
		t.Fatalf("create sql mock: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	redisServer := miniredis.RunT(t)
	conf := cache.CacheConf{{
		RedisConf: redis.RedisConf{
			Host:     redisServer.Addr(),
			Type:     redis.NodeType,
			NonBlock: true,
		},
		Weight: 100,
	}}

	bookModel := NewBookModelWithCacheFallback(sqlx.NewSqlConnFromDB(db), conf, fallback)
	return bookModel.(*defaultBookModel), mock, redisServer
}

func TestBookModelFindOneFallsBackToMySQLWhenRedisIsUnavailable(t *testing.T) {
	m, mock, redisServer := newTestBookModelWithRedis(t)
	redisServer.Close()

	book := Book{BookID: 51, ISBN: "9787111555555", Title: "Redis fallback", Author: "Tester", Price: 5200, StockCount: 8}
	query, args := m.identifierQuery(book.ISBN)
	mock.ExpectQuery(regexp.QuoteMeta(query)).
		WithArgs(asDriverValues(args)...).
		WillReturnRows(sqlmock.NewRows(bookColumns).
			AddRow(book.BookID, book.ISBN, book.Title, book.Author, book.Price, book.StockCount))

	got, err := m.FindOne(book.ISBN)
	if err != nil {
		t.Fatalf("FindOne with Redis unavailable: %v", err)
	}
	if *got != book {
		t.Fatalf("FindOne with Redis unavailable = %+v; want %+v", got, book)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelFindOneDoesNotRetryMySQLErrorAsCacheFailure(t *testing.T) {
	m, mock := newTestBookModel(t)
	databaseErr := errors.New("mysql unavailable")
	query, args := m.identifierQuery("9787111666666")
	mock.ExpectQuery(regexp.QuoteMeta(query)).
		WithArgs(asDriverValues(args)...).
		WillReturnError(databaseErr)

	_, err := m.FindOne("9787111666666")
	if !errors.Is(err, databaseErr) {
		t.Fatalf("FindOne error = %v; want original MySQL error", err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelFindOneMissingBookFinishesWithinCacheBudgetWhenRedisIsUnavailable(t *testing.T) {
	m, mock, redisServer := newTestBookModelWithFallback(t, CacheFallbackConfig{
		CacheTimeout:  50 * time.Millisecond,
		MaxConcurrent: 2,
	})
	redisServer.Close()

	query, args := m.identifierQuery("missing-redis-down")
	mock.ExpectQuery(regexp.QuoteMeta(query)).
		WithArgs(asDriverValues(args)...).
		WillReturnRows(sqlmock.NewRows(bookColumns))

	started := time.Now()
	_, err := m.FindOneCtx(t.Context(), "missing-redis-down")
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("FindOne error = %v; want ErrNotFound", err)
	}
	if elapsed := time.Since(started); elapsed >= 500*time.Millisecond {
		t.Fatalf("FindOne took %s; want bounded Redis failure handling", elapsed)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelFallbackSingleFlightLoadsSameKeyOnce(t *testing.T) {
	m, mock, _ := newTestBookModelWithFallback(t, CacheFallbackConfig{
		CacheTimeout:  50 * time.Millisecond,
		MaxConcurrent: 4,
	})
	book := Book{BookID: 61, ISBN: "singleflight-isbn", Title: "SingleFlight", Author: "Tester", Price: 6100, StockCount: 6}
	query, args := m.identifierQuery(book.ISBN)
	mock.ExpectQuery(regexp.QuoteMeta(query)).
		WithArgs(asDriverValues(args)...).
		WillDelayFor(150 * time.Millisecond).
		WillReturnRows(sqlmock.NewRows(bookColumns).
			AddRow(book.BookID, book.ISBN, book.Title, book.Author, book.Price, book.StockCount))

	const callers = 24
	start := make(chan struct{})
	errs := make(chan error, callers)
	var wg sync.WaitGroup
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			got, err := m.findOneWithFallback(t.Context(), cacheKeyForIdentifier(book.ISBN), book.ISBN)
			if err == nil && *got != book {
				err = fmt.Errorf("got %+v; want %+v", got, book)
			}
			errs <- err
		}()
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatalf("SingleFlight should issue exactly one MySQL query: %v", err)
	}
}

func TestBookModelFallbackRejectsAboveConcurrencyLimit(t *testing.T) {
	const (
		maxConcurrent = 2
		callers       = 10
	)
	m, mock, _ := newTestBookModelWithFallback(t, CacheFallbackConfig{
		CacheTimeout:  50 * time.Millisecond,
		MaxConcurrent: maxConcurrent,
	})
	mock.MatchExpectationsInOrder(false)
	query, _ := m.identifierQuery("any")
	for i := 0; i < maxConcurrent; i++ {
		mock.ExpectQuery(regexp.QuoteMeta(query)).
			WithArgs(sqlmock.AnyArg(), sqlmock.AnyArg()).
			WillDelayFor(250 * time.Millisecond).
			WillReturnRows(sqlmock.NewRows(bookColumns).
				AddRow(70+i, fmt.Sprintf("limit-%d", i), "Limit", "Tester", 7000, 7))
	}

	start := make(chan struct{})
	errs := make(chan error, callers)
	var wg sync.WaitGroup
	for i := 0; i < callers; i++ {
		identifier := fmt.Sprintf("limit-%d", i)
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, err := m.findOneWithFallback(t.Context(), cacheKeyForIdentifier(identifier), identifier)
			errs <- err
		}()
	}
	close(start)
	wg.Wait()
	close(errs)

	var success, overloaded int
	for err := range errs {
		switch {
		case err == nil:
			success++
		case errors.Is(err, ErrCacheFallbackBusy):
			overloaded++
		default:
			t.Fatalf("unexpected fallback error: %v", err)
		}
	}
	if success != maxConcurrent || overloaded != callers-maxConcurrent {
		t.Fatalf("success=%d overloaded=%d; want %d and %d", success, overloaded, maxConcurrent, callers-maxConcurrent)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelInsertAndFindByISBNUsesNewFieldsAndCache(t *testing.T) {
	m, mock := newTestBookModel(t)
	book := Book{
		ISBN:       "9787111544937",
		Title:      "Redis实战",
		Author:     "Josiah Carlson",
		Price:      8800,
		StockCount: 25,
	}

	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`insert into book
			(isbn, title, price, language_code, currency, status)
			values (?, ?, ?, 'zh-CN', 'CNY', 'ACTIVE')`)).
		WithArgs(book.ISBN, book.Title, book.Price).
		WillReturnResult(sqlmock.NewResult(42, 1))
	mock.ExpectExec(regexp.QuoteMeta(`insert into inventory
			(book_id, available_stock, reserved_stock, version)
			values (?, ?, 0, 1)`)).
		WithArgs(int64(42), book.StockCount).
		WillReturnResult(sqlmock.NewResult(0, 1))
	expectInventoryOutbox(mock, 42)
	mock.ExpectExec(regexp.QuoteMeta("delete from book_author where book_id = ? and role = 'AUTHOR'")).
		WithArgs(int64(42)).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectExec(regexp.QuoteMeta(`insert into author (name)
		values (?)
		on duplicate key update author_id = LAST_INSERT_ID(author_id), name = VALUES(name)`)).
		WithArgs(book.Author).WillReturnResult(sqlmock.NewResult(9, 1))
	mock.ExpectExec(regexp.QuoteMeta(`insert into book_author (book_id, author_id, role, sort_order)
		values (?, ?, 'AUTHOR', 0)`)).
		WithArgs(int64(42), int64(9)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()

	result, err := m.Insert(book)
	if err != nil {
		t.Fatalf("insert book: %v", err)
	}
	bookID, err := result.LastInsertId()
	if err != nil || bookID != 42 {
		t.Fatalf("LastInsertId = %d, %v; want 42, nil", bookID, err)
	}

	query, args := m.identifierQuery(book.ISBN)
	mock.ExpectQuery(regexp.QuoteMeta(query)).
		WithArgs(asDriverValues(args)...).
		WillReturnRows(sqlmock.NewRows(bookColumns).
			AddRow(bookID, book.ISBN, book.Title, book.Author, book.Price, book.StockCount))

	got, err := m.FindOne(book.ISBN)
	if err != nil {
		t.Fatalf("find by ISBN: %v", err)
	}
	if got.BookID != 42 || got.ISBN != book.ISBN || got.Title != book.Title ||
		got.Author != book.Author || got.Price != book.Price || got.StockCount != book.StockCount {
		t.Fatalf("unexpected book: %+v", got)
	}

	// The second read has no SQL expectation: it must be served from Redis.
	cached, err := m.FindOne(book.ISBN)
	if err != nil {
		t.Fatalf("find cached book: %v", err)
	}
	if *cached != *got {
		t.Fatalf("cached book = %+v; want %+v", cached, got)
	}

	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelRejectsTitleLookupAndUsesRelationalUpdateAndSoftDelete(t *testing.T) {
	m, mock := newTestBookModel(t)
	book := Book{
		BookID:     7,
		ISBN:       "legacy-0123456789abcdef0123",
		Title:      "旧接口书名",
		Author:     "Unknown",
		Price:      1000,
		StockCount: 0,
	}

	queryByTitle, titleArgs := m.identifierQuery(book.Title)
	mock.ExpectQuery(regexp.QuoteMeta(queryByTitle)).
		WithArgs(asDriverValues(titleArgs)...).
		WillReturnRows(sqlmock.NewRows(bookColumns))
	if _, err := m.FindOne(book.Title); !errors.Is(err, ErrNotFound) {
		t.Fatalf("title lookup error = %v; want ErrNotFound", err)
	}

	queryByID, idArgs := m.identifierQuery("7")
	mock.ExpectQuery(regexp.QuoteMeta(queryByID)).
		WithArgs(asDriverValues(idArgs)...).
		WillReturnRows(sqlmock.NewRows(bookColumns).
			AddRow(book.BookID, book.ISBN, book.Title, book.Author, book.Price, book.StockCount))

	book.Price = 1200
	book.StockCount = 9
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`update book
			set isbn = ?, title = ?, price = ?, version = version + 1
			where book_id = ? and status = 'ACTIVE'`)).
		WithArgs(book.ISBN, book.Title, book.Price, book.BookID).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec(regexp.QuoteMeta(`update inventory
			set available_stock = ?, version = version + 1
			where book_id = ?`)).
		WithArgs(book.StockCount, book.BookID).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery(regexp.QuoteMeta("select available_stock, version from inventory where book_id = ?")).
		WithArgs(book.BookID).
		WillReturnRows(sqlmock.NewRows([]string{"available_stock", "version"}).AddRow(book.StockCount, 2))
	expectInventoryOutbox(mock, book.BookID)
	mock.ExpectExec(regexp.QuoteMeta("delete from book_author where book_id = ? and role = 'AUTHOR'")).
		WithArgs(book.BookID).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec(regexp.QuoteMeta(`insert into author (name)
		values (?)
		on duplicate key update author_id = LAST_INSERT_ID(author_id), name = VALUES(name)`)).
		WithArgs(book.Author).WillReturnResult(sqlmock.NewResult(4, 1))
	mock.ExpectExec(regexp.QuoteMeta(`insert into book_author (book_id, author_id, role, sort_order)
		values (?, ?, 'AUTHOR', 0)`)).
		WithArgs(book.BookID, int64(4)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	if err := m.Update(book); err != nil {
		t.Fatalf("update relational book: %v", err)
	}

	queryByISBN, isbnArgs := m.identifierQuery(book.ISBN)
	mock.ExpectQuery(regexp.QuoteMeta(queryByISBN)).
		WithArgs(asDriverValues(isbnArgs)...).
		WillReturnRows(sqlmock.NewRows(bookColumns).
			AddRow(book.BookID, book.ISBN, book.Title, book.Author, book.Price, book.StockCount))
	mock.ExpectExec(regexp.QuoteMeta(`update book
		set status = 'INACTIVE', version = version + 1
		where book_id = ? and status = 'ACTIVE'`)).
		WithArgs(book.BookID).
		WillReturnResult(sqlmock.NewResult(0, 1))
	if err := m.Delete(book.ISBN); err != nil {
		t.Fatalf("soft delete by ISBN: %v", err)
	}

	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelDeductStockUsesConditionalAtomicUpdate(t *testing.T) {
	m, mock := newTestBookModel(t)
	book := Book{BookID: 9, ISBN: "9787111111111", Title: "并发编程", Author: "Tester", Price: 5000, StockCount: 10}

	queryByID, args := m.identifierQuery("9")
	mock.ExpectQuery(regexp.QuoteMeta(queryByID)).
		WithArgs(asDriverValues(args)...).
		WillReturnRows(sqlmock.NewRows(bookColumns).AddRow(book.BookID, book.ISBN, book.Title, book.Author, book.Price, book.StockCount))

	updateQuery := `update inventory i
		join book b on b.book_id = i.book_id
		set i.available_stock = i.available_stock - ?, i.version = i.version + 1
		where i.book_id = ? and b.status = 'ACTIVE' and i.available_stock >= ?`
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(updateQuery)).
		WithArgs(int64(3), book.BookID, int64(3)).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery(regexp.QuoteMeta("select available_stock, version from inventory where book_id = ?")).
		WithArgs(book.BookID).
		WillReturnRows(sqlmock.NewRows([]string{"available_stock", "version"}).AddRow(7, 2))
	expectInventoryOutbox(mock, book.BookID)
	mock.ExpectCommit()

	if err := m.DeductStock(book.BookID, 3); err != nil {
		t.Fatalf("deduct stock: %v", err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelDeductStockReturnsInsufficientStock(t *testing.T) {
	m, mock := newTestBookModel(t)
	book := Book{BookID: 10, ISBN: "9787111222222", Title: "库存测试", Author: "Tester", Price: 1000, StockCount: 1}
	queryByID, args := m.identifierQuery("10")
	rows := func() *sqlmock.Rows {
		return sqlmock.NewRows(bookColumns).AddRow(book.BookID, book.ISBN, book.Title, book.Author, book.Price, book.StockCount)
	}

	mock.ExpectQuery(regexp.QuoteMeta(queryByID)).WithArgs(asDriverValues(args)...).WillReturnRows(rows())
	updateQuery := `update inventory i
		join book b on b.book_id = i.book_id
		set i.available_stock = i.available_stock - ?, i.version = i.version + 1
		where i.book_id = ? and b.status = 'ACTIVE' and i.available_stock >= ?`
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(updateQuery)).
		WithArgs(int64(2), book.BookID, int64(2)).
		WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectCommit()
	mock.ExpectQuery(regexp.QuoteMeta(queryByID)).WithArgs(asDriverValues(args)...).WillReturnRows(rows())

	err := m.DeductStock(book.BookID, 2)
	if !errors.Is(err, ErrInsufficientStock) {
		t.Fatalf("DeductStock error = %v; want ErrInsufficientStock", err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelDeductStockReturnsNotFound(t *testing.T) {
	m, mock := newTestBookModel(t)
	queryByID, args := m.identifierQuery("404")
	mock.ExpectQuery(regexp.QuoteMeta(queryByID)).
		WithArgs(asDriverValues(args)...).
		WillReturnRows(sqlmock.NewRows(bookColumns))

	err := m.DeductStock(404, 1)
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("DeductStock error = %v; want ErrNotFound", err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelDeductStockIdempotentCommitsStockAndResultTogether(t *testing.T) {
	m, mock := newTestBookModel(t)
	book := Book{BookID: 11, ISBN: "9787111333333", Title: "幂等测试", Author: "Tester", Price: 2000, StockCount: 5}
	key := "request-success-1"

	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`insert into stock_deduction
			(idempotency_key, book_id, quantity, outcome)
			values (?, ?, ?, ?)`)).
		WithArgs(key, book.BookID, int64(2), stockDeductionProcessing).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec(regexp.QuoteMeta(`update inventory i
			join book b on b.book_id = i.book_id
			set i.available_stock = i.available_stock - ?, i.version = i.version + 1
			where i.book_id = ? and b.status = 'ACTIVE' and i.available_stock >= ?`)).
		WithArgs(int64(2), book.BookID, int64(2)).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery(regexp.QuoteMeta("select available_stock, version from inventory where book_id = ?")).
		WithArgs(book.BookID).
		WillReturnRows(sqlmock.NewRows([]string{"available_stock", "version"}).AddRow(3, 2))
	expectInventoryOutbox(mock, book.BookID)
	mock.ExpectExec(regexp.QuoteMeta("update stock_deduction set outcome = ? where idempotency_key = ?")).
		WithArgs(stockDeductionSucceeded, key).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()

	queryByID, args := m.identifierQuery("11")
	mock.ExpectQuery(regexp.QuoteMeta(queryByID)).WithArgs(asDriverValues(args)...).
		WillReturnRows(sqlmock.NewRows(bookColumns).AddRow(book.BookID, book.ISBN, book.Title, book.Author, book.Price, int64(3)))

	replayed, err := m.DeductStockIdempotent(context.Background(), book.BookID, 2, key)
	if err != nil || replayed {
		t.Fatalf("replayed = %v, err = %v", replayed, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelDeductStockIdempotentReplaysCommittedResult(t *testing.T) {
	m, mock := newTestBookModel(t)
	key := "request-replay-1"
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`insert into stock_deduction
			(idempotency_key, book_id, quantity, outcome)
			values (?, ?, ?, ?)`)).
		WithArgs(key, int64(12), int64(1), stockDeductionProcessing).
		WillReturnError(&mysql.MySQLError{Number: 1062, Message: "duplicate entry"})
	mock.ExpectRollback()
	mock.ExpectQuery(regexp.QuoteMeta("select book_id, quantity, outcome from stock_deduction where idempotency_key = ? limit 1")).
		WithArgs(key).
		WillReturnRows(sqlmock.NewRows([]string{"book_id", "quantity", "outcome"}).AddRow(12, 1, stockDeductionSucceeded))

	replayed, err := m.DeductStockIdempotent(context.Background(), 12, 1, key)
	if err != nil || !replayed {
		t.Fatalf("replayed = %v, err = %v", replayed, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelDeductStockIdempotentRejectsKeyReuseWithDifferentRequest(t *testing.T) {
	m, mock := newTestBookModel(t)
	key := "request-conflict-1"
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`insert into stock_deduction
			(idempotency_key, book_id, quantity, outcome)
			values (?, ?, ?, ?)`)).
		WithArgs(key, int64(13), int64(2), stockDeductionProcessing).
		WillReturnError(&mysql.MySQLError{Number: 1062, Message: "duplicate entry"})
	mock.ExpectRollback()
	mock.ExpectQuery(regexp.QuoteMeta("select book_id, quantity, outcome from stock_deduction where idempotency_key = ? limit 1")).
		WithArgs(key).
		WillReturnRows(sqlmock.NewRows([]string{"book_id", "quantity", "outcome"}).AddRow(13, 1, stockDeductionSucceeded))

	replayed, err := m.DeductStockIdempotent(context.Background(), 13, 2, key)
	if !replayed || !errors.Is(err, ErrIdempotencyConflict) {
		t.Fatalf("replayed = %v, err = %v; want conflict", replayed, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestBookModelDeductStockIdempotentPersistsInsufficientOutcome(t *testing.T) {
	m, mock := newTestBookModel(t)
	key := "request-insufficient-1"
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`insert into stock_deduction
			(idempotency_key, book_id, quantity, outcome)
			values (?, ?, ?, ?)`)).
		WithArgs(key, int64(14), int64(5), stockDeductionProcessing).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec(regexp.QuoteMeta(`update inventory i
			join book b on b.book_id = i.book_id
			set i.available_stock = i.available_stock - ?, i.version = i.version + 1
			where i.book_id = ? and b.status = 'ACTIVE' and i.available_stock >= ?`)).
		WithArgs(int64(5), int64(14), int64(5)).
		WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery(regexp.QuoteMeta("select count(1) from book where book_id = ? and status = 'ACTIVE'")).
		WithArgs(int64(14)).
		WillReturnRows(sqlmock.NewRows([]string{"count(1)"}).AddRow(1))
	mock.ExpectExec(regexp.QuoteMeta("update stock_deduction set outcome = ? where idempotency_key = ?")).
		WithArgs(stockDeductionInsufficient, key).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()

	replayed, err := m.DeductStockIdempotent(context.Background(), 14, 5, key)
	if replayed || !errors.Is(err, ErrInsufficientStock) {
		t.Fatalf("replayed = %v, err = %v; want insufficient", replayed, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func asDriverValues(values []interface{}) []driver.Value {
	result := make([]driver.Value, len(values))
	for i, value := range values {
		result[i] = value
	}
	return result
}
