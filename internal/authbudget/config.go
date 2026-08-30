// Package authbudget defines the shared authentication deadline chain.
package authbudget

import (
	"fmt"
	"time"
)

type Config struct {
	SMTP time.Duration `json:",default=5s"`
	RPC  time.Duration `json:",default=10s"`
	HTTP time.Duration `json:",default=15s"`
	Web  time.Duration `json:",default=20s"`
}

func (c Config) Validate() error {
	if c.SMTP <= 0 || c.SMTP >= c.RPC || c.RPC >= c.HTTP || c.HTTP >= c.Web {
		return fmt.Errorf("authentication deadlines must satisfy 0 < SMTP < RPC < HTTP < Web")
	}
	return nil
}
