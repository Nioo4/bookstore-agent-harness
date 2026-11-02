package authn

import (
	"crypto/ed25519"
	"crypto/rand"
	"database/sql"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v4"
)

func TestEd25519AccessTokenClaimsAndTTL(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	token, ttl, err := NewEd25519Signer(privateKey, "bookstore").Sign(User{
		UserID: 7, Email: "user@example.com", DisplayName: "Alice", EmailVerifiedAt: sql.NullTime{Valid: true},
	}, now)
	if err != nil {
		t.Fatal(err)
	}
	if ttl != 8*time.Hour {
		t.Fatalf("ttl=%s; want 8h", ttl)
	}
	claims := &AccessClaims{}
	parser := jwt.NewParser(jwt.WithoutClaimsValidation())
	parsed, err := parser.ParseWithClaims(token, claims, func(token *jwt.Token) (any, error) {
		return publicKey, nil
	})
	if err != nil || !parsed.Valid {
		t.Fatalf("parse signed token: valid=%v err=%v", parsed.Valid, err)
	}
	if claims.Subject != "7" || claims.Issuer != "bookstore" || claims.Email != "user@example.com" || claims.DisplayName != "Alice" || !claims.EmailVerified {
		t.Fatalf("unexpected claims: %+v", claims)
	}
	if claims.ExpiresAt.Time.Sub(claims.IssuedAt.Time) != 8*time.Hour {
		t.Fatalf("claim lifetime=%s", claims.ExpiresAt.Time.Sub(claims.IssuedAt.Time))
	}
}
