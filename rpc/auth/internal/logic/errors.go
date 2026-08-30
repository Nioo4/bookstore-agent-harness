package logic

import (
	"errors"

	"bookstore/rpc/auth/internal/authn"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func mapAuthError(err error) error {
	switch {
	case errors.Is(err, authn.ErrEmailExists):
		return status.Error(codes.AlreadyExists, "email already registered")
	case errors.Is(err, authn.ErrInvalidCredentials):
		return status.Error(codes.Unauthenticated, "invalid email or password")
	case errors.Is(err, authn.ErrVerificationInvalid):
		return status.Error(codes.InvalidArgument, "verification token is invalid")
	case errors.Is(err, authn.ErrVerificationExpired):
		return status.Error(codes.FailedPrecondition, "verification token has expired")
	case errors.Is(err, authn.ErrVerificationUsed):
		return status.Error(codes.AlreadyExists, "verification token was already used")
	case errors.Is(err, authn.ErrRateLimited):
		return status.Error(codes.ResourceExhausted, "too many authentication attempts")
	case errors.Is(err, authn.ErrDependencyUnavailable):
		return status.Error(codes.Unavailable, "authentication dependency unavailable")
	default:
		// Only exact, application-authored validation messages are public.
		// Driver errors may also contain words like "password" or "email".
		switch err.Error() {
		case "invalid email", "password must contain 8 to 128 characters", "display_name must contain 1 to 100 characters":
			return status.Error(codes.InvalidArgument, err.Error())
		}
		return status.Error(codes.Internal, "authentication operation failed")
	}
}
