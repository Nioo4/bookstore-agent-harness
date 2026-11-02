package logic

import (
	"context"
	"errors"
	"testing"

	"bookstore/rpc/check/check"
	"bookstore/rpc/check/internal/svc"
	"bookstore/rpc/model"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fakeCatalogModel struct {
	filter model.BookSearchFilter
	result *model.BookSearchResult
	err    error
}

func (m *fakeCatalogModel) GetCatalogBook(context.Context, string) (*model.CatalogBook, error) {
	return nil, model.ErrNotFound
}

func (m *fakeCatalogModel) SearchBooks(_ context.Context, filter model.BookSearchFilter) (*model.BookSearchResult, error) {
	m.filter = filter
	return m.result, m.err
}

func TestSearchBooksLogicMapsRequestAndCatalogResult(t *testing.T) {
	minPrice := int64(1000)
	inStock := true
	catalog := &fakeCatalogModel{result: &model.BookSearchResult{
		Books: []model.CatalogBook{{
			BookID: 7, ISBN: "isbn-7", Title: "Systems", LanguageCode: "en",
			Price: 9900, Currency: "CNY", Status: "ACTIVE", AvailableStock: 3,
			Authors: []string{"Alice"}, Categories: []string{"Distributed"},
		}},
		NextCursor: "next",
	}}
	logic := NewSearchBooksLogic(t.Context(), &svc.ServiceContext{Catalog: catalog})
	resp, err := logic.SearchBooks(&check.SearchBooksReq{
		Q: "systems", MinPrice: &minPrice, InStock: &inStock, Sort: "price_asc", Limit: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	if catalog.filter.Query != "systems" || catalog.filter.MinPrice == nil || *catalog.filter.MinPrice != minPrice || catalog.filter.Limit != 10 {
		t.Fatalf("request not mapped: %+v", catalog.filter)
	}
	if len(resp.Books) != 1 || resp.Books[0].BookId != 7 || resp.NextCursor != "next" {
		t.Fatalf("response not mapped: %+v", resp)
	}
}

func TestSearchBooksLogicMapsValidationAndInternalErrors(t *testing.T) {
	tests := []struct {
		err  error
		code codes.Code
	}{
		{err: model.ErrInvalidBookSearch, code: codes.InvalidArgument},
		{err: errors.New("mysql failed"), code: codes.Internal},
	}
	for _, tt := range tests {
		catalog := &fakeCatalogModel{err: tt.err}
		logic := NewSearchBooksLogic(t.Context(), &svc.ServiceContext{Catalog: catalog})
		_, err := logic.SearchBooks(&check.SearchBooksReq{})
		if status.Code(err) != tt.code {
			t.Fatalf("error %v mapped to %v; want %v", tt.err, status.Code(err), tt.code)
		}
	}
}
