# Bookstore observability stack

Docker Compose is used only for the observability dependencies. The Go API and RPC services may continue to run directly on Windows.

## Components

- Prometheus: http://127.0.0.1:9090
- Grafana: http://127.0.0.1:3001 (local credentials: admin / admin)
- Tempo API: http://127.0.0.1:3200
- OpenTelemetry Collector: OTLP gRPC 127.0.0.1:4317, HTTP 127.0.0.1:4318

## Commands

From this directory:

~~~powershell
docker compose config
docker compose up -d
docker compose ps
docker compose down
~~~

Prometheus scrapes the three Go processes through host.docker.internal on ports 9101, 9102 and 9103. The Go processes send traces to the Collector on the host-mapped port 4317; the Collector batches and forwards them to Tempo. Grafana is provisioned with both data sources and the Bookstore Overview dashboard.

Redis read degradation in the check RPC uses a 250 ms cache budget,
SingleFlight coalescing per lookup key, and at most 32 concurrent fallback MySQL
loads. The dashboard exposes these protection metrics:

- `bookstore_check_cache_fallback_requests_total{result}`
- `bookstore_check_cache_fallback_mysql_loads_total{result}`
- `bookstore_check_cache_fallback_singleflight_total{role}`
- `bookstore_check_cache_fallback_inflight`

This stack is for local development. Change the Grafana password, add authentication and restrict exposed ports before any non-local deployment.
