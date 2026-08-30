package svc

import (
	"bookstore/rpc/check/internal/config"
	"bookstore/rpc/model"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

type ServiceContext struct {
	c       config.Config
	Model   model.BookModel
	Catalog model.CatalogModel
}

func NewServiceContext(c config.Config) *ServiceContext {
	store := model.NewBookModelWithCacheFallback(sqlx.NewMysql(c.DataSource), c.Cache, model.CacheFallbackConfig{
		CacheTimeout:  c.CacheFallback.CacheTimeout,
		MaxConcurrent: c.CacheFallback.MaxConcurrent,
	})
	return &ServiceContext{
		c:       c,
		Model:   store,
		Catalog: store.(model.CatalogModel),
	}
}
