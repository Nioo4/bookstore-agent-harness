package authn

import (
	"strings"
	"testing"
)

func TestArgon2idPasswordRoundTrip(t *testing.T) {
	encoded, err := HashPassword("correct horse battery staple")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(encoded, "$argon2id$v=19$m=65536,t=3,p=4$") {
		t.Fatalf("unexpected password hash format: %s", encoded)
	}
	if strings.Contains(encoded, "correct horse") {
		t.Fatal("password hash contains plaintext")
	}
	if !VerifyPassword(encoded, "correct horse battery staple") {
		t.Fatal("correct password rejected")
	}
	if VerifyPassword(encoded, "wrong password") {
		t.Fatal("wrong password accepted")
	}
	if VerifyPassword("not-an-argon-hash", "anything") {
		t.Fatal("malformed hash accepted")
	}
}

func TestVerificationTokenHas256BitsAndStableDigest(t *testing.T) {
	raw, digest, err := GenerateVerificationToken()
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) != 43 {
		t.Fatalf("base64url token length=%d; want 43", len(raw))
	}
	if got := VerificationDigest(raw); got != digest {
		t.Fatal("verification digest is not reproducible")
	}
}
