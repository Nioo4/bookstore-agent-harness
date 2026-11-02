package ordering

import (
	"context"
	"errors"
	"testing"
	"time"

	"bookstore/rpc/auth/jwtauth"
)

type fakeVerifier struct {
	user jwtauth.UserContext
	err  error
}

func (v *fakeVerifier) Verify(string) (jwtauth.UserContext, error) { return v.user, v.err }

type fakeOrderStore struct {
	userID         int64
	items          []ItemInput
	digest         []byte
	preview        *Preview
	order          *Order
	result         *ConfirmationResult
	conversationID string
	decision       string
	err            error
	listBefore     int64
	listLimit      int
	orders         []Order
}

func (s *fakeOrderStore) Prepare(_ context.Context, userID int64, _ string, items []ItemInput, _ string, digest []byte, expiresAt time.Time) (*Preview, error) {
	s.userID, s.items, s.digest = userID, items, append([]byte(nil), digest...)
	if s.preview != nil {
		s.preview.ExpiresAt = expiresAt
	}
	return s.preview, s.err
}
func (s *fakeOrderStore) Decide(_ context.Context, userID int64, conversationID string, digest []byte, decision string, _ time.Time) (*ConfirmationResult, error) {
	s.userID, s.conversationID, s.decision, s.digest = userID, conversationID, decision, append([]byte(nil), digest...)
	return s.result, s.err
}
func (s *fakeOrderStore) Get(context.Context, int64, int64) (*Order, error) { return s.order, s.err }
func (s *fakeOrderStore) List(_ context.Context, _ int64, before int64, limit int) ([]Order, error) {
	s.listBefore, s.listLimit = before, limit
	return s.orders, s.err
}

type fakeInvalidator struct {
	calls int
	items []Item
}

func (i *fakeInvalidator) InvalidateBooks(_ context.Context, items []Item) error {
	i.calls++
	i.items = items
	return nil
}

func newOrderService(t *testing.T, store *fakeOrderStore, verified bool) (*Service, *fakeInvalidator) {
	t.Helper()
	invalidation := &fakeInvalidator{}
	service, err := NewService(store, &fakeVerifier{user: jwtauth.UserContext{UserID: 77, EmailVerified: verified}}, invalidation, 10*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	service.now = func() time.Time { return time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC) }
	return service, invalidation
}

func TestPrepareRequiresVerifiedEmailAndNormalizesItems(t *testing.T) {
	store := &fakeOrderStore{preview: &Preview{Currency: "CNY"}}
	service, _ := newOrderService(t, store, false)
	if _, _, err := service.Prepare(t.Context(), "jwt", "conversation", "idem", []ItemInput{{BookID: 1, Quantity: 1}}); !errors.Is(err, ErrEmailNotVerified) {
		t.Fatalf("unverified prepare error=%v", err)
	}
	service, _ = newOrderService(t, store, true)
	preview, token, err := service.Prepare(t.Context(), "jwt", "conversation", "idem", []ItemInput{
		{BookID: 2, Quantity: 1}, {BookID: 1, Quantity: 2}, {BookID: 2, Quantity: 3},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(token) != 43 || len(store.digest) != 32 || preview.ExpiresAt.Sub(service.now()) != 10*time.Minute {
		t.Fatalf("confirmation metadata token=%d digest=%d expiry=%s", len(token), len(store.digest), preview.ExpiresAt)
	}
	if len(store.items) != 2 || store.items[0] != (ItemInput{BookID: 1, Quantity: 2}) || store.items[1] != (ItemInput{BookID: 2, Quantity: 4}) {
		t.Fatalf("items not merged/sorted: %+v", store.items)
	}
}

func TestDecideUsesAuthenticatedUserAndInvalidatesOnlyNewApprovedOrder(t *testing.T) {
	item := Item{BookID: 1, ISBN: "isbn-1", Quantity: 1}
	store := &fakeOrderStore{result: &ConfirmationResult{ConfirmationID: "action-1", Decision: "APPROVE",
		Status: "COMPLETED", Order: &Order{OrderID: 9, Items: []Item{item}}}}
	service, invalidation := newOrderService(t, store, true)
	created, err := service.Decide(t.Context(), "jwt", "conversation", "confirmation", "approve")
	if err != nil || created.Order.OrderID != 9 || store.userID != 77 || len(store.digest) != 32 ||
		store.conversationID != "conversation" || store.decision != "APPROVE" {
		t.Fatalf("created=%+v user=%d digest=%d err=%v", created, store.userID, len(store.digest), err)
	}
	if invalidation.calls != 1 || len(invalidation.items) != 1 {
		t.Fatalf("cache invalidation=%+v", invalidation)
	}
	store.result = &ConfirmationResult{ConfirmationID: "action-1", Decision: "APPROVE", Status: "COMPLETED",
		Order: &Order{OrderID: 9, Items: []Item{item}, Replayed: true}, Replayed: true}
	if _, err := service.Decide(t.Context(), "jwt", "conversation", "confirmation", "APPROVE"); err != nil {
		t.Fatal(err)
	}
	if invalidation.calls != 1 {
		t.Fatal("replayed confirmation invalidated cache again")
	}
}

func TestRejectDoesNotInvalidateAndInvalidDecisionIsRejected(t *testing.T) {
	store := &fakeOrderStore{result: &ConfirmationResult{ConfirmationID: "action-1", Decision: "REJECT", Status: "CANCELLED"}}
	service, invalidation := newOrderService(t, store, true)
	result, err := service.Decide(t.Context(), "jwt", "conversation", "confirmation", "REJECT")
	if err != nil || result.Status != "CANCELLED" || invalidation.calls != 0 {
		t.Fatalf("result=%+v invalidations=%d err=%v", result, invalidation.calls, err)
	}
	if _, err := service.Decide(t.Context(), "jwt", "conversation", "confirmation", "maybe"); !errors.Is(err, ErrInvalidOrderRequest) {
		t.Fatalf("invalid decision error=%v", err)
	}
}

func TestListUsesOpaqueCursorAndBoundsLimit(t *testing.T) {
	store := &fakeOrderStore{orders: []Order{{OrderID: 9}, {OrderID: 8}, {OrderID: 7}}}
	service, _ := newOrderService(t, store, true)
	page, err := service.List(t.Context(), "jwt", encodeOrderCursor(10), 2)
	if err != nil {
		t.Fatal(err)
	}
	if store.listBefore != 10 || store.listLimit != 3 || len(page.Orders) != 2 || page.NextCursor != encodeOrderCursor(8) {
		t.Fatalf("page=%+v before=%d limit=%d", page, store.listBefore, store.listLimit)
	}
	if _, err := service.List(t.Context(), "jwt", "invalid!", 2); err == nil {
		t.Fatal("invalid cursor accepted")
	}
	if _, err := service.List(t.Context(), "jwt", "", 51); err == nil {
		t.Fatal("oversized limit accepted")
	}
}
