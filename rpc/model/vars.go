package model

import (
	"errors"

	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

var (
	ErrNotFound            = sqlx.ErrNotFound
	ErrInsufficientStock   = errors.New("insufficient stock")
	ErrIdempotencyConflict = errors.New("idempotency key conflicts with a different request")
	ErrCacheFallbackBusy   = errors.New("cache fallback concurrency limit reached")
	ErrInvalidBookSearch   = errors.New("invalid book search")
)
