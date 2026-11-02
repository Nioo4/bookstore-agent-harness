// Command bookstore-service runs the v2 consolidation boundary in one
// process. The HTTP API and the internal gRPC services share the same binary;
// the gRPC listener is intended for the Agent container only and is never
// published by the reverse proxy.
//
// The HTTP handlers call Auth/Add directly through module adapters. Only the
// Catalog/Check and Order protobuf services are exposed on the internal
// listener for the Python Agent.
package main

import (
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"

	apiservice "bookstore/api/service"
	"bookstore/internal/safelog"
	addservice "bookstore/rpc/add/service"
	authservice "bookstore/rpc/auth/service"
	checkservice "bookstore/rpc/check/service"
	orderservice "bookstore/rpc/order/service"

	"github.com/zeromicro/go-zero/core/conf"
	"google.golang.org/grpc"
)

var (
	apiConfigFile   = flag.String("api", envDefault("BOOKSTORE_API_CONFIG", "config/api.yaml"), "HTTP API config")
	addConfigFile   = flag.String("add", envDefault("BOOKSTORE_ADD_CONFIG", "config/add.yaml"), "legacy Add module config")
	checkConfigFile = flag.String("check", envDefault("BOOKSTORE_CHECK_CONFIG", "config/check.yaml"), "Catalog module config")
	authConfigFile  = flag.String("auth", envDefault("BOOKSTORE_AUTH_CONFIG", "config/auth.yaml"), "Auth module config")
	orderConfigFile = flag.String("order", envDefault("BOOKSTORE_ORDER_CONFIG", "config/order.yaml"), "Order module config")
	httpListen      = flag.String("http", os.Getenv("BOOKSTORE_HTTP_ADDR"), "HTTP listen address; defaults to the API YAML")
	grpcListen      = flag.String("grpc", envDefault("BOOKSTORE_GRPC_ADDR", envDefault("BOOKSTORE_GRPC_LISTEN", ":9090")), "internal gRPC listen address")
)

func main() {
	flag.Parse()

	var apiConfig apiservice.Config
	conf.MustLoad(*apiConfigFile, &apiConfig, conf.UseEnv())
	if strings.TrimSpace(*httpListen) != "" {
		applyHTTPAddr(&apiConfig, *httpListen)
	}
	safelog.Install(apiConfig.Log)

	var addConfig addservice.Config
	conf.MustLoad(*addConfigFile, &addConfig, conf.UseEnv())
	var checkConfig checkservice.Config
	conf.MustLoad(*checkConfigFile, &checkConfig, conf.UseEnv())
	var authConfig authservice.Config
	conf.MustLoad(*authConfigFile, &authConfig, conf.UseEnv())
	var orderConfig orderservice.Config
	conf.MustLoad(*orderConfigFile, &orderConfig, conf.UseEnv())

	listener, err := net.Listen("tcp", *grpcListen)
	if err != nil {
		panic(fmt.Errorf("listen internal gRPC: %w", err))
	}

	grpcServer := grpc.NewServer()
	checkservice.Register(grpcServer, checkConfig)
	orderservice.Register(grpcServer, orderConfig)

	go func() {
		if err := grpcServer.Serve(listener); err != nil {
			fmt.Fprintln(os.Stderr, "internal gRPC stopped:", err)
		}
	}()

	httpServer := apiservice.NewInProcessHTTPServer(apiConfig,
		addservice.NewLocalClient(addConfig),
		checkservice.NewLocalClient(checkConfig),
		authservice.NewLocalClient(authConfig),
		orderservice.NewLocalClient(orderConfig),
	)
	go httpServer.Start()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	signal.Stop(stop)
	httpServer.Stop()
	grpcServer.GracefulStop()
}

func envDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func applyHTTPAddr(c *apiservice.Config, address string) {
	host, portText, err := net.SplitHostPort(address)
	if err != nil {
		panic(fmt.Errorf("invalid BOOKSTORE_HTTP_ADDR: %w", err))
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 {
		panic(fmt.Errorf("invalid BOOKSTORE_HTTP_ADDR port %q", portText))
	}
	if host == "" {
		host = "0.0.0.0"
	}
	c.Host = host
	c.Port = port
}
