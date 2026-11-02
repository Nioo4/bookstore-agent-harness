package jwtauth

import (
	"crypto/ed25519"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/golang-jwt/jwt/v4"
)

var ErrInvalidToken = errors.New("invalid access token")

type UserContext struct {
	UserID        int64
	Email         string
	DisplayName   string
	EmailVerified bool
}

type claims struct {
	Email         string `json:"email"`
	DisplayName   string `json:"display_name"`
	EmailVerified bool   `json:"email_verified"`
	jwt.RegisteredClaims
}

type Verifier struct {
	publicKey ed25519.PublicKey
	issuer    string
}

func NewVerifier(publicKey ed25519.PublicKey, issuer string) *Verifier {
	return &Verifier{publicKey: publicKey, issuer: issuer}
}

func LoadVerifier(path, issuer string) (*Verifier, error) {
	if strings.TrimSpace(path) == "" {
		return nil, fmt.Errorf("JWT public key path is required")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read JWT public key: %w", err)
	}
	block, _ := pem.Decode(contents)
	if block == nil {
		return nil, fmt.Errorf("JWT public key is not PEM")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse JWT public key: %w", err)
	}
	publicKey, ok := parsed.(ed25519.PublicKey)
	if !ok {
		return nil, fmt.Errorf("JWT public key must be Ed25519")
	}
	return NewVerifier(publicKey, issuer), nil
}

func (v *Verifier) Verify(raw string) (UserContext, error) {
	raw = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(raw), "Bearer "))
	if raw == "" {
		return UserContext{}, ErrInvalidToken
	}
	parsedClaims := &claims{}
	token, err := jwt.ParseWithClaims(raw, parsedClaims, func(token *jwt.Token) (any, error) {
		if token.Method != jwt.SigningMethodEdDSA {
			return nil, ErrInvalidToken
		}
		return v.publicKey, nil
	}, jwt.WithValidMethods([]string{jwt.SigningMethodEdDSA.Alg()}))
	if err != nil || !token.Valid {
		return UserContext{}, ErrInvalidToken
	}
	if !parsedClaims.VerifyIssuer(v.issuer, true) || parsedClaims.ExpiresAt == nil {
		return UserContext{}, ErrInvalidToken
	}
	userID, err := strconv.ParseInt(parsedClaims.Subject, 10, 64)
	if err != nil || userID <= 0 {
		return UserContext{}, ErrInvalidToken
	}
	return UserContext{UserID: userID, Email: parsedClaims.Email, DisplayName: parsedClaims.DisplayName, EmailVerified: parsedClaims.EmailVerified}, nil
}
