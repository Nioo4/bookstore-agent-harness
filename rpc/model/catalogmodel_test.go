package model

import (
	"database/sql/driver"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
)

var catalogBookColumns = []string{
	"book_id", "isbn", "title", "subtitle", "description", "publisher",
	"publication_date", "language_code", "page_count", "cover_url", "price",
	"currency", "status", "version", "available_stock", "authors_hex",
	"categories_hex", "relevance",
}

func TestGetCatalogBookUsesOnlyIDOrISBNAndReturnsRelations(t *testing.T) {
	m, mock := newTestBookModel(t)
	mock.ExpectQuery(`(?s)SELECT .*FROM book b.*b\.book_id = \? OR b\.isbn = \?.*b\.status = 'ACTIVE'`).
		WithArgs(int64(42), "42").
		WillReturnRows(sqlmock.NewRows(catalogBookColumns).AddRow(
			42, "9787111544937", "Distributed Systems", "Concepts", "Description",
			"Publisher", "2026-08-25", "en", 640, "https://example.test/cover.jpg",
			9900, "CNY", "ACTIVE", 3, 8, "416C696365,426F62", "4469737472696275746564", 0,
		))

	book, err := m.GetCatalogBook(t.Context(), "42")
	if err != nil {
		t.Fatal(err)
	}
	if book.BookID != 42 || book.AvailableStock != 8 || book.PageCount == nil || *book.PageCount != 640 {
		t.Fatalf("unexpected catalog book: %+v", book)
	}
	if strings.Join(book.Authors, ",") != "Alice,Bob" || strings.Join(book.Categories, ",") != "Distributed" {
		t.Fatalf("relations not decoded: authors=%v categories=%v", book.Authors, book.Categories)
	}

	// No second SQL expectation: the canonical ID cache must serve this read.
	cached, err := m.GetCatalogBook(t.Context(), "42")
	if err != nil || cached.BookID != book.BookID {
		t.Fatalf("cached catalog book = %+v, err=%v", cached, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestNormalizeBookSearchFilterDefaultsAndCursor(t *testing.T) {
	filter, offset, err := normalizeBookSearchFilter(BookSearchFilter{
		Query:  " Distributed Systems ",
		Author: " ALICE ",
		Cursor: encodeBookSearchCursor(40),
	})
	if err != nil {
		t.Fatal(err)
	}
	if filter.Query != "distributed systems" || filter.Author != "alice" || filter.Sort != "relevance" || filter.Limit != 20 || offset != 40 {
		t.Fatalf("unexpected normalized filter=%+v offset=%d", filter, offset)
	}
	if filter.Cursor != "" {
		t.Fatalf("normalized cache input must not retain cursor: %q", filter.Cursor)
	}
}

func TestNormalizeBookSearchFilterRejectsInvalidInput(t *testing.T) {
	negative := int64(-1)
	high := int64(100)
	low := int64(200)
	tests := []BookSearchFilter{
		{Limit: 51},
		{MinPrice: &negative},
		{MinPrice: &low, MaxPrice: &high},
		{Sort: "random"},
		{Cursor: "not-base64!"},
	}
	for _, filter := range tests {
		if _, _, err := normalizeBookSearchFilter(filter); err == nil {
			t.Fatalf("expected invalid filter to fail: %+v", filter)
		}
	}
}

func TestBuildBookSearchQueryCoversAllFiltersAndStableSort(t *testing.T) {
	minPrice := int64(1000)
	maxPrice := int64(10000)
	inStock := true
	filter, offset, err := normalizeBookSearchFilter(BookSearchFilter{
		Query: "distributed", Author: "Alice", Category: "systems",
		MinPrice: &minPrice, MaxPrice: &maxPrice, Language: "EN",
		InStock: &inStock, Sort: "price_desc", Limit: 12,
	})
	if err != nil {
		t.Fatal(err)
	}
	query, args := buildBookSearchQuery(filter, offset)
	for _, fragment := range []string{
		"MATCH(b.title, b.subtitle, b.description)",
		"JOIN author fa",
		"JOIN category fc",
		"b.price >= ?",
		"b.price <= ?",
		"LOWER(b.language_code) = ?",
		"available_stock, 0) > 0",
		"ORDER BY b.price DESC, b.book_id ASC",
		"LIMIT ? OFFSET ?",
	} {
		if !strings.Contains(query, fragment) {
			t.Errorf("query missing %q\n%s", fragment, query)
		}
	}
	if len(args) != 13 {
		t.Fatalf("args=%v; want 13 positional values", args)
	}
	if args[0] != "distributed" || args[1] != "distributed" {
		t.Fatalf("fulltext expression occurs twice and needs two args: %v", args[:2])
	}
}

func TestSearchLiteralFallbackEscapesWildcards(t *testing.T) {
	query, args := buildBookSearchQuery(BookSearchFilter{Query: "python_50%=", Limit: 20}, 0)
	if !strings.Contains(query, "LOWER(b.title) LIKE ? ESCAPE '='") {
		t.Fatal("mixed-language title fallback is missing")
	}
	for _, arg := range args[2:5] {
		if arg != "%python=_50=%==%" {
			t.Fatalf("wildcards must be searched literally: %v", arg)
		}
	}
}

func TestSearchLiteralFallbackMatchesEverySpaceSeparatedTerm(t *testing.T) {
	query, args := buildBookSearchQuery(BookSearchFilter{Query: "分布式 基础", Limit: 20}, 0)
	if strings.Count(query, "LOWER(b.title) LIKE ? ESCAPE '='") != 2 {
		t.Fatalf("expected one literal title match per term:\n%s", query)
	}
	want := []any{"%分布式%", "%分布式%", "%分布式%", "%基础%", "%基础%", "%基础%"}
	if !reflect.DeepEqual(args[2:8], want) {
		t.Fatalf("literal args=%v; want %v", args[2:8], want)
	}
}

func TestSearchBooksReturnsCursorAndCachesNormalizedResult(t *testing.T) {
	m, mock := newTestBookModel(t)
	filter := BookSearchFilter{Language: "en", Sort: "newest", Limit: 1}
	normalized, offset, err := normalizeBookSearchFilter(filter)
	if err != nil {
		t.Fatal(err)
	}
	query, args := buildBookSearchQuery(normalized, offset)
	rows := sqlmock.NewRows(catalogBookColumns).
		AddRow(2, "isbn-2", "New", "", "", "", "2026-08-25", "en", nil, "", 2000, "CNY", "ACTIVE", 1, 3, "416C696365", "53797374656D73", 0).
		AddRow(1, "isbn-1", "Old", "", "", "", "2025-08-25", "en", nil, "", 1000, "CNY", "ACTIVE", 1, 2, "426F62", "53797374656D73", 0)
	mock.ExpectQuery(regexp.QuoteMeta(query)).WithArgs(toDriverValues(args)...).WillReturnRows(rows)

	result, err := m.SearchBooks(t.Context(), filter)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Books) != 1 || result.Books[0].BookID != 2 || result.NextCursor != encodeBookSearchCursor(1) {
		t.Fatalf("unexpected result: %+v", result)
	}

	// Equivalent normalized input must hit the 30-second search cache.
	cached, err := m.SearchBooks(t.Context(), BookSearchFilter{Language: " EN ", Sort: "NEWEST", Limit: 1})
	if err != nil || len(cached.Books) != 1 || cached.Books[0].BookID != 2 {
		t.Fatalf("cached search=%+v err=%v", cached, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func toDriverValues(values []any) []driver.Value {
	result := make([]driver.Value, len(values))
	for i, value := range values {
		result[i] = value
	}
	return result
}
