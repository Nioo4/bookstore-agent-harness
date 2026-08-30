// Package safelog projects framework logs to technical fields before persistence.
package safelog

import (
	"encoding/json"
	"io"
	"log"
	"os"
	"regexp"
	"strings"
	"sync"

	"github.com/zeromicro/go-zero/core/logx"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

var technicalID = regexp.MustCompile(`^[0-9a-f]{16}([0-9a-f]{16})?$`)

type Writer struct {
	Destination io.Writer
	mu          sync.Mutex
}

func (w *Writer) Write(p []byte) (int, error) {
	var entry map[string]any
	_ = json.Unmarshal(p, &entry)
	projection := map[string]any{"event": "RUNTIME_LOG"}
	if strings.Contains(string(p), "ALERT inventory outbox event ") {
		projection["event"] = "OUTBOX_RETRY_ALERT"
		projection["level"] = "error"
	}
	if content, ok := entry["content"].(string); ok {
		switch {
		case strings.HasPrefix(content, "stmt:") || strings.HasPrefix(content, "[SQL]") || strings.HasPrefix(content, "sql "):
			projection["event"] = "DATABASE_OPERATION"
		case strings.HasPrefix(content, "rpc "):
			projection["event"] = "RPC_OPERATION"
		}
	}
	for _, name := range []string{"trace", "span"} {
		if value, ok := entry[name].(string); ok && technicalID.MatchString(value) {
			projection[name] = value
		}
	}
	if level, ok := entry["level"].(string); ok {
		switch level {
		case "info", "error", "slow", "severe", "debug", "stat":
			projection["level"] = level
		}
	}
	encoded, _ := json.Marshal(projection)
	w.mu.Lock()
	defer w.mu.Unlock()
	_, err := w.Destination.Write(append(encoded, '\n'))
	if err != nil {
		return 0, err
	}
	return len(p), nil
}

func Install(config logx.LogConf) {
	logx.MustSetup(config)
	writer := &Writer{Destination: os.Stderr}
	logx.SetWriter(logx.NewWriter(writer))
	log.SetOutput(writer)
	sqlx.DisableLog()
}
