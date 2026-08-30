// Code scaffolded by goctl. Safe to edit.
// goctl 1.10.1

package svc

import (
	"bookstore/api/internal/config"
	"bookstore/api/internal/hotcache"
	"bookstore/rpc/add/client/adder"
	"bookstore/rpc/auth/authenticator"
	"bookstore/rpc/auth/jwtauth"
	"bookstore/rpc/check/checker"
	"bookstore/rpc/order/orderservice"
	"context"
	"github.com/zeromicro/go-zero/core/limit"
	"github.com/zeromicro/go-zero/core/stores/redis"
	"github.com/zeromicro/go-zero/zrpc"
	"net/netip"
)

type RateLimiter interface {
	AllowCtx(ctx context.Context) bool
}

type ServiceContext struct {
	Config           config.Config
	Adder            adder.Adder
	Checker          checker.Checker
	Authenticator    authenticator.Authenticator
	Orders           orderservice.OrderService
	StockRateLimiter RateLimiter
	HotBookCache     *hotcache.Cache
	AuthVerifier     *jwtauth.Verifier
}

func NewServiceContext(c config.Config) *ServiceContext {
	if err := c.AuthBudget.Validate(); err != nil {
		panic(err)
	}
	for _, cidr := range c.TrustedProxyCIDRs {
		if _, err := netip.ParsePrefix(cidr); err != nil {
			panic("invalid trusted proxy CIDR")
		}
	}
	verifier, err := jwtauth.LoadVerifier(c.JWT.PublicKeyPath, c.JWT.Issuer)
	if err != nil {
		panic("authentication verifier configuration invalid")
	}
	c.Auth.Timeout = c.AuthBudget.RPC.Milliseconds()
	var stockRateLimiter RateLimiter
	if c.StockRateLimit.Enabled {
		if c.StockRateLimit.Rate <= 0 || c.StockRateLimit.Burst <= 0 {
			panic("stock rate limit rate and burst must be positive")
		}
		store := redis.MustNewRedis(c.StockRateLimit.Redis)
		stockRateLimiter = limit.NewTokenLimiter(c.StockRateLimit.Rate, c.StockRateLimit.Burst, store, c.StockRateLimit.Key)
	}
	return &ServiceContext{
		Config:           c,
		AuthVerifier:     verifier,
		Adder:            adder.NewAdder(zrpc.MustNewClient(c.Add)),
		Checker:          checker.NewChecker(zrpc.MustNewClient(c.Check)),
		Authenticator:    authenticator.NewAuthenticator(zrpc.MustNewClient(c.Auth)),
		Orders:           orderservice.NewOrderService(zrpc.MustNewClient(c.Order)),
		StockRateLimiter: stockRateLimiter,
		HotBookCache:     hotcache.MustNew(c.HotCache.Enabled, c.HotCache.TTL, c.HotCache.Limit),
	}
}
