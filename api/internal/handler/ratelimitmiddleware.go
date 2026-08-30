package handler

import (
	"net/http"

	"bookstore/api/internal/svc"

	"github.com/zeromicro/go-zero/core/metric"
)

var rateLimitRejected = metric.NewCounterVec(&metric.CounterVecOpts{
	Namespace: "bookstore",
	Subsystem: "api",
	Name:      "rate_limit_rejected_total",
	Help:      "Number of requests rejected by the bookstore API rate limiter.",
	Labels:    []string{"route"},
})

type RateLimitMiddleware struct {
	limiter svc.RateLimiter
}

func NewRateLimitMiddleware(limiter svc.RateLimiter) *RateLimitMiddleware {
	return &RateLimitMiddleware{limiter: limiter}
}

func (m *RateLimitMiddleware) Handle(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if m.limiter != nil && !m.limiter.AllowCtx(r.Context()) {
			rateLimitRejected.Inc("stock_deduct")
			w.Header().Set("Retry-After", "1")
			writeAPIError(w, r, http.StatusTooManyRequests, "RATE_LIMITED", "too many requests", true)
			return
		}
		next(w, r)
	}
}
