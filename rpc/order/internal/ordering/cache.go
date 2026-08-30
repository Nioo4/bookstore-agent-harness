package ordering

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	redis "github.com/redis/go-redis/v9"
)

type RedisCacheInvalidator struct{ client *redis.Client }

func NewRedisCacheInvalidator(address, password string, db int) (*RedisCacheInvalidator, error) {
	if strings.TrimSpace(address) == "" {
		return nil, fmt.Errorf("Redis address is required")
	}
	return &RedisCacheInvalidator{client: redis.NewClient(&redis.Options{Addr: address, Password: password, DB: db})}, nil
}

func (i *RedisCacheInvalidator) InvalidateBooks(ctx context.Context, items []Item) error {
	keys := make([]string, 0, len(items)*4)
	for _, item := range items {
		id := strconv.FormatInt(item.BookID, 10)
		keys = append(keys,
			"cache#Book#lookup#id#"+id,
			"cache#Book#lookup#isbn#"+strings.ToLower(item.ISBN),
			"cache#Book#catalog#id#"+id,
			"cache#Book#catalog#isbn#"+strings.ToLower(item.ISBN),
		)
	}
	if len(keys) == 0 {
		return nil
	}
	return i.client.Del(ctx, keys...).Err()
}
