package handler

import (
	"net/http"

	"bookstore/api/internal/logic"
	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"

	"github.com/zeromicro/go-zero/rest/httpx"
)

func GetOrderHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		var req types.GetOrderReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		token, ok := verifiedBrowserAccessToken(w, r, svcCtx)
		if !ok {
			return
		}
		resp, err := logic.NewOrderLogic(r.Context(), svcCtx).Get(token, req.OrderID)
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}
func ListOrdersHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		var req types.ListOrdersReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		token, ok := verifiedBrowserAccessToken(w, r, svcCtx)
		if !ok {
			return
		}
		resp, err := logic.NewOrderLogic(r.Context(), svcCtx).List(token, &req)
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}

func verifiedBrowserAccessToken(w http.ResponseWriter, r *http.Request, svcCtx *svc.ServiceContext) (string, bool) {
	token := accessTokenFromRequest(r)
	if token == "" || svcCtx == nil || svcCtx.AuthVerifier == nil {
		writeAPIError(w, r, http.StatusUnauthorized, "AUTH_REQUIRED", "authentication required", false)
		return "", false
	}
	if _, err := svcCtx.AuthVerifier.Verify(token); err != nil {
		writeAPIError(w, r, http.StatusUnauthorized, "AUTH_REQUIRED", "authentication required", false)
		return "", false
	}
	return token, true
}
