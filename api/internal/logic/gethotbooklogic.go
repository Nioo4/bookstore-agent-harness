package logic

import (
	"context"
	"strconv"

	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/check/check"

	"github.com/zeromicro/go-zero/core/logx"
)

type GetHotBookLogic struct {
	logx.Logger
	ctx    context.Context
	svcCtx *svc.ServiceContext
}

func NewGetHotBookLogic(ctx context.Context, svcCtx *svc.ServiceContext) *GetHotBookLogic {
	return &GetHotBookLogic{Logger: logx.WithContext(ctx), ctx: ctx, svcCtx: svcCtx}
}

func (l *GetHotBookLogic) GetHotBook(req *types.HotBookReq) (*types.HotBookResp, error) {
	key := strconv.FormatInt(req.BookID, 10)
	value, cacheStatus, err := l.svcCtx.HotBookCache.Get(key, func() (any, error) {
		return l.svcCtx.Checker.Check(l.ctx, &check.CheckReq{Book: key})
	})
	if err != nil {
		return nil, err
	}
	book := value.(*check.CheckResp)
	return &types.HotBookResp{
		Found:          book.Found,
		BookID:         book.BookId,
		ISBN:           book.Isbn,
		Title:          book.Title,
		Author:         book.Author,
		Price:          book.Price,
		StockCount:     book.StockCount,
		CacheStatus:    cacheStatus,
		MaxStalenessMS: l.svcCtx.HotBookCache.MaxStaleness().Milliseconds(),
	}, nil
}
