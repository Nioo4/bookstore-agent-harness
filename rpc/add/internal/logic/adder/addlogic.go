package adderlogic

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	"bookstore/rpc/add/adder"
	"bookstore/rpc/add/internal/svc"
	"bookstore/rpc/model"

	"github.com/zeromicro/go-zero/core/logx"
)

type AddLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

func NewAddLogic(ctx context.Context, svcCtx *svc.ServiceContext) *AddLogic {
	return &AddLogic{
		ctx:    ctx,
		svcCtx: svcCtx,
		Logger: logx.WithContext(ctx),
	}
}

func (l *AddLogic) Add(in *adder.AddReq) (*adder.AddResp, error) {
	book, err := normalizeAddRequest(in)
	if err != nil {
		return nil, err
	}

	result, err := l.svcCtx.Model.Insert(book)
	if err != nil {
		return nil, err
	}
	bookID, err := result.LastInsertId()
	if err != nil {
		return nil, err
	}
	return &adder.AddResp{Ok: true, BookId: bookID}, nil
}

func normalizeAddRequest(in *adder.AddReq) (model.Book, error) {
	legacyTitle := strings.TrimSpace(in.Book)
	isbn := strings.TrimSpace(in.Isbn)
	title := strings.TrimSpace(in.Title)
	author := strings.TrimSpace(in.Author)

	if title == "" {
		title = legacyTitle
	}
	if isbn == "" && legacyTitle != "" {
		digest := sha256.Sum256([]byte(legacyTitle))
		isbn = "legacy-" + hex.EncodeToString(digest[:10])
	}
	if author == "" && legacyTitle != "" {
		author = "Unknown"
	}

	if isbn == "" {
		return model.Book{}, fmt.Errorf("isbn is required")
	}
	if title == "" {
		return model.Book{}, fmt.Errorf("title is required")
	}
	if in.Price < 0 {
		return model.Book{}, fmt.Errorf("price must not be negative")
	}
	if in.StockCount < 0 {
		return model.Book{}, fmt.Errorf("stock_count must not be negative")
	}

	return model.Book{
		ISBN:       isbn,
		Title:      title,
		Author:     author,
		Price:      in.Price,
		StockCount: in.StockCount,
	}, nil
}
