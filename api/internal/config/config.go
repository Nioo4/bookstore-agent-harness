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
	// CookieSecure is false for the local HTTP demo and must be enabled when
	// the browser-facing API is served over HTTPS.
	CookieSecure bool `json:",default=false"`
	// AppBaseURL is the browser origin used for state-changing request checks.
	// Deployments should override it with APP_BASE_URL; the default keeps the
	// local Vite/reverse-proxy demo usable when no environment is supplied.
	AppBaseURL  string `json:",default=http://localhost:8088"`
	AgentAPIURL string `json:",default=http://agent-api:8000"`
	JWT         struct {
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
