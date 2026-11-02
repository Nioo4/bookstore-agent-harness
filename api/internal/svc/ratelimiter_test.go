package svc

import (
	"context"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/zeromicro/go-zero/core/limit"
	"github.com/zeromicro/go-zero/core/stores/redis"
)

func TestTokenLimitersShareRedisStateAcrossInstances(t *testing.T) {
	server := miniredis.RunT(t)
	storeA := redis.MustNewRedis(redis.RedisConf{Host: server.Addr(), Type: redis.NodeType})
	storeB := redis.MustNewRedis(redis.RedisConf{Host: server.Addr(), Type: redis.NodeType})
	limiterA := limit.NewTokenLimiter(1, 1, storeA, "test:distributed-limit")
	limiterB := limit.NewTokenLimiter(1, 1, storeB, "test:distributed-limit")

	if !limiterA.AllowCtx(context.Background()) {
		t.Fatal("first limiter should acquire the only token")
	}
	if limiterB.AllowCtx(context.Background()) {
		t.Fatal("second limiter should observe the shared Redis bucket as empty")
	}
}
