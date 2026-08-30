package logic

import (
	"context"

	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/internal/svc"

	"github.com/zeromicro/go-zero/core/logx"
)

type LoginLogic struct {
	ctx context.Context
	svc *svc.ServiceContext
	logx.Logger
}

func NewLoginLogic(ctx context.Context, svc *svc.ServiceContext) *LoginLogic {
	return &LoginLogic{ctx: ctx, svc: svc, Logger: logx.WithContext(ctx)}
}

func (l *LoginLogic) Login(in *auth.LoginReq) (*auth.LoginResp, error) {
	result, err := l.svc.Auth.Login(l.ctx, in.Email, in.Password, in.ClientIp)
	if err != nil {
		return nil, mapAuthError(err)
	}
	return &auth.LoginResp{
		AccessToken: result.AccessToken, ExpiresIn: result.ExpiresIn,
		User: &auth.AuthUser{UserId: result.User.UserID, Email: result.User.Email,
			DisplayName: result.User.DisplayName, EmailVerified: result.User.EmailVerifiedAt.Valid},
	}, nil
}
