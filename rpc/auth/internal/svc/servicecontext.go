package svc

import (
	"bookstore/rpc/auth/internal/authn"
	"bookstore/rpc/auth/internal/config"
	"bookstore/rpc/auth/jwtauth"
	"bytes"

	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

type ServiceContext struct {
	Config config.Config
	Auth   *authn.Service
}

func NewServiceContext(c config.Config) *ServiceContext {
	if err := c.AuthBudget.Validate(); err != nil {
		panic(err)
	}
	signer, err := authn.LoadEd25519Signer(c.JWT.PrivateKeyPath, c.JWT.Issuer)
	if err != nil {
		panic(err)
	}
	publicKey, err := authn.LoadEd25519PublicKey(c.JWT.PublicKeyPath)
	if err != nil {
		panic(err)
	}
	if !bytes.Equal(signer.PublicKey(), publicKey) {
		panic("JWT private and public keys do not match")
	}
	mailer, err := authn.NewSMTPMailer(c.SMTP.Host, c.SMTP.Port, c.SMTP.Username, c.SMTP.Password, c.SMTP.From, c.AuthBudget.SMTP)
	if err != nil {
		panic(err)
	}
	limiter, err := authn.NewRedisRateLimiter(c.BusinessRedis.Address, c.BusinessRedis.Password, c.BusinessRedis.DB)
	if err != nil {
		panic(err)
	}
	service, err := authn.NewService(
		authn.NewSQLStore(sqlx.NewMysql(c.DataSource)), mailer, limiter, signer, jwtauth.NewVerifier(publicKey, c.JWT.Issuer), c.AppBaseURL,
		authn.RateLimitPolicy{RegisterLimit: c.RateLimit.RegisterLimit, LoginLimit: c.RateLimit.LoginLimit, Window: c.RateLimit.Window,
			ResendIPLimit: c.RateLimit.ResendIPLimit, ResendEmailLimit: c.RateLimit.ResendEmailLimit, ResendWindow: c.RateLimit.ResendWindow, ResendCooldown: c.RateLimit.ResendCooldown},
	)
	if err != nil {
		panic(err)
	}
	return &ServiceContext{Config: c, Auth: service}
}
