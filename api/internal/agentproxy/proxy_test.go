package agentproxy

import "testing"

func TestNewRejectsNonOriginAgentURLs(t *testing.T) {
	for _, raw := range []string{
		"",
		"ftp://agent-api:8000",
		"agent-api:8000",
		"http://user:pass@agent-api:8000",
		"http://agent-api:8000/v1",
		"http://agent-api:8000/?tenant=x",
		"http://agent-api:8000/#fragment",
		"http://agent-api:bad-port",
		"http://169.254.169.254",
		"http://mysql:3306",
		"https://example.com",
	} {
		if _, err := New(raw); err == nil {
			t.Fatalf("New(%q) accepted an invalid fixed origin", raw)
		}
	}
	for _, raw := range []string{"http://agent-api:8000", "https://agent-api", "http://[::1]:8000/"} {
		if _, err := New(raw); err != nil {
			t.Fatalf("New(%q) rejected a valid fixed origin: %v", raw, err)
		}
	}
}
