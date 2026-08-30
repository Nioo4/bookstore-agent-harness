package logic

import (
	"context"

	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/check/checker"

	"github.com/zeromicro/go-zero/core/logx"
)

type SearchBooksLogic struct {
	logx.Logger
	ctx    context.Context
	svcCtx *svc.ServiceContext
}

func NewSearchBooksLogic(ctx context.Context, svcCtx *svc.ServiceContext) *SearchBooksLogic {
	return &SearchBooksLogic{Logger: logx.WithContext(ctx), ctx: ctx, svcCtx: svcCtx}
}

func (l *SearchBooksLogic) SearchBooks(req *types.SearchBooksReq) (*types.SearchBooksResp, error) {
	result, err := l.svcCtx.Checker.SearchBooks(l.ctx, &checker.SearchBooksReq{
		Q: req.Q, Author: req.Author, Category: req.Category,
		MinPrice: req.MinPrice, MaxPrice: req.MaxPrice, Language: req.Language,
		InStock: req.InStock, Sort: req.Sort, Cursor: req.Cursor, Limit: req.Limit,
	})
	if err != nil {
		return nil, err
	}
	books := make([]types.BookSummary, 0, len(result.Books))
	for _, book := range result.Books {
		books = append(books, types.BookSummary{
			BookID: book.BookId, ISBN: book.Isbn, Title: book.Title,
			Subtitle: book.Subtitle, Description: book.Description, Publisher: book.Publisher,
			PublicationDate: book.PublicationDate, LanguageCode: book.LanguageCode,
			PageCount: book.PageCount, CoverURL: book.CoverUrl, Price: book.Price,
			Currency: book.Currency, Status: book.Status, Version: book.Version,
			AvailableStock: book.AvailableStock, InStock: book.AvailableStock > 0,
			Authors: book.Authors, Categories: book.Categories,
		})
	}
	return &types.SearchBooksResp{Books: books, NextCursor: result.NextCursor}, nil
}
