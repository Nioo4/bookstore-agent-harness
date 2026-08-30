package logic

import (
	"context"
	"errors"

	"bookstore/rpc/check/check"
	"bookstore/rpc/check/internal/svc"
	"bookstore/rpc/model"

	"github.com/zeromicro/go-zero/core/logx"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type SearchBooksLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

func NewSearchBooksLogic(ctx context.Context, svcCtx *svc.ServiceContext) *SearchBooksLogic {
	return &SearchBooksLogic{ctx: ctx, svcCtx: svcCtx, Logger: logx.WithContext(ctx)}
}

func (l *SearchBooksLogic) SearchBooks(in *check.SearchBooksReq) (*check.SearchBooksResp, error) {
	result, err := l.svcCtx.Catalog.SearchBooks(l.ctx, model.BookSearchFilter{
		Query: in.Q, Author: in.Author, Category: in.Category,
		MinPrice: in.MinPrice, MaxPrice: in.MaxPrice, Language: in.Language,
		InStock: in.InStock, Sort: in.Sort, Cursor: in.Cursor, Limit: int(in.Limit),
	})
	if err != nil {
		if errors.Is(err, model.ErrInvalidBookSearch) {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
		return nil, status.Error(codes.Internal, "failed to search books")
	}
	books := make([]*check.BookSummary, 0, len(result.Books))
	for i := range result.Books {
		book := result.Books[i]
		books = append(books, &check.BookSummary{
			BookId: book.BookID, Isbn: book.ISBN, Title: book.Title,
			Subtitle: book.Subtitle, Description: book.Description, Publisher: book.Publisher,
			PublicationDate: book.PublicationDate, LanguageCode: book.LanguageCode,
			PageCount: book.PageCount, CoverUrl: book.CoverURL, Price: book.Price,
			Currency: book.Currency, Status: book.Status, Version: book.Version,
			AvailableStock: book.AvailableStock, Authors: book.Authors, Categories: book.Categories,
		})
	}
	return &check.SearchBooksResp{Books: books, NextCursor: result.NextCursor}, nil
}
