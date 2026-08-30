package adderlogic

import (
	"context"
	"errors"
	"strings"

	"bookstore/rpc/add/adder"
	"bookstore/rpc/add/internal/svc"
	"bookstore/rpc/model"

	"github.com/zeromicro/go-zero/core/logx"
	"github.com/zeromicro/go-zero/core/metric"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var stockDeductionTotal = metric.NewCounterVec(&metric.CounterVecOpts{
	Namespace: "bookstore",
	Subsystem: "stock",
	Name:      "deduction_total",
	Help:      "Number of stock deduction requests by outcome.",
	Labels:    []string{"outcome"},
})

type DeductStockLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

func NewDeductStockLogic(ctx context.Context, svcCtx *svc.ServiceContext) *DeductStockLogic {
	return &DeductStockLogic{ctx: ctx, svcCtx: svcCtx, Logger: logx.WithContext(ctx)}
}

func (l *DeductStockLogic) DeductStock(in *adder.DeductStockReq) (*adder.DeductStockResp, error) {
	if in.BookId <= 0 {
		return nil, status.Error(codes.InvalidArgument, "book_id must be positive")
	}
	if in.Quantity <= 0 {
		return nil, status.Error(codes.InvalidArgument, "quantity must be positive")
	}
	if strings.TrimSpace(in.IdempotencyKey) == "" {
		return nil, status.Error(codes.InvalidArgument, "Idempotency-Key header is required")
	}
	if len(in.IdempotencyKey) > 64 {
		return nil, status.Error(codes.InvalidArgument, "Idempotency-Key must not exceed 64 characters")
	}

	replayed, err := l.svcCtx.Model.DeductStockIdempotent(l.ctx, in.BookId, in.Quantity, in.IdempotencyKey)
	switch {
	case err == nil:
		if replayed {
			stockDeductionTotal.Inc("replayed")
		} else {
			stockDeductionTotal.Inc("succeeded")
		}
		return &adder.DeductStockResp{Ok: true, BookId: in.BookId, Deducted: in.Quantity, Replayed: replayed}, nil
	case errors.Is(err, model.ErrNotFound):
		stockDeductionTotal.Inc("not_found")
		return nil, status.Error(codes.NotFound, "book not found")
	case errors.Is(err, model.ErrInsufficientStock):
		stockDeductionTotal.Inc("insufficient")
		return nil, status.Error(codes.FailedPrecondition, "insufficient stock")
	case errors.Is(err, model.ErrIdempotencyConflict):
		stockDeductionTotal.Inc("idempotency_conflict")
		return nil, status.Error(codes.AlreadyExists, "Idempotency-Key was already used for a different request")
	default:
		stockDeductionTotal.Inc("internal_error")
		return nil, status.Error(codes.Internal, "failed to deduct stock")
	}
}
