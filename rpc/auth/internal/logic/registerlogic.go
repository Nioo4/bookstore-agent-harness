package logic

import (
	"context"

	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/internal/svc"

	"github.com/zeromicro/go-zero/core/logx"
)

type RegisterLogic struct {
	ctx context.Context
	svc *svc.ServiceContext
	logx.Logger
}

func NewRegisterLogic(ctx context.Context, svc *svc.ServiceContext) *RegisterLogic {
	return &RegisterLogic{ctx: ctx, svc: svc, Logger: logx.WithContext(ctx)}
}

func (l *RegisterLogic) Register(in *auth.RegisterReq) (*auth.RegisterResp, error) {
	result, err := l.svc.Auth.Register(l.ctx, in.Email, in.Password, in.DisplayName, in.ClientIp)
	if err != nil {
		return nil, mapAuthError(err)
	}
	return &auth.RegisterResp{UserId: result.UserID, Email: result.Email, VerificationEmailSent: result.Sent}, nil
}
