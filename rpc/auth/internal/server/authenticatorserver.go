package server

import (
	"context"

	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/internal/logic"
	"bookstore/rpc/auth/internal/svc"
)

type AuthenticatorServer struct {
	svc *svc.ServiceContext
	auth.UnimplementedAuthenticatorServer
}

func NewAuthenticatorServer(svc *svc.ServiceContext) *AuthenticatorServer {
	return &AuthenticatorServer{svc: svc}
}

func (s *AuthenticatorServer) Register(ctx context.Context, in *auth.RegisterReq) (*auth.RegisterResp, error) {
	return logic.NewRegisterLogic(ctx, s.svc).Register(in)
}

func (s *AuthenticatorServer) VerifyEmail(ctx context.Context, in *auth.VerifyEmailReq) (*auth.VerifyEmailResp, error) {
	return logic.NewVerifyEmailLogic(ctx, s.svc).VerifyEmail(in)
}

func (s *AuthenticatorServer) Login(ctx context.Context, in *auth.LoginReq) (*auth.LoginResp, error) {
	return logic.NewLoginLogic(ctx, s.svc).Login(in)
}

func (s *AuthenticatorServer) ResendVerification(ctx context.Context, in *auth.ResendVerificationReq) (*auth.ResendVerificationResp, error) {
	return logic.ResendVerification(ctx, s.svc, in)
}
