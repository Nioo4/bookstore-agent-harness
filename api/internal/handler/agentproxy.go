package handler

import (
	"io"
	"mime"
	"net/http"
	"strings"

	"bookstore/api/internal/svc"
)

type agentRoute struct {
	guestAllowed bool
}

// AgentProxyHandler is intentionally an allowlist dispatcher. It is not a
// general reverse proxy: paths not described by the v2 public contract,
// including /internal, /api/evals and every replay endpoint, are 404.
func AgentProxyHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		route, ok := matchAgentRoute(r.Method, r.URL.Path)
		if !ok {
			http.NotFound(w, r)
			return
		}
		if svcCtx == nil || svcCtx.AgentProxy == nil {
			writeAPIError(w, r, http.StatusServiceUnavailable, "DEPENDENCY_UNAVAILABLE", "agent service is unavailable", true)
			return
		}

		token, cookiePresent := browserAccessCookie(r)
		if cookiePresent {
			if token == "" || svcCtx.AuthVerifier == nil {
				writeAPIError(w, r, http.StatusUnauthorized, "AUTH_REQUIRED", "authentication required", false)
				return
			}
			if _, err := svcCtx.AuthVerifier.Verify(token); err != nil {
				// A browser-supplied Authorization header must never replace an
				// invalid or expired access cookie.
				writeAPIError(w, r, http.StatusUnauthorized, "AUTH_REQUIRED", "authentication required", false)
				return
			}
		} else if !route.guestAllowed {
			writeAPIError(w, r, http.StatusUnauthorized, "AUTH_REQUIRED", "authentication required", false)
			return
		}

		headers := proxyRequestHeaders(r.Header, token)
		response, err := svcCtx.AgentProxy.Request(r.Context(), r.Method, r.URL.Path, r.URL.RawQuery, r.Body, headers)
		if err != nil {
			if r.Context().Err() != nil {
				// The client cancellation/deadline is already propagated to the
				// upstream context; there is no safe response to write now.
				return
			}
			writeAPIError(w, r, http.StatusServiceUnavailable, "DEPENDENCY_UNAVAILABLE", "agent service is unavailable", true)
			return
		}
		defer response.Body.Close()

		copyAgentResponseHeaders(w.Header(), response.Header)
		if isSSE(response.Header.Get("Content-Type")) {
			streamAgentResponse(w, response)
			return
		}
		w.WriteHeader(response.StatusCode)
		_, _ = io.Copy(w, response.Body)
	}
}

func matchAgentRoute(method, requestPath string) (agentRoute, bool) {
	if method == http.MethodPost && matchesDynamicPath(requestPath, "/api/chat/conversations/", "/messages") {
		return agentRoute{guestAllowed: true}, true
	}
	if method == http.MethodPost && matchesDynamicPath(requestPath, "/api/chat/conversations/", "/confirmations") {
		return agentRoute{}, true
	}
	if method == http.MethodGet && requestPath == "/api/memories" {
		return agentRoute{}, true
	}
	if method == http.MethodPost && requestPath == "/api/memories/preferences" {
		return agentRoute{}, true
	}
	if method == http.MethodDelete && (requestPath == "/api/memories" || matchesDynamicPath(requestPath, "/api/memories/", "")) {
		return agentRoute{}, true
	}
	if method == http.MethodPost && requestPath == "/api/tasks/inventory-watches" {
		return agentRoute{}, true
	}
	if method == http.MethodGet && requestPath == "/api/tasks" {
		return agentRoute{}, true
	}
	if method == http.MethodGet && matchesDynamicPath(requestPath, "/api/tasks/", "") {
		return agentRoute{}, true
	}
	if method == http.MethodPost && matchesDynamicPath(requestPath, "/api/tasks/", "/cancel") {
		return agentRoute{}, true
	}
	if method == http.MethodGet && requestPath == "/api/notifications" {
		return agentRoute{}, true
	}
	if method == http.MethodPost && matchesDynamicPath(requestPath, "/api/notifications/", "/read") {
		return agentRoute{}, true
	}
	if method == http.MethodGet && requestPath == "/api/traces" {
		return agentRoute{}, true
	}
	if method == http.MethodGet && matchesDynamicPath(requestPath, "/api/traces/", "") {
		return agentRoute{}, true
	}
	if method == http.MethodDelete && matchesDynamicPath(requestPath, "/api/traces/", "") {
		return agentRoute{}, true
	}
	return agentRoute{}, false
}

func matchesDynamicPath(requestPath, prefix, suffix string) bool {
	if !strings.HasPrefix(requestPath, prefix) || !strings.HasSuffix(requestPath, suffix) {
		return false
	}
	segment := strings.TrimSuffix(strings.TrimPrefix(requestPath, prefix), suffix)
	if segment == "" || strings.ContainsAny(segment, `/\\?#`) || segment == "." || segment == ".." {
		return false
	}
	return true
}

func browserAccessCookie(r *http.Request) (string, bool) {
	var token string
	found := false
	for _, cookie := range r.Cookies() {
		if cookie.Name != accessCookieName {
			continue
		}
		if found {
			// Duplicate access cookies are ambiguous and must not be resolved
			// using a browser-supplied Authorization header.
			return "", true
		}
		found = true
		token = strings.TrimSpace(cookie.Value)
	}
	return token, found
}

func proxyRequestHeaders(input http.Header, token string) http.Header {
	output := input.Clone()
	for _, name := range []string{
		"Authorization", "Cookie", "Content-Length", "Connection", "Keep-Alive",
		"Proxy-Authenticate", "Proxy-Authorization", "TE", "Trailer",
		"Transfer-Encoding", "Upgrade", "Host",
	} {
		output.Del(name)
	}
	if token != "" {
		output.Set("Authorization", "Bearer "+token)
	}
	return output
}

func copyAgentResponseHeaders(dst, src http.Header) {
	for key, values := range src {
		if isHopByHopHeader(key) || strings.EqualFold(key, "Set-Cookie") || strings.EqualFold(key, "Content-Length") {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}

func isHopByHopHeader(key string) bool {
	switch strings.ToLower(key) {
	case "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade":
		return true
	default:
		return false
	}
}

func isSSE(contentType string) bool {
	mediaType, _, err := mime.ParseMediaType(contentType)
	return err == nil && strings.EqualFold(mediaType, "text/event-stream")
}

func streamAgentResponse(w http.ResponseWriter, response *http.Response) {
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(response.StatusCode)
	flusher, canFlush := w.(http.Flusher)
	if canFlush {
		flusher.Flush()
	}
	buffer := make([]byte, 32*1024)
	for {
		read, err := response.Body.Read(buffer)
		if read > 0 {
			if _, writeErr := w.Write(buffer[:read]); writeErr != nil {
				return
			}
			if canFlush {
				flusher.Flush()
			}
		}
		if err == io.EOF {
			return
		}
		if err != nil {
			return
		}
	}
}
