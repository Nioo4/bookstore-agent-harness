// Package service is the composition boundary for the Auth module.
package service

import (
	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/internal/config"
	"bookstore/rpc/auth/internal/server"
	"bookstore/rpc/auth/internal/svc"
	"context"
	"google.golang.org/grpc"
)

type Config = config.Config

func NewServer(c Config) auth.AuthenticatorServer {
	return server.NewAuthenticatorServer(svc.NewServiceContext(c))
}

func Register(registrar grpc.ServiceRegistrar, c Config) {
	auth.RegisterAuthenticatorServer(registrar, NewServer(c))
}

func NewLocalClient(c Config) auth.AuthenticatorClient {
	return localClient{server: NewServer(c)}
}

type localClient struct{ server auth.AuthenticatorServer }

func (c localClient) Register(ctx context.Context, in *auth.RegisterReq, _ ...grpc.CallOption) (*auth.RegisterResp, error) {
	return c.server.Register(ctx, in)
}
func (c localClient) VerifyEmail(ctx context.Context, in *auth.VerifyEmailReq, _ ...grpc.CallOption) (*auth.VerifyEmailResp, error) {
	return c.server.VerifyEmail(ctx, in)
}
func (c localClient) Login(ctx context.Context, in *auth.LoginReq, _ ...grpc.CallOption) (*auth.LoginResp, error) {
	return c.server.Login(ctx, in)
}
func (c localClient) ResendVerification(ctx context.Context, in *auth.ResendVerificationReq, _ ...grpc.CallOption) (*auth.ResendVerificationResp, error) {
	return c.server.ResendVerification(ctx, in)
}
