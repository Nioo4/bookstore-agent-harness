package handler

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"bookstore/api/internal/svc"
	"bookstore/rpc/check/checker"

	"google.golang.org/grpc"
)

type fakeSearchChecker struct {
	req *checker.SearchBooksReq
}

func (f *fakeSearchChecker) Check(context.Context, *checker.CheckReq, ...grpc.CallOption) (*checker.CheckResp, error) {
	return &checker.CheckResp{}, nil
}

func (f *fakeSearchChecker) SearchBooks(_ context.Context, req *checker.SearchBooksReq, _ ...grpc.CallOption) (*checker.SearchBooksResp, error) {
	f.req = req
	return &checker.SearchBooksResp{Books: []*checker.BookSummary{{
		BookId: 3, Isbn: "isbn-3", Title: "Distributed Systems", Price: 9900,
		Currency: "CNY", Status: "ACTIVE", AvailableStock: 2,
		Authors: []string{"Alice"}, Categories: []string{"Systems"},
	}}}, nil
}

func TestSearchBooksHandlerParsesFiltersAndReturnsSummary(t *testing.T) {
	client := &fakeSearchChecker{}
	handler := SearchBooksHandler(&svc.ServiceContext{Checker: client})
	req := httptest.NewRequest(http.MethodGet,
		"/api/books/search?q=distributed&max_price=10000&language=en&in_stock=true&sort=price_asc&limit=3", nil)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if client.req == nil || client.req.MaxPrice == nil || *client.req.MaxPrice != 10000 || client.req.InStock == nil || !*client.req.InStock {
		t.Fatalf("query parameters not parsed: %+v", client.req)
	}
	body := recorder.Body.String()
	for _, value := range []string{`"book_id":3`, `"in_stock":true`, `"Alice"`, `"Systems"`} {
		if !strings.Contains(body, value) {
			t.Fatalf("response %s missing %s", body, value)
		}
	}
}
