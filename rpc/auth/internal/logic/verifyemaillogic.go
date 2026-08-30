package logic

import (
	"context"

	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/internal/svc"

	"github.com/zeromicro/go-zero/core/logx"
)

type VerifyEmailLogic struct {
	ctx context.Context
	svc *svc.ServiceContext
	logx.Logger
}

func NewVerifyEmailLogic(ctx context.Context, svc *svc.ServiceContext) *VerifyEmailLogic {
	return &VerifyEmailLogic{ctx: ctx, svc: svc, Logger: logx.WithContext(ctx)}
}

func (l *VerifyEmailLogic) VerifyEmail(in *auth.VerifyEmailReq) (*auth.VerifyEmailResp, error) {
	userID, err := l.svc.Auth.VerifyEmail(l.ctx, in.Token)
	if err != nil {
		return nil, mapAuthError(err)
	}
	return &auth.VerifyEmailResp{UserId: userID, EmailVerified: true}, nil
}
