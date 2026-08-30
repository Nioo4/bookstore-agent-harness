package handler

import (
	"net/http"
	"strings"

	"bookstore/api/internal/logic"
	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"

	"github.com/zeromicro/go-zero/rest/httpx"
)

func GetOrderHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req types.GetOrderReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		resp, err := logic.NewOrderLogic(r.Context(), svcCtx).Get(bearerToken(r), req.OrderID)
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}
func ListOrdersHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req types.ListOrdersReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		resp, err := logic.NewOrderLogic(r.Context(), svcCtx).List(bearerToken(r), &req)
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}
func bearerToken(r *http.Request) string {
	return strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
}
