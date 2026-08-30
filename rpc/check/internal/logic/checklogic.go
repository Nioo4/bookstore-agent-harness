package logic

import (
	"context"
	"errors"
	"strings"

	"bookstore/rpc/check/check"
	"bookstore/rpc/check/internal/svc"
	"bookstore/rpc/model"

	"github.com/zeromicro/go-zero/core/logx"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type CheckLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

func NewCheckLogic(ctx context.Context, svcCtx *svc.ServiceContext) *CheckLogic {
	return &CheckLogic{
		ctx:    ctx,
		svcCtx: svcCtx,
		Logger: logx.WithContext(ctx),
	}
}

func (l *CheckLogic) Check(in *check.CheckReq) (*check.CheckResp, error) {
	resp, err := l.svcCtx.Catalog.GetCatalogBook(l.ctx, in.Book)
	if err != nil {
		if errors.Is(err, model.ErrNotFound) {
			return &check.CheckResp{Found: false}, nil
		}
		if errors.Is(err, model.ErrCacheFallbackBusy) {
			return &check.CheckResp{Found: false}, status.Error(codes.ResourceExhausted, err.Error())
		}
		return &check.CheckResp{Found: false}, err
	}
	return &check.CheckResp{
		Found:           true,
		BookId:          resp.BookID,
		Isbn:            resp.ISBN,
		Title:           resp.Title,
		Author:          strings.Join(resp.Authors, ", "),
		Price:           resp.Price,
		StockCount:      resp.AvailableStock,
		Subtitle:        resp.Subtitle,
		Description:     resp.Description,
		Publisher:       resp.Publisher,
		PublicationDate: resp.PublicationDate,
		LanguageCode:    resp.LanguageCode,
		PageCount:       resp.PageCount,
		CoverUrl:        resp.CoverURL,
		Currency:        resp.Currency,
		Status:          resp.Status,
		Version:         resp.Version,
		Authors:         resp.Authors,
		Categories:      resp.Categories,
	}, nil
}
