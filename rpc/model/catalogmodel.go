package model

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/zeromicro/go-zero/core/logx"
	"github.com/zeromicro/go-zero/core/stores/sqlc"
)

const (
	defaultBookSearchLimit = 20
	maxBookSearchLimit     = 50
	bookSearchCacheTTL     = 30 * time.Second
	catalogCachePrefix     = "cache#Book#catalog#"
	searchCachePrefix      = "cache#Book#search#"
)

type (
	CatalogModel interface {
		GetCatalogBook(ctx context.Context, identifier string) (*CatalogBook, error)
		SearchBooks(ctx context.Context, filter BookSearchFilter) (*BookSearchResult, error)
	}

	CatalogBook struct {
		BookID          int64    `json:"book_id"`
		ISBN            string   `json:"isbn"`
		Title           string   `json:"title"`
		Subtitle        string   `json:"subtitle,omitempty"`
		Description     string   `json:"description,omitempty"`
		Publisher       string   `json:"publisher,omitempty"`
		PublicationDate string   `json:"publication_date,omitempty"`
		LanguageCode    string   `json:"language_code"`
		PageCount       *int64   `json:"page_count,omitempty"`
		CoverURL        string   `json:"cover_url,omitempty"`
		Price           int64    `json:"price"`
		Currency        string   `json:"currency"`
		Status          string   `json:"status"`
		Version         uint64   `json:"version"`
		AvailableStock  int64    `json:"available_stock"`
		Authors         []string `json:"authors"`
		Categories      []string `json:"categories"`
	}

	BookSearchFilter struct {
		Query    string `json:"q,omitempty"`
		Author   string `json:"author,omitempty"`
		Category string `json:"category,omitempty"`
		MinPrice *int64 `json:"min_price,omitempty"`
		MaxPrice *int64 `json:"max_price,omitempty"`
		Language string `json:"language,omitempty"`
		InStock  *bool  `json:"in_stock,omitempty"`
		Sort     string `json:"sort,omitempty"`
		Cursor   string `json:"cursor,omitempty"`
		Limit    int    `json:"limit,omitempty"`
	}

	BookSearchResult struct {
		Books      []CatalogBook `json:"books"`
		NextCursor string        `json:"next_cursor,omitempty"`
	}

	catalogBookRow struct {
		BookID          int64         `db:"book_id"`
		ISBN            string        `db:"isbn"`
		Title           string        `db:"title"`
		Subtitle        string        `db:"subtitle"`
		Description     string        `db:"description"`
		Publisher       string        `db:"publisher"`
		PublicationDate string        `db:"publication_date"`
		LanguageCode    string        `db:"language_code"`
		PageCount       sql.NullInt64 `db:"page_count"`
		CoverURL        string        `db:"cover_url"`
		Price           int64         `db:"price"`
		Currency        string        `db:"currency"`
		Status          string        `db:"status"`
		Version         uint64        `db:"version"`
		AvailableStock  int64         `db:"available_stock"`
		AuthorsHex      string        `db:"authors_hex"`
		CategoriesHex   string        `db:"categories_hex"`
		Relevance       float64       `db:"relevance"`
	}
)

var catalogSelectColumns = `b.book_id AS book_id,
	b.isbn AS isbn,
	b.title AS title,
	COALESCE(b.subtitle, '') AS subtitle,
	COALESCE(b.description, '') AS description,
	COALESCE(b.publisher, '') AS publisher,
	COALESCE(DATE_FORMAT(b.publication_date, '%Y-%m-%d'), '') AS publication_date,
	b.language_code AS language_code,
	b.page_count AS page_count,
	COALESCE(b.cover_url, '') AS cover_url,
	b.price AS price,
	b.currency AS currency,
	b.status AS status,
	b.version AS version,
	COALESCE(i.available_stock, 0) AS available_stock,
	COALESCE(GROUP_CONCAT(DISTINCT HEX(a.name) ORDER BY ba.sort_order, a.author_id SEPARATOR ','), '') AS authors_hex,
	COALESCE(GROUP_CONCAT(DISTINCT HEX(c.name) ORDER BY c.name SEPARATOR ','), '') AS categories_hex`

func (m *defaultBookModel) GetCatalogBook(ctx context.Context, identifier string) (*CatalogBook, error) {
	identifier = strings.TrimSpace(identifier)
	if identifier == "" {
		return nil, fmt.Errorf("book_id or isbn is required")
	}
	bookID, _ := strconv.ParseInt(identifier, 10, 64)
	cacheKey := catalogCacheKey(bookID, identifier)

	var cached CatalogBook
	if err := m.GetCacheCtx(ctx, cacheKey, &cached); err == nil {
		return &cached, nil
	}

	query := fmt.Sprintf(`SELECT %s, 0 AS relevance
		FROM book b
		LEFT JOIN inventory i ON i.book_id = b.book_id
		LEFT JOIN book_author ba ON ba.book_id = b.book_id
		LEFT JOIN author a ON a.author_id = ba.author_id
		LEFT JOIN book_category bc ON bc.book_id = b.book_id
		LEFT JOIN category c ON c.category_id = bc.category_id AND c.status = 'ACTIVE'
		WHERE (b.book_id = ? OR b.isbn = ?) AND b.status = 'ACTIVE'
		GROUP BY b.book_id, b.isbn, b.title, b.subtitle, b.description, b.publisher,
			b.publication_date, b.language_code, b.page_count, b.cover_url, b.price,
			b.currency, b.status, b.version, i.available_stock
		LIMIT 1`, catalogSelectColumns)

	var row catalogBookRow
	if err := m.QueryRowNoCacheCtx(ctx, &row, query, bookID, identifier); err != nil {
		if errors.Is(err, sqlc.ErrNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	book, err := row.toCatalogBook()
	if err != nil {
		return nil, err
	}
	if err := m.SetCacheCtx(ctx, cacheKey, book); err != nil {
		logx.WithContext(ctx).Errorf("failed to cache catalog book %d: %v", book.BookID, err)
	}
	return book, nil
}

func (m *defaultBookModel) SearchBooks(ctx context.Context, filter BookSearchFilter) (*BookSearchResult, error) {
	normalized, offset, err := normalizeBookSearchFilter(filter)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidBookSearch, err)
	}
	cacheKey, err := bookSearchCacheKey(normalized, offset)
	if err != nil {
		return nil, err
	}
	var cached BookSearchResult
	if err := m.GetCacheCtx(ctx, cacheKey, &cached); err == nil {
		return &cached, nil
	}

	query, args := buildBookSearchQuery(normalized, offset)
	var rows []catalogBookRow
	if err := m.QueryRowsNoCacheCtx(ctx, &rows, query, args...); err != nil {
		return nil, err
	}

	hasNext := len(rows) > normalized.Limit
	if hasNext {
		rows = rows[:normalized.Limit]
	}
	books := make([]CatalogBook, 0, len(rows))
	for _, row := range rows {
		book, err := row.toCatalogBook()
		if err != nil {
			return nil, err
		}
		books = append(books, *book)
	}
	result := &BookSearchResult{Books: books}
	if hasNext {
		result.NextCursor = encodeBookSearchCursor(offset + normalized.Limit)
	}
	if err := m.SetCacheWithExpireCtx(ctx, cacheKey, result, bookSearchCacheTTL); err != nil {
		logx.WithContext(ctx).Errorf("failed to cache book search: %v", err)
	}
	return result, nil
}

func normalizeBookSearchFilter(filter BookSearchFilter) (BookSearchFilter, int, error) {
	filter.Query = strings.ToLower(strings.TrimSpace(filter.Query))
	filter.Author = strings.ToLower(strings.TrimSpace(filter.Author))
	filter.Category = strings.ToLower(strings.TrimSpace(filter.Category))
	filter.Language = strings.ToLower(strings.TrimSpace(filter.Language))
	filter.Sort = strings.ToLower(strings.TrimSpace(filter.Sort))
	if filter.Sort == "" {
		filter.Sort = "relevance"
	}
	if filter.Limit == 0 {
		filter.Limit = defaultBookSearchLimit
	}
	if filter.Limit < 1 || filter.Limit > maxBookSearchLimit {
		return BookSearchFilter{}, 0, fmt.Errorf("limit must be between 1 and %d", maxBookSearchLimit)
	}
	if filter.MinPrice != nil && *filter.MinPrice < 0 {
		return BookSearchFilter{}, 0, fmt.Errorf("min_price must not be negative")
	}
	if filter.MaxPrice != nil && *filter.MaxPrice < 0 {
		return BookSearchFilter{}, 0, fmt.Errorf("max_price must not be negative")
	}
	if filter.MinPrice != nil && filter.MaxPrice != nil && *filter.MinPrice > *filter.MaxPrice {
		return BookSearchFilter{}, 0, fmt.Errorf("min_price must not exceed max_price")
	}
	switch filter.Sort {
	case "relevance", "price_asc", "price_desc", "newest":
	default:
		return BookSearchFilter{}, 0, fmt.Errorf("invalid sort %q", filter.Sort)
	}
	offset, err := decodeBookSearchCursor(filter.Cursor)
	if err != nil {
		return BookSearchFilter{}, 0, err
	}
	filter.Cursor = ""
	return filter, offset, nil
}

func buildBookSearchQuery(filter BookSearchFilter, offset int) (string, []any) {
	where := []string{"b.status = 'ACTIVE'"}
	args := make([]any, 0, 12)
	relevance := "0"
	if filter.Query != "" {
		relevance = "MATCH(b.title, b.subtitle, b.description) AGAINST (? IN NATURAL LANGUAGE MODE)"
		// MySQL's default token parser misses mixed Chinese/Latin titles such
		// as Python入门. Keep full-text ranking and add literal text matching.
		// A model may separate two relevant terms with a space ("分布式 基础");
		// require every literal term rather than looking for that exact spaced
		// phrase in a Chinese title or description.
		literalClauses, literalArgs := literalBookSearchClauses(filter.Query)
		where = append(where, "("+relevance+" > 0 OR ("+strings.Join(literalClauses, " AND ")+"))")
		// The expression occurs once in SELECT and once in WHERE. SQL
		// placeholders are positional, so both values must be supplied first.
		args = append(args, filter.Query, filter.Query)
		args = append(args, literalArgs...)
	}
	if filter.Author != "" {
		where = append(where, `EXISTS (
			SELECT 1 FROM book_author fba
			JOIN author fa ON fa.author_id = fba.author_id
			WHERE fba.book_id = b.book_id AND LOWER(fa.name) = ?)`)
		args = append(args, filter.Author)
	}
	if filter.Category != "" {
		where = append(where, `EXISTS (
			SELECT 1 FROM book_category fbc
			JOIN category fc ON fc.category_id = fbc.category_id
			WHERE fbc.book_id = b.book_id AND fc.status = 'ACTIVE'
				AND (LOWER(fc.slug) = ? OR LOWER(fc.name) = ?))`)
		args = append(args, filter.Category, filter.Category)
	}
	if filter.MinPrice != nil {
		where = append(where, "b.price >= ?")
		args = append(args, *filter.MinPrice)
	}
	if filter.MaxPrice != nil {
		where = append(where, "b.price <= ?")
		args = append(args, *filter.MaxPrice)
	}
	if filter.Language != "" {
		where = append(where, "LOWER(b.language_code) = ?")
		args = append(args, filter.Language)
	}
	if filter.InStock != nil {
		if *filter.InStock {
			where = append(where, "COALESCE(i.available_stock, 0) > 0")
		} else {
			where = append(where, "COALESCE(i.available_stock, 0) = 0")
		}
	}

	order := "relevance DESC, b.book_id ASC"
	switch filter.Sort {
	case "price_asc":
		order = "b.price ASC, b.book_id ASC"
	case "price_desc":
		order = "b.price DESC, b.book_id ASC"
	case "newest":
		order = "b.publication_date DESC, b.book_id DESC"
	}
	query := fmt.Sprintf(`SELECT %s, %s AS relevance
		FROM book b
		LEFT JOIN inventory i ON i.book_id = b.book_id
		LEFT JOIN book_author ba ON ba.book_id = b.book_id
		LEFT JOIN author a ON a.author_id = ba.author_id
		LEFT JOIN book_category bc ON bc.book_id = b.book_id
		LEFT JOIN category c ON c.category_id = bc.category_id AND c.status = 'ACTIVE'
		WHERE %s
		GROUP BY b.book_id, b.isbn, b.title, b.subtitle, b.description, b.publisher,
			b.publication_date, b.language_code, b.page_count, b.cover_url, b.price,
			b.currency, b.status, b.version, i.available_stock
		ORDER BY %s
		LIMIT ? OFFSET ?`, catalogSelectColumns, relevance, strings.Join(where, " AND "), order)
	args = append(args, filter.Limit+1, offset)
	return query, args
}

func literalBookSearchClauses(query string) ([]string, []any) {
	terms := strings.Fields(query)
	if len(terms) == 0 {
		terms = []string{query}
	}
	clauses := make([]string, 0, len(terms))
	args := make([]any, 0, len(terms)*3)
	for _, term := range terms {
		literal := "%" + strings.NewReplacer("=", "==", "%", "=%", "_", "=_").Replace(term) + "%"
		clauses = append(clauses, "(LOWER(b.title) LIKE ? ESCAPE '=' OR LOWER(COALESCE(b.subtitle, '')) LIKE ? ESCAPE '=' OR LOWER(COALESCE(b.description, '')) LIKE ? ESCAPE '=')")
		args = append(args, literal, literal, literal)
	}
	return clauses, args
}

func (row catalogBookRow) toCatalogBook() (*CatalogBook, error) {
	authors, err := decodeHexList(row.AuthorsHex)
	if err != nil {
		return nil, fmt.Errorf("decode authors for book %d: %w", row.BookID, err)
	}
	categories, err := decodeHexList(row.CategoriesHex)
	if err != nil {
		return nil, fmt.Errorf("decode categories for book %d: %w", row.BookID, err)
	}
	var pageCount *int64
	if row.PageCount.Valid {
		value := row.PageCount.Int64
		pageCount = &value
	}
	return &CatalogBook{
		BookID:          row.BookID,
		ISBN:            row.ISBN,
		Title:           row.Title,
		Subtitle:        row.Subtitle,
		Description:     row.Description,
		Publisher:       row.Publisher,
		PublicationDate: row.PublicationDate,
		LanguageCode:    row.LanguageCode,
		PageCount:       pageCount,
		CoverURL:        row.CoverURL,
		Price:           row.Price,
		Currency:        row.Currency,
		Status:          row.Status,
		Version:         row.Version,
		AvailableStock:  row.AvailableStock,
		Authors:         authors,
		Categories:      categories,
	}, nil
}

func decodeHexList(value string) ([]string, error) {
	if value == "" {
		return []string{}, nil
	}
	parts := strings.Split(value, ",")
	result := make([]string, 0, len(parts))
	for _, part := range parts {
		decoded, err := hex.DecodeString(part)
		if err != nil {
			return nil, err
		}
		result = append(result, string(decoded))
	}
	return result, nil
}

func catalogCacheKey(bookID int64, identifier string) string {
	if bookID > 0 {
		return fmt.Sprintf("%sid#%d", catalogCachePrefix, bookID)
	}
	return fmt.Sprintf("%sisbn#%s", catalogCachePrefix, strings.ToLower(identifier))
}

func bookSearchCacheKey(filter BookSearchFilter, offset int) (string, error) {
	payload, err := json.Marshal(struct {
		Filter BookSearchFilter `json:"filter"`
		Offset int              `json:"offset"`
	}{Filter: filter, Offset: offset})
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(payload)
	return searchCachePrefix + hex.EncodeToString(digest[:]), nil
}

func encodeBookSearchCursor(offset int) string {
	return base64.RawURLEncoding.EncodeToString([]byte(strconv.Itoa(offset)))
}

func decodeBookSearchCursor(cursor string) (int, error) {
	cursor = strings.TrimSpace(cursor)
	if cursor == "" {
		return 0, nil
	}
	decoded, err := base64.RawURLEncoding.DecodeString(cursor)
	if err != nil {
		return 0, fmt.Errorf("invalid cursor")
	}
	offset, err := strconv.Atoi(string(decoded))
	if err != nil || offset < 0 {
		return 0, fmt.Errorf("invalid cursor")
	}
	return offset, nil
}
