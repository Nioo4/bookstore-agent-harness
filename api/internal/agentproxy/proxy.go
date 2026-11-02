// Package agentproxy contains the small, fixed-origin HTTP client used by the
// Go BFF. It deliberately knows nothing about browser cookies or route
// authorization; those decisions stay in the handler layer.
package agentproxy

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	DefaultBaseURL = "http://agent-api:8000"
	RequestTimeout = 120 * time.Second
)

type Proxy struct {
	baseURL *url.URL
	client  *http.Client
}

func New(rawBaseURL string) (*Proxy, error) {
	return NewWithClient(rawBaseURL, nil)
}

// NewWithClient is kept small so handler tests can use an isolated transport
// without changing the production fixed-origin validation.
func NewWithClient(rawBaseURL string, client *http.Client) (*Proxy, error) {
	baseURL, err := parseBaseURL(rawBaseURL)
	if err != nil {
		return nil, err
	}
	if client == nil {
		client = &http.Client{
			Timeout: RequestTimeout,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		}
	}
	return &Proxy{baseURL: baseURL, client: client}, nil
}

func parseBaseURL(raw string) (*url.URL, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, errors.New("agent API URL is required")
	}
	u, err := url.Parse(raw)
	if err != nil || u.Scheme == "" || u.Host == "" || u.Hostname() == "" || u.User != nil || u.Opaque != "" {
		return nil, errors.New("agent API URL must be an absolute HTTP(S) origin")
	}
	if !strings.EqualFold(u.Scheme, "http") && !strings.EqualFold(u.Scheme, "https") {
		return nil, errors.New("agent API URL must use http or https")
	}
	if !allowedAgentHost(u.Hostname()) {
		return nil, errors.New("agent API URL host is not an allowed local service")
	}
	if u.RawQuery != "" || u.Fragment != "" || u.RawPath != "" || (u.Path != "" && u.Path != "/") {
		return nil, errors.New("agent API URL must not include a path, query, or fragment")
	}
	if port := u.Port(); port != "" {
		value, err := strconv.Atoi(port)
		if err != nil || value < 1 || value > 65535 {
			return nil, errors.New("agent API URL contains an invalid port")
		}
	}
	// Normalize the configured origin once. Forwarded paths are assigned to a
	// fresh URL in Request, so no browser input can replace scheme or host.
	return &url.URL{Scheme: u.Scheme, Host: u.Host}, nil
}

// The V2 deployment contract places the Agent API in the same Compose network.
// Keeping the only production hostname explicit prevents an accidental config
// value from turning the BFF into a proxy for arbitrary private or metadata
// endpoints. Loopback remains available for isolated local tests and debugging.
func allowedAgentHost(host string) bool {
	if strings.EqualFold(host, "agent-api") || strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func (p *Proxy) Request(ctx context.Context, method, requestPath, rawQuery string, body io.Reader, headers http.Header) (*http.Response, error) {
	if p == nil || p.baseURL == nil || p.client == nil {
		return nil, errors.New("agent API proxy is not configured")
	}
	target := *p.baseURL
	target.Path = requestPath
	target.RawPath = ""
	target.RawQuery = rawQuery
	req, err := http.NewRequestWithContext(ctx, method, target.String(), body)
	if err != nil {
		return nil, err
	}
	req.Header = headers.Clone()
	return p.client.Do(req)
}
