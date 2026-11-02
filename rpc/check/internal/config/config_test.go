package config

import (
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/zeromicro/go-zero/core/conf"
)

func TestCheckRPCResilienceConfig(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(file), "..", "..", "etc", "check.yaml")
	var c Config
	if err := conf.Load(path, &c); err != nil {
		t.Fatalf("load config: %v", err)
	}
	if c.Timeout != 800 || c.CpuThreshold != 800 || !c.Middlewares.Breaker {
		t.Fatalf("Check RPC resilience config invalid: timeout=%d cpu=%d middlewares=%+v", c.Timeout, c.CpuThreshold, c.Middlewares)
	}
	if c.Prometheus.Port != 9103 || c.Telemetry.Name != "bookstore-check-rpc" || c.Telemetry.Endpoint != "127.0.0.1:4317" {
		t.Fatalf("Check RPC observability config invalid: prometheus=%+v telemetry=%+v", c.Prometheus, c.Telemetry)
	}
	if c.CacheFallback.CacheTimeout != 250*time.Millisecond || c.CacheFallback.MaxConcurrent != 32 {
		t.Fatalf("Check RPC cache fallback config invalid: %+v", c.CacheFallback)
	}
}
