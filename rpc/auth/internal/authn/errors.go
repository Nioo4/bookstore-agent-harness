package authn

import "errors"

var (
	ErrEmailExists           = errors.New("email already registered")
	ErrAuthRequired          = errors.New("authentication required")
	ErrInvalidCredentials    = errors.New("invalid email or password")
	ErrVerificationInvalid   = errors.New("verification token is invalid")
	ErrVerificationExpired   = errors.New("verification token has expired")
	ErrVerificationUsed      = errors.New("verification token was already used")
	ErrRateLimited           = errors.New("too many authentication attempts")
	ErrDependencyUnavailable = errors.New("authentication dependency unavailable")
)

type RateLimitError struct{ RetryAfterSeconds int64 }

func (e *RateLimitError) Error() string { return ErrRateLimited.Error() }
func (e *RateLimitError) Unwrap() error { return ErrRateLimited }
