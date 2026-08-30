package main

import (
	"bookstore/internal/safelog"
	"flag"
	"fmt"

	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/internal/config"
	server2 "bookstore/rpc/auth/internal/server"
	"bookstore/rpc/auth/internal/svc"

	"github.com/zeromicro/go-zero/core/conf"
	"github.com/zeromicro/go-zero/core/service"
	"github.com/zeromicro/go-zero/zrpc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/reflection"
)

var configFile = flag.String("f", "etc/auth.yaml", "the config file")

func main() {
	flag.Parse()
	var c config.Config
	conf.MustLoad(*configFile, &c, conf.UseEnv())
	safelog.Install(c.Log)
	if err := c.AuthBudget.Validate(); err != nil {
		panic(err)
	}
	c.Timeout = c.AuthBudget.RPC.Milliseconds()
	ctx := svc.NewServiceContext(c)
	s := zrpc.MustNewServer(c.RpcServerConf, func(server *grpc.Server) {
		auth.RegisterAuthenticatorServer(server, server2.NewAuthenticatorServer(ctx))
		if c.Mode == service.DevMode || c.Mode == service.TestMode {
			reflection.Register(server)
		}
	})
	defer s.Stop()
	fmt.Printf("Starting auth RPC server at %s...\n", c.ListenOn)
	s.Start()
}
