package ordering

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"

	"bookstore/rpc/auth/jwtauth"
)

type IdentityVerifier interface {
	Verify(raw string) (jwtauth.UserContext, error)
}

type CacheInvalidator interface {
	InvalidateBooks(ctx context.Context, items []Item) error
}

type Service struct {
	store           Store
	verifier        IdentityVerifier
	invalidation    CacheInvalidator
	confirmationTTL time.Duration
	now             func() time.Time
}

func NewService(store Store, verifier IdentityVerifier, invalidation CacheInvalidator, confirmationTTL time.Duration) (*Service, error) {
	if store == nil || verifier == nil || invalidation == nil {
		return nil, fmt.Errorf("order store, identity verifier and cache invalidator are required")
	}
	if confirmationTTL <= 0 {
		return nil, fmt.Errorf("confirmation TTL must be positive")
	}
	return &Service{store: store, verifier: verifier, invalidation: invalidation,
		confirmationTTL: confirmationTTL, now: time.Now}, nil
}

func (s *Service) Prepare(ctx context.Context, accessToken, conversationID, idempotencyKey string, items []ItemInput) (*Preview, string, error) {
	user, err := s.authenticate(accessToken, true)
	if err != nil {
		return nil, "", err
	}
	conversationID = strings.TrimSpace(conversationID)
	idempotencyKey = strings.TrimSpace(idempotencyKey)
	if conversationID == "" || len(conversationID) > 64 {
		return nil, "", fmt.Errorf("%w: conversation_id must contain 1 to 64 characters", ErrInvalidOrderRequest)
	}
	if idempotencyKey == "" || len(idempotencyKey) > 64 {
		return nil, "", fmt.Errorf("%w: idempotency_key must contain 1 to 64 characters", ErrInvalidOrderRequest)
	}
	normalized, err := normalizeItems(items)
	if err != nil {
		return nil, "", err
	}
	raw, digest, err := generateConfirmationToken()
	if err != nil {
		return nil, "", err
	}
	preview, err := s.store.Prepare(ctx, user.UserID, conversationID, normalized, idempotencyKey,
		digest[:], s.now().UTC().Add(s.confirmationTTL))
	if err != nil {
		return nil, "", err
	}
	return preview, raw, nil
}

func (s *Service) Decide(ctx context.Context, accessToken, conversationID, rawConfirmation, decision string) (*ConfirmationResult, error) {
	user, err := s.authenticate(accessToken, true)
	if err != nil {
		return nil, err
	}
	rawConfirmation = strings.TrimSpace(rawConfirmation)
	conversationID = strings.TrimSpace(conversationID)
	decision = strings.ToUpper(strings.TrimSpace(decision))
	if rawConfirmation == "" || conversationID == "" || len(conversationID) > 64 {
		return nil, ErrInvalidOrderRequest
	}
	if decision != "APPROVE" && decision != "REJECT" {
		return nil, ErrInvalidOrderRequest
	}
	digest := sha256.Sum256([]byte(rawConfirmation))
	result, err := s.store.Decide(ctx, user.UserID, conversationID, digest[:], decision, s.now().UTC())
	if err != nil {
		return nil, err
	}
	if decision == "APPROVE" && !result.Replayed && result.Order != nil {
		_ = s.invalidation.InvalidateBooks(ctx, result.Order.Items)
	}
	return result, nil
}

func (s *Service) Get(ctx context.Context, accessToken string, orderID int64) (*Order, error) {
	user, err := s.authenticate(accessToken, false)
	if err != nil {
		return nil, err
	}
	if orderID <= 0 {
		return nil, fmt.Errorf("%w: order_id must be positive", ErrInvalidOrderRequest)
	}
	return s.store.Get(ctx, user.UserID, orderID)
}

func (s *Service) List(ctx context.Context, accessToken, cursor string, limit int) (*OrderPage, error) {
	user, err := s.authenticate(accessToken, false)
	if err != nil {
		return nil, err
	}
	if limit == 0 {
		limit = 20
	}
	if limit < 1 || limit > 50 {
		return nil, fmt.Errorf("%w: limit must be between 1 and 50", ErrInvalidOrderRequest)
	}
	before, err := decodeOrderCursor(cursor)
	if err != nil {
		return nil, err
	}
	orders, err := s.store.List(ctx, user.UserID, before, limit+1)
	if err != nil {
		return nil, err
	}
	page := &OrderPage{Orders: orders}
	if len(page.Orders) > limit {
		page.Orders = page.Orders[:limit]
		page.NextCursor = encodeOrderCursor(page.Orders[len(page.Orders)-1].OrderID)
	}
	return page, nil
}

func (s *Service) authenticate(raw string, requireVerified bool) (jwtauth.UserContext, error) {
	user, err := s.verifier.Verify(raw)
	if err != nil {
		return jwtauth.UserContext{}, ErrUnauthorized
	}
	if requireVerified && !user.EmailVerified {
		return jwtauth.UserContext{}, ErrEmailNotVerified
	}
	return user, nil
}

func normalizeItems(items []ItemInput) ([]ItemInput, error) {
	if len(items) == 0 {
		return nil, fmt.Errorf("%w: at least one order item is required", ErrInvalidOrderRequest)
	}
	quantities := make(map[int64]int64, len(items))
	for _, item := range items {
		if item.BookID <= 0 || item.Quantity <= 0 {
			return nil, fmt.Errorf("%w: book_id and quantity must be positive", ErrInvalidOrderRequest)
		}
		quantity, err := checkedAdd(quantities[item.BookID], item.Quantity)
		if err != nil {
			return nil, err
		}
		quantities[item.BookID] = quantity
	}
	result := make([]ItemInput, 0, len(quantities))
	for bookID, quantity := range quantities {
		result = append(result, ItemInput{BookID: bookID, Quantity: quantity})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].BookID < result[j].BookID })
	return result, nil
}

func checkedMultiply(a, b int64) (int64, error) {
	if a < 0 || b < 0 || (a != 0 && b > math.MaxInt64/a) {
		return 0, fmt.Errorf("%w: order amount overflow", ErrInvalidOrderRequest)
	}
	return a * b, nil
}
func checkedAdd(a, b int64) (int64, error) {
	if a < 0 || b < 0 || b > math.MaxInt64-a {
		return 0, fmt.Errorf("%w: order amount overflow", ErrInvalidOrderRequest)
	}
	return a + b, nil
}

func generateConfirmationToken() (string, [32]byte, error) {
	value := make([]byte, 32)
	var digest [32]byte
	if _, err := rand.Read(value); err != nil {
		return "", digest, err
	}
	raw := base64.RawURLEncoding.EncodeToString(value)
	digest = sha256.Sum256([]byte(raw))
	return raw, digest, nil
}

func encodeOrderCursor(orderID int64) string {
	return base64.RawURLEncoding.EncodeToString([]byte(strconv.FormatInt(orderID, 10)))
}
func decodeOrderCursor(cursor string) (int64, error) {
	if strings.TrimSpace(cursor) == "" {
		return 0, nil
	}
	decoded, err := base64.RawURLEncoding.DecodeString(cursor)
	if err != nil {
		return 0, fmt.Errorf("%w: invalid cursor", ErrInvalidOrderRequest)
	}
	value, err := strconv.ParseInt(string(decoded), 10, 64)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%w: invalid cursor", ErrInvalidOrderRequest)
	}
	return value, nil
}
