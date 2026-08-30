// Code scaffolded by goctl. Safe to edit.
// goctl 1.10.1

package logic

import (
	"context"

	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/add/adder"

	"github.com/zeromicro/go-zero/core/logx"
)

type UpdateBookLogic struct {
	logx.Logger
	ctx    context.Context
	svcCtx *svc.ServiceContext
}

func NewUpdateBookLogic(ctx context.Context, svcCtx *svc.ServiceContext) *UpdateBookLogic {
	return &UpdateBookLogic{
		Logger: logx.WithContext(ctx),
		ctx:    ctx,
		svcCtx: svcCtx,
	}
}

func (l *UpdateBookLogic) UpdateBook(req *types.UpdateBookReq) (resp *types.UpdateBookResp, err error) {
	rpcReq := &adder.UpdateReq{Book: req.Book}
	if req.ISBN != nil {
		rpcReq.Isbn = req.ISBN
	}
	if req.Title != nil {
		rpcReq.Title = req.Title
	}
	if req.Author != nil {
		rpcReq.Author = req.Author
	}
	if req.Price != nil {
		rpcReq.Price = req.Price
	}
	if req.StockCount != nil {
		rpcReq.StockCount = req.StockCount
	}

	r, err := l.svcCtx.Adder.Update(l.ctx, rpcReq)
	if err != nil {
		return nil, err
	}
	return &types.UpdateBookResp{Ok: r.Ok}, nil
}
