package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestWriteRPCErrorMapsInsufficientStockToConflict(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/books/1/stock/deduct", nil)
	recorder := httptest.NewRecorder()
	writeRPCError(recorder, req, status.Error(codes.FailedPrecondition, "insufficient stock"))

	if recorder.Code != http.StatusConflict {
		t.Fatalf("status = %d; want %d", recorder.Code, http.StatusConflict)
	}
	if body := recorder.Body.String(); !strings.Contains(body, "INVENTORY_INSUFFICIENT") {
		t.Fatalf("body = %s; want business error code", body)
	}
}

func TestWriteRPCErrorMapsFallbackOverloadToTooManyRequests(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/books/1", nil)
	recorder := httptest.NewRecorder()
	writeRPCError(recorder, req, status.Error(codes.ResourceExhausted, "cache fallback concurrency limit reached"))

	if recorder.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d; want %d", recorder.Code, http.StatusTooManyRequests)
	}
	if body := recorder.Body.String(); !strings.Contains(body, "DEPENDENCY_OVERLOADED") {
		t.Fatalf("body = %s; want fallback overload code", body)
	}
}

func TestWriteRPCErrorMapsProtectedEndpointUnauthenticatedToAuthRequired(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/orders", nil)
	recorder := httptest.NewRecorder()
	writeRPCError(recorder, req, status.Error(codes.Unauthenticated, "invalid access token"))

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d; want %d", recorder.Code, http.StatusUnauthorized)
	}
	if body := recorder.Body.String(); !strings.Contains(body, "AUTH_REQUIRED") {
		t.Fatalf("body = %s; want protected-resource authentication error code", body)
	}
}
