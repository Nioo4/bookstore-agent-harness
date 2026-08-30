package handler

import (
	"encoding/json"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"strings"

	"bookstore/api/internal/logic"
	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"bookstore/rpc/auth/auth"
	"bookstore/rpc/auth/authenticator"

	"github.com/zeromicro/go-zero/rest/httpx"
)

func RegisterHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		var req types.RegisterReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		resp, err := logic.NewAuthLogic(r.Context(), svcCtx).Register(&req, clientIP(r, svcCtx.Config.TrustedProxyCIDRs))
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.WriteJsonCtx(r.Context(), w, http.StatusAccepted, resp)
	}
}

func VerifyEmailHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		var req types.VerifyEmailReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		resp, err := logic.NewAuthLogic(r.Context(), svcCtx).VerifyEmail(&req, clientIP(r, svcCtx.Config.TrustedProxyCIDRs))
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}

func LoginHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		var req types.LoginReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		resp, err := logic.NewAuthLogic(r.Context(), svcCtx).Login(&req, clientIP(r, svcCtx.Config.TrustedProxyCIDRs))
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}

func clientIP(r *http.Request, trusted []string) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	peer, err := netip.ParseAddr(host)
	if err != nil {
		return "unknown"
	}
	for _, cidr := range trusted {
		prefix, err := netip.ParsePrefix(cidr)
		if err == nil && prefix.Contains(peer.Unmap()) {
			// Trusted proxies overwrite this single value; never accept a caller
			// supplied forwarding chain or use headers from an untrusted peer.
			if ip, err := netip.ParseAddr(strings.TrimSpace(r.Header.Get("X-Real-IP"))); err == nil {
				return ip.Unmap().String()
			}
		}
	}
	return peer.Unmap().String()
}

func ResendVerificationHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		var body map[string]json.RawMessage
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		if err != nil || mediaType != "application/json" || r.URL.RawQuery != "" || decoder.Decode(&body) != nil || body == nil || len(body) != 0 || decoder.Decode(new(any)) != io.EOF {
			writeAPIError(w, r, http.StatusBadRequest, "INVALID_ARGUMENT", "request must be an empty JSON object without query parameters", false)
			return
		}
		header := r.Header.Get("Authorization")
		if svcCtx.AuthVerifier == nil || !strings.HasPrefix(header, "Bearer ") {
			writeAPIError(w, r, http.StatusUnauthorized, "AUTH_REQUIRED", "authentication required", false)
			return
		}
		if _, err := svcCtx.AuthVerifier.Verify(header); err != nil {
			writeAPIError(w, r, http.StatusUnauthorized, "AUTH_REQUIRED", "authentication required", false)
			return
		}
		resp, err := svcCtx.Authenticator.ResendVerification(r.Context(), &authenticator.ResendVerificationReq{AccessToken: strings.TrimSpace(strings.TrimPrefix(header, "Bearer ")), ClientIp: clientIP(r, svcCtx.Config.TrustedProxyCIDRs)})
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		if resp == nil || (resp.Status != auth.ResendVerificationStatus_SMTP_ACCEPTED && resp.Status != auth.ResendVerificationStatus_DELIVERY_UNCONFIRMED && resp.Status != auth.ResendVerificationStatus_ALREADY_VERIFIED) {
			writeAPIError(w, r, http.StatusServiceUnavailable, "DEPENDENCY_UNAVAILABLE", "authentication dependency unavailable", true)
			return
		}
		code := http.StatusAccepted
		if resp.Status == auth.ResendVerificationStatus_ALREADY_VERIFIED {
			code = http.StatusOK
		}
		httpx.WriteJsonCtx(r.Context(), w, code, types.ResendVerificationResp{Status: resp.Status.String(), VerificationEmailSent: resp.VerificationEmailSent, RetryAfterSeconds: resp.RetryAfterSeconds})
	}
}
