package config

import (
	"github.com/zeromicro/go-zero/core/conf"
	"testing"
)

func TestDeploymentConfigDoesNotCollideWithRPCAuthenticationRedis(t *testing.T) {
	t.Setenv("MYSQL_DSN", "fixture:unused@tcp(mysql:3306)/bookstore")
	t.Setenv("APP_BASE_URL", "http://localhost:8088")
	t.Setenv("SMTP_HOST", "mailpit")
	t.Setenv("SMTP_PORT", "1025")
	t.Setenv("SMTP_USER", "")
	t.Setenv("SMTP_PASSWORD", "")
	t.Setenv("SMTP_FROM", "no-reply@bookstore.local")
	for key, value := range map[string]string{"SMTP_SEND_TIMEOUT": "5s", "AUTH_RPC_TIMEOUT": "10s", "AUTH_HTTP_TIMEOUT": "15s", "AUTH_WEB_TIMEOUT": "20s", "AUTH_RESEND_IP_LIMIT": "30", "AUTH_RESEND_EMAIL_LIMIT": "5", "AUTH_RESEND_WINDOW": "1h", "AUTH_RESEND_COOLDOWN": "60s"} {
		t.Setenv(key, value)
	}
	var c Config
	if err := conf.Load("../../../../deploy/config/auth.yaml", &c, conf.UseEnv()); err != nil {
		t.Fatal(err)
	}
	if c.BusinessRedis.Address != "redis:6379" || c.RpcServerConf.Redis.Host != "" || c.Auth {
		t.Fatal("business Redis must not enable go-zero RPC auth")
	}
	if err := c.AuthBudget.Validate(); err != nil {
		t.Fatal(err)
	}
}
