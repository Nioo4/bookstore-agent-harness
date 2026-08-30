package server

import (
	"context"

	"bookstore/rpc/order/internal/logic"
	"bookstore/rpc/order/internal/svc"
	orderpb "bookstore/rpc/order/order"
)

type OrderServer struct {
	svc *svc.ServiceContext
	orderpb.UnimplementedOrderServiceServer
}

func NewOrderServer(svc *svc.ServiceContext) *OrderServer { return &OrderServer{svc: svc} }
func (s *OrderServer) PrepareOrder(ctx context.Context, in *orderpb.PrepareOrderReq) (*orderpb.PrepareOrderResp, error) {
	return logic.New(ctx, s.svc).PrepareOrder(in)
}
func (s *OrderServer) DecideConfirmation(ctx context.Context, in *orderpb.DecideConfirmationReq) (*orderpb.ConfirmationResult, error) {
	return logic.New(ctx, s.svc).DecideConfirmation(in)
}
func (s *OrderServer) GetOrder(ctx context.Context, in *orderpb.GetOrderReq) (*orderpb.OrderRecord, error) {
	return logic.New(ctx, s.svc).GetOrder(in)
}
func (s *OrderServer) ListMyOrders(ctx context.Context, in *orderpb.ListMyOrdersReq) (*orderpb.ListMyOrdersResp, error) {
	return logic.New(ctx, s.svc).ListMyOrders(in)
}
