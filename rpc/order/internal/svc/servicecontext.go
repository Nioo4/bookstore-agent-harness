package svc

import (
	"bookstore/rpc/auth/jwtauth"
	"bookstore/rpc/order/internal/config"
	"bookstore/rpc/order/internal/ordering"

	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

type ServiceContext struct {
	Config config.Config
	Orders *ordering.Service
}

func NewServiceContext(c config.Config) *ServiceContext {
	verifier, err := jwtauth.LoadVerifier(c.JWT.PublicKeyPath, c.JWT.Issuer)
	if err != nil {
		panic(err)
	}
	invalidation, err := ordering.NewRedisCacheInvalidator(c.BusinessRedis.Address, c.BusinessRedis.Password, c.BusinessRedis.DB)
	if err != nil {
		panic(err)
	}
	service, err := ordering.NewService(ordering.NewSQLStore(sqlx.NewMysql(c.DataSource)), verifier, invalidation, c.ConfirmationTTL)
	if err != nil {
		panic(err)
	}
	return &ServiceContext{Config: c, Orders: service}
}
