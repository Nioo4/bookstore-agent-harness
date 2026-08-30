package logic

import (
	"context"
	"errors"
	"time"

	"bookstore/rpc/order/internal/ordering"
	"bookstore/rpc/order/internal/svc"
	orderpb "bookstore/rpc/order/order"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type Logic struct {
	ctx context.Context
	svc *svc.ServiceContext
}

func New(ctx context.Context, svc *svc.ServiceContext) *Logic { return &Logic{ctx: ctx, svc: svc} }

func (l *Logic) PrepareOrder(in *orderpb.PrepareOrderReq) (*orderpb.PrepareOrderResp, error) {
	items := make([]ordering.ItemInput, 0, len(in.Items))
	for _, item := range in.Items {
		items = append(items, ordering.ItemInput{BookID: item.BookId, Quantity: item.Quantity})
	}
	preview, token, err := l.svc.Orders.Prepare(l.ctx, in.AccessToken, in.ConversationId, in.IdempotencyKey, items)
	if err != nil {
		return nil, mapError(err)
	}
	return &orderpb.PrepareOrderResp{ConfirmationToken: token, ExpiresAt: preview.ExpiresAt.Format(time.RFC3339Nano),
		Items: itemsToProto(preview.Items), TotalAmount: preview.TotalAmount, Currency: preview.Currency}, nil
}

func (l *Logic) DecideConfirmation(in *orderpb.DecideConfirmationReq) (*orderpb.ConfirmationResult, error) {
	result, err := l.svc.Orders.Decide(l.ctx, in.AccessToken, in.ConversationId, in.ConfirmationToken, in.Decision)
	if err != nil {
		return nil, mapError(err)
	}
	response := &orderpb.ConfirmationResult{ConfirmationId: result.ConfirmationID, Decision: result.Decision,
		Status: result.Status, Replayed: result.Replayed}
	if result.Order != nil {
		response.Order = orderToProto(*result.Order)
	}
	return response, nil
}

func (l *Logic) GetOrder(in *orderpb.GetOrderReq) (*orderpb.OrderRecord, error) {
	order, err := l.svc.Orders.Get(l.ctx, in.AccessToken, in.OrderId)
	if err != nil {
		return nil, mapError(err)
	}
	return orderToProto(*order), nil
}

func (l *Logic) ListMyOrders(in *orderpb.ListMyOrdersReq) (*orderpb.ListMyOrdersResp, error) {
	page, err := l.svc.Orders.List(l.ctx, in.AccessToken, in.Cursor, int(in.Limit))
	if err != nil {
		return nil, mapError(err)
	}
	orders := make([]*orderpb.OrderRecord, 0, len(page.Orders))
	for _, order := range page.Orders {
		orders = append(orders, orderToProto(order))
	}
	return &orderpb.ListMyOrdersResp{Orders: orders, NextCursor: page.NextCursor}, nil
}

func itemsToProto(items []ordering.Item) []*orderpb.OrderItem {
	result := make([]*orderpb.OrderItem, 0, len(items))
	for _, item := range items {
		result = append(result, &orderpb.OrderItem{BookId: item.BookID, Isbn: item.ISBN,
			Title: item.Title, UnitPrice: item.UnitPrice, Quantity: item.Quantity, LineTotal: item.LineTotal})
	}
	return result
}
func orderToProto(order ordering.Order) *orderpb.OrderRecord {
	return &orderpb.OrderRecord{OrderId: order.OrderID, Status: order.Status, Items: itemsToProto(order.Items),
		TotalAmount: order.TotalAmount, Currency: order.Currency, CreatedAt: order.CreatedAt.Format(time.RFC3339Nano), Replayed: order.Replayed}
}

func mapError(err error) error {
	switch {
	case errors.Is(err, ordering.ErrUnauthorized):
		return status.Error(codes.Unauthenticated, err.Error())
	case errors.Is(err, ordering.ErrEmailNotVerified):
		return status.Error(codes.PermissionDenied, err.Error())
	case errors.Is(err, ordering.ErrBookNotFound), errors.Is(err, ordering.ErrOrderNotFound),
		errors.Is(err, ordering.ErrConfirmationNotFound):
		return status.Error(codes.NotFound, err.Error())
	case errors.Is(err, ordering.ErrBookInactive), errors.Is(err, ordering.ErrInventoryInsufficient),
		errors.Is(err, ordering.ErrConfirmationExpired), errors.Is(err, ordering.ErrPriceChanged):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, ordering.ErrConfirmationAlreadyUsed), errors.Is(err, ordering.ErrIdempotencyConflict),
		errors.Is(err, ordering.ErrConfirmationAlreadyDecided):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.Is(err, ordering.ErrInvalidOrderRequest):
		return status.Error(codes.InvalidArgument, err.Error())
	default:
		if errors.Is(err, context.Canceled) {
			return status.Error(codes.Canceled, "request cancelled")
		}
		return status.Error(codes.Internal, "order operation failed")
	}
}
