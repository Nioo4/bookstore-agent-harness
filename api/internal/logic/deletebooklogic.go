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

type DeleteBookLogic struct {
	logx.Logger
	ctx    context.Context
	svcCtx *svc.ServiceContext
}

func NewDeleteBookLogic(ctx context.Context, svcCtx *svc.ServiceContext) *DeleteBookLogic {
	return &DeleteBookLogic{
		Logger: logx.WithContext(ctx),
		ctx:    ctx,
		svcCtx: svcCtx,
	}
}

func (l *DeleteBookLogic) DeleteBook(req *types.DeleteBookReq) (resp *types.DeleteBookResp, err error) {
	r, err := l.svcCtx.Adder.Delete(l.ctx, &adder.DeleteReq{
		Book: req.Book,
	})
	if err != nil {
		return nil, err
	}
	return &types.DeleteBookResp{Ok: r.Ok}, nil
}
