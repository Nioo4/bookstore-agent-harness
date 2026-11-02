package adderlogic

import (
	"context"
	"database/sql"
	"errors"
	"testing"

	"bookstore/rpc/add/adder"
	"bookstore/rpc/add/internal/svc"
	"bookstore/rpc/model"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type deductStockModel struct {
	err      error
	called   bool
	replayed bool
}

func (m *deductStockModel) Insert(model.Book) (sql.Result, error) { panic("not used") }
func (m *deductStockModel) FindOne(string) (*model.Book, error)   { panic("not used") }
func (m *deductStockModel) FindOneCtx(context.Context, string) (*model.Book, error) {
	panic("not used")
}
func (m *deductStockModel) Update(model.Book) error { panic("not used") }
func (m *deductStockModel) Delete(string) error     { panic("not used") }
func (m *deductStockModel) DeductStock(_, _ int64) error {
	panic("not used")
}
func (m *deductStockModel) DeductStockIdempotent(_ context.Context, _, _ int64, _ string) (bool, error) {
	m.called = true
	return m.replayed, m.err
}

func TestDeductStockLogicSuccess(t *testing.T) {
	m := &deductStockModel{}
	logic := NewDeductStockLogic(t.Context(), &svc.ServiceContext{Model: m})
	resp, err := logic.DeductStock(&adder.DeductStockReq{BookId: 7, Quantity: 2, IdempotencyKey: "req-1"})
	if err != nil || !m.called || !resp.Ok || resp.BookId != 7 || resp.Deducted != 2 {
		t.Fatalf("response = %+v, called = %v, err = %v", resp, m.called, err)
	}
}

func TestDeductStockLogicMapsBusinessErrors(t *testing.T) {
	tests := []struct {
		name string
		err  error
		code codes.Code
	}{
		{name: "not found", err: model.ErrNotFound, code: codes.NotFound},
		{name: "insufficient", err: model.ErrInsufficientStock, code: codes.FailedPrecondition},
		{name: "internal", err: errors.New("db failed"), code: codes.Internal},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m := &deductStockModel{err: tt.err}
			logic := NewDeductStockLogic(t.Context(), &svc.ServiceContext{Model: m})
			_, err := logic.DeductStock(&adder.DeductStockReq{BookId: 7, Quantity: 1, IdempotencyKey: "req-1"})
			if status.Code(err) != tt.code {
				t.Fatalf("code = %v; want %v (err=%v)", status.Code(err), tt.code, err)
			}
		})
	}
}

func TestDeductStockLogicRejectsInvalidInputBeforeModel(t *testing.T) {
	m := &deductStockModel{}
	logic := NewDeductStockLogic(t.Context(), &svc.ServiceContext{Model: m})
	_, err := logic.DeductStock(&adder.DeductStockReq{BookId: 7, Quantity: 0})
	if status.Code(err) != codes.InvalidArgument || m.called {
		t.Fatalf("code = %v, called = %v", status.Code(err), m.called)
	}
}

func TestDeductStockLogicReturnsReplayFlag(t *testing.T) {
	m := &deductStockModel{replayed: true}
	logic := NewDeductStockLogic(t.Context(), &svc.ServiceContext{Model: m})
	resp, err := logic.DeductStock(&adder.DeductStockReq{BookId: 7, Quantity: 2, IdempotencyKey: "req-1"})
	if err != nil || !resp.Replayed {
		t.Fatalf("response = %+v, err = %v", resp, err)
	}
}
