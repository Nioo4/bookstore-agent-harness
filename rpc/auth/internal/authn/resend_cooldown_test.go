package authn

import (
	"errors"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

func TestResendCooldownMonotonicBounds(t *testing.T) {
	for _, tc := range []struct {
		initial, elapsed time.Duration
		want             int64
	}{
		{time.Minute, 0, 60}, {time.Minute, -time.Second, 60},
		{time.Minute, time.Minute - time.Nanosecond, 1}, {time.Minute, time.Minute, 0},
		{time.Minute, time.Minute + time.Nanosecond, 0}, {time.Minute, 5250 * time.Millisecond, 55},
		{1500 * time.Millisecond, time.Millisecond, 2}, {1500 * time.Millisecond, 500 * time.Millisecond, 1},
	} {
		if got := cooldownHint(tc.initial, tc.elapsed); got != tc.want {
			t.Fatalf("cooldown hint %v/%v = %d, want %d", tc.initial, tc.elapsed, got, tc.want)
		}
	}
}

func TestResendMalformedReservationCannotCreateTokenOrSend(t *testing.T) {
	for _, values := range [][]int64{nil, {1}, {1, 60000, 1}, {2, 60000}, {-1, 60000},
		{1, 0}, {1, -1}, {1, 59999}, {1, 9223372036854775807}, {0, 0}, {0, -1}} {
		store := &fakeStore{user: &User{UserID: 101, Email: "synthetic@example.test", Status: "ACTIVE"}}
		mailer, limiter := &fakeMailer{}, &fakeLimiter{allowed: true}
		limiter.reserve = func() (time.Duration, error) { return parseResendReservation(values, 60000) }
		service := newTestAuthService(t, store, mailer, limiter)
		_, err := service.ResendVerification(t.Context(), "synthetic-access", "192.0.2.1")
		if !errors.Is(err, ErrDependencyUnavailable) || len(store.digest) != 0 || mailer.sends != 0 || len(limiter.calls) != 1 {
			t.Fatal("malformed reservation did not fail before token/SMTP without retry")
		}
	}
	if duration, err := parseResendReservation([]int64{1, 1500}, 1500); err != nil || duration != 1500*time.Millisecond {
		t.Fatal("valid exact PX rejected")
	}
	_, err := parseResendReservation([]int64{0, 61001}, 60000)
	var limited *RateLimitError
	if !errors.As(err, &limited) || limited.RetryAfterSeconds != 62 {
		t.Fatal("rejection must preserve atomic longest TTL")
	}
}

func TestResendTimerStartsAfterReservationAndIgnoresWallClock(t *testing.T) {
	for _, smtpError := range []bool{false, true} {
		for _, wallJump := range []time.Duration{-24 * time.Hour, 24 * time.Hour} {
			store := &fakeStore{user: &User{UserID: 101, Email: "synthetic@example.test", Status: "ACTIVE"}}
			mailer, limiter := &fakeMailer{}, &fakeLimiter{allowed: true}
			service := newTestAuthService(t, store, mailer, limiter)
			var monotonic time.Duration
			wall := service.now()
			service.now = func() time.Time { return wall }
			limiter.reserve = func() (time.Duration, error) { monotonic += 20 * time.Second; return time.Minute, nil }
			service.startElapsed = func() func() time.Duration {
				if monotonic != 20*time.Second || len(store.digest) != 0 {
					t.Fatal("timer did not start immediately after reservation")
				}
				start := monotonic
				return func() time.Duration { return monotonic - start }
			}
			mailer.afterSend = func() {
				monotonic += 5250 * time.Millisecond
				wall = wall.Add(wallJump)
				limiter.err = ErrDependencyUnavailable      // No post-SMTP Redis operation is permitted.
				service.policy.ResendCooldown = time.Second // Cannot replace the reserved C.
			}
			if smtpError {
				mailer.err = errors.New("synthetic unconfirmed SMTP")
			}
			result, err := service.ResendVerification(t.Context(), "synthetic-access", "192.0.2.1")
			if err != nil || result.Sent == smtpError || result.RetryAfterSeconds != 55 || mailer.sends != 1 || len(limiter.calls) != 1 || len(store.digest) != 32 {
				t.Fatal("known SMTP result or request-local cooldown lost")
			}
			want := "SMTP_ACCEPTED"
			if smtpError {
				want = "DELIVERY_UNCONFIRMED"
			}
			if result.Status != want {
				t.Fatal("SMTP status changed")
			}
		}
	}
}

func TestResendExpiredLocalHintDoesNotAuthorizeAnotherRequest(t *testing.T) {
	store := &fakeStore{user: &User{UserID: 101, Email: "synthetic@example.test", Status: "ACTIVE"}}
	mailer, limiter := &fakeMailer{}, &fakeLimiter{allowed: true}
	service := newTestAuthService(t, store, mailer, limiter)
	service.startElapsed = func() func() time.Duration { return func() time.Duration { return 2 * time.Minute } }
	first, err := service.ResendVerification(t.Context(), "synthetic-access", "192.0.2.1")
	if err != nil || first.RetryAfterSeconds != 0 {
		t.Fatal("exhausted hint must be zero")
	}
	limiter.allowed = false
	_, err = service.ResendVerification(t.Context(), "synthetic-access", "192.0.2.1")
	if !errors.Is(err, ErrRateLimited) || mailer.sends != 1 || len(limiter.calls) != 2 {
		t.Fatal("new request bypassed reservation")
	}
}

func TestResendUsesExactPXPrecisionAndSurvivesClosedRedisClientAfterSMTP(t *testing.T) {
	for _, smtpError := range []bool{false, true} {
		server := miniredis.RunT(t)
		limiter, _ := NewRedisRateLimiter(server.Addr(), "", 0)
		defer limiter.client.Close()
		if limiter.client.Options().MaxRetries != 0 {
			t.Fatal("uncertain reservation must not retry")
		}
		store := &fakeStore{user: &User{UserID: 101, Email: "synthetic@example.test", Status: "ACTIVE"}}
		mailer := &fakeMailer{}
		service := newTestAuthService(t, store, mailer, &fakeLimiter{allowed: true})
		service.limiter = limiter
		service.policy.ResendCooldown = 1500*time.Millisecond + 500*time.Microsecond
		service.startElapsed = func() func() time.Duration { return func() time.Duration { return 500 * time.Millisecond } }
		mailer.afterSend = func() { _ = limiter.client.Close() }
		if smtpError {
			mailer.err = errors.New("synthetic missing DATA acknowledgment")
		}
		result, err := service.ResendVerification(t.Context(), "synthetic-access", "192.0.2.1")
		if err != nil || result.Sent == smtpError || result.RetryAfterSeconds != 1 || mailer.sends != 1 {
			t.Fatal("post-SMTP Redis failure changed result")
		}
		if count, _ := server.Get(resendKey("email", store.user.Email)); count != "1" {
			t.Fatal("reserved quota was refunded or repeated")
		}
		if server.TTL(resendKey("cooldown", store.user.Email)) != 1500*time.Millisecond {
			t.Fatal("cooldown did not use actual PX precision")
		}
	}
}
