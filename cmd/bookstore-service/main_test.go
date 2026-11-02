package main

import (
	"testing"

	apiservice "bookstore/api/service"
)

func TestApplyHTTPAddr(t *testing.T) {
	var c apiservice.Config
	applyHTTPAddr(&c, ":8888")
	if c.Host != "0.0.0.0" || c.Port != 8888 {
		t.Fatalf("HTTP address = %s:%d, want 0.0.0.0:8888", c.Host, c.Port)
	}
}
