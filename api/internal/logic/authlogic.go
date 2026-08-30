package logic

import (
	"context"

	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/auth/authenticator"

	"github.com/zeromicro/go-zero/core/logx"
)

type AuthLogic struct {
	ctx context.Context
	svc *svc.ServiceContext
	logx.Logger
}

func NewAuthLogic(ctx context.Context, svc *svc.ServiceContext) *AuthLogic {
	return &AuthLogic{ctx: ctx, svc: svc, Logger: logx.WithContext(ctx)}
}

func (l *AuthLogic) Register(req *types.RegisterReq, clientIP string) (*types.RegisterResp, error) {
	resp, err := l.svc.Authenticator.Register(l.ctx, &authenticator.RegisterReq{
		Email: req.Email, Password: req.Password, DisplayName: req.DisplayName, ClientIp: clientIP,
	})
	if err != nil {
		return nil, err
	}
	return &types.RegisterResp{UserID: resp.UserId, Email: resp.Email, VerificationEmailSent: resp.VerificationEmailSent}, nil
}

func (l *AuthLogic) VerifyEmail(req *types.VerifyEmailReq, clientIP string) (*types.VerifyEmailResp, error) {
	resp, err := l.svc.Authenticator.VerifyEmail(l.ctx, &authenticator.VerifyEmailReq{Token: req.Token, ClientIp: clientIP})
	if err != nil {
		return nil, err
	}
	return &types.VerifyEmailResp{UserID: resp.UserId, EmailVerified: resp.EmailVerified}, nil
}

func (l *AuthLogic) Login(req *types.LoginReq, clientIP string) (*types.LoginResp, error) {
	resp, err := l.svc.Authenticator.Login(l.ctx, &authenticator.LoginReq{Email: req.Email, Password: req.Password, ClientIp: clientIP})
	if err != nil {
		return nil, err
	}
	return &types.LoginResp{AccessToken: resp.AccessToken, ExpiresIn: resp.ExpiresIn, User: types.AuthUser{
		UserID: resp.User.UserId, Email: resp.User.Email, DisplayName: resp.User.DisplayName, EmailVerified: resp.User.EmailVerified,
	}}, nil
}
