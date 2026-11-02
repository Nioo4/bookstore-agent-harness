// Code scaffolded by goctl. Safe to edit.
// goctl 1.10.1

package svc

import (
	"bookstore/api/internal/agentproxy"
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
	AgentProxy       *agentproxy.Proxy
}

func NewServiceContext(c config.Config) *ServiceContext {
	return newServiceContext(c,
		adder.NewAdder(zrpc.MustNewClient(c.Add)),
		checker.NewChecker(zrpc.MustNewClient(c.Check)),
		authenticator.NewAuthenticator(zrpc.MustNewClient(c.Auth)),
		orderservice.NewOrderService(zrpc.MustNewClient(c.Order)),
	)
}

// NewInProcessServiceContext keeps the HTTP API on the same process as the
// business modules. The generated RPC-shaped interfaces are retained so the
// existing handlers and compatibility tests do not change, while the
// adapters supplied by rpc/*/service avoid a network hop and etcd.
func NewInProcessServiceContext(c config.Config, a adder.Adder, ch checker.Checker,
	auth authenticator.Authenticator, orders orderservice.OrderService) *ServiceContext {
	return newServiceContext(c, a, ch, auth, orders)
}

func newServiceContext(c config.Config, a adder.Adder, ch checker.Checker,
	auth authenticator.Authenticator, orders orderservice.OrderService) *ServiceContext {
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
	agentURL := c.AgentAPIURL
	if agentURL == "" {
		agentURL = agentproxy.DefaultBaseURL
	}
	proxy, err := agentproxy.New(agentURL)
	if err != nil {
		panic("agent API URL configuration invalid")
	}
	c.AgentAPIURL = agentURL
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
		AgentProxy:       proxy,
		Adder:            a,
		Checker:          ch,
		Authenticator:    auth,
		Orders:           orders,
		StockRateLimiter: stockRateLimiter,
		HotBookCache:     hotcache.MustNew(c.HotCache.Enabled, c.HotCache.TTL, c.HotCache.Limit),
	}
}
