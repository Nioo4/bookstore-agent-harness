package ordering

import "errors"

var (
	ErrUnauthorized               = errors.New("invalid or missing access token")
	ErrEmailNotVerified           = errors.New("email must be verified")
	ErrBookNotFound               = errors.New("book not found")
	ErrBookInactive               = errors.New("book is inactive")
	ErrInventoryInsufficient      = errors.New("inventory insufficient")
	ErrConfirmationExpired        = errors.New("confirmation expired")
	ErrConfirmationAlreadyUsed    = errors.New("confirmation already used")
	ErrConfirmationNotFound       = errors.New("confirmation not found")
	ErrConfirmationAlreadyDecided = errors.New("confirmation already decided")
	ErrPriceChanged               = errors.New("price changed")
	ErrIdempotencyConflict        = errors.New("idempotency key conflict")
	ErrOrderNotFound              = errors.New("order not found")
	ErrInvalidOrderRequest        = errors.New("invalid order request")
)
