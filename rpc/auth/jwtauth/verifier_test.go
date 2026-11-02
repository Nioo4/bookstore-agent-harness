package jwtauth

import (
	"crypto/ed25519"
	"crypto/rand"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v4"
)

func TestVerifierAcceptsValidEd25519Token(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}

	now := time.Now()
	raw := signTestToken(t, privateKey, claims{
		Email:         "reader@example.com",
		DisplayName:   "Alice",
		EmailVerified: true,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    "bookstore-auth",
			Subject:   "42",
			IssuedAt:  jwt.NewNumericDate(now.Add(-time.Minute)),
			ExpiresAt: jwt.NewNumericDate(now.Add(time.Hour)),
		},
	})

	user, err := NewVerifier(publicKey, "bookstore-auth").Verify("Bearer " + raw)
	if err != nil {
		t.Fatalf("Verify() error = %v", err)
	}
	if user.UserID != 42 || user.Email != "reader@example.com" || user.DisplayName != "Alice" || !user.EmailVerified {
		t.Fatalf("Verify() user = %+v", user)
	}
}

func TestVerifierRejectsInvalidClaimsAndAlgorithms(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	verifier := NewVerifier(publicKey, "bookstore-auth")

	tests := []struct {
		name   string
		claims claims
	}{
		{
			name: "wrong issuer",
			claims: claims{RegisteredClaims: jwt.RegisteredClaims{
				Issuer: "someone-else", Subject: "42", ExpiresAt: jwt.NewNumericDate(now.Add(time.Hour)),
			}},
		},
		{
			name: "expired",
			claims: claims{RegisteredClaims: jwt.RegisteredClaims{
				Issuer: "bookstore-auth", Subject: "42", ExpiresAt: jwt.NewNumericDate(now.Add(-time.Minute)),
			}},
		},
		{
			name:   "missing expiration",
			claims: claims{RegisteredClaims: jwt.RegisteredClaims{Issuer: "bookstore-auth", Subject: "42"}},
		},
		{
			name: "invalid subject",
			claims: claims{RegisteredClaims: jwt.RegisteredClaims{
				Issuer: "bookstore-auth", Subject: "not-a-user", ExpiresAt: jwt.NewNumericDate(now.Add(time.Hour)),
			}},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			raw := signTestToken(t, privateKey, test.claims)
			if _, err := verifier.Verify(raw); err != ErrInvalidToken {
				t.Fatalf("Verify() error = %v, want %v", err, ErrInvalidToken)
			}
		})
	}

	hmacToken := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"iss": "bookstore-auth", "sub": "42", "exp": now.Add(time.Hour).Unix(),
	})
	hmacRaw, err := hmacToken.SignedString([]byte("not-an-ed25519-key"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verifier.Verify(hmacRaw); err != ErrInvalidToken {
		t.Fatalf("Verify(HS256) error = %v, want %v", err, ErrInvalidToken)
	}
}

func signTestToken(t *testing.T, privateKey ed25519.PrivateKey, tokenClaims claims) string {
	t.Helper()
	raw, err := jwt.NewWithClaims(jwt.SigningMethodEdDSA, tokenClaims).SignedString(privateKey)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestVerifierFrozenExpiryAndWrongKey(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	expires := time.Date(2026, 8, 28, 12, 0, 0, 0, time.UTC)
	originalClock := jwt.TimeFunc
	t.Cleanup(func() { jwt.TimeFunc = originalClock })
	raw := signTestToken(t, privateKey, claims{RegisteredClaims: jwt.RegisteredClaims{
		Issuer: "bookstore-auth", Subject: "42", ExpiresAt: jwt.NewNumericDate(expires),
	}})
	for _, offset := range []time.Duration{-time.Second, 0, time.Second} {
		jwt.TimeFunc = func() time.Time { return expires.Add(offset) }
		_, err := NewVerifier(publicKey, "bookstore-auth").Verify(raw)
		if (err == nil) != (offset < 0) {
			t.Fatal("JWT expiry must be rejected")
		}
	}
	otherKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	jwt.TimeFunc = func() time.Time { return expires.Add(-time.Second) }
	if _, err := NewVerifier(otherKey, "bookstore-auth").Verify(raw); err != ErrInvalidToken {
		t.Fatal("wrong signing key accepted")
	}
}
