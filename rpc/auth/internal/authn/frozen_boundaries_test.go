package authn

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

func TestFixedAuthWindowRealRedisConcurrentQuota(t *testing.T) {
	address := os.Getenv("AUTH_TEST_REDIS_ADDR")
	if address == "" {
		t.Skip("AUTH_TEST_REDIS_ADDR required (dedicated local test Redis)")
	}
	if !strings.HasPrefix(address, "127.0.0.1:") {
		t.Fatal("local test Redis required")
	}
	limiter, err := NewRedisRateLimiter(address, "", 14)
	if err != nil {
		t.Fatal(err)
	}
	defer limiter.client.Close()
	for _, scope := range []string{"register_ip", "register_email", "login_ip", "login_email"} {
		t.Run(scope, func(t *testing.T) {
			limit := 5
			if strings.HasPrefix(scope, "login") {
				limit = 10
			}
			subject := fmt.Sprintf("frozen-test-%d-%s", time.Now().UnixNano(), scope)
			digest := sha256.Sum256([]byte(subject))
			key := "auth:rate:" + scope + ":" + hex.EncodeToString(digest[:])
			defer limiter.client.Del(t.Context(), key)
			start := make(chan struct{})
			var group sync.WaitGroup
			var allowed, rejected, failed atomic.Int32
			for range 20 {
				group.Add(1)
				go func() {
					defer group.Done()
					<-start
					ok, err := limiter.Allow(t.Context(), scope, subject, limit, 15*time.Minute)
					if err != nil {
						failed.Add(1)
					} else if ok {
						allowed.Add(1)
					} else {
						rejected.Add(1)
					}
				}()
			}
			close(start)
			group.Wait()
			if allowed.Load() != int32(limit) || rejected.Load() != int32(20-limit) || failed.Load() != 0 {
				t.Fatal("concurrent fixed-window admission was not atomic")
			}
			before, err := limiter.client.Do(t.Context(), "PEXPIRETIME", key).Int64()
			if err != nil || before <= time.Now().UnixMilli() {
				t.Fatal("admitted window missing expiry")
			}
			if ok, err := limiter.Allow(t.Context(), scope, subject, limit, 15*time.Minute); err != nil || ok {
				t.Fatal("over-limit request admitted")
			}
			after, err := limiter.client.Do(t.Context(), "PEXPIRETIME", key).Int64()
			count, countErr := limiter.client.Get(t.Context(), key).Int()
			if err != nil || countErr != nil || count != limit || before != after {
				t.Fatal("rejection changed quota or absolute expiry")
			}
			// Only this test's unique key is shortened; production's default was verified above.
			if err := limiter.client.PExpire(t.Context(), key, time.Millisecond).Err(); err != nil {
				t.Fatal(err)
			}
			deadline := time.Now().Add(time.Second)
			for {
				remaining, err := limiter.client.Exists(t.Context(), key).Result()
				if err != nil {
					t.Fatal(err)
				}
				if remaining == 0 {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("test window did not expire")
				}
				time.Sleep(time.Millisecond)
			}
			if ok, err := limiter.Allow(t.Context(), scope, subject, limit, 15*time.Minute); err != nil || !ok {
				t.Fatal("expired window did not admit a new request")
			}
		})
	}
}

func TestRegistrationPasswordFrozenBoundaries(t *testing.T) {
	for _, character := range []string{"x", "书"} {
		for _, length := range []int{7, 8, 128, 129} {
			t.Run(fmt.Sprintf("bytes-per-character-%d/characters-%d", len(character), length), func(t *testing.T) {
				store, mailer := &fakeStore{}, &fakeMailer{}
				service := newTestAuthService(t, store, mailer, &fakeLimiter{allowed: true})
				password := strings.Repeat(character, length)
				_, err := service.Register(t.Context(), "boundary@example.test", password, "Boundary", "test-ip")
				valid := length >= 8 && length <= 128
				if valid {
					if err != nil || !VerifyPassword(store.passwordHash, password) {
						t.Fatal("legal character-count boundary rejected")
					}
				} else if err == nil || store.passwordHash != "" {
					t.Fatal("invalid password length reached persistence")
				}
			})
		}
	}
}

func TestFixedAuthWindowFrozenLimitsAndRejection(t *testing.T) {
	for _, boundary := range []struct {
		scope string
		limit int
	}{{"register_ip", 5}, {"register_email", 5}, {"login_ip", 10}, {"login_email", 10}} {
		t.Run(boundary.scope, func(t *testing.T) {
			server := miniredis.RunT(t)
			limiter, err := NewRedisRateLimiter(server.Addr(), "", 0)
			if err != nil {
				t.Fatal(err)
			}
			defer limiter.client.Close()
			for range boundary.limit {
				if allowed, err := limiter.Allow(t.Context(), boundary.scope, "synthetic-subject", boundary.limit, 15*time.Minute); err != nil || !allowed {
					t.Fatal("request at or below the frozen quota rejected")
				}
			}
			key := server.Keys()[0]
			server.FastForward(time.Second)
			beforeCount, _ := server.Get(key)
			beforeTTL := server.TTL(key)
			for range 2 {
				if allowed, err := limiter.Allow(t.Context(), boundary.scope, "synthetic-subject", boundary.limit, 15*time.Minute); err != nil || allowed {
					t.Fatal("request above the frozen quota accepted")
				}
			}
			afterCount, _ := server.Get(key)
			if beforeCount != afterCount || beforeTTL != server.TTL(key) {
				t.Fatal("rejected request changed counter or TTL")
			}
			server.FastForward(beforeTTL)
			if allowed, err := limiter.Allow(t.Context(), boundary.scope, "synthetic-subject", boundary.limit, 15*time.Minute); err != nil || !allowed {
				t.Fatal("exact window expiration did not admit a new attempt")
			}
		})
	}
}

func TestResendFrozenIPThirtyThirtyOneBoundary(t *testing.T) {
	server := miniredis.RunT(t)
	limiter, _ := NewRedisRateLimiter(server.Addr(), "", 0)
	defer limiter.client.Close()
	for index := range 30 {
		if _, err := limiter.ReserveResend(t.Context(), "synthetic-ip", fmt.Sprintf("user-%d@example.test", index), resendTestPolicy); err != nil {
			t.Fatal(err)
		}
	}
	ipKey := resendKey("ip", "synthetic-ip")
	beforeTTL := server.TTL(ipKey)
	var limited *RateLimitError
	if _, err := limiter.ReserveResend(t.Context(), "synthetic-ip", "over-limit@example.test", resendTestPolicy); !errors.As(err, &limited) || limited.RetryAfterSeconds != 3600 {
		t.Fatal("31st distinct-email request did not hit the IP quota")
	}
	count, _ := server.Get(ipKey)
	if count != "30" || server.TTL(ipKey) != beforeTTL || server.Exists(resendKey("email", "over-limit@example.test")) {
		t.Fatal("denied IP attempt changed reservations")
	}
	server.FastForward(time.Hour)
	if _, err := limiter.ReserveResend(t.Context(), "synthetic-ip", "after-window@example.test", resendTestPolicy); err != nil {
		t.Fatal(err)
	}
}
