package authn

import (
	"crypto/ed25519"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/golang-jwt/jwt/v4"
)

const AccessTokenTTL = 8 * time.Hour

type AccessClaims struct {
	Email         string `json:"email"`
	EmailVerified bool   `json:"email_verified"`
	jwt.RegisteredClaims
}

type TokenSigner interface {
	Sign(user User, now time.Time) (string, time.Duration, error)
}

type Ed25519Signer struct {
	privateKey ed25519.PrivateKey
	issuer     string
}

func NewEd25519Signer(privateKey ed25519.PrivateKey, issuer string) *Ed25519Signer {
	return &Ed25519Signer{privateKey: privateKey, issuer: issuer}
}

func (s *Ed25519Signer) PublicKey() ed25519.PublicKey {
	return s.privateKey.Public().(ed25519.PublicKey)
}

func LoadEd25519Signer(path, issuer string) (*Ed25519Signer, error) {
	if path == "" {
		return nil, fmt.Errorf("JWT private key path is required")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read JWT private key: %w", err)
	}
	block, _ := pem.Decode(contents)
	if block == nil {
		return nil, fmt.Errorf("JWT private key is not PEM")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse JWT private key: %w", err)
	}
	privateKey, ok := parsed.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("JWT private key must be Ed25519")
	}
	return NewEd25519Signer(privateKey, issuer), nil
}

func LoadEd25519PublicKey(path string) (ed25519.PublicKey, error) {
	if path == "" {
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
	return publicKey, nil
}

func (s *Ed25519Signer) Sign(user User, now time.Time) (string, time.Duration, error) {
	expires := now.Add(AccessTokenTTL)
	claims := AccessClaims{
		Email: user.Email, EmailVerified: user.EmailVerifiedAt.Valid,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer: s.issuer, Subject: strconv.FormatInt(user.UserID, 10),
			IssuedAt: jwt.NewNumericDate(now), ExpiresAt: jwt.NewNumericDate(expires),
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodEdDSA, claims)
	signed, err := token.SignedString(s.privateKey)
	if err != nil {
		return "", 0, fmt.Errorf("sign access token: %w", err)
	}
	return signed, AccessTokenTTL, nil
}
