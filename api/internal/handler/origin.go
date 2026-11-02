package handler

import (
	"net/http"
	"net/url"
	"strings"

	"bookstore/api/internal/config"
)

// withBrowserOrigin protects state-changing browser requests from cross-site
// cookie writes. Read-only requests do not need this check. A missing Origin
// is accepted only when Fetch Metadata proves same-origin, which supports
// same-origin clients that omit Origin without accepting ambiguous requests.
func withBrowserOrigin(c config.Config, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !isStateChangingMethod(r.Method) {
			next.ServeHTTP(w, r)
			return
		}
		origin := strings.TrimSpace(r.Header.Get("Origin"))
		if origin == "" {
			if strings.EqualFold(strings.TrimSpace(r.Header.Get("Sec-Fetch-Site")), "same-origin") {
				next.ServeHTTP(w, r)
				return
			}
			writeAPIError(w, r, http.StatusForbidden, "REQUEST_ORIGIN_DENIED", "request origin denied", false)
			return
		}
		if !sameOrigin(origin, c.AppBaseURL) {
			writeAPIError(w, r, http.StatusForbidden, "REQUEST_ORIGIN_DENIED", "request origin denied", false)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func isStateChangingMethod(method string) bool {
	switch method {
	case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
		return true
	default:
		return false
	}
}

func sameOrigin(raw, expected string) bool {
	got, ok := parseOrigin(raw, false)
	if !ok {
		return false
	}
	want, ok := parseOrigin(expected, true)
	if !ok {
		return false
	}
	return strings.EqualFold(got.Scheme, want.Scheme) &&
		strings.EqualFold(got.Hostname(), want.Hostname()) && effectivePort(got) == effectivePort(want)
}

func parseOrigin(raw string, allowPath bool) (*url.URL, bool) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme == "" || u.Host == "" || u.User != nil || u.Opaque != "" || u.RawQuery != "" || u.Fragment != "" || u.Hostname() == "" {
		return nil, false
	}
	if !allowPath && u.Path != "" {
		return nil, false
	}
	if !strings.EqualFold(u.Scheme, "http") && !strings.EqualFold(u.Scheme, "https") {
		return nil, false
	}
	if strings.Contains(u.Host, "@") || strings.Contains(u.Host, ",") {
		return nil, false
	}
	return u, true
}

func effectivePort(u *url.URL) string {
	if port := u.Port(); port != "" {
		return port
	}
	if strings.EqualFold(u.Scheme, "https") {
		return "443"
	}
	return "80"
}
