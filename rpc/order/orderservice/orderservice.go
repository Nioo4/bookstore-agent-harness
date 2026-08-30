package orderservice

import (
	"context"

	orderpb "bookstore/rpc/order/order"

	"github.com/zeromicro/go-zero/zrpc"
	"google.golang.org/grpc"
)

type (
	OrderItemInput        = orderpb.OrderItemInput
	OrderItem             = orderpb.OrderItem
	PrepareOrderReq       = orderpb.PrepareOrderReq
	PrepareOrderResp      = orderpb.PrepareOrderResp
	DecideConfirmationReq = orderpb.DecideConfirmationReq
	ConfirmationResult    = orderpb.ConfirmationResult
	OrderRecord           = orderpb.OrderRecord
	GetOrderReq           = orderpb.GetOrderReq
	ListMyOrdersReq       = orderpb.ListMyOrdersReq
	ListMyOrdersResp      = orderpb.ListMyOrdersResp
	OrderService          interface {
		PrepareOrder(context.Context, *PrepareOrderReq, ...grpc.CallOption) (*PrepareOrderResp, error)
		DecideConfirmation(context.Context, *DecideConfirmationReq, ...grpc.CallOption) (*ConfirmationResult, error)
		GetOrder(context.Context, *GetOrderReq, ...grpc.CallOption) (*OrderRecord, error)
		ListMyOrders(context.Context, *ListMyOrdersReq, ...grpc.CallOption) (*ListMyOrdersResp, error)
	}
	client struct{ cli zrpc.Client }
)

func NewOrderService(cli zrpc.Client) OrderService { return &client{cli: cli} }
func (c *client) PrepareOrder(ctx context.Context, in *PrepareOrderReq, opts ...grpc.CallOption) (*PrepareOrderResp, error) {
	return orderpb.NewOrderServiceClient(c.cli.Conn()).PrepareOrder(ctx, in, opts...)
}
func (c *client) DecideConfirmation(ctx context.Context, in *DecideConfirmationReq, opts ...grpc.CallOption) (*ConfirmationResult, error) {
	return orderpb.NewOrderServiceClient(c.cli.Conn()).DecideConfirmation(ctx, in, opts...)
}
func (c *client) GetOrder(ctx context.Context, in *GetOrderReq, opts ...grpc.CallOption) (*OrderRecord, error) {
	return orderpb.NewOrderServiceClient(c.cli.Conn()).GetOrder(ctx, in, opts...)
}
func (c *client) ListMyOrders(ctx context.Context, in *ListMyOrdersReq, opts ...grpc.CallOption) (*ListMyOrdersResp, error) {
	return orderpb.NewOrderServiceClient(c.cli.Conn()).ListMyOrders(ctx, in, opts...)
}
