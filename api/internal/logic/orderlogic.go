package logic

import (
	"context"

	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/order/orderservice"
)

type OrderLogic struct {
	ctx context.Context
	svc *svc.ServiceContext
}

func NewOrderLogic(ctx context.Context, svc *svc.ServiceContext) *OrderLogic {
	return &OrderLogic{ctx: ctx, svc: svc}
}

func (l *OrderLogic) Get(token string, orderID int64) (*types.OrderRecord, error) {
	resp, err := l.svc.Orders.GetOrder(l.ctx, &orderservice.GetOrderReq{AccessToken: token, OrderId: orderID})
	if err != nil {
		return nil, err
	}
	result := orderFromRPC(resp)
	return &result, nil
}
func (l *OrderLogic) List(token string, req *types.ListOrdersReq) (*types.ListOrdersResp, error) {
	resp, err := l.svc.Orders.ListMyOrders(l.ctx, &orderservice.ListMyOrdersReq{AccessToken: token, Limit: req.Limit, Cursor: req.Cursor})
	if err != nil {
		return nil, err
	}
	orders := make([]types.OrderRecord, 0, len(resp.Orders))
	for _, order := range resp.Orders {
		orders = append(orders, orderFromRPC(order))
	}
	return &types.ListOrdersResp{Orders: orders, NextCursor: resp.NextCursor}, nil
}
func orderItemsFromRPC(items []*orderservice.OrderItem) []types.OrderItem {
	result := make([]types.OrderItem, 0, len(items))
	for _, item := range items {
		result = append(result, types.OrderItem{BookID: item.BookId, ISBN: item.Isbn, Title: item.Title,
			UnitPrice: item.UnitPrice, Quantity: item.Quantity, LineTotal: item.LineTotal})
	}
	return result
}
func orderFromRPC(order *orderservice.OrderRecord) types.OrderRecord {
	return types.OrderRecord{OrderID: order.OrderId, Status: order.Status, Items: orderItemsFromRPC(order.Items),
		TotalAmount: order.TotalAmount, Currency: order.Currency, CreatedAt: order.CreatedAt, Replayed: order.Replayed}
}
