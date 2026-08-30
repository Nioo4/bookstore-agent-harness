package config

import (
	"time"

	"github.com/zeromicro/go-zero/zrpc"
)

type Config struct {
	zrpc.RpcServerConf
	DataSource    string
	BusinessRedis struct {
		Address  string
		Password string `json:",optional"`
		DB       int    `json:",optional"`
	}
	JWT struct {
		PublicKeyPath string
		Issuer        string
	}
	ConfirmationTTL time.Duration
}
