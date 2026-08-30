package authn

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"

	redis "github.com/redis/go-redis/v9"
)

type RateLimiter interface {
	Allow(ctx context.Context, scope, subject string, limit int, window time.Duration) (bool, error)
	ReserveResend(ctx context.Context, ip, email string, policy RateLimitPolicy) (time.Duration, error)
}

type RedisRateLimiter struct{ client *redis.Client }

func NewRedisRateLimiter(address, password string, db int) (*RedisRateLimiter, error) {
	if address == "" {
		return nil, fmt.Errorf("Redis address is required")
	}
	// An uncertain Lua response must never result in another reservation attempt.
	return &RedisRateLimiter{client: redis.NewClient(&redis.Options{Addr: address, Password: password, DB: db, MaxRetries: -1})}, nil
}

var fixedWindowScript = redis.NewScript(`
local previous = tonumber(redis.call('GET', KEYS[1]) or '0')
if previous >= tonumber(ARGV[2]) then return 0 end
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return 1
`)

func (l *RedisRateLimiter) Allow(ctx context.Context, scope, subject string, limit int, window time.Duration) (bool, error) {
	if limit <= 0 || window <= 0 {
		return false, fmt.Errorf("rate limit and window must be positive")
	}
	digest := sha256.Sum256([]byte(subject))
	key := "auth:rate:" + scope + ":" + hex.EncodeToString(digest[:])
	allowed, err := fixedWindowScript.Run(ctx, l.client, []string{key}, int64(window.Seconds()), limit).Int64()
	if err != nil {
		return false, err
	}
	return allowed == 1, nil
}

// All three checks and reservations happen in one Redis operation. Rejection
// never changes a counter or TTL. Windows begin at the first admitted attempt.
var resendScript = redis.NewScript(`
local retry = 0
for i = 1, 2 do
  local count = tonumber(redis.call('GET', KEYS[i]) or '0')
  if count >= tonumber(ARGV[i]) then
    retry = math.max(retry, redis.call('PTTL', KEYS[i]))
  end
end
retry = math.max(retry, redis.call('PTTL', KEYS[3]))
if retry > 0 then return {0, retry} end
for i = 1, 2 do
  local count = redis.call('INCR', KEYS[i])
  if count == 1 then redis.call('PEXPIRE', KEYS[i], ARGV[3]) end
end
redis.call('SET', KEYS[3], '1', 'PX', ARGV[4])
return {1, tonumber(ARGV[4])}
`)

func resendKey(scope, subject string) string {
	digest := sha256.Sum256([]byte(subject))
	return "auth:resend:" + scope + ":" + hex.EncodeToString(digest[:])
}

func ceilSeconds(d time.Duration) int64 {
	if d <= 0 {
		return 0
	}
	return int64((d + time.Second - 1) / time.Second)
}

func (l *RedisRateLimiter) ReserveResend(ctx context.Context, ip, email string, p RateLimitPolicy) (time.Duration, error) {
	px := p.ResendCooldown.Milliseconds()
	if px <= 0 || p.ResendWindow < p.ResendCooldown || p.ResendWindow > time.Hour || p.ResendIPLimit <= 0 || p.ResendEmailLimit <= 0 {
		return 0, ErrDependencyUnavailable
	}
	values, err := resendScript.Run(ctx, l.client, []string{resendKey("ip", ip), resendKey("email", email), resendKey("cooldown", email)},
		p.ResendIPLimit, p.ResendEmailLimit, p.ResendWindow.Milliseconds(), px).Int64Slice()
	if err != nil {
		return 0, ErrDependencyUnavailable
	}
	return parseResendReservation(values, px)
}

func parseResendReservation(values []int64, expectedPX int64) (time.Duration, error) {
	if len(values) != 2 || values[1] <= 0 || values[1] > int64(time.Hour/time.Millisecond) {
		return 0, ErrDependencyUnavailable
	}
	remaining := time.Duration(values[1]) * time.Millisecond
	if values[0] == 0 {
		return 0, &RateLimitError{RetryAfterSeconds: ceilSeconds(remaining)}
	}
	if values[0] != 1 || expectedPX <= 0 || values[1] != expectedPX {
		return 0, ErrDependencyUnavailable
	}
	return remaining, nil
}
