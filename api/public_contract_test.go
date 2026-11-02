package main

import (
	"os"
	"strings"
	"testing"
)

func TestPublicAPIHasNoOrderPreparationOrConfirmationBypass(t *testing.T) {
	for _, path := range []string{"bookstore.api", "internal/handler/routes.go"} {
		contents, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		text := strings.ToLower(string(contents))
		for _, forbidden := range []string{"/api/orders/prepare", "/api/orders/confirm"} {
			if strings.Contains(text, forbidden) {
				t.Errorf("%s still exposes forbidden public route %s", path, forbidden)
			}
		}
	}
}
