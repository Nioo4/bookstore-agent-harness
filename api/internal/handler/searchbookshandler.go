package handler

import (
	"net/http"

	"bookstore/api/internal/logic"
	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"

	"github.com/zeromicro/go-zero/rest/httpx"
)

func SearchBooksHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req types.SearchBooksReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}
		l := logic.NewSearchBooksLogic(r.Context(), svcCtx)
		resp, err := l.SearchBooks(&req)
		if err != nil {
			writeRPCError(w, r, err)
			return
		}
		httpx.OkJsonCtx(r.Context(), w, resp)
	}
}
