package authn

import (
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

var resendTestPolicy = RateLimitPolicy{ResendIPLimit: 30, ResendEmailLimit: 5, ResendWindow: time.Hour, ResendCooldown: time.Minute}

func TestResendLuaWindowsAndRejectionDoNotExtendTTL(t *testing.T) {
	server := miniredis.RunT(t)
	limiter, _ := NewRedisRateLimiter(server.Addr(), "", 0)
	defer limiter.client.Close()
	email, ip := "window@example.test", "192.0.2.9"
	reserve := func() error { _, err := limiter.ReserveResend(t.Context(), ip, email, resendTestPolicy); return err }
	if err := reserve(); err != nil {
		t.Fatal(err)
	}
	server.FastForward(30*time.Second + time.Millisecond)
	var limited *RateLimitError
	if err := reserve(); !errors.As(err, &limited) || limited.RetryAfterSeconds != 30 {
		t.Fatal("remaining TTL not used")
	}
	if server.TTL(resendKey("email", email)) != time.Hour-30*time.Second-time.Millisecond {
		t.Fatal("rejection extended window")
	}
	if count, _ := server.Get(resendKey("email", email)); count != "1" {
		t.Fatal("rejection consumed quota")
	}
	server.FastForward(30*time.Second - time.Millisecond)
	if err := reserve(); err != nil {
		t.Fatal("exact cooldown boundary must allow")
	}
	for range 3 {
		server.FastForward(time.Minute)
		if err := reserve(); err != nil {
			t.Fatal(err)
		}
	}
	server.FastForward(time.Minute)
	if err := reserve(); !errors.As(err, &limited) || limited.RetryAfterSeconds != 3300 {
		t.Fatal("hourly email limit did not win over expired cooldown")
	}
	server.FastForward(3300 * time.Second)
	if err := reserve(); err != nil {
		t.Fatal("exact fixed window boundary must allow")
	}
	for _, key := range server.Keys() {
		if strings.Contains(key, email) || strings.Contains(key, ip) {
			t.Fatal("PII in rate key")
		}
	}
}

func TestResendLuaLongestLimitAndFailClosed(t *testing.T) {
	server := miniredis.RunT(t)
	limiter, _ := NewRedisRateLimiter(server.Addr(), "", 0)
	defer limiter.client.Close()
	p := resendTestPolicy
	p.ResendIPLimit = 1
	if _, err := limiter.ReserveResend(t.Context(), "ip", "one@example.test", p); err != nil {
		t.Fatal(err)
	}
	server.FastForward(time.Second)
	_, err := limiter.ReserveResend(t.Context(), "ip", "two@example.test", p)
	var limited *RateLimitError
	if !errors.As(err, &limited) || limited.RetryAfterSeconds != 3599 {
		t.Fatal("IP hourly TTL must be returned")
	}
	if server.Exists(resendKey("email", "two@example.test")) {
		t.Fatal("denied email acquired quota")
	}
	server.Close()
	if _, err := limiter.ReserveResend(t.Context(), "ip", "one@example.test", p); !errors.Is(err, ErrDependencyUnavailable) {
		t.Fatal("Redis failure did not fail closed")
	}
}

func TestResendRealRedisConcurrentReservations(t *testing.T) {
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
	suffix := fmt.Sprint(time.Now().UnixNano())
	ip, email := "test-ip-"+suffix, "redis-"+suffix+"@example.test"
	keys := []string{resendKey("ip", ip), resendKey("email", email), resendKey("cooldown", email)}
	defer limiter.client.Del(t.Context(), keys...)
	start := make(chan struct{})
	var wg sync.WaitGroup
	var allowed, rejected, failed atomic.Int32
	for range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, err := limiter.ReserveResend(t.Context(), ip, email, resendTestPolicy)
			if err == nil {
				allowed.Add(1)
			} else if errors.Is(err, ErrRateLimited) {
				rejected.Add(1)
			} else {
				failed.Add(1)
			}
		}()
	}
	close(start)
	wg.Wait()
	if allowed.Load() != 1 || rejected.Load() != 19 || failed.Load() != 0 {
		t.Fatal("concurrent Lua reservation was not atomic")
	}
	for _, key := range keys[:2] {
		count, err := limiter.client.Get(t.Context(), key).Int()
		if err != nil || count != 1 {
			t.Fatal("denied request altered counter")
		}
	}
}
