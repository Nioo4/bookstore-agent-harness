package safelog

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestWriterDropsPayloadCredentialsAndIdentity(t *testing.T) {
	var destination bytes.Buffer
	writer := &Writer{Destination: &destination}
	payload := []byte(`{"level":"error","content":"stmt: INSERT secret-password private@example.com","trace":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","span":"bbbbbbbbbbbbbbbb","user_id":123,"ip":"127.0.0.99","password":"secret"}`)
	if n, err := writer.Write(payload); err != nil || n != len(payload) {
		t.Fatalf("write failed: %d %v", n, err)
	}
	var result map[string]any
	if err := json.Unmarshal(destination.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result["event"] != "DATABASE_OPERATION" || len(result) != 4 {
		t.Fatalf("unexpected projection: %v", result)
	}
	for _, forbidden := range []string{"password", "private@example.com", "user_id", "127.0.0.99"} {
		if strings.Contains(destination.String(), forbidden) {
			t.Fatal("sensitive field escaped projection")
		}
	}
	destination.Reset()
	_, _ = writer.Write([]byte("raw fatal: secret credential"))
	if destination.String() != "{\"event\":\"RUNTIME_LOG\"}\n" {
		t.Fatal("unstructured message escaped projection")
	}
	destination.Reset()
	_, _ = writer.Write([]byte("ALERT inventory outbox event secret-payload failed after 10 attempts"))
	if !strings.Contains(destination.String(), "OUTBOX_RETRY_ALERT") || strings.Contains(destination.String(), "secret-payload") {
		t.Fatal("publisher alert must preserve only its fixed event code")
	}
}
