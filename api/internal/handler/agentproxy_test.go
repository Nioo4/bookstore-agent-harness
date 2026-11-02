package handler

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"bookstore/api/internal/agentproxy"
	"bookstore/api/internal/config"
	"bookstore/api/internal/svc"
	"bookstore/rpc/auth/jwtauth"

	"github.com/golang-jwt/jwt/v4"
)

func TestAgentProxyAllowlistCookieIdentityAndGuestChat(t *testing.T) {
	var mu sync.Mutex
	var lastAuthorization, lastCookie string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		lastAuthorization = r.Header.Get("Authorization")
		lastCookie = r.Header.Get("Cookie")
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer upstream.Close()

	ctx, token := newAgentProxyTestContext(t, upstream.URL)
	handler := AgentProxyHandler(ctx)

	protected := httptest.NewRequest(http.MethodGet, "/api/memories", nil)
	protected.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
	protected.Header.Set("Authorization", "Bearer browser-header-must-not-win")
	protectedResponse := httptest.NewRecorder()
	handler(protectedResponse, protected)
	if protectedResponse.Code != http.StatusOK {
		t.Fatalf("protected proxy status=%d body=%s", protectedResponse.Code, protectedResponse.Body.String())
	}
	mu.Lock()
	authHeader, cookieHeader := lastAuthorization, lastCookie
	mu.Unlock()
	if authHeader != "Bearer "+token || cookieHeader != "" {
		t.Fatalf("upstream identity headers authorization=%q cookie=%q", authHeader, cookieHeader)
	}

	unauthenticatedHeader := httptest.NewRequest(http.MethodGet, "/api/memories", nil)
	unauthenticatedHeader.Header.Set("Authorization", "Bearer browser-header-must-not-be-trusted")
	unauthenticatedResponse := httptest.NewRecorder()
	handler(unauthenticatedResponse, unauthenticatedHeader)
	if unauthenticatedResponse.Code != http.StatusUnauthorized {
		t.Fatalf("browser Authorization without cookie status=%d body=%s", unauthenticatedResponse.Code, unauthenticatedResponse.Body.String())
	}

	guest := httptest.NewRequest(http.MethodPost, "/api/chat/conversations/guest-1/messages", strings.NewReader(`{"content":"hello"}`))
	guest.Header.Set("Authorization", "Bearer browser-header-must-be-ignored")
	guestResponse := httptest.NewRecorder()
	handler(guestResponse, guest)
	if guestResponse.Code != http.StatusOK {
		t.Fatalf("guest chat status=%d body=%s", guestResponse.Code, guestResponse.Body.String())
	}
	mu.Lock()
	authHeader, cookieHeader = lastAuthorization, lastCookie
	mu.Unlock()
	if authHeader != "" || cookieHeader != "" {
		t.Fatalf("guest credentials leaked upstream authorization=%q cookie=%q", authHeader, cookieHeader)
	}

	for _, path := range []string{"/api/traces/run-1/replays", "/api/evals", "/internal/tools", "/api/internal/tools"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		response := httptest.NewRecorder()
		handler(response, req)
		if response.Code != http.StatusNotFound {
			t.Fatalf("path %s status=%d; want 404", path, response.Code)
		}
	}
}

func TestMatchAgentRouteCoversOnlyV2PublicPaths(t *testing.T) {
	allowed := []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/api/chat/conversations/c-1/messages"},
		{http.MethodPost, "/api/chat/conversations/c-1/confirmations"},
		{http.MethodGet, "/api/memories"},
		{http.MethodPost, "/api/memories/preferences"},
		{http.MethodDelete, "/api/memories/m-1"},
		{http.MethodDelete, "/api/memories"},
		{http.MethodPost, "/api/tasks/inventory-watches"},
		{http.MethodGet, "/api/tasks"},
		{http.MethodGet, "/api/tasks/t-1"},
		{http.MethodPost, "/api/tasks/t-1/cancel"},
		{http.MethodGet, "/api/notifications"},
		{http.MethodPost, "/api/notifications/n-1/read"},
		{http.MethodGet, "/api/traces"},
		{http.MethodGet, "/api/traces/r-1"},
		{http.MethodDelete, "/api/traces/r-1"},
	}
	for _, route := range allowed {
		if _, ok := matchAgentRoute(route.method, route.path); !ok {
			t.Errorf("allowlist rejected %s %s", route.method, route.path)
		}
	}
	for _, route := range []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/api/evals"},
		{http.MethodGet, "/internal/tools"},
		{http.MethodGet, "/api/internal/tools"},
		{http.MethodPost, "/api/traces/r-1/replays"},
		{http.MethodGet, "/api/traces/r-1/replays"},
		{http.MethodPost, "/api/chat/conversations/c-1/messages/extra"},
		{http.MethodGet, "/api/tasks/t-1/cancel"},
	} {
		if _, ok := matchAgentRoute(route.method, route.path); ok {
			t.Errorf("allowlist accepted forbidden %s %s", route.method, route.path)
		}
	}
}

func TestAgentProxyRejectsCrossSiteStateChange(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTeapot)
	}))
	defer upstream.Close()
	ctx, _ := newAgentProxyTestContext(t, upstream.URL)
	handler := withBrowserOrigin(config.Config{AppBaseURL: "http://localhost:8088"}, AgentProxyHandler(ctx))
	req := httptest.NewRequest(http.MethodPost, "/api/chat/conversations/guest-1/messages", strings.NewReader(`{"content":"hello"}`))
	req.Header.Set("Origin", "https://attacker.example")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	if response.Code != http.StatusForbidden || !strings.Contains(response.Body.String(), `"code":"REQUEST_ORIGIN_DENIED"`) {
		t.Fatalf("cross-site proxy request status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestAgentProxyStreamsSSEChunksAndPreservesUpstreamErrors(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/chat/conversations/c-1/messages" {
			w.Header().Set("Content-Type", "text/event-stream")
			flusher := w.(http.Flusher)
			_, _ = io.WriteString(w, "data: one\n\n")
			flusher.Flush()
			time.Sleep(30 * time.Millisecond)
			_, _ = io.WriteString(w, "data: two\n\n")
			flusher.Flush()
			return
		}
		w.Header().Set("X-Upstream-Error", "preserved")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = io.WriteString(w, `{"error":{"code":"UPSTREAM_LIMIT"}}`)
	}))
	defer upstream.Close()
	ctx, token := newAgentProxyTestContext(t, upstream.URL)
	handler := AgentProxyHandler(ctx)

	sseRequest := httptest.NewRequest(http.MethodPost, "/api/chat/conversations/c-1/messages", strings.NewReader(`{"content":"stream"}`))
	sseRequest.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
	sseResponse := &flushRecorder{header: make(http.Header)}
	handler(sseResponse, sseRequest)
	if sseResponse.status != http.StatusOK || sseResponse.flushes < 3 || sseResponse.Body.String() != "data: one\n\ndata: two\n\n" || sseResponse.Header().Get("X-Accel-Buffering") != "no" {
		t.Fatalf("SSE status=%d flushes=%d headers=%v body=%q", sseResponse.status, sseResponse.flushes, sseResponse.Header(), sseResponse.Body.String())
	}

	errorRequest := httptest.NewRequest(http.MethodGet, "/api/traces", nil)
	errorRequest.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
	errorResponse := httptest.NewRecorder()
	handler(errorResponse, errorRequest)
	if errorResponse.Code != http.StatusTooManyRequests || errorResponse.Header().Get("X-Upstream-Error") != "preserved" || !strings.Contains(errorResponse.Body.String(), "UPSTREAM_LIMIT") {
		t.Fatalf("upstream error not preserved status=%d headers=%v body=%s", errorResponse.Code, errorResponse.Header(), errorResponse.Body.String())
	}
}

func TestAgentProxyClientCancellationReachesUpstream(t *testing.T) {
	started := make(chan struct{})
	cancelled := make(chan struct{})
	transport := blockingRoundTripper{started: started, cancelled: cancelled}
	proxy, err := agentproxy.NewWithClient("http://localhost:8000", &http.Client{Transport: transport})
	if err != nil {
		t.Fatal(err)
	}
	ctx := &svc.ServiceContext{AgentProxy: proxy}
	handler := AgentProxyHandler(ctx)

	requestContext, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodPost, "/api/chat/conversations/cancel-1/messages", strings.NewReader(`{"content":"cancel"}`)).WithContext(requestContext)
	response := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		handler(response, request)
		close(done)
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("upstream request did not start")
	}
	cancel()
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("client cancellation did not reach upstream")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("proxy did not return after client cancellation")
	}
}

type blockingRoundTripper struct {
	started   chan struct{}
	cancelled chan struct{}
}

func (t blockingRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	close(t.started)
	<-request.Context().Done()
	close(t.cancelled)
	return nil, request.Context().Err()
}

func newAgentProxyTestContext(t *testing.T, upstreamURL string) (*svc.ServiceContext, string) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	token, err := jwt.NewWithClaims(jwt.SigningMethodEdDSA, jwt.MapClaims{
		"iss": "test", "sub": "7", "exp": time.Now().Add(time.Hour).Unix(),
		"email": "user@example.test", "display_name": "Alice", "email_verified": true,
	}).SignedString(private)
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Timeout: 2 * time.Second}
	proxy, err := agentproxy.NewWithClient(upstreamURL, client)
	if err != nil {
		t.Fatal(err)
	}
	return &svc.ServiceContext{AgentProxy: proxy, AuthVerifier: jwtauth.NewVerifier(public, "test")}, token
}

type flushRecorder struct {
	header  http.Header
	status  int
	Body    bytes.Buffer
	flushes int
}

func (r *flushRecorder) Header() http.Header { return r.header }

func (r *flushRecorder) WriteHeader(status int) {
	if r.status == 0 {
		r.status = status
	}
}

func (r *flushRecorder) Write(value []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	return r.Body.Write(value)
}

func (r *flushRecorder) Flush() { r.flushes++ }
