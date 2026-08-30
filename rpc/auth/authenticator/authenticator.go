package authenticator

import (
	"context"

	"bookstore/rpc/auth/auth"

	"github.com/zeromicro/go-zero/zrpc"
	"google.golang.org/grpc"
)

type (
	RegisterReq            = auth.RegisterReq
	RegisterResp           = auth.RegisterResp
	VerifyEmailReq         = auth.VerifyEmailReq
	VerifyEmailResp        = auth.VerifyEmailResp
	LoginReq               = auth.LoginReq
	LoginResp              = auth.LoginResp
	AuthUser               = auth.AuthUser
	ResendVerificationReq  = auth.ResendVerificationReq
	ResendVerificationResp = auth.ResendVerificationResp

	Authenticator interface {
		Register(context.Context, *RegisterReq, ...grpc.CallOption) (*RegisterResp, error)
		VerifyEmail(context.Context, *VerifyEmailReq, ...grpc.CallOption) (*VerifyEmailResp, error)
		Login(context.Context, *LoginReq, ...grpc.CallOption) (*LoginResp, error)
		ResendVerification(context.Context, *ResendVerificationReq, ...grpc.CallOption) (*ResendVerificationResp, error)
	}

	client struct{ cli zrpc.Client }
)

func NewAuthenticator(cli zrpc.Client) Authenticator { return &client{cli: cli} }

func (c *client) Register(ctx context.Context, in *RegisterReq, opts ...grpc.CallOption) (*RegisterResp, error) {
	return auth.NewAuthenticatorClient(c.cli.Conn()).Register(ctx, in, opts...)
}
func (c *client) VerifyEmail(ctx context.Context, in *VerifyEmailReq, opts ...grpc.CallOption) (*VerifyEmailResp, error) {
	return auth.NewAuthenticatorClient(c.cli.Conn()).VerifyEmail(ctx, in, opts...)
}
func (c *client) Login(ctx context.Context, in *LoginReq, opts ...grpc.CallOption) (*LoginResp, error) {
	return auth.NewAuthenticatorClient(c.cli.Conn()).Login(ctx, in, opts...)
}

func (c *client) ResendVerification(ctx context.Context, in *ResendVerificationReq, opts ...grpc.CallOption) (*ResendVerificationResp, error) {
	return auth.NewAuthenticatorClient(c.cli.Conn()).ResendVerification(ctx, in, opts...)
}
