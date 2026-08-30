package handler

import (
	"net/http"
	"strings"

	"bookstore/api/internal/logic"
	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"

	"github.com/zeromicro/go-zero/rest/httpx"
)

func DeductStockHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req types.DeductStockReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		req.IdempotencyKey = strings.TrimSpace(r.Header.Get("Idempotency-Key"))
		resp, err := logic.NewDeductStockLogic(r.Context(), svcCtx).DeductStock(&req)
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}
