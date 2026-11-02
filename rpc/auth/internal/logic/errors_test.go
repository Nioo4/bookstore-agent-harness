package logic

import (
	"errors"
	"fmt"
	"testing"

	"bookstore/rpc/auth/internal/authn"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestUnknownAuthErrorsNeverExposeDriverMessages(t *testing.T) {
	for _, raw := range []string{
		"database access denied: password=private-secret",
		"email index failed for private@example.test",
		"display_name query failed: private-row-data",
	} {
		got := status.Convert(mapAuthError(errors.New(raw)))
		if got.Code() != codes.Internal || got.Message() != "authentication operation failed" {
			t.Fatal("unknown infrastructure error escaped the auth boundary")
		}
	}
	got := status.Convert(mapAuthError(fmt.Errorf("%w: password=private-secret", authn.ErrDependencyUnavailable)))
	if got.Code() != codes.Unavailable || got.Message() != "authentication dependency unavailable" {
		t.Fatal("wrapped dependency error escaped the auth boundary")
	}
}

func TestKnownAuthValidationErrorsKeepTheirContract(t *testing.T) {
	for _, message := range []string{"invalid email", "password must contain 8 to 128 characters", "display_name must contain 1 to 100 characters"} {
		got := status.Convert(mapAuthError(errors.New(message)))
		if got.Code() != codes.InvalidArgument || got.Message() != message {
			t.Fatal("public validation error contract changed")
		}
	}
}
