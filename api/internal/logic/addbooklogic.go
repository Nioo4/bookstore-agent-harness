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

type AddBookLogic struct {
	logx.Logger
	ctx    context.Context
	svcCtx *svc.ServiceContext
}

func NewAddBookLogic(ctx context.Context, svcCtx *svc.ServiceContext) *AddBookLogic {
	return &AddBookLogic{
		Logger: logx.WithContext(ctx),
		ctx:    ctx,
		svcCtx: svcCtx,
	}
}

func (l *AddBookLogic) AddBook(req *types.AddBookReq) (resp *types.AddBookResp, err error) {
	r, err := l.svcCtx.Adder.Add(l.ctx, &adder.AddReq{
		Book:       req.Book,
		Isbn:       req.ISBN,
		Title:      req.Title,
		Author:     req.Author,
		Price:      req.Price,
		StockCount: req.StockCount,
	})
	if err != nil {
		return nil, err
	}
	return &types.AddBookResp{Ok: r.Ok, BookID: r.BookId}, nil
}
