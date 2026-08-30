package config

import (
	"bookstore/internal/authbudget"
	"time"

	"github.com/zeromicro/go-zero/zrpc"
)

type Config struct {
	zrpc.RpcServerConf
	AuthBudget    authbudget.Config
	DataSource    string
	BusinessRedis struct {
		Address  string
		Password string `json:",optional"`
		DB       int    `json:",optional"`
	}
	JWT struct {
		PrivateKeyPath string
		PublicKeyPath  string
		Issuer         string
	}
	SMTP struct {
		Host     string
		Port     int
		Username string `json:",optional"`
		Password string `json:",optional"`
		From     string
	}
	AppBaseURL string
	RateLimit  struct {
		RegisterLimit    int
		LoginLimit       int
		Window           time.Duration
		ResendIPLimit    int           `json:",default=30"`
		ResendEmailLimit int           `json:",default=5"`
		ResendWindow     time.Duration `json:",default=1h"`
		ResendCooldown   time.Duration `json:",default=60s"`
	}
}
