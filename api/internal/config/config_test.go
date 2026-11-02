package config

import (
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/zeromicro/go-zero/core/conf"
)

func TestBookstoreAPIResilienceConfig(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(file), "..", "..", "etc", "bookstore-api.yaml")
	var c Config
	if err := conf.Load(path, &c); err != nil {
		t.Fatalf("load config: %v", err)
	}
	if c.Timeout != 1500 || c.CpuThreshold != 800 {
		t.Fatalf("REST timeout/cpu = %d/%d; want 1500/800", c.Timeout, c.CpuThreshold)
	}
	if c.AppBaseURL != "http://localhost:8088" || c.AgentAPIURL != "http://agent-api:8000" || c.CookieSecure {
		t.Fatalf("browser/proxy/cookie config = %q/%q/%t; want local defaults", c.AppBaseURL, c.AgentAPIURL, c.CookieSecure)
	}
	if !c.Middlewares.Timeout || !c.Middlewares.Breaker || !c.Middlewares.Shedding {
		t.Fatalf("REST resilience middleware not enabled: %+v", c.Middlewares)
	}
	if c.Add.Timeout != 1000 || !c.Add.Middlewares.Breaker || !c.Add.Middlewares.Timeout {
		t.Fatalf("Add RPC client resilience config invalid: %+v", c.Add)
	}
	if c.Check.Timeout != 1000 || !c.Check.Middlewares.Breaker || !c.Check.Middlewares.Timeout {
		t.Fatalf("Check RPC client resilience config invalid: %+v", c.Check)
	}
	if c.Auth.Timeout != 1000 || !c.Auth.Middlewares.Breaker || !c.Auth.Middlewares.Timeout || c.Auth.Etcd.Key != "auth.rpc" {
		t.Fatalf("Auth RPC client resilience config invalid: %+v", c.Auth)
	}
	if c.Order.Timeout != 1000 || !c.Order.Middlewares.Breaker || !c.Order.Middlewares.Timeout || c.Order.Etcd.Key != "order.rpc" {
		t.Fatalf("Order RPC client resilience config invalid: %+v", c.Order)
	}
	if c.Prometheus.Port != 9101 || c.Prometheus.Path != "/metrics" {
		t.Fatalf("API Prometheus config invalid: %+v", c.Prometheus)
	}
	if c.Telemetry.Name != "bookstore-api" || c.Telemetry.Endpoint != "127.0.0.1:4317" || c.Telemetry.Batcher != "otlpgrpc" {
		t.Fatalf("API telemetry config invalid: %+v", c.Telemetry)
	}
	if !c.HotCache.Enabled || c.HotCache.TTL != 2*time.Second || c.HotCache.Limit != 1024 {
		t.Fatalf("hot cache config invalid: %+v", c.HotCache)
	}
}
