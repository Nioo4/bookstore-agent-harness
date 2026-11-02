package config

import (
	"github.com/zeromicro/go-zero/core/conf"
	"testing"
)

func TestDeploymentConfigDoesNotCollideWithRPCAuthenticationRedis(t *testing.T) {
	t.Setenv("MYSQL_DSN", "fixture:unused@tcp(mysql:3306)/bookstore")
	t.Setenv("APP_BASE_URL", "http://localhost:8088")
	t.Setenv("CONFIRMATION_TTL", "10m")
	var c Config
	if err := conf.Load("../../../../deploy/config/order.yaml", &c, conf.UseEnv()); err != nil {
		t.Fatal(err)
	}
	if c.BusinessRedis.Address != "redis:6379" || c.RpcServerConf.Redis.Host != "" || c.Auth {
		t.Fatal("business Redis must not enable go-zero RPC auth")
	}
}
