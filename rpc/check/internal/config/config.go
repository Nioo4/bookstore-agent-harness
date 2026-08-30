package config

import (
	"time"

	"github.com/zeromicro/go-zero/core/stores/cache"
	"github.com/zeromicro/go-zero/zrpc"
)

type Config struct {
	zrpc.RpcServerConf
	DataSource    string
	Cache         cache.CacheConf
	CacheFallback struct {
		CacheTimeout  time.Duration
		MaxConcurrent int
	}
}
