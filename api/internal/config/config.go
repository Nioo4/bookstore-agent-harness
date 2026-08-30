// Code scaffolded by goctl. Safe to edit.
// goctl 1.10.1

package config

import (
	"bookstore/internal/authbudget"
	"github.com/zeromicro/go-zero/core/stores/redis"
	"github.com/zeromicro/go-zero/rest"
	"github.com/zeromicro/go-zero/zrpc"
	"time"
)

type RateLimitConf struct {
	Enabled bool   `json:",default=true"`
	Rate    int    `json:",default=100"`
	Burst   int    `json:",default=200"`
	Key     string `json:",default=bookstore:stock-deduct"`
	Redis   redis.RedisConf
}

type HotCacheConf struct {
	Enabled bool          `json:",default=true"`
	TTL     time.Duration `json:",default=2s"`
	Limit   int           `json:",default=1024"`
}

type Config struct {
	rest.RestConf
	AuthBudget        authbudget.Config
	TrustedProxyCIDRs []string `json:",optional"`
	JWT               struct {
		PublicKeyPath string `json:",default=../deploy/local/keys/public/jwt-public.pem"`
		Issuer        string `json:",default=bookstore"`
	}
	Add            zrpc.RpcClientConf
	Check          zrpc.RpcClientConf
	Auth           zrpc.RpcClientConf
	Order          zrpc.RpcClientConf
	StockRateLimit RateLimitConf
	HotCache       HotCacheConf
}
