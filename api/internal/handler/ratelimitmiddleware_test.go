package handler

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

type fixedRateLimiter bool

func (l fixedRateLimiter) AllowCtx(context.Context) bool { return bool(l) }

func TestRateLimitMiddlewareRejectsWithoutCallingNext(t *testing.T) {
	called := false
	next := func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusNoContent)
	}
	recorder := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/books/1/stock/deduct", nil)
	NewRateLimitMiddleware(fixedRateLimiter(false)).Handle(next)(recorder, req)

	if recorder.Code != http.StatusTooManyRequests || called {
		t.Fatalf("status = %d, called = %v", recorder.Code, called)
	}
	if recorder.Header().Get("Retry-After") != "1" {
		t.Fatalf("Retry-After = %q; want 1", recorder.Header().Get("Retry-After"))
	}
}

func TestRateLimitMiddlewareAllowsRequest(t *testing.T) {
	called := false
	next := func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusNoContent)
	}
	recorder := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/books/1/stock/deduct", nil)
	NewRateLimitMiddleware(fixedRateLimiter(true)).Handle(next)(recorder, req)
	if recorder.Code != http.StatusNoContent || !called {
		t.Fatalf("status = %d, called = %v", recorder.Code, called)
	}
}

func TestRateLimitMiddlewareCanBeDisabled(t *testing.T) {
	called := false
	next := func(w http.ResponseWriter, _ *http.Request) { called = true }
	NewRateLimitMiddleware(nil).Handle(next)(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/", nil))
	if !called {
		t.Fatal("disabled limiter should call next")
	}
}
