package adderlogic

import (
	"context"

	"bookstore/rpc/add/adder"
	"bookstore/rpc/add/internal/svc"

	"github.com/zeromicro/go-zero/core/logx"
)

type DeleteLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

func NewDeleteLogic(ctx context.Context, svcCtx *svc.ServiceContext) *DeleteLogic {
	return &DeleteLogic{
		ctx:    ctx,
		svcCtx: svcCtx,
		Logger: logx.WithContext(ctx),
	}
}

func (l *DeleteLogic) Delete(in *adder.DeleteReq) (*adder.DeleteResp, error) {
	err := l.svcCtx.Model.Delete(in.Book)
	if err != nil {
		return nil, err
	}
	return &adder.DeleteResp{Ok: true}, nil
}
