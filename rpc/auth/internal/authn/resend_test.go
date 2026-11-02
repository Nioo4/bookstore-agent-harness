package authn

import (
	"database/sql"
	"errors"
	"testing"
	"time"
)

func TestResendIdentityQuotaCommitAndSMTP(t *testing.T) {
	for _, name := range []string{"accepted", "unconfirmed", "invalid JWT", "missing account", "database unavailable", "already verified", "rate limited", "redis unavailable", "commit uncertain"} {
		t.Run(name, func(t *testing.T) {
			store := &fakeStore{user: &User{UserID: 101, Email: "owner@example.test", Status: "ACTIVE"}}
			mailer, limiter := &fakeMailer{}, &fakeLimiter{allowed: true}
			service := newTestAuthService(t, store, mailer, limiter)
			var wantErr error
			wantStatus, wantSend, wantReserve := "SMTP_ACCEPTED", true, true
			switch name {
			case "unconfirmed":
				mailer.err = errors.New("synthetic SMTP failure")
				wantStatus = "DELIVERY_UNCONFIRMED"
			case "invalid JWT":
				service.verifier = fakeVerifier{err: errors.New("invalid")}
				wantErr, wantSend, wantReserve = ErrAuthRequired, false, false
			case "missing account":
				store.findErr = ErrAuthRequired
				wantErr, wantSend, wantReserve = ErrAuthRequired, false, false
			case "database unavailable":
				store.findErr = ErrDependencyUnavailable
				wantErr, wantSend, wantReserve = ErrDependencyUnavailable, false, false
			case "already verified":
				store.user.EmailVerifiedAt = sql.NullTime{Time: service.now(), Valid: true}
				wantStatus, wantSend, wantReserve = "ALREADY_VERIFIED", false, false
			case "rate limited":
				limiter.allowed = false
				wantErr, wantSend = ErrRateLimited, false
			case "redis unavailable":
				limiter.err = ErrDependencyUnavailable
				wantErr, wantSend = ErrDependencyUnavailable, false
			case "commit uncertain":
				store.registerErr = ErrDependencyUnavailable
				wantErr, wantSend = ErrDependencyUnavailable, false
			}
			result, err := service.ResendVerification(t.Context(), "synthetic-access", "192.0.2.1")
			if !errors.Is(err, wantErr) {
				t.Fatalf("unexpected error category: %v", err)
			}
			if (mailer.recipient != "") != wantSend {
				t.Fatal("SMTP side effect boundary violated")
			}
			if (len(limiter.calls) > 0) != wantReserve {
				t.Fatal("quota boundary violated")
			}
			if wantErr == nil {
				if result.Status != wantStatus || result.Sent != (wantStatus == "SMTP_ACCEPTED") {
					t.Fatal("incorrect resend result")
				}
				if wantSend && (len(store.digest) != 32 || store.expiresAt.Sub(service.now()) != verificationTTL || mailer.recipient != store.user.Email) {
					t.Fatal("token lifetime or target invalid")
				}
				if wantStatus == "ALREADY_VERIFIED" && result.RetryAfterSeconds != 0 {
					t.Fatal("already verified consumed cooldown")
				}
			}
		})
	}
}

func TestCooldownCeiling(t *testing.T) {
	for d, want := range map[time.Duration]int64{-time.Second: 0, 0: 0, time.Nanosecond: 1, time.Second: 1, time.Second + time.Nanosecond: 2, 60 * time.Second: 60} {
		if ceilSeconds(d) != want {
			t.Fatal("cooldown must round upward")
		}
	}
}
