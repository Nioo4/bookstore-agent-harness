package handler

import (
	"net/http"

	"bookstore/api/internal/logic"
	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"

	"github.com/zeromicro/go-zero/rest/httpx"
)

func GetHotBookHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req types.HotBookReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		if req.BookID <= 0 {
			writeAPIError(w, r, http.StatusBadRequest, "INVALID_ARGUMENT", "book_id must be positive", false)
			return
		}
		resp, err := logic.NewGetHotBookLogic(r.Context(), svcCtx).GetHotBook(&req)
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}
