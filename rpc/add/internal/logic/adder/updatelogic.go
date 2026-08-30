package adderlogic

import (
	"context"
	"fmt"
	"strings"

	"bookstore/rpc/add/adder"
	"bookstore/rpc/add/internal/svc"

	"github.com/zeromicro/go-zero/core/logx"
)

type UpdateLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

func NewUpdateLogic(ctx context.Context, svcCtx *svc.ServiceContext) *UpdateLogic {
	return &UpdateLogic{
		ctx:    ctx,
		svcCtx: svcCtx,
		Logger: logx.WithContext(ctx),
	}
}

func (l *UpdateLogic) Update(in *adder.UpdateReq) (*adder.UpdateResp, error) {
	book, err := l.svcCtx.Model.FindOne(in.Book)
	if err != nil {
		return nil, err
	}

	if in.Isbn != nil {
		book.ISBN = strings.TrimSpace(*in.Isbn)
	}
	if in.Title != nil {
		book.Title = strings.TrimSpace(*in.Title)
	}
	if in.Author != nil {
		book.Author = strings.TrimSpace(*in.Author)
	}
	if in.Price != nil {
		book.Price = *in.Price
	}
	if in.StockCount != nil {
		book.StockCount = *in.StockCount
	}

	if book.ISBN == "" || book.Title == "" {
		return nil, fmt.Errorf("isbn and title must not be empty")
	}
	if book.Price < 0 || book.StockCount < 0 {
		return nil, fmt.Errorf("price and stock_count must not be negative")
	}

	err = l.svcCtx.Model.Update(*book)
	if err != nil {
		return nil, err
	}
	return &adder.UpdateResp{Ok: true}, nil
}
