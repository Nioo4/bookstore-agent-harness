// Package service is the composition boundary for the Order module.
package service

import (
	"bookstore/rpc/order/internal/config"
	"bookstore/rpc/order/internal/server"
	"bookstore/rpc/order/internal/svc"
	orderpb "bookstore/rpc/order/order"
	"context"
	"google.golang.org/grpc"
)

type Config = config.Config

func NewServer(c Config) orderpb.OrderServiceServer {
	return server.NewOrderServer(svc.NewServiceContext(c))
}

func Register(registrar grpc.ServiceRegistrar, c Config) {
	orderpb.RegisterOrderServiceServer(registrar, NewServer(c))
}

func NewLocalClient(c Config) orderpb.OrderServiceClient {
	return localClient{server: NewServer(c)}
}

type localClient struct{ server orderpb.OrderServiceServer }

func (c localClient) PrepareOrder(ctx context.Context, in *orderpb.PrepareOrderReq, _ ...grpc.CallOption) (*orderpb.PrepareOrderResp, error) {
	return c.server.PrepareOrder(ctx, in)
}
func (c localClient) DecideConfirmation(ctx context.Context, in *orderpb.DecideConfirmationReq, _ ...grpc.CallOption) (*orderpb.ConfirmationResult, error) {
	return c.server.DecideConfirmation(ctx, in)
}
func (c localClient) GetOrder(ctx context.Context, in *orderpb.GetOrderReq, _ ...grpc.CallOption) (*orderpb.OrderRecord, error) {
	return c.server.GetOrder(ctx, in)
}
func (c localClient) ListMyOrders(ctx context.Context, in *orderpb.ListMyOrdersReq, _ ...grpc.CallOption) (*orderpb.ListMyOrdersResp, error) {
	return c.server.ListMyOrders(ctx, in)
}
