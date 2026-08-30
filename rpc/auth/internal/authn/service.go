package authn

import (
	"bookstore/rpc/auth/jwtauth"
	"context"
	"fmt"
	"net/mail"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"
)

const verificationTTL = 24 * time.Hour

type RateLimitPolicy struct {
	RegisterLimit    int
	LoginLimit       int
	Window           time.Duration
	ResendIPLimit    int
	ResendEmailLimit int
	ResendWindow     time.Duration
	ResendCooldown   time.Duration
}

type AccessVerifier interface {
	Verify(string) (jwtauth.UserContext, error)
}

type Service struct {
	store        Store
	mailer       Mailer
	limiter      RateLimiter
	signer       TokenSigner
	verifier     AccessVerifier
	baseURL      string
	policy       RateLimitPolicy
	now          func() time.Time
	startElapsed func() func() time.Duration
}

type RegisterResult struct {
	UserID int64
	Email  string
	Sent   bool
}

type LoginResult struct {
	AccessToken string
	ExpiresIn   int64
	User        User
}

func NewService(store Store, mailer Mailer, limiter RateLimiter, signer TokenSigner, verifier AccessVerifier, baseURL string, policy RateLimitPolicy) (*Service, error) {
	if store == nil || mailer == nil || limiter == nil || signer == nil || verifier == nil {
		return nil, fmt.Errorf("auth store, mailer, limiter and signer are required")
	}
	if strings.TrimSpace(baseURL) == "" {
		return nil, fmt.Errorf("application base URL is required")
	}
	if policy.RegisterLimit <= 0 || policy.LoginLimit <= 0 || policy.Window <= 0 {
		return nil, fmt.Errorf("authentication rate limits must be positive")
	}
	if policy.ResendIPLimit <= 0 || policy.ResendEmailLimit <= 0 || policy.ResendCooldown < time.Millisecond || policy.ResendWindow < policy.ResendCooldown || policy.ResendWindow > time.Hour {
		return nil, fmt.Errorf("invalid resend limits, window or cooldown")
	}
	return &Service{store: store, mailer: mailer, limiter: limiter, signer: signer, verifier: verifier,
		baseURL: strings.TrimRight(baseURL, "/"), policy: policy, now: time.Now,
		startElapsed: func() func() time.Duration {
			started := time.Now() // Preserve the monotonic reading; never convert to UTC.
			return func() time.Duration { return time.Since(started) }
		}}, nil
}

type ResendResult struct {
	Status            string
	Sent              bool
	RetryAfterSeconds int64
}

func (s *Service) ResendVerification(ctx context.Context, accessToken, clientIP string) (*ResendResult, error) {
	identity, err := s.verifier.Verify(accessToken)
	if err != nil {
		return nil, ErrAuthRequired
	}
	user, err := s.store.FindByID(ctx, identity.UserID)
	if err != nil {
		return nil, err
	}
	already := &ResendResult{Status: "ALREADY_VERIFIED"}
	if user.EmailVerifiedAt.Valid {
		return already, nil
	}
	cooldown, err := s.limiter.ReserveResend(ctx, strings.TrimSpace(clientIP), user.Email, s.policy)
	if err != nil {
		return nil, err
	}
	if cooldown <= 0 {
		return nil, ErrDependencyUnavailable
	}
	elapsed := s.startElapsed()
	raw, digest, err := GenerateVerificationToken()
	if err != nil {
		return nil, ErrDependencyUnavailable
	}
	user, created, err := s.store.CreateVerification(ctx, identity.UserID, digest[:], s.now().UTC())
	if err != nil {
		return nil, err
	}
	if !created {
		return already, nil
	}
	// The transaction has committed and all locks are released before SMTP.
	sent := s.mailer.SendVerification(ctx, user.Email, s.baseURL+"/verify-email?token="+url.QueryEscape(raw)) == nil
	result := &ResendResult{Status: "DELIVERY_UNCONFIRMED", Sent: sent, RetryAfterSeconds: cooldownHint(cooldown, elapsed())}
	if sent {
		result.Status = "SMTP_ACCEPTED"
	}
	return result, nil
}

func cooldownHint(initial, elapsed time.Duration) int64 {
	if elapsed < 0 {
		elapsed = 0
	}
	if elapsed >= initial {
		return 0
	}
	return ceilSeconds(initial - elapsed)
}

func (s *Service) Register(ctx context.Context, email, password, displayName, clientIP string) (*RegisterResult, error) {
	email, err := normalizeEmail(email)
	if err != nil {
		return nil, err
	}
	displayName = strings.TrimSpace(displayName)
	if displayName == "" || utf8.RuneCountInString(displayName) > 100 {
		return nil, fmt.Errorf("display_name must contain 1 to 100 characters")
	}
	if count := utf8.RuneCountInString(password); count < 8 || count > 128 {
		return nil, fmt.Errorf("password must contain 8 to 128 characters")
	}
	if err := s.enforceRate(ctx, "register_ip", clientIP, s.policy.RegisterLimit); err != nil {
		return nil, err
	}
	if err := s.enforceRate(ctx, "register_email", email, s.policy.RegisterLimit); err != nil {
		return nil, err
	}
	passwordHash, err := HashPassword(password)
	if err != nil {
		return nil, err
	}
	rawToken, digest, err := GenerateVerificationToken()
	if err != nil {
		return nil, err
	}
	now := s.now().UTC()
	userID, err := s.store.Register(ctx, email, displayName, passwordHash, digest[:], now.Add(verificationTTL))
	if err != nil {
		return nil, err
	}
	verificationURL := s.baseURL + "/verify-email?token=" + url.QueryEscape(rawToken)
	sent := s.mailer.SendVerification(ctx, email, verificationURL) == nil
	return &RegisterResult{UserID: userID, Email: email, Sent: sent}, nil
}

func (s *Service) VerifyEmail(ctx context.Context, rawToken string) (int64, error) {
	rawToken = strings.TrimSpace(rawToken)
	decoded, err := url.QueryUnescape(rawToken)
	if err != nil || decoded == "" {
		return 0, ErrVerificationInvalid
	}
	digest := VerificationDigest(decoded)
	return s.store.VerifyEmail(ctx, digest[:], s.now().UTC())
}

func (s *Service) Login(ctx context.Context, email, password, clientIP string) (*LoginResult, error) {
	email, err := normalizeEmail(email)
	if err != nil {
		return nil, ErrInvalidCredentials
	}
	if err := s.enforceRate(ctx, "login_ip", clientIP, s.policy.LoginLimit); err != nil {
		return nil, err
	}
	if err := s.enforceRate(ctx, "login_email", email, s.policy.LoginLimit); err != nil {
		return nil, err
	}
	user, err := s.store.FindByEmail(ctx, email)
	if err != nil {
		if err == ErrInvalidCredentials {
			return nil, ErrInvalidCredentials
		}
		return nil, err
	}
	if !VerifyPassword(user.PasswordHash, password) {
		return nil, ErrInvalidCredentials
	}
	token, ttl, err := s.signer.Sign(*user, s.now().UTC())
	if err != nil {
		return nil, err
	}
	return &LoginResult{AccessToken: token, ExpiresIn: int64(ttl.Seconds()), User: *user}, nil
}

func (s *Service) enforceRate(ctx context.Context, scope, subject string, limit int) error {
	allowed, err := s.limiter.Allow(ctx, scope, strings.TrimSpace(subject), limit, s.policy.Window)
	if err != nil {
		return ErrDependencyUnavailable
	}
	if !allowed {
		return ErrRateLimited
	}
	return nil
}

func normalizeEmail(value string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	if len(value) == 0 || len(value) > 320 {
		return "", fmt.Errorf("invalid email")
	}
	address, err := mail.ParseAddress(value)
	if err != nil || address.Address != value {
		return "", fmt.Errorf("invalid email")
	}
	return value, nil
}
