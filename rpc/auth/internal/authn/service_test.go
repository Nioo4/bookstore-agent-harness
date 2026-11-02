package authn

import (
	"bookstore/rpc/auth/jwtauth"
	"context"
	"database/sql"
	"errors"
	"strings"
	"testing"
	"time"
)

type fakeStore struct {
	email, displayName, passwordHash string
	digest                           []byte
	expiresAt                        time.Time
	registerErr                      error
	verifyDigest                     []byte
	verifyErr                        error
	user                             *User
	findErr                          error
}

func (s *fakeStore) Register(_ context.Context, email, displayName, passwordHash string, digest []byte, expiresAt time.Time) (int64, error) {
	s.email, s.displayName, s.passwordHash, s.digest, s.expiresAt = email, displayName, passwordHash, append([]byte(nil), digest...), expiresAt
	return 101, s.registerErr
}
func (s *fakeStore) VerifyEmail(_ context.Context, digest []byte, _ time.Time) (int64, error) {
	s.verifyDigest = append([]byte(nil), digest...)
	return 101, s.verifyErr
}
func (s *fakeStore) FindByEmail(_ context.Context, _ string) (*User, error) { return s.user, s.findErr }
func (s *fakeStore) FindByID(context.Context, int64) (*User, error)         { return s.user, s.findErr }
func (s *fakeStore) CreateVerification(_ context.Context, _ int64, digest []byte, now time.Time) (*User, bool, error) {
	s.digest, s.expiresAt = append([]byte(nil), digest...), now.Add(verificationTTL)
	return s.user, true, s.registerErr
}

type fakeMailer struct {
	recipient, verificationURL string
	err                        error
	afterSend                  func()
	sends                      int
}

func (m *fakeMailer) SendVerification(_ context.Context, recipient, verificationURL string) error {
	m.recipient, m.verificationURL = recipient, verificationURL
	m.sends++
	if m.afterSend != nil {
		m.afterSend()
	}
	return m.err
}

type fakeLimiter struct {
	allowed bool
	err     error
	calls   []string
	reserve func() (time.Duration, error)
}

func (l *fakeLimiter) Allow(_ context.Context, scope, subject string, _ int, _ time.Duration) (bool, error) {
	l.calls = append(l.calls, scope+":"+subject)
	return l.allowed, l.err
}
func (l *fakeLimiter) ReserveResend(_ context.Context, _, _ string, _ RateLimitPolicy) (time.Duration, error) {
	l.calls = append(l.calls, "resend")
	if l.reserve != nil {
		return l.reserve()
	}
	if l.err != nil {
		return 0, l.err
	}
	if !l.allowed {
		return 0, &RateLimitError{RetryAfterSeconds: 60}
	}
	return time.Minute, nil
}

type fakeVerifier struct{ err error }

func (v fakeVerifier) Verify(string) (jwtauth.UserContext, error) {
	return jwtauth.UserContext{UserID: 101}, v.err
}

type fakeSigner struct{ token string }

func (s *fakeSigner) Sign(User, time.Time) (string, time.Duration, error) {
	return s.token, AccessTokenTTL, nil
}

func newTestAuthService(t *testing.T, store *fakeStore, mailer *fakeMailer, limiter *fakeLimiter) *Service {
	t.Helper()
	service, err := NewService(store, mailer, limiter, &fakeSigner{token: "signed-token"}, fakeVerifier{}, "http://app.test", RateLimitPolicy{
		RegisterLimit: 5, LoginLimit: 10, Window: 15 * time.Minute,
		ResendIPLimit: 30, ResendEmailLimit: 5, ResendWindow: time.Hour, ResendCooldown: time.Minute,
	})
	if err != nil {
		t.Fatal(err)
	}
	service.now = func() time.Time { return time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC) }
	return service
}

func TestRegisterNormalizesEmailHashesPasswordAndSendsOneTimeToken(t *testing.T) {
	store, mailer, limiter := &fakeStore{}, &fakeMailer{}, &fakeLimiter{allowed: true}
	service := newTestAuthService(t, store, mailer, limiter)
	result, err := service.Register(t.Context(), " User@Example.COM ", "password123", " Alice ", "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if result.UserID != 101 || result.Email != "user@example.com" || !result.Sent {
		t.Fatalf("unexpected result: %+v", result)
	}
	if store.email != "user@example.com" || store.displayName != "Alice" || !VerifyPassword(store.passwordHash, "password123") {
		t.Fatalf("registration values not normalized/hashed: %+v", store)
	}
	if len(store.digest) != 32 || store.expiresAt.Sub(service.now()) != 24*time.Hour {
		t.Fatalf("invalid stored token metadata: digest=%d expiry=%s", len(store.digest), store.expiresAt)
	}
	if mailer.recipient != store.email || !strings.Contains(mailer.verificationURL, "/verify-email?token=") {
		t.Fatalf("verification email not built correctly: %+v", mailer)
	}
	raw := strings.Split(mailer.verificationURL, "token=")[1]
	if strings.Contains(mailer.verificationURL, string(store.digest)) || VerificationDigest(raw) != [32]byte(store.digest) {
		t.Fatal("raw verification token handling is incorrect")
	}
	if len(limiter.calls) != 2 {
		t.Fatalf("register must rate limit by IP and email: %v", limiter.calls)
	}
}

func TestRegistrationValidationAndRateLimit(t *testing.T) {
	service := newTestAuthService(t, &fakeStore{}, &fakeMailer{}, &fakeLimiter{allowed: true})
	for _, input := range []struct{ email, password, name string }{
		{"bad", "password123", "Alice"},
		{"a@example.com", "short", "Alice"},
		{"a@example.com", "password123", ""},
	} {
		if _, err := service.Register(t.Context(), input.email, input.password, input.name, "ip"); err == nil {
			t.Fatalf("invalid registration accepted: %+v", input)
		}
	}
	limited := newTestAuthService(t, &fakeStore{}, &fakeMailer{}, &fakeLimiter{allowed: false})
	if _, err := limited.Register(t.Context(), "a@example.com", "password123", "Alice", "ip"); !errors.Is(err, ErrRateLimited) {
		t.Fatalf("rate limit error=%v", err)
	}
}

func TestLoginAllowsUnverifiedUserButRejectsWrongPassword(t *testing.T) {
	hash, err := HashPassword("password123")
	if err != nil {
		t.Fatal(err)
	}
	store := &fakeStore{user: &User{UserID: 5, Email: "user@example.com", DisplayName: "Alice", PasswordHash: hash, EmailVerifiedAt: sql.NullTime{Valid: false}, Status: "ACTIVE"}}
	service := newTestAuthService(t, store, &fakeMailer{}, &fakeLimiter{allowed: true})
	result, err := service.Login(t.Context(), "USER@example.com", "password123", "ip")
	if err != nil || result.AccessToken != "signed-token" || result.ExpiresIn != 28800 {
		t.Fatalf("login result=%+v err=%v", result, err)
	}
	if _, err := service.Login(t.Context(), "user@example.com", "wrong-password", "ip"); !errors.Is(err, ErrInvalidCredentials) {
		t.Fatalf("wrong password error=%v", err)
	}
}

func TestVerifyEmailHashesRawTokenBeforeStore(t *testing.T) {
	store := &fakeStore{}
	service := newTestAuthService(t, store, &fakeMailer{}, &fakeLimiter{allowed: true})
	userID, err := service.VerifyEmail(t.Context(), "raw-token")
	if err != nil || userID != 101 {
		t.Fatalf("verify user=%d err=%v", userID, err)
	}
	want := VerificationDigest("raw-token")
	if string(store.verifyDigest) != string(want[:]) {
		t.Fatal("store did not receive SHA-256 token digest")
	}
}
