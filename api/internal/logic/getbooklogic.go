// Code scaffolded by goctl. Safe to edit.
// goctl 1.10.1

package logic

import (
	"context"

	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/check/checker"

	"github.com/zeromicro/go-zero/core/logx"
)

type GetBookLogic struct {
	logx.Logger
	ctx    context.Context
	svcCtx *svc.ServiceContext
}

func NewGetBookLogic(ctx context.Context, svcCtx *svc.ServiceContext) *GetBookLogic {
	return &GetBookLogic{
		Logger: logx.WithContext(ctx),
		ctx:    ctx,
		svcCtx: svcCtx,
	}
}

func (l *GetBookLogic) GetBook(req *types.GetBookReq) (resp *types.GetBookResp, err error) {
	r, err := l.svcCtx.Checker.Check(l.ctx, &checker.CheckReq{
		Book: req.Book,
	})
	if err != nil {
		return &types.GetBookResp{}, err
	}
	return &types.GetBookResp{
		Found:           r.Found,
		BookID:          r.BookId,
		ISBN:            r.Isbn,
		Title:           r.Title,
		Author:          r.Author,
		Price:           r.Price,
		StockCount:      r.StockCount,
		Subtitle:        r.Subtitle,
		Description:     r.Description,
		Publisher:       r.Publisher,
		PublicationDate: r.PublicationDate,
		LanguageCode:    r.LanguageCode,
		PageCount:       r.PageCount,
		CoverURL:        r.CoverUrl,
		Currency:        r.Currency,
		Status:          r.Status,
		Version:         r.Version,
		Authors:         r.Authors,
		Categories:      r.Categories,
	}, nil
}
