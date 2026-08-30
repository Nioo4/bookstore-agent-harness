// Code scaffolded by goctl. Safe to edit.
// goctl 1.10.1

package handler

import (
	"net/http"

	"bookstore/api/internal/logic"
	"bookstore/api/internal/svc"
	"bookstore/api/internal/types"
	"github.com/zeromicro/go-zero/rest/httpx"
)

func DeleteBookHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req types.DeleteBookReq
		if err := httpx.Parse(r, &req); err != nil {
			writeRequestError(w, r, err)
			return
		}

		l := logic.NewDeleteBookLogic(r.Context(), svcCtx)
		resp, err := l.DeleteBook(&req)
		if err != nil {
			writeRPCError(w, r, err)
		} else {
			httpx.OkJsonCtx(r.Context(), w, resp)
		}
	}
}
