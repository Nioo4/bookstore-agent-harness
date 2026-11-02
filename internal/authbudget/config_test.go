package authbudget

import (
	"testing"
	"time"
)

func TestDeadlineChainValidation(t *testing.T) {
	for _, c := range []Config{{0, 10 * time.Second, 15 * time.Second, 20 * time.Second}, {5 * time.Second, 5 * time.Second, 15 * time.Second, 20 * time.Second}, {5 * time.Second, 10 * time.Second, 9 * time.Second, 20 * time.Second}, {5 * time.Second, 10 * time.Second, 15 * time.Second, 15 * time.Second}} {
		if c.Validate() == nil {
			t.Fatal("invalid deadline chain accepted")
		}
	}
	if (Config{5 * time.Second, 10 * time.Second, 15 * time.Second, 20 * time.Second}).Validate() != nil {
		t.Fatal("default chain rejected")
	}
}
