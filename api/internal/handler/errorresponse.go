package handler

import (
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/zeromicro/go-zero/rest/httpx"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type apiErrorResponse struct {
	Error apiErrorDetail `json:"error"`
}

type apiErrorDetail struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
	TraceID   string `json:"trace_id,omitempty"`
}

func writeRequestError(w http.ResponseWriter, r *http.Request, err error) {
	writeAPIError(w, r, http.StatusBadRequest, "INVALID_ARGUMENT", err.Error(), false)
}

func writeRPCError(w http.ResponseWriter, r *http.Request, err error) {
	grpcStatus, ok := status.FromError(err)
	if !ok {
		writeAPIError(w, r, http.StatusInternalServerError, "INTERNAL_ERROR", "internal server error", true)
		return
	}
	if r.URL.Path == "/api/auth/resend-verification" {
		// This RPC has typed, stable business reasons. Never infer a code or
		// return raw dependency text from its status message.
		for _, detail := range grpcStatus.Details() {
			if retry, ok := detail.(*errdetails.RetryInfo); ok && retry.RetryDelay != nil {
				d := retry.RetryDelay.AsDuration()
				if d > 0 {
					w.Header().Set("Retry-After", strconv.FormatInt(int64((d+time.Second-1)/time.Second), 10))
				}
			}
		}
		for _, detail := range grpcStatus.Details() {
			info, ok := detail.(*errdetails.ErrorInfo)
			if !ok || info.Domain != "bookstore.auth" {
				continue
			}
			switch info.Reason {
			case "AUTH_REQUIRED":
				writeAPIError(w, r, 401, info.Reason, "authentication required", false)
				return
			case "AUTH_RATE_LIMITED":
				writeAPIError(w, r, 429, info.Reason, "too many authentication attempts", true)
				return
			}
		}
		writeAPIError(w, r, 503, "DEPENDENCY_UNAVAILABLE", "authentication dependency unavailable", true)
		return
	}

	httpStatus, code, retryable := http.StatusInternalServerError, "INTERNAL_ERROR", true
	message := grpcStatus.Message()
	switch grpcStatus.Code() {
	case codes.InvalidArgument:
		httpStatus, code, retryable = http.StatusBadRequest, "INVALID_ARGUMENT", false
		if strings.Contains(message, "verification token") {
			code = "EMAIL_VERIFICATION_INVALID"
		}
	case codes.Unauthenticated:
		code = "AUTH_REQUIRED"
		if r.URL.Path == "/api/auth/login" {
			code = "AUTH_INVALID_CREDENTIALS"
		}
		httpStatus, retryable = http.StatusUnauthorized, false
	case codes.PermissionDenied:
		httpStatus, code, retryable = http.StatusForbidden, "EMAIL_NOT_VERIFIED", false
	case codes.NotFound:
		httpStatus, code, retryable = http.StatusNotFound, "BOOK_NOT_FOUND", false
		if strings.Contains(message, "order") {
			code = "ORDER_NOT_FOUND"
		}
	case codes.FailedPrecondition:
		httpStatus, code, retryable = http.StatusConflict, "INVENTORY_INSUFFICIENT", false
		if strings.Contains(message, "verification token") {
			code = "EMAIL_VERIFICATION_EXPIRED"
		} else if strings.Contains(message, "confirmation") {
			code = "CONFIRMATION_EXPIRED"
		} else if strings.Contains(message, "price") {
			code = "PRICE_CHANGED"
		} else if strings.Contains(message, "inactive") {
			code = "BOOK_INACTIVE"
		}
	case codes.AlreadyExists:
		httpStatus, code, retryable = http.StatusConflict, "IDEMPOTENCY_CONFLICT", false
		if strings.Contains(message, "email already") {
			code = "AUTH_EMAIL_EXISTS"
		} else if strings.Contains(message, "verification token") {
			code = "EMAIL_VERIFICATION_ALREADY_USED"
		} else if strings.Contains(message, "confirmation") {
			code = "CONFIRMATION_ALREADY_USED"
		}
	case codes.ResourceExhausted:
		httpStatus, code, retryable = http.StatusTooManyRequests, "DEPENDENCY_OVERLOADED", true
		if strings.Contains(message, "authentication") {
			code = "AUTH_RATE_LIMITED"
		}
	case codes.DeadlineExceeded:
		httpStatus, code, retryable = http.StatusGatewayTimeout, "TOOL_TIMEOUT", true
	case codes.Unavailable:
		httpStatus, code, retryable = http.StatusServiceUnavailable, "DEPENDENCY_UNAVAILABLE", true
	}
	writeAPIError(w, r, httpStatus, code, message, retryable)
}

func writeAPIError(w http.ResponseWriter, r *http.Request, statusCode int, code, message string, retryable bool) {
	traceID := ""
	spanContext := trace.SpanContextFromContext(r.Context())
	if spanContext.IsValid() {
		traceID = spanContext.TraceID().String()
	}
	httpx.WriteJsonCtx(r.Context(), w, statusCode, apiErrorResponse{Error: apiErrorDetail{
		Code: code, Message: message, Retryable: retryable, TraceID: traceID,
	}})
}
