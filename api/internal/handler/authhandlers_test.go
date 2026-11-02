package handler

import (
	"context"
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
	"bookstore/rpc/auth/authenticator"
	"bookstore/rpc/auth/jwtauth"

	"github.com/golang-jwt/jwt/v4"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fakeAuthenticator struct {
	registerReq *authenticator.RegisterReq
	loginErr    error
	resendReq   *authenticator.ResendVerificationReq
	resendResp  *authenticator.ResendVerificationResp
	resendErr   error
}

func (f *fakeAuthenticator) ResendVerification(_ context.Context, req *authenticator.ResendVerificationReq, _ ...grpc.CallOption) (*authenticator.ResendVerificationResp, error) {
	f.resendReq = req
	return f.resendResp, f.resendErr
}

func (f *fakeAuthenticator) Register(_ context.Context, req *authenticator.RegisterReq, _ ...grpc.CallOption) (*authenticator.RegisterResp, error) {
	f.registerReq = req
	return &authenticator.RegisterResp{UserId: 1001, Email: "user@example.com", VerificationEmailSent: true}, nil
}
func (f *fakeAuthenticator) VerifyEmail(context.Context, *authenticator.VerifyEmailReq, ...grpc.CallOption) (*authenticator.VerifyEmailResp, error) {
	return &authenticator.VerifyEmailResp{UserId: 1001, EmailVerified: true}, nil
}
func (f *fakeAuthenticator) Login(context.Context, *authenticator.LoginReq, ...grpc.CallOption) (*authenticator.LoginResp, error) {
	if f.loginErr != nil {
		return nil, f.loginErr
	}
	return &authenticator.LoginResp{AccessToken: "token", ExpiresIn: 28800, User: &authenticator.AuthUser{UserId: 1001}}, nil
}

func TestRegisterHandlerReturnsAcceptedAndForwardsTrustedClientIP(t *testing.T) {
	client := &fakeAuthenticator{}
	handler := RegisterHandler(&svc.ServiceContext{Authenticator: client, Config: config.Config{TrustedProxyCIDRs: []string{"192.0.2.0/24"}}})
	req := httptest.NewRequest(http.MethodPost, "/api/auth/register", strings.NewReader(`{
		"email":"user@example.com","password":"password123","display_name":"Alice"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Real-IP", "203.0.113.7")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusAccepted {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if client.registerReq == nil || client.registerReq.ClientIp != "203.0.113.7" || client.registerReq.Password != "password123" {
		t.Fatalf("RPC request not mapped: %+v", client.registerReq)
	}
	if body := recorder.Body.String(); strings.Contains(body, "password123") || !strings.Contains(body, `"verification_email_sent":true`) {
		t.Fatalf("unsafe or incomplete response: %s", body)
	}
}

func TestLoginHandlerUsesUnifiedCredentialError(t *testing.T) {
	client := &fakeAuthenticator{loginErr: status.Error(codes.Unauthenticated, "invalid email or password")}
	handler := LoginHandler(&svc.ServiceContext{Authenticator: client})
	req := httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(`{"email":"user@example.com","password":"wrongpass"}`))
	req.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	if recorder.Code != http.StatusUnauthorized || !strings.Contains(recorder.Body.String(), `"code":"AUTH_INVALID_CREDENTIALS"`) {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestLoginHandlerSetsHttpOnlyCookieAndHidesAccessToken(t *testing.T) {
	client := &fakeAuthenticator{}
	handler := LoginHandler(&svc.ServiceContext{Authenticator: client, Config: config.Config{CookieSecure: false}})
	req := httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(`{"email":"user@example.com","password":"password123"}`))
	req.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	cookie := recorder.Result().Cookies()
	if len(cookie) != 1 || cookie[0].Name != accessCookieName || cookie[0].Value != "token" || !cookie[0].HttpOnly || cookie[0].Secure || cookie[0].SameSite != http.SameSiteLaxMode || cookie[0].MaxAge != 28800 {
		t.Fatalf("unexpected access cookie: %+v", cookie)
	}
	if body := recorder.Body.String(); strings.Contains(body, "access_token") || strings.Contains(body, "token") {
		t.Fatalf("access token escaped into login response: %s", body)
	}
}

func TestSessionHandlerReturnsMinimalUnauthenticatedAndAuthenticatedShapes(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	token, err := jwt.NewWithClaims(jwt.SigningMethodEdDSA, jwt.MapClaims{
		"iss": "test", "sub": "101", "exp": time.Now().Add(time.Hour).Unix(),
		"email": "private@example.test", "display_name": "Alice", "email_verified": true,
	}).SignedString(private)
	if err != nil {
		t.Fatal(err)
	}
	ctx := &svc.ServiceContext{AuthVerifier: jwtauth.NewVerifier(public, "test")}

	unauthenticated := httptest.NewRecorder()
	SessionHandler(ctx)(unauthenticated, httptest.NewRequest(http.MethodGet, "/api/session", nil))
	if unauthenticated.Code != http.StatusOK || unauthenticated.Header().Get("Cache-Control") != "no-store" || strings.TrimSpace(unauthenticated.Body.String()) != `{"authenticated":false,"user":null}` {
		t.Fatalf("unexpected unauthenticated session response: %q", unauthenticated.Body.String())
	}

	authenticatedRequest := httptest.NewRequest(http.MethodGet, "/api/session", nil)
	authenticatedRequest.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
	authenticated := httptest.NewRecorder()
	SessionHandler(ctx)(authenticated, authenticatedRequest)
	if authenticated.Code != http.StatusOK || authenticated.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("unexpected authenticated status=%d body=%s", authenticated.Code, authenticated.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(authenticated.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["authenticated"] != true || strings.Contains(authenticated.Body.String(), "private@example.test") || strings.Contains(authenticated.Body.String(), "101") || strings.Contains(authenticated.Body.String(), token) {
		t.Fatalf("session response leaked identity/token: %s", authenticated.Body.String())
	}
	user, ok := body["user"].(map[string]any)
	if !ok || user["display_name"] != "Alice" || user["email_verified"] != true || len(user) != 2 {
		t.Fatalf("unexpected authenticated user shape: %#v", body["user"])
	}
}

func TestLogoutHandlerClearsAccessCookie(t *testing.T) {
	recorder := httptest.NewRecorder()
	LogoutHandler(&svc.ServiceContext{Config: config.Config{CookieSecure: true}})(recorder, httptest.NewRequest(http.MethodPost, "/api/auth/logout", nil))
	if recorder.Code != http.StatusOK || strings.TrimSpace(recorder.Body.String()) != `{"ok":true}` {
		t.Fatalf("unexpected logout response status=%d body=%q", recorder.Code, recorder.Body.String())
	}
	cookies := recorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != accessCookieName || cookies[0].Value != "" || cookies[0].MaxAge >= 0 || !cookies[0].HttpOnly || !cookies[0].Secure || cookies[0].SameSite != http.SameSiteLaxMode {
		t.Fatalf("unexpected clearing cookie: %+v", cookies)
	}
}
