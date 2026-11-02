// Package service is the composition boundary for the Add module. It keeps
// internal implementation packages private while allowing the consolidated
// process to register the legacy descriptor during migration.
package service

import (
	adderpb "bookstore/rpc/add/adder"
	"bookstore/rpc/add/internal/config"
	addserver "bookstore/rpc/add/internal/server/adder"
	"bookstore/rpc/add/internal/svc"
	"context"
	"google.golang.org/grpc"
)

type Config = config.Config

func NewServer(c Config) adderpb.AdderServer {
	return addserver.NewAdderServer(svc.NewServiceContext(c))
}

func Register(registrar grpc.ServiceRegistrar, c Config) {
	adderpb.RegisterAdderServer(registrar, NewServer(c))
}

// NewLocalClient is the in-process adapter used by the consolidated HTTP
// layer. Call options are accepted for compatibility with the generated
// client interface and intentionally ignored because no network hop exists.
func NewLocalClient(c Config) adderpb.AdderClient {
	return localClient{server: NewServer(c)}
}

type localClient struct{ server adderpb.AdderServer }

func (c localClient) Add(ctx context.Context, in *adderpb.AddReq, _ ...grpc.CallOption) (*adderpb.AddResp, error) {
	return c.server.Add(ctx, in)
}
func (c localClient) Update(ctx context.Context, in *adderpb.UpdateReq, _ ...grpc.CallOption) (*adderpb.UpdateResp, error) {
	return c.server.Update(ctx, in)
}
func (c localClient) Delete(ctx context.Context, in *adderpb.DeleteReq, _ ...grpc.CallOption) (*adderpb.DeleteResp, error) {
	return c.server.Delete(ctx, in)
}
func (c localClient) DeductStock(ctx context.Context, in *adderpb.DeductStockReq, _ ...grpc.CallOption) (*adderpb.DeductStockResp, error) {
	return c.server.DeductStock(ctx, in)
}
