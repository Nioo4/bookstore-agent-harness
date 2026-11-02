// Package service is the composition boundary for the Catalog/Check module.
package service

import (
	"bookstore/rpc/check/check"
	"bookstore/rpc/check/internal/config"
	"bookstore/rpc/check/internal/server"
	"bookstore/rpc/check/internal/svc"
	"context"
	"google.golang.org/grpc"
)

type Config = config.Config

func NewServer(c Config) check.CheckerServer {
	return server.NewCheckerServer(svc.NewServiceContext(c))
}

func Register(registrar grpc.ServiceRegistrar, c Config) {
	check.RegisterCheckerServer(registrar, NewServer(c))
}

func NewLocalClient(c Config) check.CheckerClient {
	return localClient{server: NewServer(c)}
}

type localClient struct{ server check.CheckerServer }

func (c localClient) Check(ctx context.Context, in *check.CheckReq, _ ...grpc.CallOption) (*check.CheckResp, error) {
	return c.server.Check(ctx, in)
}
func (c localClient) SearchBooks(ctx context.Context, in *check.SearchBooksReq, _ ...grpc.CallOption) (*check.SearchBooksResp, error) {
	return c.server.SearchBooks(ctx, in)
}
