package main

import (
	"bookstore/internal/safelog"
	"flag"
	"fmt"

	"bookstore/rpc/order/internal/config"
	"bookstore/rpc/order/internal/server"
	"bookstore/rpc/order/internal/svc"
	orderpb "bookstore/rpc/order/order"

	"github.com/zeromicro/go-zero/core/conf"
	"github.com/zeromicro/go-zero/core/service"
	"github.com/zeromicro/go-zero/zrpc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/reflection"
)

var configFile = flag.String("f", "etc/order.yaml", "the config file")

func main() {
	flag.Parse()
	var c config.Config
	conf.MustLoad(*configFile, &c, conf.UseEnv())
	safelog.Install(c.Log)
	ctx := svc.NewServiceContext(c)
	s := zrpc.MustNewServer(c.RpcServerConf, func(server2 *grpc.Server) {
		orderpb.RegisterOrderServiceServer(server2, server.NewOrderServer(ctx))
		if c.Mode == service.DevMode || c.Mode == service.TestMode {
			reflection.Register(server2)
		}
	})
	defer s.Stop()
	fmt.Printf("Starting order RPC server at %s...\n", c.ListenOn)
	s.Start()
}
