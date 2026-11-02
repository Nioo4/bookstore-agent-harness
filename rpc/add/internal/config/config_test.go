package config

import (
	"path/filepath"
	"runtime"
	"testing"

	"github.com/zeromicro/go-zero/core/conf"
)

func TestAddRPCResilienceConfig(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(file), "..", "..", "etc", "add.yaml")
	var c Config
	if err := conf.Load(path, &c); err != nil {
		t.Fatalf("load config: %v", err)
	}
	if c.Timeout != 800 || c.CpuThreshold != 800 || !c.Middlewares.Breaker {
		t.Fatalf("Add RPC resilience config invalid: timeout=%d cpu=%d middlewares=%+v", c.Timeout, c.CpuThreshold, c.Middlewares)
	}
	if c.Prometheus.Port != 9102 || c.Telemetry.Name != "bookstore-add-rpc" || c.Telemetry.Endpoint != "127.0.0.1:4317" {
		t.Fatalf("Add RPC observability config invalid: prometheus=%+v telemetry=%+v", c.Prometheus, c.Telemetry)
	}
}
