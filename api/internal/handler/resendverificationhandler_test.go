package handler

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"bookstore/api/internal/config"
	"bookstore/api/internal/svc"
	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/authenticator"
	"bookstore/rpc/auth/jwtauth"
	"github.com/golang-jwt/jwt/v4"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
)

func resendTestHandler(t *testing.T, rpc *fakeAuthenticator) (http.HandlerFunc, string) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	token, err := jwt.NewWithClaims(jwt.SigningMethodEdDSA, jwt.MapClaims{"iss": "test", "sub": "101", "exp": time.Now().Add(time.Hour).Unix(), "email_verified": false}).SignedString(private)
	if err != nil {
		t.Fatal(err)
	}
	return ResendVerificationHandler(&svc.ServiceContext{Authenticator: rpc, AuthVerifier: jwtauth.NewVerifier(public, "test"), Config: config.Config{TrustedProxyCIDRs: []string{"192.0.2.0/24"}}}), token
}

func TestResendHandlerStrictBodyAndIdentity(t *testing.T) {
	for _, body := range []string{`null`, `[]`, `{"email":"other@example.test"}`, `{"user_id":1}`, `{"token":"bad"}`, `{} {}`, ``, `{"unknown":null}`} {
		client := &fakeAuthenticator{}
		handler, token := resendTestHandler(t, client)
		request := httptest.NewRequest("POST", "/api/auth/resend-verification", strings.NewReader(body))
		request.Header.Set("Content-Type", "application/json")
		request.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
		response := httptest.NewRecorder()
		handler(response, request)
		if response.Code != 400 || client.resendReq != nil || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("invalid body crossed RPC boundary")
		}
	}
	for _, scenario := range []string{"query", "invalid JWT", "missing JWT"} {
		client := &fakeAuthenticator{}
		handler, token := resendTestHandler(t, client)
		path, want := "/api/auth/resend-verification", 401
		if scenario == "query" {
			path += "?user_id=1"
			want = 400
		}
		request := httptest.NewRequest("POST", path, strings.NewReader("{}"))
		request.Header.Set("Content-Type", "application/json")
		if scenario == "invalid JWT" {
			token = "invalid"
		}
		if scenario != "missing JWT" {
			request.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
		}
		response := httptest.NewRecorder()
		handler(response, request)
		if response.Code != want || client.resendReq != nil {
			t.Fatal("unauthorized or query request crossed RPC boundary")
		}
	}
}

func TestResendHandlerIgnoresBrowserAuthorizationHeader(t *testing.T) {
	client := &fakeAuthenticator{}
	handler, token := resendTestHandler(t, client)
	request := httptest.NewRequest("POST", "/api/auth/resend-verification", strings.NewReader("{}"))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+token)
	response := httptest.NewRecorder()
	handler(response, request)
	if response.Code != http.StatusUnauthorized || client.resendReq != nil {
		t.Fatalf("header-only browser identity status=%d crossed RPC=%t", response.Code, client.resendReq != nil)
	}
}

func TestResendHandlerResponseAndTrustedIP(t *testing.T) {
	for _, state := range []auth.ResendVerificationStatus{auth.ResendVerificationStatus_SMTP_ACCEPTED, auth.ResendVerificationStatus_DELIVERY_UNCONFIRMED, auth.ResendVerificationStatus_ALREADY_VERIFIED} {
		client := &fakeAuthenticator{resendResp: &authenticator.ResendVerificationResp{Status: state, VerificationEmailSent: state == auth.ResendVerificationStatus_SMTP_ACCEPTED}}
		handler, token := resendTestHandler(t, client)
		request := httptest.NewRequest("POST", "/api/auth/resend-verification", strings.NewReader("{}"))
		request.RemoteAddr = "198.51.100.9:1234" // Untrusted peer cannot spoof IP.
		request.Header.Set("Content-Type", "application/json")
		request.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
		request.Header.Set("X-Real-IP", "203.0.113.7")
		request.Header.Set("X-Forwarded-For", "203.0.113.8")
		response := httptest.NewRecorder()
		handler(response, request)
		want := 202
		if state == auth.ResendVerificationStatus_ALREADY_VERIFIED {
			want = 200
		}
		var payload map[string]any
		_ = json.Unmarshal(response.Body.Bytes(), &payload)
		if response.Code != want || len(payload) != 3 || payload["status"] != state.String() || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("invalid public response")
		}
		if client.resendReq == nil || client.resendReq.ClientIp != "198.51.100.9" || client.resendReq.AccessToken != token {
			t.Fatal("trusted context mapping failed")
		}
		if strings.Contains(response.Body.String(), token) {
			t.Fatal("JWT escaped into response")
		}
	}
}

func TestResendHandlerStableErrorInfoAndRetryAfter(t *testing.T) {
	for _, reason := range []string{"AUTH_RATE_LIMITED", "AUTH_REQUIRED", "DEPENDENCY_UNAVAILABLE"} {
		code, httpCode := codes.Unavailable, 503
		if reason == "AUTH_RATE_LIMITED" {
			code, httpCode = codes.ResourceExhausted, 429
		}
		if reason == "AUTH_REQUIRED" {
			code, httpCode = codes.Unauthenticated, 401
		}
		s, _ := status.New(code, "DO_NOT_EXPOSE_DEPENDENCY_REPLY").WithDetails(&errdetails.ErrorInfo{Reason: reason, Domain: "bookstore.auth"})
		if httpCode == 429 {
			s, _ = s.WithDetails(&errdetails.RetryInfo{RetryDelay: durationpb.New(61001 * time.Millisecond)})
		}
		client := &fakeAuthenticator{resendErr: s.Err()}
		handler, token := resendTestHandler(t, client)
		request := httptest.NewRequest("POST", "/api/auth/resend-verification", strings.NewReader("{}"))
		request.Header.Set("Content-Type", "application/json")
		request.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
		response := httptest.NewRecorder()
		handler(response, request)
		if response.Code != httpCode || !strings.Contains(response.Body.String(), reason) || strings.Contains(response.Body.String(), "DO_NOT_EXPOSE") || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("typed error contract failed")
		}
		if httpCode == 429 && response.Header().Get("Retry-After") != "62" {
			t.Fatal("RetryInfo not preserved/rounded upward")
		}
	}
}
