// Package service exposes the small composition boundary used by the
// consolidated bookstore-service command. HTTP handlers remain in the
// generated/internal API packages; this package only makes their construction
// available to the top-level process without widening those implementation
// packages.
package service

import (
	"bookstore/api/internal/config"
	"bookstore/api/internal/handler"
	"bookstore/api/internal/svc"
	"bookstore/rpc/add/client/adder"
	"bookstore/rpc/auth/authenticator"
	"bookstore/rpc/check/checker"
	"bookstore/rpc/order/orderservice"

	"github.com/zeromicro/go-zero/rest"
)

type Config = config.Config

func NewHTTPServer(c config.Config) *rest.Server {
	server := rest.MustNewServer(c.RestConf)
	handler.RegisterHandlers(server, svc.NewServiceContext(c))
	return server
}

func NewInProcessHTTPServer(c config.Config, add adder.Adder, check checker.Checker,
	auth authenticator.Authenticator, orders orderservice.OrderService) *rest.Server {
	server := rest.MustNewServer(c.RestConf)
	handler.RegisterHandlers(server, svc.NewInProcessServiceContext(c, add, check, auth, orders))
	return server
}
