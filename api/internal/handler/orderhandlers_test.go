package handler

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"bookstore/api/internal/svc"
	"bookstore/rpc/auth/jwtauth"
	"bookstore/rpc/order/orderservice"

	"github.com/golang-jwt/jwt/v4"
	"github.com/zeromicro/go-zero/rest/pathvar"
	"google.golang.org/grpc"
)

type fakeOrders struct {
	getReq *orderservice.GetOrderReq
}

func (f *fakeOrders) PrepareOrder(context.Context, *orderservice.PrepareOrderReq, ...grpc.CallOption) (*orderservice.PrepareOrderResp, error) {
	panic("unexpected PrepareOrder")
}

func (f *fakeOrders) DecideConfirmation(context.Context, *orderservice.DecideConfirmationReq, ...grpc.CallOption) (*orderservice.ConfirmationResult, error) {
	panic("unexpected DecideConfirmation")
}

func (f *fakeOrders) GetOrder(_ context.Context, req *orderservice.GetOrderReq, _ ...grpc.CallOption) (*orderservice.OrderRecord, error) {
	f.getReq = req
	return &orderservice.OrderRecord{OrderId: req.OrderId, Status: "PLACED", Currency: "CNY"}, nil
}

func (f *fakeOrders) ListMyOrders(context.Context, *orderservice.ListMyOrdersReq, ...grpc.CallOption) (*orderservice.ListMyOrdersResp, error) {
	return &orderservice.ListMyOrdersResp{}, nil
}

func orderHandlerIdentity(t *testing.T) (*jwtauth.Verifier, string) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	token, err := jwt.NewWithClaims(jwt.SigningMethodEdDSA, jwt.MapClaims{
		"iss": "test", "sub": "101", "exp": time.Now().Add(time.Hour).Unix(),
		"email_verified": true,
	}).SignedString(private)
	if err != nil {
		t.Fatal(err)
	}
	return jwtauth.NewVerifier(public, "test"), token
}

func TestOrderHandlerRequiresVerifiedCookieAndIgnoresAuthorization(t *testing.T) {
	verifier, token := orderHandlerIdentity(t)
	orders := &fakeOrders{}
	handler := GetOrderHandler(&svc.ServiceContext{Orders: orders, AuthVerifier: verifier})

	headerOnly := httptest.NewRequest(http.MethodGet, "/api/orders/42", nil)
	headerOnly = pathvar.WithVars(headerOnly, map[string]string{"order_id": "42"})
	headerOnly.Header.Set("Authorization", "Bearer "+token)
	headerOnlyResponse := httptest.NewRecorder()
	handler(headerOnlyResponse, headerOnly)
	if headerOnlyResponse.Code != http.StatusUnauthorized || orders.getReq != nil {
		t.Fatalf("header-only order read status=%d crossed RPC=%t", headerOnlyResponse.Code, orders.getReq != nil)
	}

	invalidCookie := httptest.NewRequest(http.MethodGet, "/api/orders/42", nil)
	invalidCookie = pathvar.WithVars(invalidCookie, map[string]string{"order_id": "42"})
	invalidCookie.AddCookie(&http.Cookie{Name: accessCookieName, Value: "invalid"})
	invalidCookie.Header.Set("Authorization", "Bearer "+token)
	invalidCookieResponse := httptest.NewRecorder()
	handler(invalidCookieResponse, invalidCookie)
	if invalidCookieResponse.Code != http.StatusUnauthorized || orders.getReq != nil {
		t.Fatalf("invalid cookie fallback status=%d crossed RPC=%t", invalidCookieResponse.Code, orders.getReq != nil)
	}

	cookieRequest := httptest.NewRequest(http.MethodGet, "/api/orders/42", nil)
	cookieRequest = pathvar.WithVars(cookieRequest, map[string]string{"order_id": "42"})
	cookieRequest.AddCookie(&http.Cookie{Name: accessCookieName, Value: token})
	cookieResponse := httptest.NewRecorder()
	handler(cookieResponse, cookieRequest)
	if cookieResponse.Code != http.StatusOK || orders.getReq == nil || orders.getReq.AccessToken != token || orders.getReq.OrderId != 42 {
		t.Fatalf("verified cookie status=%d request=%+v", cookieResponse.Code, orders.getReq)
	}
	if cookieResponse.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("authenticated order response is cacheable")
	}
}
