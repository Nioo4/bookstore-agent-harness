package logic

import (
	"context"
	"errors"
	"time"

	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/internal/authn"
	"bookstore/rpc/auth/internal/svc"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
)

func ResendVerification(ctx context.Context, services *svc.ServiceContext, in *auth.ResendVerificationReq) (*auth.ResendVerificationResp, error) {
	result, err := services.Auth.ResendVerification(ctx, in.AccessToken, in.ClientIp)
	if err != nil {
		return nil, mapResendError(err)
	}
	return &auth.ResendVerificationResp{Status: auth.ResendVerificationStatus(auth.ResendVerificationStatus_value[result.Status]), VerificationEmailSent: result.Sent, RetryAfterSeconds: result.RetryAfterSeconds}, nil
}

func mapResendError(err error) error {
	code, reason := codes.Unavailable, "DEPENDENCY_UNAVAILABLE"
	if errors.Is(err, authn.ErrAuthRequired) {
		code, reason = codes.Unauthenticated, "AUTH_REQUIRED"
	}
	if errors.Is(err, authn.ErrRateLimited) {
		code, reason = codes.ResourceExhausted, "AUTH_RATE_LIMITED"
	}
	s, _ := status.New(code, reason).WithDetails(&errdetails.ErrorInfo{Reason: reason, Domain: "bookstore.auth"})
	var limited *authn.RateLimitError
	if errors.As(err, &limited) {
		s, _ = s.WithDetails(&errdetails.RetryInfo{RetryDelay: durationpb.New(time.Duration(limited.RetryAfterSeconds) * time.Second)})
	}
	return s.Err()
}
