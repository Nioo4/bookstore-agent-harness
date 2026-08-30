package authn

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
)

func GenerateVerificationToken() (raw string, digest [32]byte, err error) {
	value := make([]byte, 32)
	if _, err = rand.Read(value); err != nil {
		return "", digest, fmt.Errorf("generate verification token: %w", err)
	}
	raw = base64.RawURLEncoding.EncodeToString(value)
	digest = sha256.Sum256([]byte(raw))
	return raw, digest, nil
}

func VerificationDigest(raw string) [32]byte {
	return sha256.Sum256([]byte(raw))
}
