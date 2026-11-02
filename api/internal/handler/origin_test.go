package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"bookstore/api/internal/config"
)

func TestWithBrowserOriginAllowsSameOriginAndRejectsAmbiguousWrites(t *testing.T) {
	called := 0
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called++
		w.WriteHeader(http.StatusNoContent)
	})
	handler := withBrowserOrigin(config.Config{AppBaseURL: "http://localhost:8088"}, next)

	tests := []struct {
		name       string
		method     string
		origin     string
		fetchSite  string
		wantStatus int
	}{
		{name: "matching origin", method: http.MethodPost, origin: "http://localhost:8088", wantStatus: http.StatusNoContent},
		{name: "missing origin proven same origin", method: http.MethodPost, fetchSite: "same-origin", wantStatus: http.StatusNoContent},
		{name: "read only without origin", method: http.MethodGet, wantStatus: http.StatusNoContent},
		{name: "missing origin", method: http.MethodPost, wantStatus: http.StatusForbidden},
		{name: "cross site", method: http.MethodPost, origin: "https://attacker.example", fetchSite: "cross-site", wantStatus: http.StatusForbidden},
		{name: "same site is not same origin proof", method: http.MethodPost, fetchSite: "same-site", wantStatus: http.StatusForbidden},
		{name: "port mismatch", method: http.MethodPost, origin: "http://localhost:8080", wantStatus: http.StatusForbidden},
		{name: "path in origin", method: http.MethodPost, origin: "http://localhost:8088/app", wantStatus: http.StatusForbidden},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(test.method, "/api/auth/login", strings.NewReader(`{}`))
			if test.origin != "" {
				req.Header.Set("Origin", test.origin)
			}
			if test.fetchSite != "" {
				req.Header.Set("Sec-Fetch-Site", test.fetchSite)
			}
			recorder := httptest.NewRecorder()
			handler.ServeHTTP(recorder, req)
			if recorder.Code != test.wantStatus {
				t.Fatalf("status=%d body=%s want=%d", recorder.Code, recorder.Body.String(), test.wantStatus)
			}
			if test.wantStatus == http.StatusForbidden && !strings.Contains(recorder.Body.String(), `"code":"REQUEST_ORIGIN_DENIED"`) {
				t.Fatalf("missing stable origin error: %s", recorder.Body.String())
			}
		})
	}
	if called != 3 {
		t.Fatalf("next called %d times; want 3", called)
	}
}

func TestSameOriginRejectsMalformedExpectedOrigin(t *testing.T) {
	for _, expected := range []string{"", "not-a-url", "ftp://localhost:8088", "http://:8088"} {
		if sameOrigin("http://localhost:8088", expected) {
			t.Fatalf("sameOrigin accepted invalid expected origin %q", expected)
		}
	}
}
