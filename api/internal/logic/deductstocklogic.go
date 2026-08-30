package logic

import (
	"context"
	"strconv"

	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/add/adder"

	"github.com/zeromicro/go-zero/core/logx"
)

type DeductStockLogic struct {
	logx.Logger
	ctx    context.Context
	svcCtx *svc.ServiceContext
}

func NewDeductStockLogic(ctx context.Context, svcCtx *svc.ServiceContext) *DeductStockLogic {
	return &DeductStockLogic{Logger: logx.WithContext(ctx), ctx: ctx, svcCtx: svcCtx}
}

func (l *DeductStockLogic) DeductStock(req *types.DeductStockReq) (*types.DeductStockResp, error) {
	resp, err := l.svcCtx.Adder.DeductStock(l.ctx, &adder.DeductStockReq{BookId: req.BookID, Quantity: req.Quantity, IdempotencyKey: req.IdempotencyKey})
	if err != nil {
		return nil, err
	}
	l.svcCtx.HotBookCache.Del(strconv.FormatInt(req.BookID, 10))
	return &types.DeductStockResp{Ok: resp.Ok, BookID: resp.BookId, Deducted: resp.Deducted, Replayed: resp.Replayed}, nil
}
